"""アクセストークンの取得と更新。更新は排他制御して1か所に集める（ADR-0003）。"""
from __future__ import annotations

import time
from datetime import timedelta

from .common import Lease, iso, new_id, parse_iso, utcnow
from .xclient import AuthRequired, XError


class TokenManager:
    def __init__(self, repo, secrets, settings, x, logger, now=utcnow, sleep=time.sleep):
        self.repo, self.secrets, self.settings, self.x, self.log = repo, secrets, settings, x, logger
        self.now, self.sleep = now, sleep
        self.lease = Lease(repo, "token", now)

    def connected(self) -> bool:
        return bool(self.secrets.get("x_refresh_token") and self.secrets.get("x_access_token"))

    def save_tokens(self, data: dict) -> None:
        """新しい一対を保存する。保存に失敗すると再認可が必要になるため、保存だけ再試行する。"""
        last = None
        for _ in range(3):
            try:
                self.secrets.put("x_access_token", data["access_token"])
                if data.get("refresh_token"):
                    self.secrets.put("x_refresh_token", data["refresh_token"])
                break
            except Exception as e:  # noqa: BLE001
                last = e
                self.sleep(0.5)
        else:
            self.log.error("token", "新しいトークンの保存に失敗しました。再接続が必要になる可能性があります")
            raise XError("トークンの保存に失敗しました") from last
        exp = self.now() + timedelta(seconds=int(data.get("expires_in", 7200)))
        self.repo.put("state", "token", {"expires_at": iso(exp), "auth_error": ""})

    def _fresh(self) -> bool:
        st = self.repo.get("state", "token")
        return bool(st and not st.get("auth_error") and st.get("expires_at")
                    and parse_iso(st["expires_at"]) > self.now() + timedelta(seconds=120)
                    and self.secrets.get("x_access_token"))

    def get_access_token(self) -> str:
        if not self.connected():
            raise AuthRequired("Xに接続されていません")
        st = self.repo.get("state", "token") or {}
        if st.get("auth_error"):
            raise AuthRequired(st["auth_error"])
        if self._fresh():
            return self.secrets.get("x_access_token")
        holder = new_id()
        for _ in range(20):
            if self.lease.acquire(holder, ttl_seconds=60):
                break
            self.sleep(0.5)
            if self._fresh():
                return self.secrets.get("x_access_token")
        else:
            raise XError("トークン更新の待機がタイムアウトしました")
        try:
            if self._fresh():  # 待っている間に他の呼び出しが更新済み
                return self.secrets.get("x_access_token")
            cfg = self.settings.get("x")
            try:
                data = self.x.refresh(cfg["client_id"], self.secrets.get("x_client_secret") or "", self.secrets.get("x_refresh_token"))
            except AuthRequired as e:
                self.repo.put("state", "token", {"expires_at": "", "auth_error": str(e)})
                self.log.error("token", str(e))
                raise
            self.save_tokens(data)
            self.log.info("token", "アクセストークンを更新しました")
            return data["access_token"]
        finally:
            self.lease.release(holder)

    def clear(self) -> None:
        for n in ("x_access_token", "x_refresh_token"):
            self.secrets.delete(n)
        self.repo.delete("state", "token")
