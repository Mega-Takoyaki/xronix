"""AI接続（任意）。既定は Amazon Bedrock（IAMで認証。APIキー不要）。OpenAI互換APIも選べる。

ルールでスパム・要注意と判定したものはAIに送らない（費用と外部送信を減らすため）。
モックモード（ローカルの動作確認）では、外部のAIを呼ばない。
"""
from __future__ import annotations

import json
import re

from .classify import CATEGORIES, LABELS, classify as rule_classify, CAUTION, SPAM
from .common import TWEET_LIMIT, has_url, weighted_len
from .http import Http, NetworkError

SYSTEM = (
    "あなたはSNSアカウント運用者の補助です。受け取った返信を次のいずれかに分類し、JSONだけを出力してください。"
    "分類: fan（ファンの感想）, question（質問）, spam（スパム・勧誘）, caution（攻撃的・個人情報・トラブルの恐れ）, other（その他）。"
    '形式: {"category": "...", "reply": "..."}。replyは短い日本語の返信案で、URLを含めず、不明なら空文字にしてください。'
)
SAMPLE = "いつも素敵な投稿ありがとうございます！"


class AIError(Exception):
    pass


def _default_bedrock_factory(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client("bedrock-runtime", region_name=region, config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 2}))


class AIService:
    def __init__(self, settings, secrets, http: Http, logger, allow_external: bool = True, bedrock_factory=None):
        self.settings, self.secrets, self.http, self.log = settings, secrets, http, logger
        self.allow_external = allow_external
        self._bedrock_factory = bedrock_factory or _default_bedrock_factory

    def provider(self) -> str:
        return self.settings.get("ai")["provider"]

    def active(self) -> bool:
        if not self.allow_external:
            return False
        p = self.provider()
        return p == "bedrock" or (p == "openai" and bool(self.secrets.get("openai_api_key")))

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
        reply = ""
        if cfg["mode"] == "classify_draft":
            r = str(out.get("reply") or "").strip()
            if r and not has_url(r) and weighted_len(r) <= TWEET_LIMIT:
                reply = r
        return cat, reply, "ai"

    # --- 呼び出し ---
    def _ask(self, cfg: dict, text: str) -> dict:
        p = cfg["provider"]
        if p == "bedrock":
            content = self._ask_bedrock(cfg, text)
        elif p == "openai":
            content = self._ask_openai(cfg, text)
        else:
            raise AIError("AIが無効です")
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise AIError("応答にJSONがありません")
        try:
            return json.loads(m.group(0))
        except ValueError as e:
            raise AIError("応答の形式が不正です") from e

    def _ask_bedrock(self, cfg: dict, text: str) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            client = self._bedrock_factory(cfg["bedrock_region"])
            resp = client.converse(
                modelId=cfg["bedrock_model"],
                system=[{"text": SYSTEM}],
                messages=[{"role": "user", "content": [{"text": text}]}],
                inferenceConfig={"maxTokens": 400, "temperature": 0.2},
            )
            return "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        except ClientError as e:
            err = e.response.get("Error", {})
            raise AIError(f"Bedrockがエラーを返しました（{err.get('Code', '不明')}）: {err.get('Message', '')}"[:300]) from e
        except (BotoCoreError, KeyError, TypeError) as e:
            raise AIError(f"Bedrockの呼び出しに失敗しました（{type(e).__name__}）") from e

    def _ask_openai(self, cfg: dict, text: str) -> str:
        key = self.secrets.get("openai_api_key")
        if not key:
            raise AIError("OpenAIのAPIキーが設定されていません")
        body = json.dumps({"model": cfg["openai_model"], "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]}).encode()
        try:
            r = self.http.request("POST", cfg["openai_base_url"].rstrip("/") + "/chat/completions",
                                  {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, body, timeout=30)
        except NetworkError as e:
            raise AIError(f"通信エラー: {e}") from e
        if r.status != 200:
            raise AIError(f"OpenAIがエラーを返しました（HTTP {r.status}）")
        try:
            return json.loads(r.body)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise AIError("応答の形式が不正です") from e

    def test(self) -> dict:
        cfg = self.settings.get("ai")
        if cfg["provider"] == "rule":
            raise AIError("AIを使わない設定です")
        if not self.allow_external:
            raise AIError("モックモードでは外部のAIを呼びません")
        out = self._ask(cfg, SAMPLE)
        cat = out.get("category", "")
        return {"provider": cfg["provider"], "category": cat, "label": LABELS.get(cat, "")}
