"""Telegramで、承認待ちを受け取り、そのまま「承認して送信」「却下」を操作する。

安全のための決まり:
- Webhookは、登録時に作った合言葉（secret_token）が合わない呼び出しを、すべて拒否する。
- ボタンを押した人のユーザーIDと、押されたチャットが、設定したものと違えば、何もしない
  （個人のチャットなら、チャットIDがユーザーID。グループの場合は、許可するユーザーIDを別に設定する）。
- 承認は、画面の承認と同じ処理（Api）を通る。上限・ブロック・緊急停止・リンク禁止の検査も、同じ。投稿作成は再試行しない。
- 同じボタンが何度押されても、二重に送らない（状態が承認待ちのときだけ動く）。
- 本文の全文を、Telegramに送る（運用者が、設定で明示的に有効にしたときだけ）。
"""
from __future__ import annotations

import hmac
import re
import secrets as pysecrets

from .classify import LABELS as CATEGORY_LABELS
from .common import new_id
from .events import LABELS as EVENT_LABELS

MAX_PER_RUN = 10        # 1回のジョブで、個別にTelegramへ送る承認待ちの上限（超えた分は、件数でまとめて知らせる）
MAX_PART = 1500          # 本文・返信案の、1つあたりの最大文字数（Telegramの1通は4096文字まで）
WEBHOOK_SECRET = "telegram_webhook_secret"
STATE_LABEL = {"sent": "送信済み", "rejected": "却下済み", "send_queued": "送信待ち", "sending": "送信中", "blocked": "ブロック対象",
               "done": "対応済み", "failed": "送信失敗", "unknown": "結果不明", "review": "要確認", "spam": "スパム"}


def _clip(s: str) -> str:
    return s if len(s) <= MAX_PART else s[:MAX_PART] + "…（以下省略。画面で全文を確認できます）"


