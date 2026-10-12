import json
from datetime import timedelta

import pytest

from xronix.api import Api
from xronix.common import jst_date
from xronix.xclient import SCOPES, authorize_url, make_pkce


def connect_and_start(app):
    api = Api(app)
    api.handle("POST", "/api/x/connect")
    assert app.jobs.run("manual")["status"] == "ok"      # 初回は現在地点から始めるだけ
    return api


def only_rule(app, **kw):
    """既定のルールを全て消して、指定した1件だけにする。"""
    for r in app.rules.list():
        app.rules.delete(r["id"])
    base = {"name": "テスト", "events": ["reply_to_post", "reply_to_reply", "quote", "mention"], "categories": [], "actions": [{"type": "ignore"}]}
    base.update(kw)
    return app.rules.create(base)


def mentions(app):
    return sorted(app.repo.list("mentions"), key=lambda m: m["id"])


def ai_on(app, bedrock, *replies, cat="fan"):
    """Bedrockを有効にする。分類には cat を返し、返信文の生成には、順に replies を返す。"""
    app.settings.update("ai", {"provider": "bedrock", "bedrock_model": "m1"})
    bedrock.classification = json.dumps({"category": cat})
    for r in replies:
        bedrock.push_text(r)


# ---------- イベントの種類 ----------
def test_event_kinds_from_mention_shape(app):
    connect_and_start(app)
    app.x.add_mentions(["可愛い", "どこですか？", "こんにちは", "引用します"], shapes=["reply_to_post", "reply_to_reply", "mention", "quote"])
    app.jobs.run("manual")
    assert [m["kind"] for m in mentions(app)] == ["reply_to_post", "reply_to_reply", "mention", "quote"]
    assert mentions(app)[1]["parent_id"] == "1800000000000000001"


def test_own_tweets_are_ignored(app):
    connect_and_start(app)
    app.x.add_mentions(["自分の投稿"], author_ids=["9999"])
    app.jobs.run("manual")
    assert mentions(app) == []


def test_no_matching_rule_goes_to_human(app):
    connect_and_start(app)
    only_rule(app, events=["quote"])
    app.x.add_mentions(["可愛い"], shapes=["reply_to_post"])
    app.jobs.run("manual")
    m = mentions(app)[0]
    assert m["state"] == "review" and m["rule_id"] == "" and "ルールが無い" in m["actions"][0]["detail"]


def test_rule_actions_recorded_on_the_item(app):
    connect_and_start(app)
    r = only_rule(app, categories=["fan"], actions=[{"type": "ignore"}])
    app.x.add_mentions(["可愛い", "どこですか？"])
    app.jobs.run("manual")
    fan, q = mentions(app)
    assert fan["rule_id"] == r["id"] and fan["actions"][0]["type"] == "ignore" and fan["state"] == "done"
    assert q["rule_id"] == "" and q["state"] == "review"


# ---------- いいね ----------
def test_like_action_and_scope(app):
    assert "like.write" in SCOPES.split()
    assert "like.write" in authorize_url("cid", "http://127.0.0.1/cb", "st", make_pkce()[1]).replace("%20", " ").replace("+", " ")
    connect_and_start(app)
    only_rule(app, categories=["fan"], actions=[{"type": "like"}])
    app.x.add_mentions(["可愛い！"])
    app.jobs.run("manual")
    m = mentions(app)[0]
    assert app.x.liked == [m["id"]] and m["state"] == "done" and m["actions"][0]["ok"] is True


def test_like_daily_limit_and_failure_does_not_consume_quota(app):
    connect_and_start(app)
    app.settings.update("safety", {"like_daily_limit": 1})
    only_rule(app, actions=[{"type": "like"}])
    app.x.fail_next_like = True
    app.x.add_mentions(["可愛い1"])
    app.jobs.run("manual")
    assert app.x.liked == [] and mentions(app)[0]["actions"][0]["ok"] is False        # 失敗した
    app.x.add_mentions(["可愛い2", "可愛い3"])
    app.jobs.run("manual")
    assert len(app.x.liked) == 1                                                       # 失敗は上限に数えない。上限は1件
    over = [m for m in mentions(app) if m["actions"][0]["detail"].startswith("1日のいいね上限")]
    assert len(over) == 1 and over[0]["actions"][0]["ok"] is False


