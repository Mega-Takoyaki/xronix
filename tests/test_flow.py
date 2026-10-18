import json

import pytest

from conftest import FakeHttp
from xronix.api import Api
from xronix.sender import SendRefused
from xronix.xclient import AuthRequired, CreateError


def add(app, *texts, authors=None):
    return app.x.add_mentions(list(texts), authors)


def run(app):
    return app.jobs.run("manual")


def start(app):
    """接続→初回実行(現在地点から)→その後に新着を入れて実行できる状態にする。"""
    Api(app).handle("POST", "/api/x/connect")
    r = run(app)
    assert r["status"] == "ok" and r["new"] == 0
    return Api(app)


def mentions(app, state=None):
    return [m for m in app.repo.list("mentions") if state is None or m["state"] == state]


def test_first_run_starts_from_now_without_ingest(app):
    add(app, "過去のメンション")
    Api(app).handle("POST", "/api/x/connect")
    r = run(app)
    assert r["status"] == "ok" and r["new"] == 0 and mentions(app) == []
    assert app.repo.get("state", "cursor")["since_id"]


def test_reset_cursor_restarts_first_run(app):
    api = start(app)
    assert app.repo.get("state", "cursor")
    assert api.handle("POST", "/api/jobs/reset-cursor", {}, {"mode": "bogus"})[0] == 400
    add(app, "リセット前の反応")
    run(app)
    n = len(mentions(app))
    assert api.handle("POST", "/api/jobs/reset-cursor", {}, {"mode": "backfill"})[0] == 200
    assert app.repo.get("state", "cursor") is None and app.settings.get("job")["initial_mode"] == "backfill"
    assert len(mentions(app)) == n  # 取り込み済みの反応は消えない
    run(app)
    assert app.repo.get("state", "cursor") and len(mentions(app)) == n  # 二重に取り込まない
    api.handle("POST", "/api/jobs/reset-cursor", {}, {"mode": "from_now"})
    assert app.settings.get("job")["initial_mode"] == "from_now" and app.repo.get("state", "cursor") is None


def test_pipeline_classifies_and_drafts(app):
    start(app)
    add(app, "可愛い投稿ありがとうございます！", "どこで撮りましたか？", "副業で稼げます https://x.example", "DM送ってもいいですか？", "雨ですね")
    r = run(app)
    assert r["new"] == 5 and r["drafted"] == 2
    by = {m["text"][:3]: m for m in mentions(app)}
    assert by["可愛い"]["state"] == "pending" and by["可愛い"]["draft_text"]
    assert by["どこで"]["state"] == "pending" and by["どこで"]["category"] == "question"
    assert by["副業で"]["state"] == "spam" and by["副業で"]["draft_text"] == ""
    assert by["DM送"]["state"] == "review" and by["DM送"]["draft_text"] == ""
    assert by["雨です"]["state"] == "review"
    assert run(app)["new"] == 0  # 差分取得: 同じものを二重に取り込まない


def test_blocked_author_gets_no_draft(app):
    api = start(app)
    api.handle("POST", "/api/blocklist", body={"author_id": "555"})
    add(app, "可愛い！", authors=["555"])
    run(app)
    m = mentions(app)[0]
    assert m["state"] == "blocked" and m["draft_text"] == ""


def test_template_rotation_avoids_repeats(app):
    start(app)
    add(app, "可愛い", "かわいい", "好きです")
    run(app)
    drafts = [m["draft_text"] for m in mentions(app)]
    assert len(set(drafts)) == 3  # 定型文は最近使っていないものから選ぶ


def test_job_tick_respects_interval_enable_and_kill(app, clock):
    api = start(app)
    assert app.jobs.tick() is None                      # 無効
    api.handle("PUT", "/api/settings/job", body={"enabled": True, "interval_minutes": 30})
    assert app.jobs.tick() is None                      # 直前に実行済み
    clock.advance(minutes=29)
    assert app.jobs.tick() is None
    clock.advance(minutes=2)
    assert app.jobs.tick()["trigger"] == "schedule"
    api.handle("POST", "/api/kill", body={"on": True})
    clock.advance(minutes=31)
    assert app.jobs.tick() is None
    assert run(app)["status"] == "skipped"


