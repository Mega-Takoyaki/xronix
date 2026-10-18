"""通知（Telegram）。本文は載せず、件数（と任意の画面URL）だけを送る。"""
from __future__ import annotations

import json

from .http import Http, NetworkError


class NotifyError(Exception):
    pass


class Notifier:
    def __init__(self, settings, secrets, http: Http, logger):
        self.settings, self.secrets, self.http, self.log = settings, secrets, http, logger

    def configured(self) -> bool:
        cfg = self.settings.get("notify")
        return bool(self.secrets.get("telegram_bot_token") and cfg["telegram_chat_id"])

    def enabled(self) -> bool:
        return bool(self.settings.get("notify")["telegram_enabled"] and self.configured())

    def send(self, message: str) -> tuple[bool, str]:
        """(成功か, 失敗時の理由)。トークンは理由やログに出さない。"""
        token = self.secrets.get("telegram_bot_token")
        chat = self.settings.get("notify")["telegram_chat_id"]
        if not token or not chat:
            return False, "ボットトークンとチャットIDの両方が必要です"
        try:
            r = self.http.request("POST", f"https://api.telegram.org/bot{token}/sendMessage", {"Content-Type": "application/json"},
                                  json.dumps({"chat_id": chat, "text": message, "disable_web_page_preview": True}).encode(), timeout=10)
        except NetworkError:
            self.log.warn("notify", "Telegramへの通知に失敗しました（通信エラー）")
            return False, "Telegramに接続できませんでした"
        if r.status == 200:
            return True, ""
        try:
            reason = json.loads(r.body).get("description", "")
        except ValueError:
            reason = ""
        self.log.warn("notify", f"Telegramへの通知に失敗しました（HTTP {r.status}）: {reason}")
        return False, f"Telegramがエラーを返しました（HTTP {r.status}）: {reason}"

    def call(self, method: str, payload: dict) -> tuple[bool, dict, str]:
        """Telegram Bot APIの呼び出し。(成功か, 結果, 失敗の理由)。トークンは、理由やログに出さない。"""
        token = self.secrets.get("telegram_bot_token")
        if not token:
            return False, {}, "ボットトークンが未設定です"
        try:
            r = self.http.request("POST", f"https://api.telegram.org/bot{token}/{method}", {"Content-Type": "application/json"},
                                  json.dumps(payload).encode(), timeout=10)
        except NetworkError:
            return False, {}, "Telegramに接続できませんでした"
        try:
            body = json.loads(r.body or b"{}")
        except ValueError:
            body = {}
        if r.status == 200 and body.get("ok", True):
            return True, body.get("result") if isinstance(body.get("result"), dict) else {}, ""
        return False, {}, f"Telegramがエラーを返しました（HTTP {r.status}）: {body.get('description', '')}"

    def detect_chats(self) -> list[dict]:
        """ボットに届いた最近のメッセージから、チャット（ID・種類・名前）の一覧を作る。新しい順。

        ボットに、先にメッセージを送っておく必要がある。個人とのチャットでは、チャットIDはユーザーIDと同じ。
        """
        token = self.secrets.get("telegram_bot_token")
        if not token:
            raise NotifyError("先にボットトークンを保存してください")
        try:
            r = self.http.request("GET", f"https://api.telegram.org/bot{token}/getUpdates?limit=100", timeout=10)
        except NetworkError:
            raise NotifyError("Telegramに接続できませんでした") from None
        if r.status != 200:
            try:
                reason = json.loads(r.body).get("description", "")
            except ValueError:
                reason = ""
            hint = {401: "（ボットトークンが正しくありません）", 409: "（ボットにWebhookが設定されているため、取得できません）"}.get(r.status, "")
            raise NotifyError(f"Telegramがエラーを返しました（HTTP {r.status}）{hint}: {reason}")
        try:
            updates = json.loads(r.body).get("result", [])
        except ValueError:
            raise NotifyError("Telegramの応答を読めませんでした") from None
        seen: dict[str, dict] = {}
        for u in reversed(updates):  # 新しいものを先に
            for key in ("message", "edited_message", "channel_post", "my_chat_member"):
                chat = (u.get(key) or {}).get("chat")
                if chat and str(chat.get("id")) not in seen:
                    name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username", "")
                    seen[str(chat["id"])] = {"id": str(chat["id"]), "type": chat.get("type", ""), "name": name,
                                             "username": chat.get("username", "")}
        return list(seen.values())

    def notify_text(self, text: str) -> bool:
        """任意の文面を、通知が有効なときだけ送る。画面のURLが設定されていれば添える。"""
        if not self.enabled():
            return False
        url = self.settings.get("notify")["site_url"]
        return self.send(text + (f"\n{url}" if url else ""))[0]

    def notify_pending(self, count: int) -> bool:
        if not self.enabled():
            return False
        url = self.settings.get("notify")["site_url"]
        return self.send(f"XroniX: 承認待ちが{count}件あります。" + (f"\n{url}" if url else ""))[0]
