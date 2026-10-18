"""承認された返信の送信。二重投稿を避けるため、投稿作成は1回だけ呼び、再試行しない。"""
from __future__ import annotations

from .common import TWEET_LIMIT, has_url, iso, jst_date, utcnow, weighted_len
from .xclient import CreateError

SENDABLE = ("pending", "review", "failed")


class SendRefused(Exception):
    """送信前の検査で止めた。Xには何も送っていない。"""


class Sender:
    def __init__(self, repo, settings, tokens, x, templates, logger, now=utcnow):
        self.repo, self.settings, self.tokens, self.x = repo, settings, tokens, x
        self.templates, self.log, self.now = templates, logger, now

    def today_key(self) -> str:
        return "sent#" + jst_date(self.now())

    def sent_today(self) -> int:
        return int((self.repo.get("state", self.today_key()) or {}).get("count", 0))

    def check_text(self, text: str) -> str:
        text = text.strip()
        if not text:
            raise SendRefused("返信文が空です")
        if has_url(text):
            raise SendRefused("リンクを含む返信は送信できません（料金が約13倍になるため）")
        if weighted_len(text) > TWEET_LIMIT:
            raise SendRefused(f"文字数が上限を超えています（全角は2として{TWEET_LIMIT}まで）")
        return text

    def send(self, mention_id: str, text: str, allow_queued: bool = False) -> dict:
        safety = self.settings.get("safety")
        if safety["kill_switch"]:
            raise SendRefused("緊急停止中のため送信できません")
        m = self.repo.get("mentions", mention_id)
        if not m:
            raise SendRefused("対象が見つかりません")
        if m["state"] not in SENDABLE and not (allow_queued and m["state"] == "send_queued"):
            raise SendRefused(f"この状態では送信できません（{m['state']}）")
        if self.repo.get("blocklist", m["author_id"]):
            raise SendRefused("ブロック対象のユーザーです")
        text = self.check_text(text)
        # 状態を確定してから送る。同時に2回呼ばれても片方だけが進む
        cur = self.repo.update_if("mentions", mention_id, {"state": "sending", "final_text": text, "error": "", "updated_at": iso(self.now())},
                                  {"state": m["state"]})
        if cur is None:
            raise SendRefused("他の操作で更新されました。画面を更新してください")
        count = self.repo.incr("state", self.today_key(), "count", 1, max_value=safety["daily_limit"])
        if count is None:
            self._set(mention_id, m["state"])
            raise SendRefused(f"1日の送信上限（{safety['daily_limit']}件）に達しています")
        try:
            token = self.tokens.get_access_token()
        except Exception as e:  # noqa: BLE001  トークン取得の失敗は未投稿が確実
            self._fail(mention_id, e, definite=True)
            raise
        try:
            tweet_id = self.x.create_reply(token, text, m.get("target_tweet_id") or mention_id)
        except CreateError as e:
            self._fail(mention_id, e, definite=e.definite)
            raise
        except Exception as e:  # noqa: BLE001  想定外は投稿済みかもしれないため不明扱い
            self._fail(mention_id, e, definite=False)
            raise
        self._set(mention_id, "sent", sent_tweet_id=tweet_id, sent_at=iso(self.now()))
        if m["draft_template_id"] and m["draft_text"] == text:
            self.templates.mark_used(m["draft_template_id"])
        self.log.info("send", "返信を送信しました", mention_id=mention_id, tweet_id=tweet_id)
        return self.repo.get("mentions", mention_id)

    def _fail(self, mention_id: str, e: Exception, definite: bool) -> None:
        if definite:
            self.repo.incr("state", self.today_key(), "count", -1)
            self._set(mention_id, "failed", error=str(e))
            self.log.error("send", f"送信失敗（未投稿）: {e}", mention_id=mention_id)
        else:
            self._set(mention_id, "unknown", error=str(e))
            self.log.error("send", f"送信結果が不明です。Xで確認してください: {e}", mention_id=mention_id)

    def _set(self, mention_id: str, state: str, **extra) -> None:
        self.repo.update_if("mentions", mention_id, {"state": state, "updated_at": iso(self.now()), **extra})