def test_job_not_connected_is_error(app):
    r = run(app)
    assert r["status"] == "error"
    assert app.repo.list("runs")[0]["status"] == "error"


def test_job_lease_blocks_concurrent_run(app, clock):
    start(app)
    from xronix.common import Lease
    assert Lease(app.repo, "job", clock).acquire("other", 600)
    assert run(app)["status"] == "skipped"


# --- 送信 ---
def pending_id(app, text="可愛い！"):
    add(app, text)
    run(app)
    return [m for m in mentions(app, "pending")][-1]["id"]


def test_send_success_updates_state_counter_template(app):
    start(app)
    mid = pending_id(app)
    m = app.repo.get("mentions", mid)
    out = app.sender.send(mid, m["draft_text"])
    assert out["state"] == "sent" and out["sent_tweet_id"].startswith("mock-")
    assert app.x.sent == [{"text": m["draft_text"], "in_reply_to": mid}]
    assert app.sender.sent_today() == 1
    assert app.repo.get("templates", m["draft_template_id"])["use_count"] == 1
    with pytest.raises(SendRefused):                      # 二重送信は不可
        app.sender.send(mid, "again")
    assert len(app.x.sent) == 1


@pytest.mark.parametrize("text,msg", [("", "空"), ("見て https://a.example", "リンク"), ("あ" * 141, "文字数")])
def test_send_refuses_bad_text(app, text, msg):
    start(app)
    mid = pending_id(app)
    with pytest.raises(SendRefused, match=msg):
        app.sender.send(mid, text)
    assert app.x.sent == [] and app.repo.get("mentions", mid)["state"] == "pending"


def test_send_daily_limit_and_kill_switch(app):
    api = start(app)
    api.handle("PUT", "/api/settings/safety", body={"daily_limit": 1})
    a, b = pending_id(app, "可愛い1"), pending_id(app, "可愛い2")
    app.sender.send(a, "ありがとう")
    with pytest.raises(SendRefused, match="上限"):
        app.sender.send(b, "ありがとう")
    assert app.repo.get("mentions", b)["state"] == "pending" and len(app.x.sent) == 1
    api.handle("POST", "/api/kill", body={"on": True})
    with pytest.raises(SendRefused, match="緊急停止"):
        app.sender.send(b, "ありがとう")


def test_send_failure_definite_vs_unknown(app):
    start(app)
    a, b = pending_id(app, "可愛い1"), pending_id(app, "可愛い2")
    app.x.fail_next_create = "definite"
    with pytest.raises(CreateError):
        app.sender.send(a, "ありがとう")
    assert app.repo.get("mentions", a)["state"] == "failed" and app.sender.sent_today() == 0
    app.x.fail_next_create = "unknown"
    with pytest.raises(CreateError):
        app.sender.send(b, "ありがとう")
    assert app.repo.get("mentions", b)["state"] == "unknown" and app.sender.sent_today() == 1
    with pytest.raises(SendRefused):                      # 結果不明のものは自動で再送しない
        app.sender.send(b, "ありがとう")
    assert app.sender.send(a, "ありがとう")["state"] == "sent"   # 未投稿が確実なものは再送できる


def test_send_blocked_author_refused(app):
    api = start(app)
    mid = pending_id(app)
    api.handle("POST", f"/api/mentions/{mid}/block")
    assert app.repo.get("mentions", mid)["state"] == "blocked"
    with pytest.raises(SendRefused):
        app.sender.send(mid, "ありがとう")


def test_concurrent_send_only_one_posts(app):
    start(app)
    mid = pending_id(app)
    m = app.repo.get("mentions", mid)
    # 別の呼び出しが先に sending へ進めた状況を再現
    app.repo.update_if("mentions", mid, {"state": "sending"})
    with pytest.raises(SendRefused):
        app.sender.send(mid, "ありがとう")
    assert app.x.sent == []


