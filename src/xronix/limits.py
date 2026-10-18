"""会話が終わらなくなることを防ぐ上限。同じ相手への返信の回数を数える。

相手が返信を繰り返す、または相手も似た仕組みで自動返信している場合、会話が際限なく続くことがある。
ルールによる返信案づくりと、自動送信は、次の2つの上限を超えたら止める（人が承認する送信は、止めない）。
"""
from __future__ import annotations

from .common import jst_date, parse_iso

COUNTED = ("sent", "sending", "send_queued")  # 送った・送信中・送信待ち（キュー）


def _when(m: dict) -> str:
    return m.get("sent_at") or m.get("updated_at") or ""


def count_replies(repo, now, author_id: str, conversation_id: str = "", exclude_id: str = "", include_queued: bool = True) -> tuple[int, int]:
    """(同じ会話での、この相手への返信数, 今日（日本時間）の、この相手への返信数)。"""
    today = jst_date(now)
    in_conv = today_n = 0
    for m in repo.list("mentions"):
        if m.get("id") == exclude_id or m.get("author_id") != author_id or m.get("state") not in COUNTED:
            continue
        if m.get("state") == "send_queued" and not include_queued:
            continue
        if conversation_id and m.get("conversation_id") == conversation_id:
            in_conv += 1
        w = _when(m)
        if w and jst_date(parse_iso(w)) == today:
            today_n += 1
    return in_conv, today_n


def reached(repo, settings, now, item: dict, include_queued: bool = True) -> str:
    """上限に達していれば、その理由（日本語）。達していなければ空。"""
    aid = item.get("author_id", "")
    if not aid or item.get("metric"):
        return ""
    s = settings.get("safety")
    in_conv, today_n = count_replies(repo, now, aid, item.get("conversation_id", ""), item.get("id", ""), include_queued)
    if in_conv >= s["conversation_reply_limit"]:
        return f"同じ相手との、この会話での返信が上限（{s['conversation_reply_limit']}回）に達しているため、返信しませんでした（会話が終わらなくなることを避けるため）"
    if today_n >= s["author_daily_limit"]:
        return f"同じ相手への、今日の返信が上限（{s['author_daily_limit']}回）に達しているため、返信しませんでした"
    return ""
