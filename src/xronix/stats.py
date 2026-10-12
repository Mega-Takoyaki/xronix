"""統計。保存済みのデータから集計する（外部APIは呼ばない）。"""
from __future__ import annotations

from datetime import timedelta

from .classify import LABELS
from .common import JST, jst_date, parse_iso, utcnow

COST_REPLY = 0.015  # 返信の料金区分は未確認。投稿作成と同額と仮置き


def build(repo, now=utcnow, days: int = 14) -> dict:
    mentions = repo.list("mentions")
    runs = repo.list("runs", limit=200, reverse=True)
    today = now().astimezone(JST).date()
    labels = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    received = {d: 0 for d in labels}
    sent = {d: 0 for d in labels}
    by_cat: dict[str, int] = {}
    by_state: dict[str, int] = {}
    for m in mentions:
        by_cat[m["category"]] = by_cat.get(m["category"], 0) + 1
        by_state[m["state"]] = by_state.get(m["state"], 0) + 1
        d = jst_date(parse_iso(m["fetched_at"]))
        if d in received:
            received[d] += 1
        if m["state"] == "sent" and m.get("sent_at"):
            d = jst_date(parse_iso(m["sent_at"]))
            if d in sent:
                sent[d] += 1
    month = today.strftime("%Y-%m")
    month_runs = [r for r in runs if jst_date(parse_iso(r["started_at"])).startswith(month)]
    reads = sum(r.get("reads", 0) for r in month_runs)
    sent_month = sum(1 for m in mentions if m["state"] == "sent" and m.get("sent_at") and jst_date(parse_iso(m["sent_at"])).startswith(month))
    ok = sum(1 for r in runs if r["status"] == "ok")
    templates = sorted(repo.list("templates"), key=lambda t: -t["use_count"])[:5]
    return {
        "days": labels,
        "received": [received[d] for d in labels],
        "sent": [sent[d] for d in labels],
        "by_category": [{"key": k, "label": LABELS.get(k, k), "count": v} for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])],
        "by_state": by_state,
        "total_mentions": len(mentions),
        "runs": {"total": len(runs), "ok": ok, "error": sum(1 for r in runs if r["status"] == "error")},
        "month": {"reads": reads, "sent": sent_month,
                  "est_cost_usd": round(reads * 0.001 + sent_month * COST_REPLY, 3)},
        "top_templates": [{"id": t["id"], "text": t["text"], "use_count": t["use_count"]} for t in templates if t["use_count"]],
    }
