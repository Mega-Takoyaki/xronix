"""共通部品: 時刻、文字数、URL検出、リース、ログ。"""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
TWEET_LIMIT = 280

URL_RE = re.compile(
    r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|jp|co|io|me|ly|to|app|xyz|info|biz|gl|cc|tv|fm)\b)",
    re.IGNORECASE,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def jst_date(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%Y-%m-%d")


def weighted_len(text: str) -> int:
    """Xの文字数の概算。全角・絵文字は2、半角は1。"""
    n = 0
    for ch in text:
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") or ord(ch) > 0x1F000 else 1
    return n


def has_url(text: str) -> bool:
    return bool(URL_RE.search(text))


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Lease:
    """条件付き書き込みによる期限付きの排他。取れた呼び出しだけが処理を進める。"""

    def __init__(self, repo, name: str, now=utcnow):
        self.repo, self.name, self.now = repo, name, now

    def acquire(self, holder: str, ttl_seconds: int) -> bool:
        t = self.now()
        until = iso(t + timedelta(seconds=ttl_seconds))
        self.repo.put_if_absent("lease", self.name, {"holder": "", "until": "1970-01-01T00:00:00.000Z"})
        cur = self.repo.get("lease", self.name)
        if cur["until"] > iso(t):
            return False
        return self.repo.update_if("lease", self.name, {"holder": holder, "until": until}, {"until": cur["until"]}) is not None

    def release(self, holder: str) -> None:
        self.repo.update_if("lease", self.name, {"holder": "", "until": "1970-01-01T00:00:00.000Z"}, {"holder": holder})


class Logger:
    """ログをリポジトリに保存する。秘密情報は渡さないこと。"""

    KEEP = 1000

    def __init__(self, repo, now=utcnow):
        self.repo, self.now = repo, now
        self._n = 0

    def log(self, level: str, kind: str, message: str, **extra) -> None:
        t = self.now()
        lid = iso(t) + "-" + uuid.uuid4().hex[:4]
        self.repo.put("logs", lid, {
            "id": lid, "ts": iso(t), "level": level, "kind": kind, "message": message, "extra": extra,
            "ttl": int(t.timestamp()) + 60 * 86400,
        })
        self._n += 1
        if self._n % 100 == 0:
            self._prune()

    def info(self, kind, message, **extra): self.log("info", kind, message, **extra)
    def warn(self, kind, message, **extra): self.log("warn", kind, message, **extra)
    def error(self, kind, message, **extra): self.log("error", kind, message, **extra)

    def _prune(self) -> None:
        rows = self.repo.list("logs")
        for r in rows[: max(0, len(rows) - self.KEEP)]:
            self.repo.delete("logs", r["id"])
