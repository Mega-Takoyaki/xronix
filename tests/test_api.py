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
    assert call(api, "PUT", "/api/secrets/openai_api_key", {"value": "SECRET-KEY-123"})[0] == 200
    assert call(api, "PUT", "/api/secrets/x_access_token", {"value": "x"})[0] == 404  # トークンは画面から書けない
    _, s = call(api, "GET", "/api/settings")
    assert s["secrets"]["openai_api_key"] is True
    dump = json.dumps(s) + json.dumps(call(api, "GET", "/api/logs")[1], ensure_ascii=False)
    assert "SECRET-KEY-123" not in dump
    assert call(api, "DELETE", "/api/secrets/openai_api_key")[0] == 200
    assert call(api, "GET", "/api/settings")[1]["secrets"]["openai_api_key"] is False


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
    assert len(items) == 2 and items[0]["label"] == "ポジティブ反応"
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
def use_bedrock(app, bedrock, reply_json, model="test-model-id"):
    api = Api(app)
    call(api, "PUT", "/api/settings/ai", {"provider": "bedrock", "bedrock_model": model})
    bedrock.push_text(reply_json)
    return api


def use_openai(app, reply_json):
    api = Api(app)
    call(api, "PUT", "/api/settings/ai", {"provider": "openai", "openai_model": "test-gpt"})
    call(api, "PUT", "/api/secrets/openai_api_key", {"value": "k"})
    app.http.push(200, {"choices": [{"message": {"content": reply_json}}]})
    return api


def test_bedrock_needs_no_key_and_auto_picks_a_model(app, bedrock, control):
    Api(app).handle("PUT", "/api/settings/ai", {}, {"provider": "bedrock"})
    assert app.ai.provider() == "bedrock" and app.ai.active() is True
    bedrock.push_text('前置き {"category": "question"} 後ろ')
    assert app.ai.classify("明日は配信ありますか") == ("question", "ai")
    sent = bedrock.calls[-1]
    assert sent["modelId"] == "anthropic.claude-haiku-x"                 # 選択が空なので、一覧から安価なモデルを自動で選ぶ
    assert sent["messages"][0]["content"][0]["text"] == "明日は配信ありますか"
    assert app.http.calls == []                                         # Bedrockの呼び出しはHTTPではなくboto3


def test_bedrock_classification_with_fallbacks(app, bedrock):
    use_bedrock(app, bedrock, '{"category": "question"}')
    assert app.ai.classify("明日は配信ありますか") == ("question", "ai")
    assert bedrock.calls[-1]["modelId"] == "test-model-id"             # 選んだモデルを使う
    bedrock.push_error("ThrottlingException", "slow down")
    assert app.ai.classify("可愛い")[1] == "rule"                      # 失敗時はルールに戻る
    bedrock.push_text("not json")
    assert app.ai.classify("可愛い")[1] == "rule"
    bedrock.push_text('{"category":"hacker"}')
    assert app.ai.classify("可愛い")[0] == "fan"                       # 不明な分類はルールの結果を使う


def test_openai_option(app):
    use_openai(app, '{"category": "question"}')
    assert app.ai.classify("明日は配信ありますか") == ("question", "ai")
    call_ = app.http.calls[-1]
    assert call_["url"] == "https://api.openai.com/v1/chat/completions" and call_["headers"]["Authorization"] == "Bearer k"
    assert json.loads(call_["body"])["model"] == "test-gpt"
    app.http.push(500, {})
    assert app.ai.classify("可愛い")[1] == "rule"


def test_openai_without_key_does_nothing(app):
    api = Api(app)
    call(api, "PUT", "/api/settings/ai", {"provider": "openai"})
    assert app.ai.active() is False and app.ai.classify("可愛い")[1] == "rule" and app.http.calls == []


