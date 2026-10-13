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
    "分類: fan（ポジティブ反応。好意的・応援・お礼などの感想）, question（質問）, spam（スパム・勧誘）, caution（攻撃的・個人情報・トラブルの恐れ）, other（その他）。"
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


def ranked(items: list[dict], region: str = "") -> list[str]:
    """使えるモデルのIDを、「安価な小型モデルを先に」の順に並べる。利用できないと分かっているものは除く。

    同じ名前のものが複数あれば、利用可と確認できたもの、リージョンに合う推論プロファイル、の順に優先する。
    """
    geo = _geo_prefix(region)
    usable = [it for it in items if it.get("available") is not False]

    def style(it: dict) -> tuple:
        i = it["id"]
        geo_rank = 0 if geo and i.startswith(geo) else 1 if i.split(".")[0] not in ("us", "eu", "apac", "global", "jp", "au") else 2
        return (it.get("available") is not True, geo_rank, i)

    out: list[str] = []
    for words in DEFAULT_PREFERENCE:
        for it in sorted((x for x in usable if all(w in (x["id"] + " " + x["label"]).lower() for w in words)), key=style):
            if it["id"] not in out:
                out.append(it["id"])
    out += [it["id"] for it in sorted(usable, key=style) if it["id"] not in out]
    return out


def pick_default(items: list[dict], region: str = "") -> str | None:
    r = ranked(items, region)
    return r[0] if r else None


def _availability(c, model_id: str) -> tuple[bool | None, str]:
    """モデルが、このアカウント・リージョンで使えるか。(使える/使えない/不明, 理由)。"""
    try:
        r = c.get_foundation_model_availability(modelId=model_id)
    except Exception:  # noqa: BLE001  権限が無い等。不明として扱う（一覧からは除かない）
        return None, ""
    if r.get("authorizationStatus") == "NOT_AUTHORIZED":
        return False, "利用が許可されていません（モデルの利用有効化が必要）"
    if r.get("entitlementAvailability") == "NOT_AVAILABLE":
        return False, "このアカウントでは使えません（契約・提供の対象外）"
    if r.get("regionAvailability") == "NOT_AVAILABLE":
        return False, "このリージョンでは使えません"
    agreement = (r.get("agreementAvailability") or {}).get("status")
    if agreement in ("PENDING", "NOT_AVAILABLE", "ERROR"):
        return False, {"PENDING": "利用規約への同意の処理中です", "NOT_AVAILABLE": "利用規約への同意が必要です", "ERROR": "利用規約の状態を確認できません"}[agreement]
    return True, ""


def _hint(code: str, message: str) -> str:
    low = message.lower()
    if "on-demand throughput" in low or "inference profile" in low:
        return "このモデルは、推論プロファイルのIDで呼び出します。一覧の「推論プロファイル」から選んでください"
    if code == "AccessDeniedException":
        return "このアカウントでは、このモデルを使えません。一覧で「利用可」と表示されたモデルを選んでください"
    if code == "ThrottlingException":
        return "呼び出しが多すぎます。しばらく待ってください"
    return ""