# ---------- ブロック・通知 ----------
def test_blocklist_action_then_author_is_blocked(app):
    connect_and_start(app)
    only_rule(app, categories=["caution"], actions=[{"type": "blocklist"}, {"type": "ignore"}])
    app.x.add_mentions(["きもい", "また来ました"], author_ids=["777", "777"])
    app.jobs.run("manual")
    first, second = mentions(app)
    assert app.repo.get("blocklist", "777") and first["actions"][0]["ok"] is True
    assert second["state"] == "blocked" and second["actions"] == []                   # 次からは、ルールに入る前に止まる


def test_notify_action_sends_label_only(app):
    connect_and_start(app)
    api = Api(app)
    api.handle("PUT", "/api/secrets/telegram_bot_token", {}, {"value": "tok"})
    api.handle("PUT", "/api/settings/notify", {}, {"telegram_enabled": True, "telegram_chat_id": "5"})
    only_rule(app, categories=["fan"], events=["quote"], actions=[{"type": "notify"}])
    app.x.add_mentions(["とても素敵な写真ですね"], shapes=["quote"])
    app.http.push(200, {"ok": True})
    app.jobs.run("manual")
    body = json.loads(app.http.calls[-1]["body"])
    assert "自分の投稿の引用" in body["text"] and "素敵" not in body["text"]            # 本文は載せない
    assert mentions(app)[0]["actions"][0]["ok"] is True


def test_notify_action_when_telegram_disabled(app):
    connect_and_start(app)
    only_rule(app, actions=[{"type": "notify"}])
    app.x.add_mentions(["可愛い"])
    app.jobs.run("manual")
    a = mentions(app)[0]["actions"][0]
    assert a["ok"] is False and "無効" in a["detail"] and app.http.calls == []


# ---------- 返信（承認待ち） ----------
def test_queue_reply_manual_template_ai(app, bedrock):
    connect_and_start(app)
    only_rule(app, categories=["fan"], actions=[{"type": "queue_reply", "source": "manual"}])
    app.x.add_mentions(["可愛い"])
    app.jobs.run("manual")
    assert mentions(app)[0]["state"] == "review" and mentions(app)[0]["draft_text"] == ""
    only_rule(app, categories=["fan"], actions=[{"type": "queue_reply", "source": "template", "template_category": "question"}])
    app.x.add_mentions(["好きです"])
    app.jobs.run("manual")
    t = [m for m in mentions(app) if m["text"] == "好きです"][0]
    assert t["state"] == "pending" and t["draft_text"] in {x["text"] for x in app.templates.list() if x["category"] == "question"}
    ai_on(app, bedrock, "ありがとうございます！嬉しいです。")
    only_rule(app, categories=["fan"], actions=[{"type": "queue_reply", "source": "ai", "prompt": "お礼を書く"}])
    app.x.add_mentions(["最高です"])
    app.jobs.run("manual")
    a = [m for m in mentions(app) if m["text"] == "最高です"][0]
    assert a["state"] == "pending" and a["draft_text"] == "ありがとうございます！嬉しいです。" and a["draft_template_id"] == ""


def test_ai_prompt_and_single_context(app, bedrock):
    connect_and_start(app)
    ai_on(app, bedrock, "ありがとう！")
    only_rule(app, categories=["fan"], actions=[{"type": "queue_reply", "source": "ai", "prompt": "とても丁寧に返す", "context": "single"}])
    app.x.add_mentions(["可愛い"], shapes=["reply_to_reply"])
    run = app.jobs.run("manual")
    call_ = bedrock.calls[-1]
    assert call_["system"][0]["text"].startswith("とても丁寧に返す") and "140文字以内" in call_["system"][0]["text"]
    user = call_["messages"][0]["content"][0]["text"]
    assert "可愛い" in user and "スレッド" not in user                                   # 単体の文脈では、スレッドを取りに行かない
    assert run["thread_reads"] == 0


