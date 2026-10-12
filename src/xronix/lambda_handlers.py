"""AWS Lambdaの入口。API Gateway(HTTP API v2)用と、EventBridge用の2つ。"""
from __future__ import annotations

import base64
import json

from .api import Api
from .app import build_aws

_api: Api | None = None


def _get_api() -> Api:
    global _api
    if _api is None:
        _api = Api(build_aws())
    return _api


def api_handler(event, context=None):
    http = event["requestContext"]["http"]
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        return {"statusCode": 400, "headers": {"Content-Type": "application/json"}, "body": '{"error":"JSONが不正です"}'}
    status, payload, headers = _get_api().handle(http["method"], event["rawPath"], event.get("queryStringParameters") or {}, body)
    return {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", **headers},
            "body": json.dumps(payload, ensure_ascii=False)}


def job_handler(event=None, context=None):
    run = _get_api().app.jobs.tick()
    return {"ran": run is not None, "status": run["status"] if run else "not_due"}
