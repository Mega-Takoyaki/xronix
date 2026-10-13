import json
from datetime import datetime, timedelta, timezone

import pytest

from xronix.app import build
from xronix.http import NetworkError, Response
from xronix.repo import SqliteRepo
from xronix.xclient import MockX


class FakeSecrets:
    def __init__(self): self.d = {}
    def get(self, n): return self.d.get(n)
    def put(self, n, v): self.d[n] = v
    def delete(self, n): self.d.pop(n, None)


class Clock:
    def __init__(self): self.t = datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)
    def __call__(self): return self.t
    def advance(self, **kw): self.t += timedelta(**kw)


class FakeHttp:
    """リクエストを記録し、キューの応答を順に返す。Exceptionを入れると送出する。"""
    def __init__(self): self.calls, self.queue = [], []
    def push(self, status=200, body=None, headers=None):
        self.queue.append(Response(status, json.dumps(body).encode() if not isinstance(body, bytes) else body, headers or {}))
    def push_error(self): self.queue.append(NetworkError("down"))
    def request(self, method, url, headers=None, body=None, timeout=20):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "body": body})
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeBedrock:
    """bedrock-runtimeのconverseの偽物。応答テキストのキューか、例外を返す。"""
    def __init__(self):
        self.calls, self.queue = [], []
        self.classification = None  # 設定すると、分類の呼び出しには、この応答を返す（返信文の生成用のキューを消費しない）
    def push_text(self, text): self.queue.append({"output": {"message": {"content": [{"text": text}]}}})
    def push_error(self, code="AccessDeniedException", message="denied"):
        from botocore.exceptions import ClientError
        self.queue.append(ClientError({"Error": {"Code": code, "Message": message}}, "Converse"))
    def converse(self, **kw):
        self.calls.append(kw)
        if self.classification is not None and "分類" in kw["system"][0]["text"]:
            return {"output": {"message": {"content": [{"text": self.classification}]}}}
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeControl:
    """bedrock（コントロールプレーン）の偽物。モデルと推論プロファイルの一覧を返す。"""
    def __init__(self):
        self.calls = 0
        self.models = [
            {"modelId": "anthropic.claude-haiku-x", "modelName": "Claude Haiku X", "providerName": "Anthropic", "inferenceTypesSupported": ["ON_DEMAND"], "modelLifecycle": {"status": "ACTIVE"}},
            {"modelId": "anthropic.claude-big-x", "modelName": "Claude Big X", "providerName": "Anthropic", "inferenceTypesSupported": ["INFERENCE_PROFILE"], "modelLifecycle": {"status": "ACTIVE"}},
            {"modelId": "amazon.nova-micro-v1:0", "modelName": "Nova Micro", "providerName": "Amazon", "inferenceTypesSupported": ["ON_DEMAND"], "modelLifecycle": {"status": "ACTIVE"}},
            {"modelId": "old.model", "modelName": "Old", "providerName": "Old", "inferenceTypesSupported": ["ON_DEMAND"], "modelLifecycle": {"status": "LEGACY"}},
        ]
        self.profiles = [
            {"inferenceProfileId": "apac.anthropic.claude-big-x", "inferenceProfileName": "APAC Claude Big X", "status": "ACTIVE",
             "models": [{"modelArn": "arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-big-x"}]},
            {"inferenceProfileId": "apac.embed", "inferenceProfileName": "Embed", "status": "ACTIVE",
             "models": [{"modelArn": "arn:aws:bedrock:ap-northeast-1::foundation-model/cohere.embed"}]},  # テキスト出力ではないので除外
        ]
        self.error = None
        self.availability = {}        # モデルID -> 応答の上書き（既定は、すべて利用可）
        self.availability_error = None
    def get_foundation_model_availability(self, modelId):
        if self.availability_error:
            raise self.availability_error
        ok = {"modelId": modelId, "agreementAvailability": {"status": "AVAILABLE"}, "authorizationStatus": "AUTHORIZED",
              "entitlementAvailability": "AVAILABLE", "regionAvailability": "AVAILABLE"}
        return {**ok, **self.availability.get(modelId, {})}
    def list_foundation_models(self, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        assert kw.get("byOutputModality") == "TEXT"
        return {"modelSummaries": self.models}
    def list_inference_profiles(self, **kw):
        return {"inferenceProfileSummaries": self.profiles}


class FakeScheduler:
    """次回実行の登録を記録する。"""
    def __init__(self): self.armed, self.cancelled, self.fail = [], [], False
    def arm(self, when):
        if self.fail:
            raise RuntimeError("scheduler down")
        self.armed.append(when)
        return f"sched-{len(self.armed)}"
    def cancel(self, name): self.cancelled.append(name)


@pytest.fixture
def clock(): return Clock()


@pytest.fixture
def bedrock(): return FakeBedrock()


@pytest.fixture
def scheduler(): return FakeScheduler()


@pytest.fixture
def control(): return FakeControl()


@pytest.fixture
def app(clock, bedrock, scheduler, control):
    import random
    a = build(SqliteRepo(":memory:"), FakeSecrets(), MockX(), FakeHttp(), now=clock, sleep=lambda s: None,
              scheduler=scheduler, rng=random.Random(7), bedrock_factory=lambda region: bedrock,
              control_factory=lambda region: control)
    a.settings.update("ai", {"provider": "rule"})  # 既定はBedrockだが、ルール判定を前提にしたテストが多いため固定する（AIのテストは明示的に切り替える）
    return a


@pytest.fixture
def connected(app):
    from xronix.api import Api
    api = Api(app)
    api.handle("POST", "/api/x/connect")
    return app


def mentions_api(app):
    from xronix.api import Api
    return Api(app)