# --- トークン ---
def test_token_refresh_rotates_and_is_single_flight(app, clock):
    api = Api(app)
    api.handle("POST", "/api/x/connect")
    t1 = app.tokens.get_access_token()
    assert app.tokens.get_access_token() == t1          # 期限内は更新しない
    old_refresh = app.secrets.get("x_refresh_token")
    clock.advance(hours=3)
    calls = []
    orig = app.x.refresh
    app.x.refresh = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    t2 = app.tokens.get_access_token()
    assert t2 != t1 and app.secrets.get("x_refresh_token") != old_refresh and len(calls) == 1
    assert app.tokens.get_access_token() == t2 and len(calls) == 1


def test_token_waits_when_lease_held_then_uses_fresh(app, clock):
    Api(app).handle("POST", "/api/x/connect")
    clock.advance(hours=3)
    from xronix.common import Lease
    Lease(app.repo, "token", clock).acquire("other", 60)
    def other_finishes(s):
        app.tokens.save_tokens({"access_token": "fresh", "refresh_token": "r2", "expires_in": 7200})
    app.tokens.sleep = other_finishes
    assert app.tokens.get_access_token() == "fresh"


def test_token_invalid_grant_marks_reauth(app, clock):
    Api(app).handle("POST", "/api/x/connect")
    clock.advance(hours=3)
    def bad(*a, **k): raise AuthRequired("認可が無効です")
    app.x.refresh = bad
    with pytest.raises(AuthRequired):
        app.tokens.get_access_token()
    with pytest.raises(AuthRequired):                   # 以降は更新を試さず再接続を促す
        app.tokens.get_access_token()
    assert Api(app).handle("GET", "/api/status")[1]["auth_error"]


def test_resolve_unknown(app):
    api = start(app)
    a, b = pending_id(app, "可愛い1"), pending_id(app, "可愛い2")
    for mid in (a, b):
        app.x.fail_next_create = "unknown"
        with pytest.raises(CreateError):
            app.sender.send(mid, "ありがとう")
    assert app.sender.sent_today() == 2
    assert api.handle("POST", f"/api/mentions/{a}/resolve", {}, {"outcome": "sent"})[0] == 200
    assert api.handle("POST", f"/api/mentions/{b}/resolve", {}, {"outcome": "not_sent"})[0] == 200
    assert app.repo.get("mentions", a)["state"] == "sent" and app.repo.get("mentions", b)["state"] == "failed"
    assert app.sender.sent_today() == 1
    assert api.handle("POST", f"/api/mentions/{a}/resolve", {}, {"outcome": "sent"})[0] == 409


def test_wide_fetch_picks_up_replies_without_my_mention(app):
    start(app)
    # 自分の@が無い返信は、メンション一覧には出ず、検索（to:）でだけ取れる
    app.x.replies_queue.append({"id": "2100000000000000001", "author_id": "700", "text": "いい投稿ですね",
                                "conversation_id": "1", "in_reply_to_user_id": "9999", "referenced_tweets": [{"type": "replied_to", "id": "1"}]})
    r = run(app)
    assert r["new"] == 1 and r["thread_reads"] == 1
    m = mentions(app)[0]
    assert m["kind"] == "reply_to_post" and m["id"] == "2100000000000000001"
    assert run(app)["new"] == 0  # 二重に取り込まない


def test_wide_fetch_dedups_with_mentions_and_can_be_turned_off(app):
    start(app)
    app.settings.update("job", {"wide_fetch": False})
    app.x.replies_queue.append({"id": "2100000000000000002", "author_id": "701", "text": "返信", "conversation_id": "1",
                                "in_reply_to_user_id": "9999", "referenced_tweets": [{"type": "replied_to", "id": "1"}]})
    assert run(app)["new"] == 0 and mentions(app) == []


