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
SCOPES = "tweet.read tweet.write users.read like.write offline.access"


TWEET_FIELDS = "created_at,author_id,conversation_id,referenced_tweets,in_reply_to_user_id,public_metrics"


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
            raise XError(f"X APIの取得に失敗しました: GET {path}（HTTP {r.status}）{_error_detail(r)}", r.status)
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
                "tweet.fields": TWEET_FIELDS,
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

    def replies_to_me(self, token: str, username: str, since_id: str | None, max_pages: int = 3) -> dict:
        """自分宛ての返信を、検索で取る（to:）。返信文に自分の@が無い返信も拾える。直近7日が対象。"""
        tweets: list[dict] = []
        newest, next_token, reads = None, None, 0
        for _ in range(max_pages):
            params = {"query": f"to:{username} -from:{username}", "max_results": 100, "tweet.fields": TWEET_FIELDS}
            if since_id:
                params["since_id"] = since_id
            if next_token:
                params["next_token"] = next_token
            data = self._get(token, "/2/tweets/search/recent", params)
            page = data.get("data", [])
            tweets += page
            reads += len(page)
            meta = data.get("meta", {})
            newest = newest or meta.get("newest_id")
            next_token = meta.get("next_token")
            if not next_token:
                break
        return {"tweets": tweets, "newest_id": newest, "reads": reads, "truncated": bool(next_token)}

    def quotes_of(self, token: str, tweet_id: str, max_results: int = 100) -> dict:
        """ある投稿の引用を取る。"""
        data = self._get(token, f"/2/tweets/{tweet_id}/quote_tweets", {"max_results": max(10, min(100, max_results)), "tweet.fields": TWEET_FIELDS})
        tw = data.get("data", [])
        return {"tweets": tw, "reads": len(tw)}

    def tweets(self, token: str, ids: list[str]) -> dict:
        """投稿をIDで取得する（スレッドの文脈用）。読み取りなのでバックオフあり。"""
        if not ids:
            return {"tweets": [], "reads": 0}
        data = self._get(token, "/2/tweets", {"ids": ",".join(ids), "tweet.fields": TWEET_FIELDS})
        tw = data.get("data", [])
        return {"tweets": tw, "reads": len(tw)}

    def own_posts(self, token: str, user_id: str, start_time: str, max_results: int = 20) -> dict:
        """自分の投稿（返信・リポストを除く）と、その反応の数。start_time以降。"""
        data = self._get(token, f"/2/users/{user_id}/tweets", {
            "max_results": max(5, min(100, max_results)), "start_time": start_time,
            "exclude": "retweets,replies", "tweet.fields": "created_at,public_metrics",
        })
        tw = data.get("data", [])
        return {"tweets": tw, "reads": len(tw)}

    # --- 書き込み（再試行しない） ---
    def like(self, token: str, user_id: str, tweet_id: str) -> None:
        try:
            r = self.http.request("POST", f"{API}/2/users/{user_id}/likes",
                                  {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                  json.dumps({"tweet_id": tweet_id}).encode())
        except NetworkError as e:
            raise XError(f"通信エラー: {e}") from e
        if r.status in (200, 201):
            return
        if r.status == 403:
            raise XError("いいねを付けられませんでした（HTTP 403）。権限 like.write が無い場合は、Xと再接続してください", 403)
        raise XError(f"いいねに失敗しました（HTTP {r.status}）", r.status)

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
        self.liked: list[str] = []
        self.posts: list[dict] = []
        self.tweets_db: dict[str, dict] = {}
        self.replies_queue: list[dict] = []  # 自分の@を含まない返信（検索でだけ取れるもの）
        self.quotes_db: dict[str, list[dict]] = {}  # 投稿ID → その引用
        self.fail_next_like = False
        self._seq = 1_900_000_000_000_000_000
        self.fail_next_create: str | None = None  # テスト用: "definite" | "unknown"

    ME = "9999"

    def add_mentions(self, texts: list[str], author_ids: list[str] | None = None, shapes: list[str] | None = None) -> list[dict]:
        """shapes: 各メンションの形。reply_to_post（自投稿への返信）/ reply_to_reply（自分の返信への返信）/ mention（返信ではない）/ quote（引用）。"""
        out = []
        for i, t in enumerate(texts):
            self._seq += 1
            shape = (shapes or [])[i] if shapes else "reply_to_post"
            tid = str(self._seq)
            root = "1800000000000000000"
            tw = {
                "id": tid, "text": t, "author_id": (author_ids or [])[i] if author_ids else str(1000 + i),
                "created_at": "2026-10-12T00:00:00.000Z", "conversation_id": root,
                "public_metrics": {"like_count": 0, "reply_count": 0, "retweet_count": 0, "quote_count": 0},
            }
            if shape == "reply_to_post":
                tw["referenced_tweets"] = [{"type": "replied_to", "id": root}]
                tw["in_reply_to_user_id"] = self.ME
                self.tweets_db.setdefault(root, {"id": root, "text": "（自分の投稿）元の投稿の本文", "author_id": self.ME, "conversation_id": root})
            elif shape == "reply_to_reply":
                parent = "1800000000000000001"
                tw["referenced_tweets"] = [{"type": "replied_to", "id": parent}]
                tw["in_reply_to_user_id"] = self.ME
                self.tweets_db.setdefault(parent, {"id": parent, "text": "（自分の返信）ありがとうございます！", "author_id": self.ME, "conversation_id": root,
                                                  "referenced_tweets": [{"type": "replied_to", "id": root}]})
                self.tweets_db.setdefault(root, {"id": root, "text": "（自分の投稿）元の投稿の本文", "author_id": self.ME, "conversation_id": root})
            elif shape == "quote":
                tw["referenced_tweets"] = [{"type": "quoted", "id": root}]
            else:  # mention
                tw["conversation_id"] = tid
            self.queue.append(tw)
            self.tweets_db[tid] = tw
            out.append(tw)
        return out

    def add_post(self, text: str, likes: int = 0, reposts: int = 0, replies: int = 0, quotes: int = 0, impressions: int = 0) -> dict:
        self._seq += 1
        p = {"id": str(self._seq), "text": text, "created_at": "2026-10-12T00:00:00.000Z",
             "public_metrics": {"like_count": likes, "retweet_count": reposts, "reply_count": replies, "quote_count": quotes, "impression_count": impressions}}
        self.posts.append(p)
        return p

    def tweets(self, token, ids):
        found = [self.tweets_db[i] for i in ids if i in self.tweets_db]
        return {"tweets": found, "reads": len(found)}

    def own_posts(self, token, user_id, start_time, max_results=20):
        return {"tweets": list(self.posts)[:max_results], "reads": min(len(self.posts), max_results)}

    def replies_to_me(self, token, username, since_id, max_pages=3):
        tw = sorted(self.replies_queue, key=lambda t: int(t["id"]), reverse=True)
        if since_id:
            tw = [t for t in tw if int(t["id"]) > int(since_id)]
        return {"tweets": tw, "newest_id": tw[0]["id"] if tw else None, "reads": len(tw), "truncated": False}

    def quotes_of(self, token, tweet_id, max_results=100):
        tw = list(self.quotes_db.get(tweet_id, []))
        return {"tweets": tw, "reads": len(tw)}

    def like(self, token, user_id, tweet_id):
        if self.fail_next_like:
            self.fail_next_like = False
            raise XError("モックの失敗", 403)
        self.liked.append(tweet_id)

    def exchange_code(self, *a, **k): return {"access_token": "mock-access", "refresh_token": "mock-refresh", "expires_in": 7200}
    def refresh(self, *a, **k): return {"access_token": "mock-access-" + new_id(), "refresh_token": "mock-refresh-" + new_id(), "expires_in": 7200}
    def me(self, token): return {"id": "9999", "username": "example_mock", "name": "モック"}

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


def _error_detail(r: Response) -> str:
    """Xのエラー応答から、原因が分かる部分（title／detail）だけを取り出す。トークンなどは含まれない。"""
    b = _json(r)
    parts = [str(b.get(k)) for k in ("title", "detail") if isinstance(b.get(k), str)]
    if not parts and isinstance(b.get("errors"), list) and b["errors"]:
        e = b["errors"][0]
        parts = [str(e.get(k)) for k in ("title", "message", "detail") if isinstance(e, dict) and isinstance(e.get(k), str)]
    return (" — " + " / ".join(parts))[:300] if parts else ""


def _json(r: Response) -> dict:
    try:
        return json.loads(r.body or b"{}")
    except ValueError:
        return {}