def test_ai_thread_context_and_cost(app, bedrock):
    connect_and_start(app)
    ai_on(app, bedrock, "ご質問ありがとうございます", cat="question")
    only_rule(app, categories=["question"], actions=[{"type": "queue_reply", "source": "ai", "prompt": "答える", "context": "thread", "thread_depth": 5}])
    app.x.add_mentions(["どこで買えますか？"], shapes=["reply_to_reply"])
    run = app.jobs.run("manual")
    user = bedrock.calls[-1]["messages"][0]["content"][0]["text"]
    assert "スレッドの流れ（古い順）" in user
    assert user.index("あなた: （自分の投稿）") < user.index("あなた: （自分の返信）")           # 古い順（最初の投稿 → 自分の返信）
    assert run["thread_reads"] == 2 and run["est_cost_usd"] == pytest.approx(run["reads"] * 0.001 + 2 * 0.005)


def test_thread_depth_limits_how_far_back(app, bedrock):
    connect_and_start(app)
    ai_on(app, bedrock, "了解です", cat="question")
    only_rule(app, categories=["question"], actions=[{"type": "queue_reply", "source": "ai", "prompt": "答える", "context": "thread", "thread_depth": 1}])
    app.x.add_mentions(["どこで買えますか？"], shapes=["reply_to_reply"])
    app.jobs.run("manual")
    user = bedrock.calls[-1]["messages"][0]["content"][0]["text"]
    assert "（自分の返信）" in user and "（自分の投稿）" not in user                       # 1つだけ遡る


@pytest.mark.parametrize("bad", ["", "見て https://a.example", "あ" * 200, "@someone ありがとう", "ありがとう #宣伝", "ありがとう＠someone"])
def test_ai_failure_falls_back_to_human(app, bedrock, bad):
    connect_and_start(app)
    ai_on(app, bedrock, bad)
    only_rule(app, categories=["fan"], actions=[{"type": "queue_reply", "source": "ai", "prompt": "お礼"}])
    app.x.add_mentions(["可愛い"])
    app.jobs.run("manual")
    m = mentions(app)[0]
    assert m["state"] == "review" and m["draft_text"] == "" and "作れませんでした" in m["actions"][0]["detail"]


def test_ai_unavailable_in_mock_mode_falls_back(app, bedrock):
    connect_and_start(app)
    app.settings.update("ai", {"provider": "bedrock", "bedrock_model": "m1"})
    app.ai.allow_external = False
    only_rule(app, categories=["fan"], actions=[{"type": "queue_reply", "source": "ai", "prompt": "お礼"}])
    app.x.add_mentions(["可愛い"])
    app.jobs.run("manual")
    assert mentions(app)[0]["state"] == "review" and bedrock.calls == []


# ---------- 承認なしの送信 ----------
def test_send_reply_template_sends_without_approval(app):
    connect_and_start(app)
    only_rule(app, categories=["fan"], actions=[{"type": "send_reply", "source": "template"}])
    app.x.add_mentions(["可愛い"])
    app.jobs.run("manual")
    m = mentions(app)[0]
    assert m["state"] == "sent" and m["auto_sent"] is True and app.x.sent[0]["in_reply_to"] == m["id"]
    assert app.sender.sent_today() == 1 and m["actions"][0]["ok"] is True