def test_wide_fetch_first_run_after_deploy_backfills_replies(app):
    start(app)  # メンションの取得位置は、すでにある
    app.repo.delete("state", "reply_cursor")
    app.x.replies_queue.append({"id": "2100000000000000003", "author_id": "702", "text": "以前の返信", "conversation_id": "1",
                                "in_reply_to_user_id": "9999", "referenced_tweets": [{"type": "replied_to", "id": "1"}]})
    assert run(app)["new"] == 1


def test_quotes_fetched_only_when_count_grows(app, clock):
    start(app)
    p = app.x.add_post("自分の投稿", quotes=1)
    app.x.quotes_db[p["id"]] = [{"id": "2200000000000000001", "author_id": "703", "text": "引用です", "conversation_id": "2200000000000000001",
                                 "referenced_tweets": [{"type": "quoted", "id": p["id"]}]}]
    calls = []
    orig = app.x.quotes_of
    app.x.quotes_of = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    r = run(app)
    assert r["new"] == 1 and mentions(app)[0]["kind"] == "quote" and len(calls) == 1
    clock.advance(minutes=400)  # 確認の間隔を過ぎても、数が増えていなければ読まない
    run(app)
    assert len(calls) == 1  # 引用の数が増えていない投稿は、読みに行かない


def test_reset_cursor_also_resets_wide_state(app):
    api = start(app)
    app.repo.put("state", "quote_counts", {"1": 3})
    app.repo.put("state", "reply_counts", {"1": 3})
    api.handle("POST", "/api/jobs/reset-cursor", {}, {"mode": "from_now"})
    assert app.repo.get("state", "reply_cursor") is None and app.repo.get("state", "quote_counts") is None and app.repo.get("state", "reply_counts") is None


def test_wide_fetch_failure_does_not_fail_the_run(app):
    from xronix.xclient import XError
    start(app)

    def boom(*a, **k):
        raise XError("検索は使えません", 403)
    app.x.replies_to_me = boom
    r = run(app)
    assert r["status"] == "ok"
    assert any("返信の検索に失敗" in l["message"] for l in app.repo.list("logs"))


def test_truncated_mentions_message_is_honest(app):
    start(app)
    orig = app.x.mentions
    app.x.mentions = lambda *a, **k: {**orig(*a, **k), "truncated": True}
    r = run(app)
    assert "取り込めませんでした" in r["message"] and "持ち越" not in r["message"]


def test_verbose_logs_explain_each_step(app):
    start(app)
    add(app, "可愛い投稿ありがとうございます！")
    app.x.replies_queue.append({"id": "2100000000000000009", "author_id": "710", "text": "返信", "conversation_id": "1",
                                "in_reply_to_user_id": "9999", "referenced_tweets": [{"type": "replied_to", "id": "1"}]})
    r = run(app)
    msgs = [l["message"] for l in app.repo.list("logs") if l["level"] == "debug" and l["extra"].get("run_id") == r["id"]]
    text = "\n".join(msgs)
    for needle in ("開始:", "メンション一覧: 1件を取得", "返信の検索（to:@", "取り込み:", "取得元=メンション一覧", "取得元=返信の検索", "終了:"):
        assert needle in text, needle
    assert "可愛い投稿ありがとう" not in text  # 本文は記録しない
    assert all(m["source"] for m in mentions(app))
    # メンション一覧と検索の両方に出た投稿は、1回だけ取り込み、もう一方は「取り込み済み」と記録する
    both = {"id": "2300000000000000001", "author_id": "711", "text": "両方に出る返信", "conversation_id": "1",
            "in_reply_to_user_id": "9999", "referenced_tweets": [{"type": "replied_to", "id": "1"}]}
    app.x.queue.append(both)
    app.x.replies_queue.append(both)
    r2 = run(app)
    assert r2["new"] == 1
    assert any("取り込み済み" in l["message"] for l in app.repo.list("logs") if l["level"] == "debug" and l["extra"].get("run_id") == r2["id"])


