import json
import random
from datetime import timedelta

import boto3
import pytest
from moto import mock_aws

from xronix.api import Api
from xronix.common import iso, parse_iso
from xronix.scheduler import AwsScheduler


def setup_job(app, **job):
    api = Api(app)
    api.handle("POST", "/api/x/connect")
    api.handle("PUT", "/api/settings/job", {}, {"enabled": True, **job})
    return api


def st(app):
    return app.repo.get("state", "job")


def next_at(app):
    return parse_iso(st(app)["next_run_at"])


def test_enabling_first_time_runs_soon_and_registers_schedule(app, scheduler, clock):
    setup_job(app, interval_minutes=30)
    assert next_at(app) == clock() + timedelta(seconds=15)
    assert scheduler.armed == [clock() + timedelta(seconds=15)] and st(app)["schedule_name"] == "sched-1"


@pytest.mark.parametrize("minutes", [1, 7, 60, 999])
def test_any_interval_from_1_to_999_minutes(app, clock, minutes):
    setup_job(app, interval_minutes=minutes)
    app.jobs.run("manual")                       # 実行の終わりに、次回が「開始＋間隔」で決まる
    assert next_at(app) == clock() + timedelta(minutes=minutes)


def test_next_run_is_replaced_and_old_schedule_cancelled(app, scheduler, clock):
    setup_job(app, interval_minutes=30)
    first = st(app)["schedule_name"]
    app.jobs.run("manual")
    assert st(app)["schedule_name"] != first and first in scheduler.cancelled


def test_jitter_is_within_range_and_reaches_seconds(app, clock):
    setup_job(app, interval_minutes=60, jitter_minutes=5)
    app.jobs.run("manual")
    base = clock() + timedelta(minutes=60)
    deltas, seconds = [], set()
    for _ in range(300):
        app.jobs.plan("test")
        d = (next_at(app) - base).total_seconds()
        deltas.append(d)
        seconds.add(next_at(app).second)
    assert all(-300 <= d <= 300 for d in deltas)           # ±5分以内
    assert min(deltas) < -240 and max(deltas) > 240        # 範囲の端まで使う
    assert len(seconds) >= 50                              # 00秒に固定されない（秒までランダム）
    assert len({d for d in deltas}) > 100


def test_no_jitter_keeps_exact_interval(app, clock):
    setup_job(app, interval_minutes=45, jitter_minutes=0)
    app.jobs.run("manual")
    for _ in range(20):
        app.jobs.plan("test")
        assert next_at(app) == clock() + timedelta(minutes=45)


def test_jitter_larger_than_interval_never_schedules_in_the_past(app, clock):
    setup_job(app, interval_minutes=1, jitter_minutes=99)
    app.jobs.run("manual")
    for _ in range(200):
        app.jobs.plan("test")
        assert next_at(app) >= clock() + timedelta(seconds=15)


def test_settings_change_replans(app, scheduler, clock):
    api = setup_job(app, interval_minutes=60)
    app.jobs.run("manual")
    before = next_at(app)
    api.handle("PUT", "/api/settings/job", {}, {"interval_minutes": 5})
    assert next_at(app) == clock() + timedelta(minutes=5) and next_at(app) < before


def test_disable_and_kill_cancel_the_schedule_and_resume_registers_again(app, scheduler, clock):
    api = setup_job(app, interval_minutes=30)
    name = st(app)["schedule_name"]
    api.handle("PUT", "/api/settings/job", {}, {"enabled": False})
    assert st(app)["next_run_at"] == "" and name in scheduler.cancelled
    api.handle("PUT", "/api/settings/job", {}, {"enabled": True})
    name2 = st(app)["schedule_name"]
    assert name2 and st(app)["next_run_at"]
    api.handle("POST", "/api/kill", {}, {"on": True})
    assert st(app)["next_run_at"] == "" and name2 in scheduler.cancelled
    assert api.handle("GET", "/api/status")[1]["next_due"] == ""
    api.handle("POST", "/api/kill", {}, {"on": False})
    assert st(app)["next_run_at"] != ""


def test_tick_runs_only_when_due(app, clock):
    setup_job(app, interval_minutes=30)
    assert app.jobs.tick("schedule") is None                 # 予定（15秒後）の前
    clock.advance(seconds=12)
    assert app.jobs.tick("schedule") is None                 # 予定の3秒前は早すぎる
    clock.advance(seconds=1)                                 # 予定の2秒前までは、起動の誤差の範囲なので実行する
    r = app.jobs.tick("schedule")
    assert r and r["trigger"] == "schedule" and r["status"] == "ok"
    assert next_at(app) == clock() + timedelta(minutes=30)
    assert app.jobs.tick("schedule") is None                 # 古い予定が重ねて起動しても、二重に実行しない


