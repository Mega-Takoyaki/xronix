"""ルール: 「どの反応に（種類・分類）」「何をするか（アクション）」を設定する。

- 反応のルール（返信・引用・メンション）は、上から順に見て、最初に合致した1件だけを実行する。
- 数値のルール（いいね数など）は、ルールごとに独立。各投稿で、基準を初めて超えたときに1回だけ実行する。
"""
from __future__ import annotations

from .classify import CATEGORIES, FAN, QUESTION, SPAM, CAUTION, OTHER
from .common import iso, new_id, utcnow
from .events import ALL_EVENTS, METRIC_EVENTS, REACTION_EVENTS, is_metric
from .settings import ValidationError

ACTION_LABELS = {
    "ignore": "何もしない（記録のみ）",
    "notify": "Telegramで通知",
    "like": "いいねを付ける",
    "queue_reply": "返信案を作り、承認待ちに入れる",
    "send_reply": "返信を作り、承認なしで送信する",
    "blocklist": "ブロック対象（アプリ内）に追加",
}
REPLY_ACTIONS = ("queue_reply", "send_reply")
METRIC_ACTIONS = ("ignore", "notify", "queue_reply", "send_reply")  # 自分の投稿が相手なので、いいね・ブロックは対象外
SOURCES = {"queue_reply": ("template", "ai", "manual"), "send_reply": ("template", "ai")}
MAX_ACTIONS = 5
MAX_PROMPT = 2000

DEFAULT_PROMPT = ("あなたは、親しみやすく丁寧なSNSアカウントの運用者です。受け取った投稿に、短い日本語で、感謝や共感を伝える返信を書いてください。"
                  "相手を傷つけず、約束や個人情報には触れないでください。")


def default_action(kind: str) -> dict:
    return {"type": kind, "source": "template", "template_category": "auto", "prompt": DEFAULT_PROMPT, "context": "single", "thread_depth": 3}


def _norm_action(a: dict, metric: bool) -> dict:
    t = a.get("type")
    if t not in ACTION_LABELS:
        raise ValidationError("不明なアクションです")
    if metric and t not in METRIC_ACTIONS:
        raise ValidationError(f"「{ACTION_LABELS[t]}」は、自投稿の数値のルールでは使えません")
    out: dict = {"type": t}
    if t in REPLY_ACTIONS:
        src = a.get("source", "template")
        if src not in SOURCES[t]:
            raise ValidationError("返信文の作り方が不正です")
        out["source"] = src
        tc = a.get("template_category", "auto")
        if tc not in ("auto", FAN, QUESTION):
            raise ValidationError("定型文の分類が不正です")
        out["template_category"] = tc
        prompt = str(a.get("prompt", DEFAULT_PROMPT)).strip()
        if src == "ai" and not prompt:
            raise ValidationError("AIで作る場合は、指示（プロンプト）を入力してください")
        if len(prompt) > MAX_PROMPT:
            raise ValidationError(f"指示は{MAX_PROMPT}文字以内にしてください")
        out["prompt"] = prompt
        ctx = a.get("context", "single")
        if ctx not in ("single", "thread"):
            raise ValidationError("文脈の指定が不正です")
        out["context"] = ctx
        depth = a.get("thread_depth", 3)
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 10:
            raise ValidationError("スレッドの遡る数は1〜10で指定してください")
        out["thread_depth"] = depth
    return out