def test_verbose_log_can_be_turned_off_and_logs_api_filters(app):
    api = start(app)
    app.settings.update("job", {"verbose_log": False})
    add(app, "x")
    r = run(app)
    assert not [l for l in app.repo.list("logs") if l["level"] == "debug" and l["extra"].get("run_id") == r["id"]]
    app.settings.update("job", {"verbose_log": True})
    r2 = run(app)
    _, body = api.handle("GET", "/api/logs", {}, {})[:2]
    assert all(i["level"] != "debug" for i in body["items"])
    _, body = api.handle("GET", "/api/logs", {"level": "all", "run_id": r2["id"]}, {})[:2]
    assert body["items"] and all(i["extra"]["run_id"] == r2["id"] for i in body["items"])


def test_x_error_message_includes_reason(app):
    from conftest import FakeHttp
    from xronix.xclient import LiveX, XError
    http = FakeHttp()
    x = LiveX(http, sleep=lambda s: None)
    http.push(403, {"title": "Forbidden", "detail": "Your client app is not configured with the appropriate oauth1 app permissions for this endpoint."})
    with pytest.raises(XError) as e:
        x.replies_to_me("tok", "example_account", None)
    assert "/2/tweets/search/recent" in str(e.value) and "HTTP 403" in str(e.value) and "Forbidden" in str(e.value) and "tok" not in str(e.value)


def test_reply_cursor_is_never_zero(app):
    """検索は since_id=0 を拒否する。結果が空の最初の実行でも、"0" を保存しない"""
    start(app)
    app.repo.delete("state", "reply_cursor")
    app.repo.put("state", "cursor", {"since_id": "1"})
    r = run(app)
    cur = app.repo.get("state", "reply_cursor")
    assert r["status"] == "ok" and cur is not None and cur["since_id"] != "0"


# --- 取得の診断 ---
def _tweet(app, tid, **kw):
    t = {"id": tid, "author_id": "800", "text": "テスト返信です", "conversation_id": "1", "created_at": "2026-10-16T10:00:00.000Z", **kw}
    app.x.tweets_db[tid] = t
    return t


def test_parse_tweet_id():
    from xronix.diagnose import parse_tweet_id
    from xronix.settings import ValidationError
    assert parse_tweet_id("https://x.com/someone/status/2111043933341949959?s=46&t=abc") == "2111043933341949959"
    assert parse_tweet_id(" 2111043933341949959 ") == "2111043933341949959"
    for bad in ("", "abc", "https://example.com/"):
        with pytest.raises(ValidationError):
            parse_tweet_id(bad)


def test_diagnose_reply_to_other_user_is_out_of_scope(app):
    api = start(app)
    _tweet(app, "2111043933341949959", in_reply_to_user_id="12345", referenced_tweets=[{"type": "replied_to", "id": "2111000000000000000"}])
    _tweet(app, "2111000000000000000", author_id="12345")
    st, d = api.handle("POST", "/api/diagnose/tweet", {}, {"input": "https://x.com/a/status/2111043933341949959?s=46"})[:2]
    assert st == 200 and d["found"] and d["kind"] == "mention" and d["directed_at_me"] is False and d["mentions_me"] is False
    assert d["parent"]["by_me"] is False and d["reads"] == 2
    assert any("返信先が自分ではありません" in x and "対象外" in x for x in d["verdict"])
    assert d["ingested"] is None and mentions(app) == []        # 診断だけでは、取り込まない
    assert any("取得の診断" in l["message"] for l in app.repo.list("logs"))