def test_watchdog_only_rescues_overdue_runs(app, clock):
    setup_job(app, interval_minutes=30)
    clock.advance(minutes=1)                                 # 予定の少し後。通常の起動がまだ来ていないだけかもしれない
    assert app.jobs.tick("watchdog") is None
    clock.advance(minutes=2)                                 # 予定から2分以上遅れ＝予定が失われたとみなす
    r = app.jobs.tick("watchdog")
    assert r and r["trigger"] == "watchdog"


def test_scheduler_failure_is_logged_and_watchdog_still_works(app, scheduler, clock):
    scheduler.fail = True
    setup_job(app, interval_minutes=30)
    assert st(app)["next_run_at"] and st(app)["schedule_name"] == ""
    assert any("登録に失敗" in l["message"] for l in app.repo.list("logs"))
    clock.advance(minutes=3)
    assert app.jobs.tick("watchdog")["status"] == "ok"


def test_tick_when_disabled_or_killed_does_nothing(app, clock):
    api = setup_job(app, interval_minutes=30)
    clock.advance(hours=2)
    api.handle("PUT", "/api/settings/job", {}, {"enabled": False})
    assert app.jobs.tick("schedule") is None and app.jobs.tick("watchdog") is None
    assert app.repo.list("runs") == []


def test_status_shows_next_run_with_seconds(app):
    api = setup_job(app, interval_minutes=30, jitter_minutes=3)
    app.jobs.run("manual")
    nxt = api.handle("GET", "/api/status")[1]["next_due"]
    assert len(nxt) == len("2026-10-12T03:00:00.000Z") and parse_iso(nxt)


# --- EventBridge Scheduler（moto） ---
@pytest.fixture
def aws_sched():
    with mock_aws():
        client = boto3.client("scheduler", region_name="ap-northeast-1")
        yield AwsScheduler("arn:aws:lambda:ap-northeast-1:123456789012:function:xronix-staging-job",
                           "arn:aws:iam::123456789012:role/xronix-staging-scheduler", "xronix-staging", client=client), client


def test_aws_scheduler_creates_one_time_schedule_with_second_resolution(aws_sched):
    sched, client = aws_sched
    when = parse_iso("2026-10-12T04:07:33.000Z")
    name = sched.arm(when)
    got = client.get_schedule(Name=name)
    assert got["ScheduleExpression"] == "at(2026-10-12T04:07:33)"          # 秒まで指定する
    assert got["ActionAfterCompletion"] == "DELETE" and got["FlexibleTimeWindow"]["Mode"] == "OFF"
    assert got["Target"]["Arn"].endswith("function:xronix-staging-job")
    assert json.loads(got["Target"]["Input"]) == {"source": "schedule"}
    assert got["Target"]["RetryPolicy"]["MaximumRetryAttempts"] == 0       # 失敗しても自動で再実行しない
    assert name.startswith("xronix-staging-next-") and len(name) <= 64
    assert sched.arm(when) != name                                          # 同時刻でも名前は重ならない
    sched.cancel(name)
    sched.cancel(name)                                                      # 無いものを消してもエラーにしない
    assert name not in [s["Name"] for s in client.list_schedules()["Schedules"]]


def test_lambda_settings_change_registers_schedule_in_aws(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
        boto3.resource("dynamodb").create_table(
            TableName="xt", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
        monkeypatch.setenv("XRONIX_TABLE", "xt")
        monkeypatch.setenv("XRONIX_JOB_ARN", "arn:aws:lambda:ap-northeast-1:123456789012:function:xronix-staging-job")
        monkeypatch.setenv("XRONIX_SCHEDULER_ROLE_ARN", "arn:aws:iam::123456789012:role/xronix-staging-scheduler")
        monkeypatch.setenv("XRONIX_SCHEDULE_PREFIX", "xronix-staging")
        from xronix import lambda_handlers as lh
        monkeypatch.setenv("XRONIX_ADMIN_EMAIL", "me@example.com")
        lh._api = None
        claims = {"sub": "s-1", "token_use": "id", "email": "me@example.com"}

        def put(body):
            return lh.api_handler({"requestContext": {"http": {"method": "PUT"}, "authorizer": {"jwt": {"claims": claims}}}, "rawPath": "/api/settings/job", "queryStringParameters": None,
                                   "headers": {"X-Requested-With": "xronix"}, "body": json.dumps(body)})

        client = boto3.client("scheduler")
        assert put({"enabled": True, "interval_minutes": 30, "jitter_minutes": 3})["statusCode"] == 200
        names = [s["Name"] for s in client.list_schedules()["Schedules"]]
        assert len(names) == 1 and names[0].startswith("xronix-staging-next-")
        assert put({"interval_minutes": 10})["statusCode"] == 200            # 変更すると古い予定は消え、新しい予定だけが残る
        names2 = [s["Name"] for s in client.list_schedules()["Schedules"]]
        assert len(names2) == 1 and names2 != names
        assert put({"enabled": False})["statusCode"] == 200
        assert client.list_schedules()["Schedules"] == []                    # 無効にすると予定が無くなる
        lh._api = None
