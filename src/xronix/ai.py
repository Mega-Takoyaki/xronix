"""AI接続（任意）。既定はルールベースで外部へ送らない。Grok(xAI)を選ぶと本文を外部へ送る。

ルールでスパム・要注意と判定したものはAIに送らない。ClaudeやBedrockは使わない方針。
"""
from __future__ import annotations

import json
import re

from .classify import CATEGORIES, LABELS, classify as rule_classify, CAUTION, SPAM
from .common import TWEET_LIMIT, has_url, weighted_len
from .http import Http, NetworkError

SYSTEM = (
    "あなたはSNSアカウント運用者の補助です。受け取った返信を次のいずれかに分類し、JSONだけを出力してください。"
    "分類: fan（ファンの感想）, question（質問）, spam（スパム・勧誘）, caution（攻撃的・性的・個人情報・トラブルの恐れ）, other（その他）。"
    '形式: {"category": "...", "reply": "..."}。replyは短い日本語の返信案で、URLを含めず、不明なら空文字にしてください。'
)


class AIError(Exception):
    pass


class AIService:
    def __init__(self, settings, secrets, http: Http, logger):
        self.settings, self.secrets, self.http, self.log = settings, secrets, http, logger

    def active(self) -> bool:
        cfg = self.settings.get("ai")
        return cfg["provider"] == "grok" and bool(self.secrets.get("ai_api_key"))

    def classify(self, text: str) -> tuple[str, str, str]:
        """(分類, 返信案または空, 判定元) を返す。AI失敗時はルールに戻す。"""
        rule = rule_classify(text)
        if rule in (SPAM, CAUTION) or not self.active():
            return rule, "", "rule"
        cfg = self.settings.get("ai")
        try:
            out = self._ask(cfg, text)
        except AIError as e:
            self.log.warn("ai", f"AIの判定に失敗したためルールで分類しました: {e}")
            return rule, "", "rule"
        cat = out.get("category") if out.get("category") in CATEGORIES else rule
        # AIが安全側を緩めないよう、ルールが質問・ファンでAIがother等になった場合もAI結果を採用するが、cautionは常に尊重する
        reply = ""
        if cfg["mode"] == "classify_draft":
            r = str(out.get("reply") or "").strip()
            if r and not has_url(r) and weighted_len(r) <= TWEET_LIMIT:
                reply = r
        return cat, reply, "ai"

    def _ask(self, cfg: dict, text: str) -> dict:
        key = self.secrets.get("ai_api_key")
        body = json.dumps({
            "model": cfg["model"],
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
            "temperature": 0.2,
        }).encode()
        try:
            r = self.http.request(
                "POST", cfg["base_url"].rstrip("/") + "/chat/completions",
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, body, timeout=30,
            )
        except NetworkError as e:
            raise AIError(f"通信エラー: {e}") from e
        if r.status != 200:
            raise AIError(f"HTTP {r.status}")
        try:
            content = json.loads(r.body)["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                raise AIError("応答にJSONがありません")
            return json.loads(m.group(0))
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise AIError("応答の形式が不正です") from e

    def test(self) -> dict:
        if not self.secrets.get("ai_api_key"):
            raise AIError("APIキーが設定されていません")
        cfg = self.settings.get("ai")
        out = self._ask(cfg, "いつも素敵な投稿ありがとうございます！")
        return {"category": out.get("category", ""), "label": LABELS.get(out.get("category", ""), "")}
