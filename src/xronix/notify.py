"""通知（Discord Webhook）。本文は載せず、件数だけを送る。"""
from __future__ import annotations

import json

from .http import Http, NetworkError


class Notifier:
    def __init__(self, settings, secrets, http: Http, logger):
        self.settings, self.secrets, self.http, self.log = settings, secrets, http, logger

    def enabled(self) -> bool:
        return bool(self.settings.get("notify")["discord_enabled"] and self.secrets.get("discord_webhook"))

    def send(self, message: str) -> bool:
        url = self.secrets.get("discord_webhook")
        if not url:
            return False
        try:
            r = self.http.request("POST", url, {"Content-Type": "application/json"},
                                  json.dumps({"content": message}).encode(), timeout=10)
        except NetworkError as e:
            self.log.warn("notify", f"通知に失敗しました: {e}")
            return False
        ok = r.status in (200, 204)
        if not ok:
            self.log.warn("notify", f"通知に失敗しました（HTTP {r.status}）")
        return ok

    def notify_pending(self, count: int) -> bool:
        return self.enabled() and self.send(f"XroniX: 承認待ちが{count}件あります。")