def test_send_reply_ai_and_auto_limit_falls_back_to_queue(app, bedrock):
    connect_and_start(app)
    app.settings.update("safety", {"auto_daily_limit": 1})
    ai_on(app, bedrock, "ありがとう1", "ありがとう2")
    only_rule(app, categories=["fan"], actions=[{"type": "send_reply", "source": "ai", "prompt": "お礼"}])
    app.x.add_mentions(["可愛い1", "可愛い2"])
    app.jobs.run("manual")
    first, second = mentions(app)
    assert first["state"] == "sent" and [s["text"] for s in app.x.sent] == ["ありがとう1"]
    assert second["state"] == "pending" and second["draft_text"] == "ありがとう2" and "自動送信の上限" in second["actions"][0]["detail"]


def test_send_reply_never_sends_when_draft_is_missing(app, bedrock):
    connect_and_start(app)
    ai_on(app, bedrock, "見て https://a.example")
    only_rule(app, categories=["fan"], actions=[{"type": "send_reply", "source": "ai", "prompt": "お礼"}])
    app.x.add_mentions(["可愛い"])
    app.jobs.run("manual")
    assert app.x.sent == [] and mentions(app)[0]["state"] == "review"                    # 作れなければ、送らず人に回す


def test_send_reply_respects_overall_daily_limit(app, clock):
    connect_and_start(app)
    app.settings.update("safety", {"daily_limit": 0})
    only_rule(app, categories=["fan"], actions=[{"type": "send_reply", "source": "template"}])
    app.x.add_mentions(["可愛い"])
    app.jobs.run("manual")
    m = mentions(app)[0]
    assert app.x.sent == [] and m["state"] == "pending" and "上限" in m["actions"][0]["detail"]
    assert app.repo.get("state", "autosent#" + jst_date(clock()))["count"] == 0   # 送れなかった分は、自動送信の枠に数えない


def test_send_reply_unknown_result_is_not_retried(app):
    connect_and_start(app)
    only_rule(app, categories=["fan"], actions=[{"type": "send_reply", "source": "template"}])
    app.x.fail_next_create = "unknown"
    app.x.add_mentions(["可愛い"])
    app.jobs.run("manual")
    m = mentions(app)[0]
    assert m["state"] == "unknown" and len(app.x.sent) == 0 and m["actions"][0]["ok"] is False


# ---------- 自投稿の数値（いいね数など） ----------
def metric_rule(app, event="metric_likes", threshold=100, **kw):
    base = {"name": "数値", "events": [event], "threshold": threshold, "actions": [{"type": "queue_reply", "source": "template", "template_category": "fan"}]}
    base.update(kw)
    return app.rules.create(base)


def test_metric_rule_fires_once_per_post(app, bedrock):
    connect_and_start(app)
    ai_on(app, bedrock, "たくさんのいいね、ありがとうございます！")
    for r in app.rules.list():
        app.rules.delete(r["id"])
    metric_rule(app, actions=[{"type": "queue_reply", "source": "ai", "prompt": "お礼を書く"}])
    low = app.x.add_post("人気のない投稿", likes=10)
    hot = app.x.add_post("人気の投稿", likes=150)
    run = app.jobs.run("manual")
    items = mentions(app)
    assert len(items) == 1 and run["new"] == 1
    m = items[0]
    assert m["kind"] == "metric_likes" and m["target_tweet_id"] == hot["id"] and m["state"] == "pending"
    assert m["draft_text"] == "たくさんのいいね、ありがとうございます！" and m["metric"] == {"key": "like_count", "label": "いいね", "value": 150, "threshold": 100}
    assert m["text"] == "人気の投稿" and low["id"] not in m["id"]
    user = bedrock.calls[-1]["messages"][0]["content"][0]["text"]
    assert "いいねが150件になりました" in user and "あなた自身の投稿" in user
    assert run["reads"] >= 2                                                              # 自投稿の読み取りも、費用に入る


