"""定型文の管理と選択。最近選んでいないものを優先して選ぶ（同じ文面の連投を避ける）。use_countは送信実績。"""
from __future__ import annotations

from .classify import FAN, QUESTION
from .common import iso, new_id, utcnow

SEED = [
    (FAN, "ありがとうございます！とても嬉しいです☺️"),
    (FAN, "温かいお言葉、ありがとうございます。励みになります！"),
    (FAN, "見てくださって嬉しいです。これからもよろしくお願いします✨"),
    (FAN, "コメントありがとうございます！また見に来てくださいね。"),
    (QUESTION, "ご質問ありがとうございます。確認して、改めてお答えしますね。"),
    (QUESTION, "メッセージありがとうございます！お答えできる範囲でお返事しますね。"),
]


class Templates:
    def __init__(self, repo, now=utcnow):
        self.repo, self.now = repo, now

    def seed_if_empty(self) -> None:
        if self.repo.list("templates", limit=1):
            return
        for cat, text in SEED:
            self.create(cat, text)

    def list(self) -> list[dict]:
        return sorted(self.repo.list("templates"), key=lambda t: (t["category"], t["created_at"]))

    def create(self, category: str, text: str, enabled: bool = True) -> dict:
        item = {"id": new_id(), "category": category, "text": text.strip(), "enabled": enabled,
                "use_count": 0, "last_used_at": "", "last_picked_at": "", "created_at": iso(self.now())}
        self.repo.put("templates", item["id"], item)
        return item

    def update(self, id: str, **values) -> dict | None:
        allowed = {k: v for k, v in values.items() if k in ("category", "text", "enabled")}
        return self.repo.update_if("templates", id, allowed)

    def delete(self, id: str) -> None:
        self.repo.delete("templates", id)

    def pick(self, category: str, exclude_id: str | None = None) -> dict | None:
        cands = [t for t in self.repo.list("templates") if t["category"] == category and t["enabled"] and t["id"] != exclude_id]
        if not cands:
            return None
        cands.sort(key=lambda t: (t.get("last_picked_at", ""), t["use_count"]))
        chosen = cands[0]
        self.repo.update_if("templates", chosen["id"], {"last_picked_at": iso(self.now())})
        return chosen

    def mark_used(self, id: str) -> None:
        t = self.repo.get("templates", id)
        if t:
            self.repo.update_if("templates", id, {"use_count": t["use_count"] + 1, "last_used_at": iso(self.now())})