def validate_rule(data: dict, partial_of: dict | None = None) -> dict:
    cur = dict(partial_of or {"enabled": True, "categories": [], "threshold": 100, "events": [], "actions": [], "name": ""})
    cur.update({k: v for k, v in data.items() if k in ("name", "enabled", "events", "categories", "threshold", "actions")})
    name = str(cur["name"]).strip()
    if not name or len(name) > 60:
        raise ValidationError("ルール名は1〜60文字で入力してください")
    events = cur["events"]
    if not isinstance(events, list) or not events or any(e not in ALL_EVENTS for e in events):
        raise ValidationError("反応の種類を、1つ以上選んでください")
    metric = [is_metric(e) for e in events]
    if any(metric) and not all(metric):
        raise ValidationError("返信などの反応と、自投稿の数値は、同じルールにまとめられません")
    cats = cur["categories"]
    if not isinstance(cats, list) or any(c not in CATEGORIES for c in cats):
        raise ValidationError("分類の指定が不正です")
    is_m = all(metric)
    if is_m:
        cats = []  # 自投稿の数値に、分類は無い
        th = cur["threshold"]
        if isinstance(th, bool) or not isinstance(th, int) or not 1 <= th <= 100_000_000:
            raise ValidationError("基準の数は1以上の整数で指定してください")
        if len(events) != 1:
            raise ValidationError("自投稿の数値のルールは、1つの種類ごとに作ってください")
    actions = cur["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise ValidationError(f"アクションは1〜{MAX_ACTIONS}個で指定してください")
    norm = [_norm_action(a, is_m) for a in actions]
    if sum(1 for a in norm if a["type"] in REPLY_ACTIONS) > 1:
        raise ValidationError("返信のアクションは、1つのルールに1つまでです")
    return {"name": name, "enabled": bool(cur["enabled"]), "events": events, "categories": cats,
            "threshold": cur["threshold"] if is_m else 0, "actions": norm}


def _seed() -> list[dict]:
    reaction = list(REACTION_EVENTS)
    return [
        {"name": "スパムは記録のみ", "enabled": True, "events": reaction, "categories": [SPAM], "actions": [{"type": "ignore"}]},
        {"name": "要注意・その他は、人が見て返信を書く", "enabled": True, "events": reaction, "categories": [CAUTION, OTHER],
         "actions": [{"type": "queue_reply", "source": "manual"}]},
        {"name": "ポジティブ反応: 定型文の返信案", "enabled": True, "events": reaction, "categories": [FAN],
         "actions": [{"type": "queue_reply", "source": "template", "template_category": "auto"}]},
        {"name": "質問: 定型文の返信案", "enabled": True, "events": reaction, "categories": [QUESTION],
         "actions": [{"type": "queue_reply", "source": "template", "template_category": "auto"}]},
        # 以下は、使い方の見本（無効）
        {"name": "【見本】ポジティブ反応にいいねを付ける", "enabled": False, "events": ["reply_to_post"], "categories": [FAN],
         "actions": [{"type": "like"}]},
        {"name": "【見本】いいねが100件を超えたら、AIの追加コメントを承認待ちに", "enabled": False, "events": ["metric_likes"], "threshold": 100,
         "actions": [{"type": "queue_reply", "source": "ai", "prompt": "あなたの投稿に、たくさんのいいねが付きました。お礼を、短く、自然な日本語で書いてください。",
                      "context": "single"}]},
    ]


class Rules:
    def __init__(self, repo, now=utcnow):
        self.repo, self.now = repo, now

    def seed_if_empty(self) -> None:
        if self.repo.list("rules", limit=1):
            return
        for r in _seed():
            self.create(r)

    def list(self) -> list[dict]:
        return sorted(self.repo.list("rules"), key=lambda r: (r["order"], r["created_at"]))

    def get(self, id: str) -> dict | None:
        return self.repo.get("rules", id)

    def create(self, data: dict) -> dict:
        v = validate_rule(data)
        existing = self.list()
        item = {"id": new_id(), "order": (existing[-1]["order"] + 1) if existing else 0, "created_at": iso(self.now()), **v}
        self.repo.put("rules", item["id"], item)
        return item

    def update(self, id: str, data: dict) -> dict | None:
        cur = self.get(id)
        if not cur:
            return None
        v = validate_rule(data, partial_of={k: cur[k] for k in ("name", "enabled", "events", "categories", "threshold", "actions")})
        return self.repo.update_if("rules", id, v)

    def delete(self, id: str) -> None:
        self.repo.delete("rules", id)

    def move(self, id: str, direction: str) -> None:
        rules = self.list()
        i = next((n for n, r in enumerate(rules) if r["id"] == id), None)
        j = None if i is None else (i - 1 if direction == "up" else i + 1 if direction == "down" else None)
        if i is None or j is None or not 0 <= j < len(rules):
            return
        rules[i], rules[j] = rules[j], rules[i]
        for n, r in enumerate(rules):  # 並べ直して、順序を振り直す
            self.repo.update_if("rules", r["id"], {"order": n})

    def match(self, kind: str, category: str) -> dict | None:
        """反応のルール: 上から見て、最初に合致した1件。"""
        for r in self.list():
            if r["enabled"] and kind in r["events"] and (not r["categories"] or category in r["categories"]):
                return r
        return None

    def metric_rules(self) -> list[dict]:
        return [r for r in self.list() if r["enabled"] and r["events"] and is_metric(r["events"][0])]
