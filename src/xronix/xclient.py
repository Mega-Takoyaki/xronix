"""Xクライアント。実機用(LiveX)と、外部へ送信しないモック(MockX)。

方針: 読み取りだけ指数バックオフで再試行する。投稿作成は1回だけ呼び、再試行しない。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as pysecrets
import urllib.parse

from .common import new_id
from .http import Http, NetworkError, Response, read_with_backoff

API = "https://api.x.com"
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
SCOPES = "tweet.read tweet.write users.read offline.access"


class XError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthRequired(XError):
    """トークンが無効。再認可が必要。"""


class CreateError(XError):
    """投稿作成の失敗。definite=Trueなら未投稿が確実、Falseなら投稿済みかもしれない。"""

    def __init__(self, message: str, status: int | None, definite: bool):
        super().__init__(message, status)
        self.definite = definite


def make_pkce() -> tuple[str, str]:
    verifier = pysecrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return f"{AUTHORIZE_URL}?{q}"


class LiveX:
    mode = "live"

    def __init__(self, http: Http, sleep=None):
        import time

        self.http = http
        self.sleep = sleep or time.sleep

    # --- OAuth（トークンのやり取り。再試行しない） ---
    def _token_request(self, client_id: str, client_secret: str, form: dict) -> dict:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        r = self.http.request(
            "POST", f"{API}/2/oauth2/token",
            {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode(form).encode(),
        )
        data = _json(r)
        if r.status == 400 and data.get("error") == "invalid_grant":
            raise AuthRequired("認可が無効です。Xと再接続してください")
        if r.status != 200:
            raise XError(f"トークン取得に失敗しました（HTTP {r.status}）", r.status)
        return data

    def exchange_code(self, client_id, client_secret, code, redirect_uri, verifier) -> dict:
        return self._token_request(client_id, client_secret, {
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "code_verifier": verifier, "client_id": client_id,
        })

    def refresh(self, client_id, client_secret, refresh_token) -> dict:
        return self._token_request(client_id, client_secret, {
            "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id,
        })

    # --- 読み取り（バックオフあり） ---
    def _get(self, token: str, path: str, params: dict | None = None) -> dict:
        url = f"{API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        r = read_with_backoff(lambda: self.http.request("GET", url, {"Authorization": f"Bearer {token}"}), sleep=self.sleep)
        if r.status == 401:
            raise AuthRequired("アクセストークンが無効です", 401)
        if r.status != 200:
            raise XError(f"X APIの取得に失敗しました（HTTP {r.status}）", r.status)
        return _json(r)

    def me(self, token: str) -> dict:
        return self._get(token, "/2/users/me")["data"]

    def mentions(self, token: str, user_id: str, since_id: str | None, max_pages: int = 3, max_results: int = 100) -> dict:
        tweets: list[dict] = []
        newest = None
        reads = 0
        next_token = None
        for _ in range(max_pages):
            params = {
                "max_results": max_results,
                "tweet.fields": "created_at,author_id,conversation_id,referenced_tweets,public_metrics",
            }
            if since_id:
                params["since_id"] = since_id
            if next_token:
                params["pagination_token"] = next_token
            data = self._get(token, f"/2/users/{user_id}/mentions", params)
            page = data.get("data", [])
            tweets += page
            reads += len(page)
            meta = data.get("meta", {})
            newest = newest or meta.get("newest_id")
            next_token = meta.get("next_token")
            if not next_token:
                break
        return {"tweets": tweets, "newest_id": newest, "reads": reads, "truncated": bool(next_token)}

    # --- 投稿作成（再試行しない） ---
    def create_reply(self, token: str, text: str, in_reply_to: str) -> str:
        body = json.dumps({"text": text, "reply": {"in_reply_to_tweet_id": in_reply_to}}).encode()
        try:
            r = self.http.request(
                "POST", f"{API}/2/tweets",
                {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, body,
            )
        except NetworkError as e:
            raise CreateError(f"通信エラー: {e}", None, definite=False) from e
        if r.status in (200, 201):
            return _json(r)["data"]["id"]
        # 4xx（429含む）はXが拒否したことが確実。5xxは処理済みかもしれない。
        raise CreateError(f"投稿に失敗しました（HTTP {r.status}）", r.status, definite=r.status < 500)


class MockX:
    """外部へ送信しない動作確認用。取得はキューから、投稿は記録するだけ。"""

    mode = "mock"

    def __init__(self):
        self.queue: list[dict] = []
        self.sent: list[dict] = []
        self._seq = 1_900_000_000_000_000_000
        self.fail_next_create: str | None = None  # テスト用: "definite" | "unknown"

    def add_mentions(self, texts: list[str], author_ids: list[str] | None = None) -> list[dict]:
        out = []
        for i, t in enumerate(texts):
            self._seq += 1
            tw = {
                "id": str(self._seq), "text": t, "author_id": (author_ids or [])[i] if author_ids else str(1000 + i),
                "created_at": "2026-10-12T00:00:00.000Z", "conversation_id": str(self._seq),
                "referenced_tweets": [{"type": "replied_to", "id": "1800000000000000000"}],
                "public_metrics": {"like_count": 0, "reply_count": 0, "retweet_count": 0, "quote_count": 0},
            }
            self.queue.append(tw)
            out.append(tw)
        return out

    def exchange_code(self, *a, **k): return {"access_token": "mock-access", "refresh_token": "mock-refresh", "expires_in": 7200}
    def refresh(self, *a, **k): return {"access_token": "mock-access-" + new_id(), "refresh_token": "mock-refresh-" + new_id(), "expires_in": 7200}
    def me(self, token): return {"id": "9999", "username": "GirlAidol_mock", "name": "モック"}

    def mentions(self, token, user_id, since_id, max_pages=3, max_results=100):
        tw = sorted(self.queue, key=lambda t: int(t["id"]), reverse=True)
        if since_id:
            tw = [t for t in tw if int(t["id"]) > int(since_id)]
        return {"tweets": tw, "newest_id": tw[0]["id"] if tw else None, "reads": len(tw), "truncated": False}

    def create_reply(self, token, text, in_reply_to):
        if self.fail_next_create:
            kind, self.fail_next_create = self.fail_next_create, None
            raise CreateError("モックの失敗", 403 if kind == "definite" else None, definite=kind == "definite")
        self.sent.append({"text": text, "in_reply_to": in_reply_to})
        return "mock-" + new_id()


def _json(r: Response) -> dict:
    try:
        return json.loads(r.body or b"{}")
    except ValueError:
        return {}
