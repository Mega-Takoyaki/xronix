"""設定（秘密を含まない）。セクションごとにリポジトリへ保存する。"""
from __future__ import annotations

DEFAULTS: dict[str, dict] = {
    "x": {"client_id": "", "redirect_uri": "", "user_id": "", "username": "", "connected_at": ""},
    "ai": {"provider": "rule", "mode": "classify", "model": "grok-4", "base_url": "https://api.x.ai/v1"},
    "job": {"enabled": False, "interval_minutes": 60, "initial_mode": "from_now", "max_pages": 3},
    "safety": {"kill_switch": False, "daily_limit": 10},
    "notify": {"discord_enabled": False},
}

# 画面から更新できる項目と型。ここに無い項目は保存しない。
SCHEMA: dict[str, dict[str, type]] = {
    "x": {"client_id": str, "redirect_uri": str},
    "ai": {"provider": str, "mode": str, "model": str, "base_url": str},
    "job": {"enabled": bool, "interval_minutes": int, "initial_mode": str, "max_pages": int},
    "safety": {"kill_switch": bool, "daily_limit": int},
    "notify": {"discord_enabled": bool},
}

CHOICES = {
    ("ai", "provider"): ("rule", "grok"),
    ("ai", "mode"): ("classify", "classify_draft"),
    ("job", "initial_mode"): ("from_now", "backfill"),
}


class ValidationError(ValueError):
    pass


class Settings:
    def __init__(self, repo):
        self.repo = repo

    def get(self, section: str) -> dict:
        return {**DEFAULTS[section], **(self.repo.get("settings", section) or {})}

    def all(self) -> dict:
        return {s: self.get(s) for s in DEFAULTS}

    def update(self, section: str, values: dict) -> dict:
        if section not in SCHEMA:
            raise ValidationError("不明な設定です")
        cur = self.get(section)
        for k, v in values.items():
            typ = SCHEMA[section].get(k)
            if typ is None:
                continue
            if typ is bool:
                if not isinstance(v, bool):
                    raise ValidationError(f"{k} は真偽値で指定してください")
            elif typ is int:
                if isinstance(v, bool) or not isinstance(v, int):
                    raise ValidationError(f"{k} は整数で指定してください")
            else:
                if not isinstance(v, str):
                    raise ValidationError(f"{k} は文字列で指定してください")
                v = v.strip()
            if (section, k) in CHOICES and v not in CHOICES[(section, k)]:
                raise ValidationError(f"{k} の値が不正です")
            cur[k] = v
        if section == "job":
            if not 15 <= cur["interval_minutes"] <= 1440:
                raise ValidationError("実行間隔は15〜1440分で指定してください")
            if not 1 <= cur["max_pages"] <= 10:
                raise ValidationError("最大ページ数は1〜10で指定してください")
        if section == "safety" and not 0 <= cur["daily_limit"] <= 100:
            raise ValidationError("1日の上限は0〜100で指定してください")
        if section == "ai" and cur["provider"] == "grok" and not cur["base_url"].startswith("https://"):
            raise ValidationError("接続先URLは https:// で始めてください")
        if section == "x" and cur["redirect_uri"] and not cur["redirect_uri"].startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValidationError("コールバックURLは https:// または http://127.0.0.1 で指定してください")
        self.repo.put("settings", section, cur)
        return cur

    def set_internal(self, section: str, **values) -> None:
        """画面外の更新（接続情報など）。型検査は行わない。"""
        cur = self.get(section)
        cur.update(values)
        self.repo.put("settings", section, cur)
