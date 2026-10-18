"""送信キュー: 自動送信は、すぐには送らず、間隔（秒、ランダムな幅つき）をあけて1件ずつ送る。"""
from datetime import timedelta

import pytest

from test_actions import connect_and_start, drain, mentions, only_rule
from xronix.api import Api
from xronix.common import parse_iso
from xronix.sender import SendRefused


def auto_rule(app):
    only_rule(app, categories=["fan"], actions=[{"type": "send_reply", "source": "template"}])


def run_three(app, authors=("1", "2", "3")):
    auto_rule(app)
    app.settings.update("safety", {"auto_daily_limit": 10, "daily_limit": 10, "conversation_reply_limit": 50, "author_daily_limit": 50})
    app.x.add_mentions(["可愛い1", "可愛い2", "可愛い3"], list(authors))
    app.jobs.run("manual")


def test_auto_send_is_queued_and_spaced_by_random_seconds(app, clock, scheduler):
    connect_and_start(app)
    app.settings.update("send", {"queue_interval_seconds": 100, "queue_jitter_seconds": 30})
    run_three(app)
    assert [m["state"] for m in mentions(app)] == ["send_queued"] * 3 and app.x.sent == []
    # 最初の1件は、最短15秒後。それまでは送らない
    assert scheduler.payloads[-1] == {"source": "sendqueue"}
    clock.advance(seconds=5)
    assert app.sendq.tick("schedule") is None and app.x.sent == []
    times, delays = [], []
    for _ in range(3):
        due = parse_iso(app.sendq.state()["next_at"])
        clock.advance(seconds=max(0, (due - clock()).total_seconds()))
        assert app.sendq.tick("schedule") is not None
        times.append(clock())
    delays = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    assert [s["text"] for s in app.x.sent] and len(app.x.sent) == 3
    assert all(70 <= d <= 130 for d in delays), delays                      # 間隔100秒 ± 30秒（秒までランダム）
    assert [m["state"] for m in mentions(app)] == ["sent"] * 3 and not app.sendq.queued()


def test_queue_sends_oldest_first_one_at_a_time(app, clock):
    connect_and_start(app)
    run_three(app)
    ids = [m["id"] for m in app.sendq.queued()]
    clock.advance(seconds=20)
    app.sendq.tick("schedule")
    assert [s["in_reply_to"] for s in app.x.sent] == ids[:1]                  # 1回の起動で、1件だけ
    assert app.sendq.tick("schedule") is None                                  # 間隔が過ぎるまで、次は送らない
    clock.advance(minutes=10)
    app.sendq.tick("schedule")
    assert [s["in_reply_to"] for s in app.x.sent] == ids[:2]


def test_cancel_returns_to_review_and_gives_back_the_auto_quota(app, clock):
    connect_and_start(app)
    auto_rule(app)
    app.settings.update("safety", {"auto_daily_limit": 1})
    app.x.add_mentions(["可愛い"])
    app.jobs.run("manual")
    m = mentions(app)[0]
    key = m["autosent_key"]
    assert app.repo.get("state", key)["count"] == 1
    out = Api(app).handle("POST", "/api/sendq/cancel", {}, {"id": m["id"]})
    assert out[0] == 200 and out[1]["state"] == "review" and out[1]["draft_text"] == m["queued_text"]
    assert app.repo.get("state", key)["count"] == 0 and not app.sendq.queued()
    clock.advance(minutes=30)
    assert app.sendq.tick("schedule") is None and app.x.sent == []
    assert Api(app).handle("POST", "/api/sendq/cancel", {}, {"id": m["id"]})[0] == 409  # 送信待ちでなければ、外せない


def test_manual_approval_goes_through_queue_only_when_enabled(app, clock):
    connect_and_start(app)
    only_rule(app, categories=["fan"], actions=[{"type": "queue_reply", "source": "template", "template_category": "fan"}])
    app.x.add_mentions(["可愛い", "可愛い"], ["1", "2"])
    app.jobs.run("manual")
    a, b = [m for m in mentions(app)]
    api = Api(app)
    assert api.handle("POST", f"/api/mentions/{a['id']}/reply", {}, {"text": "ありがとう"})[1]["state"] == "sent"   # 既定は、即時
    app.settings.update("send", {"manual_via_queue": True})
    st, body = api.handle("POST", f"/api/mentions/{b['id']}/reply", {}, {"text": "ありがとうございます"})[:2]
    assert st == 200 and body["state"] == "send_queued" and len(app.x.sent) == 1
    assert drain(app, clock) == 1 and mentions(app)[1]["state"] == "sent" and mentions(app)[1]["final_text"] == "ありがとうございます"
    # 文面の誤りは、キューに入れる時に断る
    app.x.add_mentions(["可愛い"], ["3"])
    app.jobs.run("manual")
    c = mentions(app)[-1]
    assert api.handle("POST", f"/api/mentions/{c['id']}/reply", {}, {"text": "見て https://example.com"})[0] == 409


