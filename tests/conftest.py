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


@pytest.fixture
def clock(): return Clock()


@pytest.fixture
def app(clock):
    a = build(SqliteRepo(":memory:"), FakeSecrets(), MockX(), FakeHttp(), now=clock, sleep=lambda s: None)
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
