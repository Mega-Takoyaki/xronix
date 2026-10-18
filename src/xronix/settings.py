"""設定（秘密を含まない）。セクションごとにリポジトリへ保存する。"""
from __future__ import annotations

import re

DEFAULTS: dict[str, dict] = {
    "x": {"client_id": "", "redirect_uri": "", "user_id": "", "username": "", "connected_at": ""},
    "ai": {
        "provider": "bedrock",
        "bedrock_model": "", "bedrock_region": "ap-northeast-1",  # モデルが空なら、使える一覧から安価なモデルを自動で選ぶ
        "openai_model": "gpt-4o-mini", "openai_base_url": "https://api.openai.com/v1",
    },
    "job": {"enabled": False, "interval_minutes": 60, "jitter_minutes": 0, "initial_mode": "from_now", "max_pages": 3,
            "metrics_interval_minutes": 360, "metrics_window_days": 7, "metrics_max_posts": 20, "wide_fetch": True, "verbose_log": True},
    "safety": {"kill_switch": False, "daily_limit": 10, "like_daily_limit": 20, "auto_daily_limit": 5,
               "conversation_reply_limit": 3, "author_daily_limit": 5},
    "notify": {"telegram_enabled": False, "telegram_chat_id": "", "site_url": ""},
}

# 画面から更新できる項目と型。ここに無い項目は保存しない。
SCHEMA: dict[str, dict[str, type]] = {
    "x": {"client_id": str, "redirect_uri": str},
    "ai": {"provider": str, "bedrock_model": str, "bedrock_region": str, "openai_model": str, "openai_base_url": str},
    "job": {"enabled": bool, "interval_minutes": int, "jitter_minutes": int, "initial_mode": str, "max_pages": int,
            "metrics_interval_minutes": int, "metrics_window_days": int, "metrics_max_posts": int, "wide_fetch": bool, "verbose_log": bool},
    "safety": {"kill_switch": bool, "daily_limit": int, "like_daily_limit": int, "auto_daily_limit": int,
               "conversation_reply_limit": int, "author_daily_limit": int},
    "notify": {"telegram_enabled": bool, "telegram_chat_id": str, "site_url": str},
}

CHOICES = {
    ("ai", "provider"): ("bedrock", "openai", "rule"),
    ("job", "initial_mode"): ("from_now", "backfill"),
}

INTERVAL_RANGE = (1, 999)
JITTER_RANGE = (0, 99)  # 0はずらさない


class ValidationError(ValueError):
    pass


class Settings:
    def __init__(self, repo):
        self.repo = repo

    def get(self, section: str) -> dict:
        cur = {**DEFAULTS[section], **(self.repo.get("settings", section) or {})}
        for (sec, key), choices in CHOICES.items():
            if sec == section and cur.get(key) not in choices:
                cur[key] = DEFAULTS[section][key]  # 古い保存値（廃止した選択肢）は既定に戻す
        return cur

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
        self._validate(section, cur)
        self.repo.put("settings", section, cur)
        return cur

    @staticmethod
    def _validate(section: str, cur: dict) -> None:
        if section == "job":
            lo, hi = INTERVAL_RANGE
            if not lo <= cur["interval_minutes"] <= hi:
                raise ValidationError(f"実行間隔は{lo}〜{hi}分で指定してください")
            lo, hi = JITTER_RANGE
            if not lo <= cur["jitter_minutes"] <= hi:
                raise ValidationError(f"ランダムなずらし幅は{lo}〜{hi}分で指定してください（0でずらさない）")
            if not 1 <= cur["max_pages"] <= 10:
                raise ValidationError("最大ページ数は1〜10で指定してください")
            if not 5 <= cur["metrics_interval_minutes"] <= 1440:
                raise ValidationError("数値の確認間隔は5〜1440分で指定してください")
            if not 1 <= cur["metrics_window_days"] <= 30:
                raise ValidationError("数値を見る投稿の期間は1〜30日で指定してください")
            if not 5 <= cur["metrics_max_posts"] <= 100:
                raise ValidationError("数値を見る投稿の数は5〜100件で指定してください")
        if section == "safety":
            for k, label in (("daily_limit", "1日の送信上限"), ("like_daily_limit", "1日のいいね上限"), ("auto_daily_limit", "1日の自動送信の上限")):
                if not 0 <= cur[k] <= 100:
                    raise ValidationError(f"{label}は0〜100で指定してください")
            for k, label in (("conversation_reply_limit", "同じ相手との会話の返信上限"), ("author_daily_limit", "同じ相手への1日の返信上限")):
                if not 1 <= cur[k] <= 50:
                    raise ValidationError(f"{label}は1〜50で指定してください")
        if section == "ai":
            if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", cur["bedrock_region"]):
                raise ValidationError("Bedrockのリージョンは ap-northeast-1 のような形式で指定してください")
            if any(c.isspace() for c in cur["bedrock_model"]):
                raise ValidationError("BedrockのモデルIDに、空白は使えません")
            if not cur["openai_base_url"].startswith("https://"):
                raise ValidationError("OpenAIの接続先URLは https:// で始めてください")
        if section == "x" and cur["redirect_uri"] and not cur["redirect_uri"].startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValidationError("コールバックURLは https:// または http://127.0.0.1 で指定してください")
        if section == "notify":
            if cur["telegram_chat_id"] and not re.fullmatch(r"-?\d{1,20}|@[A-Za-z0-9_]{4,64}", cur["telegram_chat_id"]):
                raise ValidationError("TelegramのチャットIDは数字（例: 123456789、グループは -100…）または @チャンネル名で指定してください")
            if cur["site_url"] and not cur["site_url"].startswith("https://"):
                raise ValidationError("画面のURLは https:// で始めてください")

    def set_internal(self, section: str, **values) -> None:
        """画面外の更新（接続情報など）。型検査は行わない。"""
        cur = self.get(section)
        cur.update(values)
        self.repo.put("settings", section, cur)