def test_kill_switch_pauses_queue_and_resume_rearms(app, clock, scheduler):
    connect_and_start(app)
    run_three(app)
    api = Api(app)
    api.handle("POST", "/api/kill", {}, {"on": True})
    clock.advance(minutes=30)
    assert app.sendq.tick("schedule") is None and app.x.sent == [] and scheduler.cancelled
    assert len(app.sendq.queued()) == 3                                          # 消えない
    api.handle("POST", "/api/kill", {}, {"on": False})
    assert app.sendq.state()["schedule_name"]                                    # 解除すると、再登録される
    clock.advance(minutes=1)
    assert app.sendq.tick("schedule") is not None


def test_watchdog_sends_when_the_schedule_was_lost(app, clock, scheduler):
    connect_and_start(app)
    scheduler.fail = True                                                          # 次の送信の登録に失敗した（見守りが代わりに送る）
    run_three(app)
    due = parse_iso(app.sendq.state()["next_at"])
    clock.advance(seconds=(due - clock()).total_seconds() + 60)
    assert app.sendq.tick("watchdog") is None                                    # 予定から2分たっていなければ、見守りは送らない
    clock.advance(minutes=15)                                                      # 次の見守り（15分後）
    assert app.sendq.tick("watchdog") is not None and len(app.x.sent) == 1


def test_lost_schedule_is_rearmed_when_ticked_early(app, clock, scheduler):
    connect_and_start(app)
    run_three(app)
    n = len(scheduler.armed)
    clock.advance(seconds=60)                                                      # 予定を少し過ぎたのに、起動されなかった
    assert app.sendq.tick("watchdog") is None and len(scheduler.armed) == n + 1  # 登録し直す（自己修復）


def test_queue_recheck_at_send_time_returns_to_review(app, clock):
    connect_and_start(app)
    run_three(app, authors=("1", "1", "1"))   # 同じ相手に3通。会話の上限は、送る時にも確認する
    assert [m["state"] for m in mentions(app)] == ["send_queued"] * 3
    app.settings.update("safety", {"conversation_reply_limit": 1})
    drain(app, clock)
    states = [m["state"] for m in mentions(app)]
    assert states.count("sent") == 1 and states.count("review") == 2 and len(app.x.sent) == 1
    assert any("上限" in m["error"] for m in mentions(app) if m["state"] == "review")


def test_failed_send_does_not_block_the_queue(app, clock):
    connect_and_start(app)
    run_three(app)
    app.x.fail_next_create = "definite"
    drain(app, clock)
    states = sorted(m["state"] for m in mentions(app))
    assert states == ["failed", "sent", "sent"] and not app.sendq.queued()


def test_enqueue_refuses_blocked_author_and_bad_state(app):
    connect_and_start(app)
    only_rule(app, categories=["fan"], actions=[{"type": "queue_reply", "source": "template", "template_category": "fan"}])
    app.x.add_mentions(["可愛い"], ["9"])
    app.jobs.run("manual")
    m = mentions(app)[0]
    app.repo.put("blocklist", "9", {"id": "9", "note": "x"})
    with pytest.raises(SendRefused):
        app.sendq.enqueue(m["id"], "ありがとう")
    app.repo.delete("blocklist", "9")
    app.sendq.enqueue(m["id"], "ありがとう")
    with pytest.raises(SendRefused):
        app.sendq.enqueue(m["id"], "ありがとう")                                  # 二重にはキューに入れない


def test_send_settings_validation(app):
    from xronix.settings import ValidationError
    for bad in ({"queue_interval_seconds": 9}, {"queue_interval_seconds": 3601}, {"queue_jitter_seconds": -1}, {"queue_jitter_seconds": 1801}):
        with pytest.raises(ValidationError):
            app.settings.update("send", bad)
    assert app.settings.update("send", {"queue_interval_seconds": 10, "queue_jitter_seconds": 0})["queue_interval_seconds"] == 10
