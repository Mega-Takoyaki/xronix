import json

import boto3
import pytest
from moto import mock_aws

from conftest import FakeHttp
from xronix.api import Api


def call(api, method, path, body=None, query=None):
    status, payload, headers = api.handle(method, path, query or {}, body or {})
    return status, payload


def test_unknown_route_and_method(app):
    api = Api(app)
    assert call(api, "GET", "/api/nope")[0] == 404
    assert call(api, "DELETE", "/api/status")[0] == 405


def test_secrets_never_returned(app):
    api = Api(app)
    assert call(api, "PUT", "/api/secrets/ai_api_key", {"value": "SECRET-KEY-123"})[0] == 200
    assert call(api, "PUT", "/api/secrets/x_access_token", {"value": "x"})[0] == 404  # トークンは画面から書けない
    _, s = call(api, "GET", "/api/settings")
    assert s["secrets"]["ai_api_key"] is True
    dump = json.dumps(s) + json.dumps(call(api, "GET", "/api/logs")[1], ensure_ascii=False)
    assert "SECRET-KEY-123" not in dump
    assert call(api, "DELETE", "/api/secrets/ai_api_key")[0] == 200
    assert call(api, "GET", "/api/settings")[1]["secrets"]["ai_api_key"] is False


def test_templates_crud_and_validation(app):
    api = Api(app)
    st, t = call(api, "POST", "/api/templates", {"category": "fan", "text": "ありがとう！"})
    assert st == 201
    assert call(api, "POST", "/api/templates", {"category": "fan", "text": "見て https://x.example"})[0] == 400
    assert call(api, "POST", "/api/templates", {"category": "spam", "text": "x"})[0] == 400
    assert call(api, "PUT", f"/api/templates/{t['id']}", {"enabled": False})[1]["enabled"] is False
    assert call(api, "DELETE", f"/api/templates/{t['id']}")[0] == 200
    assert call(api, "PUT", "/api/templates/none", {"enabled": True})[0] == 404


def test_disabled_templates_not_used(app):
    api = Api(app)
    for t in call(api, "GET", "/api/templates")[1]["items"]:
        call(api, "PUT", f"/api/templates/{t['id']}", {"enabled": False})
    call(api, "POST", "/api/x/connect")
    app.jobs.run()
    app.x.add_mentions(["可愛い！"])
    app.jobs.run()
    m = app.repo.list("mentions")[0]
    assert m["state"] == "review" and m["draft_text"] == ""   # 定型文が無ければ人が書く


def test_queue_reply_reject_redraft_via_api(app):
    api = Api(app)
    call(api, "POST", "/api/x/connect")
    app.jobs.run()
    app.x.add_mentions(["可愛い！", "好き！"])
    app.jobs.run()
    items = call(api, "GET", "/api/mentions", query={"view": "queue"})[1]["items"]
    assert len(items) == 2 and items[0]["label"] == "ファンの感想"
    a, b = items[0]["id"], items[1]["id"]
    new = call(api, "POST", f"/api/mentions/{a}/redraft")[1]
    assert new["draft_text"] != items[0]["draft_text"]
    assert call(api, "POST", f"/api/mentions/{a}/reply", {"text": "ありがとう"})[0] == 200
    assert call(api, "POST", f"/api/mentions/{a}/reply", {"text": "again"})[0] == 409
    assert call(api, "POST", f"/api/mentions/{b}/reject")[0] == 200
    assert call(api, "GET", "/api/mentions", query={"view": "queue"})[1]["items"] == []
    states = {m["state"] for m in call(api, "GET", "/api/mentions", query={"view": "history"})[1]["items"]}
    assert states == {"sent", "rejected"}


def test_blocklist_api(app):
    api = Api(app)
    assert call(api, "POST", "/api/blocklist", {"author_id": "abc"})[0] == 400
    assert call(api, "POST", "/api/blocklist", {"author_id": "123", "note": "荒らし"})[0] == 201
    assert [x["id"] for x in call(api, "GET", "/api/blocklist")[1]["items"]] == ["123"]
    call(api, "DELETE", "/api/blocklist/123")
    assert call(api, "GET", "/api/blocklist")[1]["items"] == []


def test_stats_and_logs(app):
    api = Api(app)
    call(api, "POST", "/api/x/connect")
    app.jobs.run()
    app.x.add_mentions(["可愛い！", "副業で稼げます", "どこですか？"])
    app.jobs.run()
    mid = next(m["id"] for m in app.repo.list("mentions") if m["category"] == "fan")
    call(api, "POST", f"/api/mentions/{mid}/reply", {"text": "ありがとう"})
    s = call(api, "GET", "/api/stats")[1]
    assert s["total_mentions"] == 3 and s["sent"][-1] == 1 and s["received"][-1] == 3
    assert {c["key"] for c in s["by_category"]} == {"fan", "spam", "question"}
    assert s["runs"]["ok"] == 2 and s["month"]["sent"] == 1 and s["month"]["est_cost_usd"] > 0
    logs = call(api, "GET", "/api/logs")[1]["items"]
    assert any(l["kind"] == "send" for l in logs)
    assert all(l["level"] == "error" for l in call(api, "GET", "/api/logs", query={"level": "error"})[1]["items"])


