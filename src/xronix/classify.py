"""ルールベースの分類。迷う場合は人が見る側（要注意）に倒す。"""
from __future__ import annotations

import re

from .common import has_url

SPAM = "spam"
QUESTION = "question"
FAN = "fan"
CAUTION = "caution"
OTHER = "other"

CATEGORIES = (FAN, QUESTION, SPAM, CAUTION, OTHER)
LABELS = {FAN: "ポジティブ反応", QUESTION: "質問", SPAM: "スパム", CAUTION: "要注意", OTHER: "その他"}

_SPAM_WORDS = ("副業", "稼げ", "稼ぐ", "儲", "投資", "仮想通貨", "ビットコイン", "無料でプレゼント", "プレゼント企画",
               "フォロワー増", "相互フォロー", "拡散希望", "LINE追加", "公式LINE", "出会い", "パパ活", "ライブ配信", "副収入",
               "crypto", "airdrop", "giveaway", "followback", "dm me", "check my profile")
_CAUTION_WORDS = ("死ね", "消えろ", "きもい", "キモい", "ブス", "詐欺", "通報", "訴え", "弁護士", "晒", "住所", "本名", "年齢",
                  "未成年", "JK", "JC", "ヌード", "裸", "エロ", "えっち", "抱きたい", "やらせ", "会いたい", "DM", "ＤＭ",
                  "いくら", "料金", "写真送", "動画送", "無修正", "fanvue", "ファンビュー", "ブロック", "炎上")
_QUESTION_RE = re.compile(r"[?？]|ですか|ますか|でしょうか|どこ|いつ(から|まで|頃|ごろ|発売|更新|投稿|予定)|どうやって|教えて|何の|なんの|誰|だれ")
_FAN_WORDS = ("かわいい", "可愛い", "かっこいい", "綺麗", "きれい", "好き", "最高", "素敵", "すてき", "えらい", "神", "ありがとう",
              "応援", "癒", "尊い", "天使", "女神", "ファン", "楽しみ", "love", "cute", "beautiful", "😍", "🥰", "❤", "💕", "😊")
_MENTION_RE = re.compile(r"@\w+")


def clean_text(text: str) -> str:
    return _MENTION_RE.sub("", text).strip()


def classify(text: str) -> str:
    body = clean_text(text)
    low = body.lower()
    if has_url(body) or any(w in low for w in (x.lower() for x in _SPAM_WORDS)):
        return SPAM
    if any(w.lower() in low for w in _CAUTION_WORDS):
        return CAUTION
    if _QUESTION_RE.search(body):
        return QUESTION
    if any(w.lower() in low for w in _FAN_WORDS):
        return FAN
    return OTHER
