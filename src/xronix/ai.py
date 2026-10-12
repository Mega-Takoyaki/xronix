"""AI接続（任意）。既定は Amazon Bedrock（IAMで認証。APIキー不要）。OpenAI互換APIも選べる。

役割は2つ: ①メンションの分類  ②返信文の生成（ルールのアクション「AIで作る」から呼ばれる）。
ルールでスパム・要注意と判定したものは、分類でもAIに送らない。モックモードでは、外部のAIを呼ばない。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

from .classify import CATEGORIES, LABELS, classify as rule_classify, CAUTION, SPAM
from .common import TWEET_LIMIT, has_url, iso, parse_iso, utcnow, weighted_len
from .http import Http, NetworkError

CLASSIFY_SYSTEM = (
    "あなたはSNSアカウント運用者の補助です。受け取った返信を次のいずれかに分類し、JSONだけを出力してください。"
    "分類: fan（ファンの感想）, question（質問）, spam（スパム・勧誘）, caution（攻撃的・個人情報・トラブルの恐れ）, other（その他）。"
    '形式: {"category": "..."}'
)
GENERATE_RULES = (
    "\n\n【出力の決まり】返信文だけを出力する（前置き・説明・引用符は不要）。日本語で、全角140文字以内。"
    "URL・ハッシュタグ・@メンションは含めない。個人情報や、できない約束は書かない。"
)
SAMPLE = "いつも素敵な投稿ありがとうございます！"
MODELS_CACHE_SECONDS = 3600

# 既定のモデルを選ぶときの優先順（上ほど優先）。すべての語を含むものが該当。名前は小文字で比べる。
# 「安価な小型モデル」を優先する。価格はAPIから取れないため、名前で判断する。
DEFAULT_PREFERENCE = [
    ("gpt-6", "luna"), ("luna",), ("gpt", "nano"), ("gpt", "mini"), ("gpt-oss", "20b"),
    ("haiku",), ("nova", "micro"), ("nova", "lite"), ("llama", "8b"), ("mistral", "7b"),
]


class AIError(Exception):
    pass


def _default_bedrock_factory(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client("bedrock-runtime", region_name=region, config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 2}))


def _default_control_factory(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client("bedrock", region_name=region, config=Config(read_timeout=20, connect_timeout=5, retries={"max_attempts": 2}))


def _geo_prefix(region: str) -> str:
    return {"ap": "apac.", "us": "us.", "eu": "eu."}.get(region.split("-")[0], "")


def pick_default(items: list[dict], region: str = "") -> str | None:
    """使える一覧から、安価なモデルを1つ選ぶ。推論プロファイルは、リージョンに合うものを優先する。"""
    if not items:
        return None
    geo = _geo_prefix(region)

    def style(it: dict) -> int:
        i = it["id"]
        return 0 if geo and i.startswith(geo) else 1 if not i.split(".")[0] in ("us", "eu", "apac", "global", "jp", "au") else 2

    for words in DEFAULT_PREFERENCE:
        hits = [it for it in items if all(w in (it["id"] + " " + it["label"]).lower() for w in words)]
        if hits:
            return sorted(hits, key=lambda it: (style(it), it["id"]))[0]["id"]
    return sorted(items, key=lambda it: (style(it), it["id"]))[0]["id"]


class AIService:
    def __init__(self, settings, secrets, http: Http, logger, allow_external: bool = True, bedrock_factory=None, control_factory=None,
                 repo=None, now=utcnow):
        self.settings, self.secrets, self.http, self.log = settings, secrets, http, logger
        self.allow_external = allow_external
        self._bedrock_factory = bedrock_factory or _default_bedrock_factory
        self._control_factory = control_factory or _default_control_factory
        self.repo, self.now = repo, now

    def provider(self) -> str:
        return self.settings.get("ai")["provider"]

    def active(self) -> bool:
        if not self.allow_external:
            return False
        p = self.provider()
        return p == "bedrock" or (p == "openai" and bool(self.secrets.get("openai_api_key")))

    # --- 分類 ---
    def classify(self, text: str) -> tuple[str, str]:
        """(分類, 判定元)。AI失敗時はルールに戻す。"""
        rule = rule_classify(text)
        if rule in (SPAM, CAUTION) or not self.active():
            return rule, "rule"
        try:
            out = self._json(self._chat(self.settings.get("ai"), CLASSIFY_SYSTEM, text, 100))
        except AIError as e:
            self.log.warn("ai", f"AIの判定に失敗したためルールで分類しました: {e}")
            return rule, "rule"
        cat = out.get("category")
        return (cat if cat in CATEGORIES else rule), "ai"

    # --- 返信文の生成 ---
    def generate_reply(self, instruction: str, target: str, thread: list[str] | None = None, facts: str = "") -> str:
        """指示（プロンプト）と文脈から、返信文を作る。リンク入り・長すぎる場合はエラー。"""
        if not self.active():
            raise AIError("AIが使えません（設定が無効か、モックモードです）")
        parts = [f"【対象の投稿】\n{target}"]
        if facts:
            parts.append(f"【状況】\n{facts}")
        if thread:
            parts.append("【スレッドの流れ（古い順）】\n" + "\n".join(f"- {t}" for t in thread))
        text = self._chat(self.settings.get("ai"), instruction.strip() + GENERATE_RULES, "\n\n".join(parts), 400).strip()
        text = text.strip("「」\"'` \n")
        if not text:
            raise AIError("返信文が空でした")
        if has_url(text):
            raise AIError("返信文にリンクが含まれていたため、使いませんでした")
        if re.search(r"[@＠#＃]", text):  # 他人の投稿に含まれる指示で、メンションやハッシュタグが混ざるのを防ぐ
            raise AIError("返信文に @ や # が含まれていたため、使いませんでした")
        if weighted_len(text) > TWEET_LIMIT:
            raise AIError("返信文が長すぎたため、使いませんでした")
        return text

    # --- 呼び出し ---
    @staticmethod
    def _json(content: str) -> dict:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise AIError("応答にJSONがありません")
        try:
            return json.loads(m.group(0))
        except ValueError as e:
            raise AIError("応答の形式が不正です") from e

    def _chat(self, cfg: dict, system: str, user: str, max_tokens: int) -> str:
        p = cfg["provider"]
        if p == "bedrock":
            return self._chat_bedrock(cfg, system, user, max_tokens)
        if p == "openai":
            return self._chat_openai(cfg, system, user)
        raise AIError("AIが無効です")

    def _chat_bedrock(self, cfg: dict, system: str, user: str, max_tokens: int) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            model = self.resolve_model(cfg)
            client = self._bedrock_factory(cfg["bedrock_region"])
            resp = client.converse(modelId=model, system=[{"text": system}], messages=[{"role": "user", "content": [{"text": user}]}],
                                   inferenceConfig={"maxTokens": max_tokens, "temperature": 0.3})
            return "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        except ClientError as e:
            err = e.response.get("Error", {})
            raise AIError(f"Bedrockがエラーを返しました（{err.get('Code', '不明')}）: {err.get('Message', '')}"[:300]) from e
        except (BotoCoreError, KeyError, TypeError) as e:
            raise AIError(f"Bedrockの呼び出しに失敗しました（{type(e).__name__}）") from e

    def _chat_openai(self, cfg: dict, system: str, user: str) -> str:
        key = self.secrets.get("openai_api_key")
        if not key:
            raise AIError("OpenAIのAPIキーが設定されていません")
        body = json.dumps({"model": cfg["openai_model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
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

    # --- Bedrockのモデル一覧 ---
    def list_models(self, refresh: bool = False) -> dict:
        """いま使えるモデル（と推論プロファイル）の一覧。1時間キャッシュする。"""
        cfg = self.settings.get("ai")
        region = cfg["bedrock_region"]
        if not self.allow_external:
            items = [{"id": "mock.small-model", "label": "（サンプル）小型モデル", "provider": "mock", "kind": "model"},
                     {"id": "mock.large-model", "label": "（サンプル）大型モデル", "provider": "mock", "kind": "model"}]
            return {"items": items, "default": pick_default(items, region), "region": region, "cached": False, "sample": True}
        cache = self.repo.get("state", "bedrock_models") if self.repo else None
        if (not refresh and cache and cache.get("region") == region
                and parse_iso(cache["fetched_at"]) > self.now() - timedelta(seconds=MODELS_CACHE_SECONDS)):
            return {"items": cache["items"], "default": pick_default(cache["items"], region), "region": region, "cached": True, "sample": False}
        items = self._fetch_models(region)
        if self.repo:
            self.repo.put("state", "bedrock_models", {"region": region, "fetched_at": iso(self.now()), "items": items})
        return {"items": items, "default": pick_default(items, region), "region": region, "cached": False, "sample": False}

    def _fetch_models(self, region: str) -> list[dict]:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            c = self._control_factory(region)
            fms = c.list_foundation_models(byOutputModality="TEXT").get("modelSummaries", [])
            text_ids = {m["modelId"] for m in fms}
            items: list[dict] = []
            for m in fms:
                if m.get("modelLifecycle", {}).get("status", "ACTIVE") != "ACTIVE" or "ON_DEMAND" not in m.get("inferenceTypesSupported", ["ON_DEMAND"]):
                    continue
                items.append({"id": m["modelId"], "label": f"{m.get('providerName', '')} {m.get('modelName', m['modelId'])}".strip(),
                              "provider": m.get("providerName", ""), "kind": "model"})
            try:
                profiles = c.list_inference_profiles(typeEquals="SYSTEM_DEFINED", maxResults=1000).get("inferenceProfileSummaries", [])
            except ClientError:
                profiles = []  # 推論プロファイルの権限が無くても、モデルの一覧は返す
            for p in profiles:
                under = [x.get("modelArn", "").split("foundation-model/")[-1] for x in p.get("models", [])]
                if p.get("status", "ACTIVE") == "ACTIVE" and under and all(u in text_ids for u in under):
                    items.append({"id": p["inferenceProfileId"], "label": p.get("inferenceProfileName", p["inferenceProfileId"]),
                                  "provider": "", "kind": "profile"})
            return sorted(items, key=lambda it: (it["kind"], it["label"].lower()))
        except ClientError as e:
            err = e.response.get("Error", {})
            raise AIError(f"モデル一覧を取得できませんでした（{err.get('Code', '不明')}）: {err.get('Message', '')}"[:300]) from e
        except (BotoCoreError, KeyError, TypeError) as e:
            raise AIError(f"モデル一覧の取得に失敗しました（{type(e).__name__}）") from e

    def resolve_model(self, cfg: dict) -> str:
        """画面で選んだモデル。未選択（自動）なら、使える一覧から、安価なモデルを選ぶ。"""
        if cfg["bedrock_model"]:
            return cfg["bedrock_model"]
        picked = self.list_models()["default"]
        if not picked:
            raise AIError("使えるモデルが見つかりません。AWSコンソールのBedrockで、モデルの利用を有効にしてください")
        return picked

    def test(self) -> dict:
        cfg = self.settings.get("ai")
        if cfg["provider"] == "rule":
            raise AIError("AIを使わない設定です")
        if not self.allow_external:
            raise AIError("モックモードでは外部のAIを呼びません")
        out = self._json(self._chat(cfg, CLASSIFY_SYSTEM, SAMPLE, 100))
        cat = out.get("category", "")
        model = self.resolve_model(cfg) if cfg["provider"] == "bedrock" else cfg["openai_model"]
        return {"provider": cfg["provider"], "model": model, "category": cat, "label": LABELS.get(cat, "")}