class AIService:
    def __init__(self, settings, secrets, http: Http, logger, allow_external: bool = True, bedrock_factory=None, control_factory=None,
                 repo=None, now=utcnow):
        self.settings, self.secrets, self.http, self.log = settings, secrets, http, logger
        self.allow_external = allow_external
        self._bedrock_factory = bedrock_factory or _default_bedrock_factory
        self._control_factory = control_factory or _default_control_factory
        self.repo, self.now = repo, now
        self.last_model = ""  # 直近に呼び出したBedrockのモデルID（接続テストの表示用）

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
        """モデルが選ばれていればそれを使う。「自動」なら、使える候補を順に試し、拒否されたものは1時間、候補から外す。"""
        from botocore.exceptions import BotoCoreError, ClientError

        auto = not cfg["bedrock_model"]
        try:
            models = self.candidates(cfg)[:3] if auto else [cfg["bedrock_model"]]
        except AIError:
            raise
        if not models:
            raise AIError("使えるモデルが見つかりません。AWSコンソールのBedrockで、モデルの利用を有効にしてください")
        last: Exception | None = None
        for model in models:
            try:
                client = self._bedrock_factory(cfg["bedrock_region"])
                resp = client.converse(modelId=model, system=[{"text": system}], messages=[{"role": "user", "content": [{"text": user}]}],
                                       inferenceConfig={"maxTokens": max_tokens, "temperature": 0.3})
                self.last_model = model
                return "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
            except ClientError as e:
                err = e.response.get("Error", {})
                code, msg = err.get("Code", "不明"), err.get("Message", "")
                if auto and code in ("AccessDeniedException", "ValidationException", "ResourceNotFoundException"):
                    self._block(model)  # 使えないモデル。次の候補を試す
                    last = e
                    continue
                hint = _hint(code, msg)
                raise AIError(f"Bedrockがエラーを返しました（{code}）: {msg}" + (f"　→ {hint}" if hint else "")) from e
            except (BotoCoreError, KeyError, TypeError) as e:
                raise AIError(f"Bedrockの呼び出しに失敗しました（{type(e).__name__}）") from e
        err = last.response.get("Error", {}) if last else {}
        raise AIError(f"使えるモデルが見つかりませんでした（試したモデルは、{', '.join(models)}）。最後のエラー: {err.get('Code', '不明')}: {err.get('Message', '')}"[:300])

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
    def _blocked(self) -> dict:
        rec = (self.repo.get("state", "bedrock_blocked") if self.repo else None) or {}
        now = iso(self.now())
        return {m: until for m, until in rec.get("items", {}).items() if until > now}

    def _block(self, model: str) -> None:
        if self.repo:
            items = self._blocked()
            items[model] = iso(self.now() + timedelta(seconds=MODELS_CACHE_SECONDS))
            self.repo.put("state", "bedrock_blocked", {"items": items})
            self.log.warn("ai", f"モデル {model} は使えなかったため、1時間、候補から外しました")

    def candidates(self, cfg: dict) -> list[str]:
        """「自動」のとき、試す順のモデルID。利用できないと分かっているもの、拒否されたものを除く。"""
        blocked = self._blocked()
        r = self.list_models()
        return [m for m in ranked(r["items"], r["region"]) if m not in blocked]

    def list_models(self, refresh: bool = False) -> dict:
        """いま使えるモデル（と推論プロファイル）の一覧。各モデルが、このアカウントで使えるかも確認する。1時間キャッシュ。"""
        cfg = self.settings.get("ai")
        region = cfg["bedrock_region"]
        if not self.allow_external:
            items = [{"id": "mock.small-model", "label": "（サンプル）小型モデル", "provider": "mock", "kind": "model", "available": True, "reason": ""},
                     {"id": "mock.large-model", "label": "（サンプル）大型モデル", "provider": "mock", "kind": "model", "available": True, "reason": ""},
                     {"id": "mock.restricted-model", "label": "（サンプル）使えないモデル", "provider": "mock", "kind": "model", "available": False,
                      "reason": "このアカウントでは使えません（契約・提供の対象外）"}]
            return {"items": items, "default": pick_default(items, region), "region": region, "cached": False, "sample": True}
        cache = self.repo.get("state", "bedrock_models") if self.repo else None
        if (not refresh and cache and cache.get("region") == region
                and parse_iso(cache["fetched_at"]) > self.now() - timedelta(seconds=MODELS_CACHE_SECONDS)):
            return {"items": cache["items"], "default": pick_default(cache["items"], region), "region": region, "cached": True, "sample": False}
        if refresh and self.repo:
            self.repo.delete("state", "bedrock_blocked")  # 更新したときは、外した候補も戻す
        items = self._fetch_models(region)
        if self.repo:
            self.repo.put("state", "bedrock_models", {"region": region, "fetched_at": iso(self.now()), "items": items})
        return {"items": items, "default": pick_default(items, region), "region": region, "cached": False, "sample": False}

    def _fetch_models(self, region: str) -> list[dict]:
        from concurrent.futures import ThreadPoolExecutor

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
                              "provider": m.get("providerName", ""), "kind": "model", "under": [m["modelId"]]})
            try:
                profiles = c.list_inference_profiles(typeEquals="SYSTEM_DEFINED", maxResults=1000).get("inferenceProfileSummaries", [])
            except ClientError:
                profiles = []  # 推論プロファイルの権限が無くても、モデルの一覧は返す
            for p in profiles:
                under = [x.get("modelArn", "").split("foundation-model/")[-1] for x in p.get("models", [])]
                if p.get("status", "ACTIVE") == "ACTIVE" and under and all(u in text_ids for u in under):
                    items.append({"id": p["inferenceProfileId"], "label": p.get("inferenceProfileName", p["inferenceProfileId"]),
                                  "provider": "", "kind": "profile", "under": under})
            # 利用可否を、並行して確認する（モデルごとのAPIのため）。プロファイルは、元のモデルの状態に従う
            need = sorted({u for it in items for u in it["under"]})
            with ThreadPoolExecutor(max_workers=8) as pool:
                status = dict(zip(need, pool.map(lambda mid: _availability(c, mid), need)))
            for it in items:
                states = [status[u] for u in it["under"]]
                bad = next((x for x in states if x[0] is False), None)
                it["available"] = False if bad else (True if all(x[0] is True for x in states) else None)
                it["reason"] = bad[1] if bad else ""
                del it["under"]
            return sorted(items, key=lambda it: (it["kind"], it["label"].lower()))
        except ClientError as e:
            err = e.response.get("Error", {})
            raise AIError(f"モデル一覧を取得できませんでした（{err.get('Code', '不明')}）: {err.get('Message', '')}"[:300]) from e
        except (BotoCoreError, KeyError, TypeError) as e:
            raise AIError(f"モデル一覧の取得に失敗しました（{type(e).__name__}）") from e

    def resolve_model(self, cfg: dict) -> str:
        """画面で選んだモデル。未選択（自動）なら、使える候補の先頭（安価なモデル）。"""
        if cfg["bedrock_model"]:
            return cfg["bedrock_model"]
        cands = self.candidates(cfg)
        if not cands:
            raise AIError("使えるモデルが見つかりません。AWSコンソールのBedrockで、モデルの利用を有効にしてください")
        return cands[0]

    def test(self) -> dict:
        cfg = self.settings.get("ai")
        if cfg["provider"] == "rule":
            raise AIError("AIを使わない設定です")
        if not self.allow_external:
            raise AIError("モックモードでは外部のAIを呼びません")
        out = self._json(self._chat(cfg, CLASSIFY_SYSTEM, SAMPLE, 100))
        cat = out.get("category", "")
        model = self.last_model if cfg["provider"] == "bedrock" else cfg["openai_model"]
        return {"provider": cfg["provider"], "model": model, "category": cat, "label": LABELS.get(cat, "")}
