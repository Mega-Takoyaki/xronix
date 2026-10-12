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