def test_diagnose_reply_to_me_and_manual_ingest(app):
    api = start(app)
    _tweet(app, "2111043933341949960", in_reply_to_user_id="9999", referenced_tweets=[{"type": "replied_to", "id": "1"}])
    _tweet(app, "1", author_id="9999")
    _, d = api.handle("POST", "/api/diagnose/tweet", {}, {"input": "2111043933341949960"})[:2]
    assert d["directed_at_me"] is True and d["kind"] == "reply_to_post" and d["mentions_me"] is False
    assert any("返信先は、自分です" in x for x in d["verdict"]) and any("返信の検索（to:）だけ" in x for x in d["verdict"])
    _, d2 = api.handle("POST", "/api/diagnose/tweet", {}, {"input": "2111043933341949960", "ingest": True})[:2]
    assert d2["ingest_result"]["state"] and mentions(app)[0]["source"] == "手動の取り込み（診断）"
    _, d3 = api.handle("POST", "/api/diagnose/tweet", {}, {"input": "2111043933341949960"})[:2]
    assert d3["ingested"]["source"] == "手動の取り込み（診断）" and any("取り込み済み" in x for x in d3["verdict"])


def test_diagnose_missing_tweet_and_errors(app):
    api = start(app)
    _, d = api.handle("POST", "/api/diagnose/tweet", {}, {"input": "2111043933341949999"})[:2]
    assert d["found"] is False and "返しませんでした" in d["verdict"][0]
    assert api.handle("POST", "/api/diagnose/tweet", {}, {"input": "nope"})[0] == 400


def test_diagnose_posts_compares_x_counts_with_ingested(app):
    api = start(app)
    p = app.x.add_post("自分の投稿", quotes=2)
    p["public_metrics"]["reply_count"] = 3
    app.repo.put("mentions", "r1", {"id": "r1", "kind": "reply_to_post", "conversation_id": p["id"], "parent_id": p["id"], "state": "done"})
    st, d = api.handle("POST", "/api/diagnose/posts", {}, {})[:2]
    row = d["posts"][0]
    assert st == 200 and (row["x_replies"], row["got_replies"], row["x_quotes"], row["got_quotes"]) == (3, 1, 2, 0)


def test_diagnose_requires_connection(app):
    st, body = Api(app).handle("POST", "/api/diagnose/tweet", {}, {"input": "2111043933341949959"})[:2]
    assert st == 502 and "接続されていません" in body["error"]


def test_verbose_log_lists_own_posts_with_x_counts(app):
    start(app)
    p = app.x.add_post("自分の投稿", quotes=1)
    p["public_metrics"]["reply_count"] = 4
    r = run(app)
    text = "\n".join(l["message"] for l in app.repo.list("logs") if l["level"] == "debug" and l["extra"].get("run_id") == r["id"])
    assert f"自投稿 {p['id']}" in text and "Xの数 返信4・引用1" in text and "取り込み済み 返信0・引用0" in text


def test_diagnose_probes_search_and_explains_when_x_hides_the_tweet(app):
    api = start(app)
    tid = str((int(app.now().timestamp() * 1000) - 1288834974657 - 3600_000) << 22)  # アプリの時計で、1時間前の投稿ID
    _tweet(app, tid, author_id="800", in_reply_to_user_id="9999", conversation_id="1", referenced_tweets=[{"type": "replied_to", "id": "1"}])
    _, d = api.handle("POST", "/api/diagnose/tweet", {}, {"input": tid})[:2]
    assert [p["contains"] for p in d["probes"]] == [False, False]
    assert any("どの検索にも、この投稿は出ませんでした" in x for x in d["verdict"])
    app.x.search_index.append(app.x.tweets_db[tid])  # Xの検索に載っている場合
    _, d = api.handle("POST", "/api/diagnose/tweet", {}, {"input": tid})[:2]
    assert all(p["contains"] for p in d["probes"]) and any("検索には、この投稿が出ます" in x for x in d["verdict"])
    assert d["probes"][0]["query"].startswith("from:800 to:")


