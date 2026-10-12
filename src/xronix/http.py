"""HTTPクライアント。差し替え可能にしてテストでは偽物を使う。"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict


class NetworkError(Exception):
    """接続できなかった、または応答を受け取れなかった（相手が処理済みかは不明）。"""


class Http(Protocol):
    def request(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None, timeout: float = 20) -> Response: ...


class UrllibHttp:
    def request(self, method, url, headers=None, body=None, timeout=20):
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Response(r.status, r.read(), dict(r.headers))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read(), dict(e.headers))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NetworkError(str(e)) from e


def read_with_backoff(fn: Callable[[], Response], tries: int = 4, sleep=time.sleep) -> Response:
    """読み取り専用。429/5xx/通信エラーは指数バックオフで再試行する。投稿作成には使わない。"""
    delay = 1.0
    for attempt in range(tries):
        try:
            r = fn()
            if r.status != 429 and r.status < 500:
                return r
            last: Exception | Response = r
        except NetworkError as e:
            last = e
        if attempt < tries - 1:
            sleep(delay)
            delay = min(delay * 2, 8)
    if isinstance(last, Response):
        return last
    raise last