def test_status_shape(app):
    api = Api(app)
    st = call(api, "GET", "/api/status")[1]
    assert st["x_connected"] is False and st["kill_switch"] is False and st["mode"] == "mock"
    call(api, "POST", "/api/x/connect")
    assert call(api, "GET", "/api/status")[1]["x_connected"] is True
    call(api, "POST", "/api/x/disconnect")
    assert call(api, "GET", "/api/status")[1]["x_connected"] is False


# --- 実機モードのOAuth（HTTPは偽物） ---
def live_app(clock):
    from xronix.app import build
    from xronix.repo import SqliteRepo
    from xronix.xclient import LiveX
    from conftest import FakeSecrets
    http = FakeHttp()
    return build(SqliteRepo(":memory:"), FakeSecrets(), LiveX(http, sleep=lambda s: None), http, now=clock, sleep=lambda s: None), http


def test_live_oauth_flow(clock):
    app, http = live_app(clock)
    api = Api(app)
    assert call(api, "POST", "/api/x/connect")[0] == 400                      # 設定前は案内
    call(api, "PUT", "/api/settings/x", {"client_id": "cid", "redirect_uri": "http://127.0.0.1:8787/api/x/callback"})
    call(api, "PUT", "/api/secrets/x_client_secret", {"value": "csecret"})
    url = call(api, "POST", "/api/x/connect")[1]["url"]
    assert url.startswith("https://x.com/i/oauth2/authorize?") and "offline.access" in url.replace("%20", " ")
    state = [s for s in url.split("&") if s.startswith("state=")][0][6:]
    http.push(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 7200})
    http.push(200, {"data": {"id": "42", "username": "GirlAidol"}})
    status, _, headers = api.handle("GET", "/api/x/callback", {"state": state, "code": "abc"}, {})
    assert status == 302 and headers["Location"].endswith("x=ok")
    assert app.secrets.get("x_refresh_token") == "RT" and app.settings.get("x")["username"] == "GirlAidol"
    assert b"code_verifier=" in http.calls[0]["body"] and b"abc" in http.calls[0]["body"]
    # 使い終わったstateは再利用できない
    assert api.handle("GET", "/api/x/callback", {"state": state, "code": "abc"}, {})[2]["Location"].endswith("x=invalid")
    assert api.handle("GET", "/api/x/callback", {"state": "bogus"}, {})[2]["Location"].endswith("x=invalid")
    assert api.handle("GET", "/api/x/callback", {"error": "access_denied"}, {})[2]["Location"].endswith("x=denied")


def test_mock_endpoint_blocked_in_live(clock):
    app, _ = live_app(clock)
    assert call(Api(app), "POST", "/api/dev/mock-mentions")[0] == 404


# --- AI・通知 ---
def grok_app(app, reply_json):
    api = Api(app)
    call(api, "PUT", "/api/settings/ai", {"provider": "grok", "mode": "classify_draft", "model": "grok-test"})
    call(api, "PUT", "/api/secrets/ai_api_key", {"value": "k"})
    app.http.push(200, {"choices": [{"message": {"content": reply_json}}]})
    return api


def test_ai_classify_and_draft_with_fallbacks(app):
    grok_app(app, '前置き {"category": "question", "reply": "ご質問ありがとうございます"} 後ろ')
    cat, reply, by = app.ai.classify("明日は配信ありますか")
    assert (cat, reply, by) == ("question", "ご質問ありがとうございます", "ai")
    call_ = app.http.calls[-1]
    assert call_["url"] == "https://api.x.ai/v1/chat/completions" and call_["headers"]["Authorization"] == "Bearer k"
    assert json.loads(call_["body"])["model"] == "grok-test"
    app.http.push(200, {"choices": [{"message": {"content": '{"category":"fan","reply":"見て https://a.example"}'}}]})
    assert app.ai.classify("可愛い")[1] == ""                      # URL入りの返信案は捨てる
    app.http.push(500, {})
    assert app.ai.classify("可愛い")[2] == "rule"                   # 失敗時はルールに戻す
    app.http.push(200, {"choices": [{"message": {"content": "not json"}}]})
    assert app.ai.classify("可愛い")[2] == "rule"


def test_ai_never_sees_spam_or_caution(app):
    grok_app(app, "{}")
    n = len(app.http.calls)
    assert app.ai.classify("副業で稼げます")[2] == "rule"
    assert app.ai.classify("DM送って")[2] == "rule"
    assert len(app.http.calls) == n                                 # 外部へ送っていない


