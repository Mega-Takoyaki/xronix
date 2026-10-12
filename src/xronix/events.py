"""Xの反応（イベント）の種類。メンションの形から判別する（追加のAPI呼び出しは不要）。"""
from __future__ import annotations

REACTION_EVENTS = ("reply_to_post", "reply_to_reply", "quote", "mention")

# 自投稿の数値が基準以上になったとき。値は public_metrics のキー
METRIC_EVENTS = {
    "metric_likes": ("like_count", "いいね"),
    "metric_reposts": ("retweet_count", "リポスト"),
    "metric_replies": ("reply_count", "返信"),
    "metric_quotes": ("quote_count", "引用"),
    "metric_impressions": ("impression_count", "インプレッション"),
}

LABELS = {
    "reply_to_post": "自分の投稿への返信",
    "reply_to_reply": "自分の返信への返信（スレッドの続き）",
    "quote": "自分の投稿の引用",
    "mention": "その他のメンション",
    "metric_likes": "自投稿のいいね数が基準以上",
    "metric_reposts": "自投稿のリポスト数が基準以上",
    "metric_replies": "自投稿の返信数が基準以上",
    "metric_quotes": "自投稿の引用数が基準以上",
    "metric_impressions": "自投稿のインプレッション数が基準以上",
}
ALL_EVENTS = REACTION_EVENTS + tuple(METRIC_EVENTS)


def is_metric(kind: str) -> bool:
    return kind in METRIC_EVENTS


def classify_event(tw: dict, my_id: str) -> str:
    """メンションの形から、反応の種類を決める。"""
    refs = {r.get("type"): r.get("id") for r in tw.get("referenced_tweets", [])}
    if "replied_to" in refs and tw.get("in_reply_to_user_id") == my_id:
        # 返信先が会話の最初の投稿なら、自分の投稿への返信。そうでなければ、自分の返信への返信
        return "reply_to_post" if refs["replied_to"] == tw.get("conversation_id") else "reply_to_reply"
    if "quoted" in refs:
        return "quote"
    return "mention"