class TelegramBot:
    def __init__(self, repo, settings, secrets, notifier, logger, now):
        self.repo, self.settings, self.secrets, self.notifier, self.log, self.now = repo, settings, secrets, notifier, logger, now
        self.app = None  # build後に attach される（承認は、画面と同じApiを通すため）

    def attach(self, app) -> None:
        self.app = app

    # --- 設定 ---
    def allowed_user_id(self) -> str:
        cfg = self.settings.get("notify")
        if cfg["telegram_user_id"]:
            return cfg["telegram_user_id"]
        chat = cfg["telegram_chat_id"]
        return chat if re.fullmatch(r"\d{1,20}", chat or "") else ""  # 個人のチャットなら、チャットID＝ユーザーID

    def active(self) -> bool:
        cfg = self.settings.get("notify")
        return bool(cfg["telegram_actions"] and self.notifier.enabled() and self.secrets.get(WEBHOOK_SECRET) and self.allowed_user_id())

    def enable(self) -> dict:
        from .notify import NotifyError
        cfg = self.settings.get("notify")
        if not self.notifier.configured():
            raise NotifyError("先に、ボットトークンとチャットIDを保存してください")
        if not cfg["site_url"]:
            raise NotifyError("先に「画面のURL」（https://…）を保存してください。TelegramのWebhookの宛先に使います")
        if not self.allowed_user_id():
            raise NotifyError("チャットがグループの場合は、「許可するユーザーID」（あなたのTelegramのユーザーID）を保存してください")
        secret = pysecrets.token_urlsafe(32)
        url = cfg["site_url"].rstrip("/") + "/api/telegram/webhook"
        ok, _, reason = self.notifier.call("setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["callback_query"], "drop_pending_updates": True})
        if not ok:
            raise NotifyError(reason)
        self.secrets.put(WEBHOOK_SECRET, secret)
        self.settings.update("notify", {"telegram_actions": True})
        self.log.info("notify", "Telegramの承認ボタンを有効にしました（Webhookを登録）")
        return {"url": url}

    def disable(self) -> None:
        self.notifier.call("deleteWebhook", {"drop_pending_updates": True})
        self.secrets.delete(WEBHOOK_SECRET)
        self.settings.update("notify", {"telegram_actions": False})
        self.log.info("notify", "Telegramの承認ボタンを無効にしました（Webhookを削除）")

    # --- 承認待ちの通知（全文＋ボタン） ---
    def format_item(self, m: dict, status: str = "") -> str:
        kind = EVENT_LABELS.get(m.get("kind", ""), "反応")
        cat = CATEGORY_LABELS.get(m.get("category", ""), "")
        head = ("【承認待ち】" if not status else "【処理済み】") + kind + (f"（{cat}）" if cat and not m.get("metric") else "")
        who = f"\nユーザーID: {m.get('author_id')}" if m.get("author_id") and not m.get("metric") else ""
        if m.get("metric"):
            mt = m["metric"]
            who = f"\n{mt['label']} {mt['value']}件（基準 {mt['threshold']}件）"
        text = f"{head}{who}\n\n▼ {'あなたの投稿' if m.get('metric') else '相手の投稿'}\n{_clip(m.get('text', ''))}\n\n▼ 返信案\n{_clip(m.get('draft_text', ''))}"
        if status:
            text += f"\n\n{status}"
        else:
            url = self.settings.get("notify")["site_url"]
            text += "\n\n（返信案を直す場合は、画面で。）" + (f"\n{url}" if url else "")
        return text

    def notify_new(self, since_iso: str, pending_total: int) -> int:
        """今回の実行で承認待ちになった、返信案つきの反応を、1件ずつ、ボタンつきで送る。送った通知の数を返す。"""
        if not self.active():
            return 0
        rows = [m for m in self.repo.list("mentions") if m.get("state") == "pending" and m.get("draft_text") and not m.get("tg_message_id")
                and m.get("fetched_at", "") >= since_iso]
        rows.sort(key=lambda m: m.get("fetched_at", ""))
        chat = self.settings.get("notify")["telegram_chat_id"]
        sent = 0
        for m in rows[:MAX_PER_RUN]:
            key = m.get("tg_key") or new_id()[:12]
            ok, res, reason = self.notifier.call("sendMessage", {
                "chat_id": chat, "text": self.format_item(m), "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": [[{"text": "✅ 承認して送信", "callback_data": f"a:{key}"}, {"text": "❌ 却下", "callback_data": f"r:{key}"}]]}})
            if not ok:
                self.log.warn("notify", f"Telegramへの承認待ちの通知に失敗しました: {reason}", mention_id=m["id"])
                break
            self.repo.update_if("mentions", m["id"], {"tg_key": key, "tg_message_id": res.get("message_id"), "tg_chat_id": chat})
            sent += 1
        rest = len(rows) - min(len(rows), MAX_PER_RUN)
        if rest > 0:
            self.notifier.notify_text(f"XroniX: ほかに、承認待ちが{rest}件あります。画面で確認してください。")
        others = pending_total - sent - rest
        if others > 0:  # 返信文が空で、人が書く必要があるもの（ボタンでは操作できない）
            self.notifier.notify_text(f"XroniX: 返信文を書く必要がある（または前からある）承認待ちが、あわせて{others}件あります。画面で確認してください。")
            sent += 1
        return sent + (1 if rest > 0 else 0)

    def mark(self, m: dict, status: str) -> None:
        """画面などで処理したとき、Telegramのメッセージに結果を書き、ボタンを消す。失敗しても、処理には影響しない。"""
        if not m.get("tg_message_id") or not m.get("tg_chat_id") or not self.notifier.configured():
            return
        ok, _, reason = self.notifier.call("editMessageText", {"chat_id": m["tg_chat_id"], "message_id": m["tg_message_id"], "text": self.format_item(m, status),
                                                                 "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": []}})
        if not ok:
            self.log.warn("notify", f"Telegramのメッセージの更新に失敗しました: {reason}", mention_id=m.get("id", ""))

    # --- ボタンの受信（Webhook） ---
    def handle(self, headers: dict, update: dict) -> tuple[int, dict]:
        secret = self.secrets.get(WEBHOOK_SECRET) or ""
        got = (headers or {}).get("x-telegram-bot-api-secret-token", "")
        if not secret or not hmac.compare_digest(got.encode(), secret.encode()):
            return 403, {"error": "forbidden"}
        cq = (update or {}).get("callback_query")
        if not isinstance(cq, dict):
            return 200, {"ok": True}
        cfg = self.settings.get("notify")
        from_id = str((cq.get("from") or {}).get("id", ""))
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not self.active() or from_id != self.allowed_user_id() or chat_id != cfg["telegram_chat_id"]:
            self.log.warn("notify", "許可されていないTelegramの操作を拒否しました")
            self._answer(cq, "この操作は許可されていません")
            return 200, {"ok": True}
        action, _, key = str(cq.get("data", "")).partition(":")
        item = next((m for m in self.repo.list("mentions") if key and m.get("tg_key") == key), None)
        if action not in ("a", "r") or not item:
            self._answer(cq, "対象が見つかりません")
            return 200, {"ok": True}
        short, line, keep = self._do(action, item)
        self._answer(cq, short)
        fresh = self.repo.get("mentions", item["id"]) or item
        edit = {"chat_id": chat_id, "message_id": msg.get("message_id"), "text": self.format_item(fresh, line), "disable_web_page_preview": True}
        if not keep:
            edit["reply_markup"] = {"inline_keyboard": []}
        self.notifier.call("editMessageText", edit)
        return 200, {"ok": True}

    def _do(self, action: str, item: dict) -> tuple[str, str, bool]:
        """(通知の短い文, メッセージに足す文, ボタンを残すか)。"""
        from .api import Api
        m = self.repo.get("mentions", item["id"]) or item
        if m["state"] != "pending":
            label = STATE_LABEL.get(m["state"], m["state"])
            return f"すでに処理済みです（{label}）", f"ℹ️ すでに処理済みです（{label}）", False
        api = Api(self.app)
        if action == "r":
            st, body, _ = api.handle("POST", f"/api/mentions/{m['id']}/reject", {}, {})
            if st == 200:
                self.log.info("send", "Telegramから却下しました", mention_id=m["id"])
                return "却下しました", "❌ 却下しました", False
            return "却下できませんでした", f"⚠️ 却下できませんでした: {body.get('error', '')}", True
        st, body, _ = api.handle("POST", f"/api/mentions/{m['id']}/reply", {}, {"text": m.get("draft_text", "")})
        if st == 200:
            queued = body.get("state") == "send_queued"
            self.log.info("send", "Telegramから承認しました", mention_id=m["id"])
            return ("承認しました（送信キュー）" if queued else "承認して送信しました"), ("✅ 承認しました（送信キューに入れました。間隔をあけて送ります）" if queued else "✅ 承認して送信しました"), False
        reason = body.get("error", "")
        state = (self.repo.get("mentions", m["id"]) or {}).get("state", "")
        if st == 409 and state == "pending":  # 送る前の検査で止まった（Xには何も送っていない）。ボタンは残す
            return "送信できませんでした", f"⚠️ 送信できませんでした: {reason}", True
        return "送信に失敗しました", f"⚠️ 送信に失敗しました: {reason}（画面で状態を確認してください）", False

    def _answer(self, cq: dict, text: str) -> None:
        self.notifier.call("answerCallbackQuery", {"callback_query_id": cq.get("id", ""), "text": text[:190]})