def test_rule_provider_and_mock_mode_send_nothing(app, bedrock):
    api = Api(app)
    call(api, "PUT", "/api/settings/ai", {"provider": "rule"})
    assert app.ai.classify("可愛い")[1] == "rule" and bedrock.calls == []
    call(api, "PUT", "/api/settings/ai", {"provider": "bedrock"})
    app.ai.allow_external = False                                       # モックモード（ローカルの動作確認）
    assert app.ai.classify("可愛い")[1] == "rule" and bedrock.calls == []


def test_ai_never_sees_spam_or_caution(app, bedrock):
    use_bedrock(app, bedrock, "{}")
    assert app.ai.classify("副業で稼げます")[1] == "rule"
    assert app.ai.classify("DM送って")[1] == "rule"
    assert bedrock.calls == []                                          # 外部へ送っていない


def test_ai_connection_test_endpoint(app, bedrock):
    api = Api(app)
    call(api, "PUT", "/api/settings/ai", {"provider": "bedrock", "bedrock_model": "m1"})
    bedrock.push_error("AccessDeniedException", "model access not enabled")
    st, body = call(api, "POST", "/api/ai/test")
    assert st == 502 and "AccessDeniedException" in body["error"]       # 原因が画面に出る
    bedrock.push_text('{"category":"fan"}')
    st, body = call(api, "POST", "/api/ai/test")
    assert st == 200 and body == {"provider": "bedrock", "model": "m1", "category": "fan", "label": "ポジティブ反応"}
    call(api, "PUT", "/api/settings/ai", {"provider": "rule"})
    assert call(api, "POST", "/api/ai/test")[0] == 502


def test_telegram_notify_sends_counts_only(app):
    api = Api(app)
    call(api, "PUT", "/api/secrets/telegram_bot_token", {"value": "123:ABC-token"})
    call(api, "PUT", "/api/settings/notify", {"telegram_enabled": True, "telegram_chat_id": "-100777", "site_url": "https://example.cloudfront.net"})
    call(api, "POST", "/api/x/connect")
    app.jobs.run()
    app.x.add_mentions(["とても可愛い投稿です"])
    app.http.push(200, {"ok": True})
    app.jobs.run()
    sent = app.http.calls[-1]
    assert sent["url"] == "https://api.telegram.org/bot123:ABC-token/sendMessage"
    payload = json.loads(sent["body"])
    assert payload["chat_id"] == "-100777" and "1件" in payload["text"] and "https://example.cloudfront.net" in payload["text"]
    assert "可愛い" not in sent["body"].decode()                        # 本文は載せない
    dump = json.dumps(call(api, "GET", "/api/logs")[1], ensure_ascii=False) + json.dumps(call(api, "GET", "/api/settings")[1])
    assert "123:ABC-token" not in dump                                   # トークンは画面にもログにも出ない


def test_telegram_disabled_or_incomplete_sends_nothing(app):
    api = Api(app)
    call(api, "PUT", "/api/settings/notify", {"telegram_enabled": True, "telegram_chat_id": "1"})
    assert app.notifier.enabled() is False                              # トークン未設定
    call(api, "PUT", "/api/secrets/telegram_bot_token", {"value": "t"})
    call(api, "PUT", "/api/settings/notify", {"telegram_enabled": False})
    assert app.notifier.notify_pending(3) is False and app.http.calls == []