def test_ai_off_by_default_sends_nothing(app):
    assert app.ai.classify("可愛い")[2] == "rule" and app.http.calls == []


def test_ai_connection_test_endpoint(app):
    api = Api(app)
    assert call(api, "POST", "/api/ai/test")[0] == 502                 # キー未設定
    grok_app(app, '{"category":"fan"}')
    st, body = call(api, "POST", "/api/ai/test")
    assert st == 200 and body["label"] == "ファンの感想"


def test_notify_sends_counts_only(app):
    api = Api(app)
    call(api, "PUT", "/api/secrets/discord_webhook", {"value": "https://discord.example/hook"})
    call(api, "PUT", "/api/settings/notify", {"discord_enabled": True})
    call(api, "POST", "/api/x/connect")
    app.jobs.run()
    app.x.add_mentions(["とても可愛い投稿です"])
    app.http.push(204, b"")
    app.jobs.run()
    sent = app.http.calls[-1]
    assert sent["url"] == "https://discord.example/hook" and "1件" in json.loads(sent["body"])["content"]
    assert "可愛い" not in sent["body"].decode()                        # 本文は載せない


# --- Lambdaアダプタ（DynamoDB・SSMはmoto） ---
import base64


def basic(user="u", pw="p"):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def lambda_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    boto3.resource("dynamodb").create_table(
        TableName="xt", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
    monkeypatch.setenv("XRONIX_TABLE", "xt")
    from xronix import lambda_handlers as lh
    monkeypatch.setenv("XRONIX_BASIC_HASH", lh.basic_hash("u", "p"))
    lh._api = None
    return lh


def ev(method, path, body=None, query=None, auth=None, csrf=True):
    headers = {}
    if auth:
        headers["Authorization"] = auth
    if csrf and method != "GET":
        headers["X-Requested-With"] = "xronix"
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "queryStringParameters": query,
            "headers": headers, "body": json.dumps(body) if body else None}


def test_lambda_basic_auth_rules(monkeypatch):
    with mock_aws():
        lh = lambda_env(monkeypatch)
        assert lh.api_handler(ev("GET", "/api/status"))["statusCode"] == 401
        r = lh.api_handler(ev("GET", "/api/status"))
        assert "Basic" in r["headers"]["WWW-Authenticate"]
        assert lh.api_handler(ev("GET", "/api/status", auth=basic("u", "wrong")))["statusCode"] == 401
        assert lh.api_handler(ev("GET", "/api/status", auth="Basic !!!notbase64"))["statusCode"] == 401
        assert lh.api_handler(ev("GET", "/api/status", auth=basic()))["statusCode"] == 200
        # 他サイトからの書き込みを想定: 認証済みでも専用ヘッダーが無ければ拒否
        assert lh.api_handler(ev("POST", "/api/kill", {"on": True}, auth=basic(), csrf=False))["statusCode"] == 403
        assert lh.api_handler(ev("POST", "/api/kill", {"on": True}, auth=basic()))["statusCode"] == 200
        # Xのコールバックだけは認証なし（stateが無ければ画面へ戻すだけ）
        r = lh.api_handler(ev("GET", "/api/x/callback", query={"state": "bogus"}))
        assert r["statusCode"] == 302 and r["headers"]["Location"].endswith("x=invalid")
        lh._api = None


def test_lambda_fails_closed_without_hash(monkeypatch):
    with mock_aws():
        lh = lambda_env(monkeypatch)
        monkeypatch.delenv("XRONIX_BASIC_HASH")
        assert lh.api_handler(ev("GET", "/api/status", auth=basic()))["statusCode"] == 401   # 設定漏れは全拒否
        lh._api = None


def test_lambda_handlers_end_to_end(monkeypatch):
    with mock_aws():
        lh = lambda_env(monkeypatch)
        a = basic()
        r = lh.api_handler(ev("GET", "/api/status", auth=a))
        assert r["statusCode"] == 200 and json.loads(r["body"])["mode"] == "live"
        assert lh.api_handler(ev("PUT", "/api/secrets/ai_api_key", {"value": "SSMVALUE"}, auth=a))["statusCode"] == 200
        assert boto3.client("ssm").get_parameter(Name="/xronix/ai_api_key", WithDecryption=True)["Parameter"]["Value"] == "SSMVALUE"
        r = lh.api_handler(ev("PUT", "/api/settings/job", {"enabled": True, "interval_minutes": 30}, auth=a))
        assert json.loads(r["body"])["interval_minutes"] == 30
        assert lh.api_handler(ev("POST", "/api/templates", {"category": "fan", "text": "ありがとう"}, auth=a))["statusCode"] == 201
        assert lh.job_handler({}) == {"ran": True, "status": "error"}   # 未接続はerrorとして記録される
        assert lh.job_handler({})["ran"] is False                       # 直後は間隔未到達で実行しない
        lh._api = None