def test_diagnose_skips_probe_for_old_tweets_and_reports_probe_errors(app):
    from xronix.xclient import XError
    api = start(app)
    old = str((int(app.now().timestamp() * 1000) - 1288834974657 - 10 * 86400_000) << 22)  # アプリの時計で、10日前の投稿ID
    _tweet(app, old, in_reply_to_user_id="9999", referenced_tweets=[{"type": "replied_to", "id": "1"}])
    _, d = api.handle("POST", "/api/diagnose/tweet", {}, {"input": old})[:2]
    assert d["probes"] == [] and any("7日以上たっている" in x for x in d["verdict"])
    tid = str((int(app.now().timestamp() * 1000) - 1288834974657 - 3600_000) << 22)
    _tweet(app, tid, in_reply_to_user_id="9999", referenced_tweets=[{"type": "replied_to", "id": "1"}])
    def boom(*a, **k): raise XError("検索は使えません", 403)
    app.x.search_recent = boom
    _, d = api.handle("POST", "/api/diagnose/tweet", {}, {"input": tid})[:2]
    assert all("error" in p for p in d["probes"]) and any("検索がエラーになりました" in x for x in d["verdict"])


def test_live_search_recent_request():
    import urllib.parse
    from conftest import FakeHttp
    from xronix.xclient import LiveX
    http = FakeHttp()
    x = LiveX(http, sleep=lambda s: None)
    http.push(200, {"data": [{"id": "5"}], "meta": {}})
    r = x.search_recent("tok", "from:800 to:me", 10)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(http.calls[0]["url"]).query)
    assert r["reads"] == 1 and q["query"] == ["from:800 to:me"] and q["max_results"] == ["10"] and "since_id" not in q


def test_conversation_search_fills_in_replies_x_counts_but_lists_miss(app, clock):
    start(app)
    p = app.x.add_post("自分の投稿")
    p["public_metrics"]["reply_count"] = 3
    pid = p["id"]
    reply = {"id": "2300000000000000101", "author_id": "811", "text": "@example_mock Yummy", "conversation_id": pid, "in_reply_to_user_id": "9999",
             "referenced_tweets": [{"type": "replied_to", "id": pid}]}
    to_other = {"id": "2300000000000000102", "author_id": "812", "text": "他の人への返信", "conversation_id": pid, "in_reply_to_user_id": "12345",
                "referenced_tweets": [{"type": "replied_to", "id": "2300000000000000101"}]}
    own = {"id": "2300000000000000103", "author_id": "9999", "text": "自分の返信", "conversation_id": pid, "in_reply_to_user_id": "811"}
    app.x.search_index += [reply, to_other, own]
    r = run(app)
    got = {m["id"]: m for m in mentions(app)}
    assert set(got) == {reply["id"]} and got[reply["id"]]["source"] == "会話の検索" and got[reply["id"]]["kind"] == "reply_to_post"   # 自分宛てだけ。他人同士と自分の投稿は取らない
    assert r["thread_reads"] == 3
    assert app.repo.get("state", "reply_counts")[pid] == 3
    # 返信数が増えていなければ、検索しない（費用を抑える）
    clock.advance(minutes=800)
    calls = []
    orig = app.x.search_recent
    app.x.search_recent = lambda *a, **k: (calls.append(a[1]), orig(*a, **k))[1]
    run(app)
    assert not [c for c in calls if c.startswith("conversation_id:")]
    p["public_metrics"]["reply_count"] = 4
    clock.advance(minutes=800)
    run(app)
    assert [c for c in calls if c.startswith("conversation_id:")] == [f"conversation_id:{pid}"]


def test_conversation_search_failure_keeps_job_ok_and_retries_later(app):
    from xronix.xclient import XError
    start(app)
    p = app.x.add_post("自分の投稿")
    p["public_metrics"]["reply_count"] = 2
    def boom(*a, **k): raise XError("検索は使えません", 403)
    app.x.search_recent = boom
    r = run(app)
    assert r["status"] == "ok" and any("返信の補完（会話の検索）に失敗" in l["message"] for l in app.repo.list("logs"))
    assert p["id"] not in (app.repo.get("state", "reply_counts") or {})   # 失敗したら、数を覚えない（次回、やり直す）
