"""通知（Telegram）。本文は載せず、件数（と任意の画面URL）だけを送る。"""
from __future__ import annotations

import json

from .http import Http, NetworkError


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

    def notify_pending(self, count: int) -> bool:
        if not self.enabled():
            return False
        url = self.settings.get("notify")["site_url"]
        return self.send(f"XroniX: 承認待ちが{count}件あります。" + (f"\n{url}" if url else ""))[0]