def test_telegram_test_endpoint_reports_reason(app):
    api = Api(app)
    st, body = call(api, "POST", "/api/notify/test")
    assert st == 200 and body["sent"] is False and "必要" in body["reason"]
    call(api, "PUT", "/api/secrets/telegram_bot_token", {"value": "t0k"})
    call(api, "PUT", "/api/settings/notify", {"telegram_chat_id": "42"})
    app.http.push(400, {"ok": False, "description": "Bad Request: chat not found"})
    body = call(api, "POST", "/api/notify/test")[1]
    assert body["sent"] is False and "chat not found" in body["reason"] and "t0k" not in body["reason"]
    app.http.push(200, {"ok": True})
    assert call(api, "POST", "/api/notify/test")[1] == {"sent": True, "reason": ""}


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
        assert lh.api_handler(ev("PUT", "/api/secrets/openai_api_key", {"value": "SSMVALUE"}, auth=a))["statusCode"] == 200
        assert boto3.client("ssm").get_parameter(Name="/xronix/openai_api_key", WithDecryption=True)["Parameter"]["Value"] == "SSMVALUE"
        r = lh.api_handler(ev("PUT", "/api/settings/job", {"enabled": True, "interval_minutes": 30}, auth=a))
        assert json.loads(r["body"])["interval_minutes"] == 30
        assert lh.api_handler(ev("POST", "/api/templates", {"category": "fan", "text": "ありがとう"}, auth=a))["statusCode"] == 201
        # 有効にした直後は、予定（15秒後）に達していないので実行しない。見守りも同じ
        assert lh.job_handler({"source": "schedule"})["ran"] is False
        assert lh.job_handler({"watchdog": True})["ran"] is False
        # 予定が過ぎていれば実行する（未接続なのでerrorとして記録される）
        lh._api.app.repo.update_if("state", "job", {"next_run_at": "2000-01-01T00:00:00.000Z"})
        assert lh.job_handler({"source": "schedule"}) == {"ran": True, "status": "error"}
        lh._api = None


# --- Telegramのチャットの自動検出 ---
def updates(*chats):
    out = []
    for i, (key, chat) in enumerate(chats):
        out.append({"update_id": i, key: {"message_id": i, "chat": chat, "text": "hi"}})
    return {"ok": True, "result": out}


def test_detect_chat_lists_recent_chats_newest_first_without_leaking_token(app):
    api = Api(app)
    call(api, "PUT", "/api/secrets/telegram_bot_token", {"value": "123:SECRET-TOKEN"})
    app.http.push(200, updates(
        ("message", {"id": 1, "type": "private", "first_name": "古い", "last_name": "人"}),
        ("message", {"id": 42, "type": "private", "first_name": "太郎", "username": "taro"}),
        ("my_chat_member", {"id": -100777, "type": "supergroup", "title": "運用グループ"}),
        ("message", {"id": 42, "type": "private", "first_name": "太郎", "username": "taro"}),   # 同じチャットは1件にまとめる
    ))
    st, body = call(api, "POST", "/api/notify/detect-chat")
    assert st == 200
    assert [c["id"] for c in body["chats"]] == ["42", "-100777", "1"]                          # 新しい順
    assert body["chats"][0] == {"id": "42", "type": "private", "name": "太郎", "username": "taro"}
    assert body["chats"][1]["name"] == "運用グループ"
    assert app.http.calls[-1]["url"].startswith("https://api.telegram.org/bot123:SECRET-TOKEN/getUpdates") and app.http.calls[-1]["method"] == "GET"
    assert "SECRET-TOKEN" not in json.dumps(body, ensure_ascii=False)


def test_detect_chat_errors_are_explained(app):
    api = Api(app)
    st, body = call(api, "POST", "/api/notify/detect-chat")
    assert st == 502 and "ボットトークン" in body["error"]                                     # トークン未設定
    call(api, "PUT", "/api/secrets/telegram_bot_token", {"value": "tok"})
    app.http.push(200, {"ok": True, "result": []})
    assert call(api, "POST", "/api/notify/detect-chat")[1] == {"chats": []}                     # メッセージ未着は、空の一覧
    app.http.push(401, {"ok": False, "description": "Unauthorized"})
    st, body = call(api, "POST", "/api/notify/detect-chat")
    assert st == 502 and "正しくありません" in body["error"] and "tok" not in body["error"].replace("トークン", "")
    app.http.push(409, {"ok": False, "description": "Conflict: can't use getUpdates method while webhook is active"})
    assert "Webhook" in call(api, "POST", "/api/notify/detect-chat")[1]["error"]
    app.http.push_error()
    assert "接続できません" in call(api, "POST", "/api/notify/detect-chat")[1]["error"]
