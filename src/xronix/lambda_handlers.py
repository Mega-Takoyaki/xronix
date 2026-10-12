"""AWS Lambdaの入口。API Gateway(HTTP API v2)用と、EventBridge用の2つ。

認証: 画面とAPIはBasic認証（資格情報のSHA-256を環境変数 XRONIX_BASIC_HASH に持つ）。
未設定なら全拒否（設定漏れで公開されない）。Xの認可コールバックだけは認証なしで、stateで保護する。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from .api import Api
from .app import build_aws

_api: Api | None = None
PUBLIC_PATHS = {"/api/x/callback"}


def _get_api() -> Api:
    global _api
    if _api is None:
        _api = Api(build_aws())
    return _api


def basic_hash(user: str, password: str) -> str:
    return hashlib.sha256(f"{user}:{password}".encode()).hexdigest()


def _authorized(headers: dict) -> bool:
    expected = os.environ.get("XRONIX_BASIC_HASH", "")
    value = headers.get("authorization", "")
    if not expected or not value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(value[6:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(hashlib.sha256(decoded.encode()).hexdigest(), expected)


def _resp(status: int, payload: dict, headers: dict | None = None) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", **(headers or {})},
            "body": json.dumps(payload, ensure_ascii=False)}


def api_handler(event, context=None):
    http = event["requestContext"]["http"]
    method, path = http["method"], event["rawPath"]
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if path not in PUBLIC_PATHS:
        if not _authorized(headers):
            return _resp(401, {"error": "認証が必要です"}, {"WWW-Authenticate": 'Basic realm="XroniX", charset="UTF-8"'})
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
    run = _get_api().app.jobs.tick()
    return {"ran": run is not None, "status": run["status"] if run else "not_due"}