def test_metric_rule_is_checked_at_its_own_interval(app, clock):
    connect_and_start(app)
    for r in app.rules.list():
        app.rules.delete(r["id"])
    metric_rule(app)
    app.settings.update("job", {"metrics_interval_minutes": 60})
    calls = []
    orig = app.x.own_posts
    app.x.own_posts = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    app.jobs.run("manual")
    assert len(calls) == 1
    clock.advance(minutes=30)
    app.jobs.run("manual")
    assert len(calls) == 1                                                                # 間隔内は、読みに行かない（費用を抑える）
    clock.advance(minutes=31)
    app.x.add_post("新しい人気投稿", likes=500)
    app.jobs.run("manual")
    assert len(calls) == 2 and len(mentions(app)) == 1


def test_metric_not_checked_without_enabled_metric_rules(app):
    connect_and_start(app)
    called = []
    app.x.own_posts = lambda *a, **k: called.append(1)
    app.x.add_post("人気", likes=1000)
    app.jobs.run("manual")
    assert called == []                                                                   # 既定の（無効な）見本では、読みに行かない


def test_metric_multiple_thresholds_fire_independently(app):
    connect_and_start(app)
    for r in app.rules.list():
        app.rules.delete(r["id"])
    a = metric_rule(app, threshold=100, name="100")
    b = metric_rule(app, threshold=1000, name="1000")
    c = metric_rule(app, event="metric_impressions", threshold=2000, name="imp")
    app.x.add_post("大人気", likes=1200, impressions=5000)
    app.jobs.run("manual")
    assert sorted(m["rule_id"] for m in mentions(app)) == sorted([a["id"], b["id"], c["id"]])
    app.jobs.run("manual")
    assert len(mentions(app)) == 3                                                        # 同じ投稿・同じルールでは、二度と出ない


def test_metric_reply_goes_to_own_post(app):
    connect_and_start(app)
    for r in app.rules.list():
        app.rules.delete(r["id"])
    metric_rule(app)
    post = app.x.add_post("人気", likes=300)
    app.jobs.run("manual")
    m = mentions(app)[0]
    app.sender.send(m["id"], "みなさん、ありがとうございます！")
    assert app.x.sent == [{"text": "みなさん、ありがとうございます！", "in_reply_to": post["id"]}]   # 自投稿へのスレッド（追加コメント）


def test_metric_send_reply_is_guarded_like_others(app):
    connect_and_start(app)
    for r in app.rules.list():
        app.rules.delete(r["id"])
    metric_rule(app, actions=[{"type": "send_reply", "source": "template", "template_category": "fan"}])
    app.settings.update("safety", {"auto_daily_limit": 0})
    app.x.add_post("人気", likes=300)
    app.jobs.run("manual")
    assert app.x.sent == [] and mentions(app)[0]["state"] == "pending"


def test_live_x_client_new_calls(app):
    import json as j
    from conftest import FakeHttp
    from xronix.xclient import LiveX, XError
    http = FakeHttp()
    x = LiveX(http, sleep=lambda s: None)
    http.push(200, {"data": {"liked": True}})
    x.like("tok", "9", "123")
    call_ = http.calls[-1]
    assert call_["url"].endswith("/2/users/9/likes") and j.loads(call_["body"]) == {"tweet_id": "123"} and call_["method"] == "POST"
    http.push(403, {})
    with pytest.raises(XError, match="like.write"):
        x.like("tok", "9", "123")
    http.push(503, {})
    with pytest.raises(XError):
        x.like("tok", "9", "123")
    assert len(http.calls) == 3                                                           # 書き込みは再試行しない
    http.push(200, {"data": [{"id": "1", "public_metrics": {"like_count": 3}}]})
    r = x.own_posts("tok", "9", "2026-10-05T00:00:00Z", 20)
    url = http.calls[-1]["url"]
    assert r["reads"] == 1 and "start_time=2026-10-05T00%3A00%3A00Z" in url and "exclude=retweets%2Creplies" in url and "/2/users/9/tweets" in url
    http.push(200, {"data": [{"id": "5", "text": "x"}]})
    assert x.tweets("tok", ["5"])["reads"] == 1 and "in_reply_to_user_id" in http.calls[-1]["url"]
