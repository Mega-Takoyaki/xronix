"""AWS Lambdaの入口。API Gateway(HTTP API v2)用と、EventBridge用の2つ。

認証: ログイン（Cognito）で得たIDトークンを、API GatewayのJWT検証が確かめる。
このLambdaでは、検証済みの印（requestContext.authorizer.jwt.claims）があるかを、もう一度確認する
（認証の設定漏れで、公開されないようにするため）。メールアドレスが環境変数 XRONIX_ADMIN_EMAIL と違う場合も拒否する。
認証なしで呼べるのは、Xの認可コールバック（stateで保護）と、ログイン画面が使う設定（公開してよい値だけ）。
"""
from __future__ import annotations

import base64
import json
import os

from .api import Api
from .app import build_aws

_api: Api | None = None
PUBLIC_PATHS = {"/api/x/callback", "/api/auth-config"}


def _get_api() -> Api:
    global _api
    if _api is None:
        _api = Api(build_aws())
    return _api


def _claims(event: dict) -> dict:
    return (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}


def _authorized(event: dict) -> bool:
    claims = _claims(event)
    if not claims.get("sub") or claims.get("token_use") != "id":
        return False
    expected = os.environ.get("XRONIX_ADMIN_EMAIL", "").strip().lower()
    return not expected or str(claims.get("email", "")).strip().lower() == expected


def _resp(status: int, payload: dict, headers: dict | None = None) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", **(headers or {})},
            "body": json.dumps(payload, ensure_ascii=False)}


def api_handler(event, context=None):
    http = event["requestContext"]["http"]
    method, path = http["method"], event["rawPath"]
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if path not in PUBLIC_PATHS:
        if not _authorized(event):
            return _resp(401, {"error": "ログインが必要です"})
        # 他サイトからの書き込み（CSRF）を防ぐ。独自ヘッダーはブラウザが事前確認を要求するため、他サイトからは付けられない
        if method != "GET" and headers.get("x-requested-with") != "xronix":
            return _resp(403, {"error": "forbidden"})
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        return _resp(400, {"error": "JSONが不正です"})
    status, payload, extra = _get_api().handle(method, path, event.get("queryStringParameters") or {}, body)
    return _resp(status, payload, extra)


def job_handler(event=None, context=None):
    """予定時刻の一回限りの起動（source=schedule）と、15分ごとの見守り（watchdog）の両方から呼ばれる。"""
    ev = event or {}
    app = _get_api().app
    if ev.get("source") == "sendqueue":  # 送信キューの、次の1件の予定時刻
        sent = app.sendq.tick("schedule")
        return {"queue_sent": sent is not None}
    source = "watchdog" if ev.get("watchdog") else "schedule"
    run = app.jobs.tick(source)
    queue = app.sendq.tick("watchdog") if source == "watchdog" else None  # 見守りは、遅れた送信も代わりに送る
    return {"ran": run is not None, "status": run["status"] if run else "not_due", "queue_sent": queue is not None}
