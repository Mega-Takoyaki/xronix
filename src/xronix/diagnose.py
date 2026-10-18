"""取得の診断。取りこぼしの原因を、実機のXの応答から調べる（読み取りだけ。取り込みは、明示したときだけ）。

投稿のURL（またはID）を渡すと、その投稿を読み、返信の種類・返信先・本文の@・取り込み状況・取得位置との関係から、
「なぜ取り込まれないか」を日本語で返す。投稿の本文は、画面に返す先頭の一部だけで、ログには残さない。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .common import iso, parse_iso
from .events import LABELS, classify_event
from .settings import ValidationError
from .xclient import SEARCH_WINDOW_DAYS, TWITTER_EPOCH_MS, XError

COST_OTHER_READ = 0.005
COST_OWN_READ = 0.001


def parse_tweet_id(text: str) -> str:
    m = re.search(r"status(?:es)?/(\d{5,25})", text or "") or re.fullmatch(r"\s*(\d{5,25})\s*", text or "")
    if not m:
        raise ValidationError("投稿のURL（https://x.com/…/status/数字）か、投稿のID（数字）を入力してください")
    return m.group(1)


def snowflake_time(tweet_id: str) -> datetime:
    return datetime.fromtimestamp(((int(tweet_id) >> 22) + TWITTER_EPOCH_MS) / 1000, timezone.utc)


def _jst(dt: datetime) -> str:
    return (dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S（日本時間）")


class Diagnose:
    def __init__(self, app):
        self.app = app

    def _ready(self):
        xcfg = self.app.settings.get("x")
        if not xcfg["user_id"]:
            raise XError("Xに接続されていません")
        return xcfg, self.app.tokens.get_access_token()

    def tweet(self, text: str, ingest: bool = False) -> dict:
        tid = parse_tweet_id(text)
        xcfg, token = self._ready()
        a = self.app
        my_id, username = xcfg["user_id"], (xcfg.get("username") or "")
        res = a.x.tweets(token, [tid])
        reads = res["reads"]
        out: dict = {"id": tid, "posted_at": _jst(snowflake_time(tid)), "found": bool(res["tweets"]), "verdict": [], "reads": reads}
        cursor = (a.repo.get("state", "cursor") or {}).get("since_id") or ""
        out["mentions_cursor"] = {"id": cursor, "at": _jst(snowflake_time(cursor)) if cursor.isdigit() and int(cursor) > 0 else ""}
        old = a.repo.get("mentions", tid)
        if not res["tweets"]:
            out["verdict"] = ["Xが、この投稿を返しませんでした。削除された、鍵付きアカウントの投稿、凍結、またはブロックの関係で、見えない可能性があります。この場合、アプリからも取得できません。"]
            self._done(out, reads, 0.0)
            return out
        tw = res["tweets"][0]
        refs = {r.get("type"): r.get("id") for r in tw.get("referenced_tweets", [])}
        kind = classify_event(tw, my_id)
        reply_to = tw.get("in_reply_to_user_id") or ""
        at_me = bool(username) and ("@" + username.lower()) in (tw.get("text") or "").lower()
        directed = reply_to == my_id
        out.update(author_id=tw.get("author_id", ""), text_len=len(tw.get("text") or ""), preview=(tw.get("text") or "")[:60],
                   kind=kind, kind_label=LABELS.get(kind, kind), in_reply_to_user_id=reply_to, directed_at_me=directed, mentions_me=at_me,
                   conversation_id=tw.get("conversation_id", ""), replied_to=refs.get("replied_to", ""), quoted=refs.get("quoted", ""),
                   own_post=tw.get("author_id") == my_id)
        cost = COST_OTHER_READ * reads
        if refs.get("replied_to"):  # 返信先の投稿の作者（自分の投稿への返信かどうか）
            try:
                p = a.x.tweets(token, [refs["replied_to"]])
                reads += p["reads"]; cost += COST_OTHER_READ * p["reads"]
                if p["tweets"]:
                    out["parent"] = {"id": refs["replied_to"], "author_id": p["tweets"][0].get("author_id", ""), "by_me": p["tweets"][0].get("author_id") == my_id}
            except XError as e:
                out["parent_error"] = str(e)
        out["reads"] = reads
        out["ingested"] = None if not old else {"state": old.get("state"), "kind": LABELS.get(old.get("kind"), old.get("kind")),
                                                 "rule": old.get("rule_name") or "", "source": old.get("source") or "（不明）", "fetched_at": old.get("fetched_at", "")}
        v = out["verdict"]
        if out["own_post"]:
            v.append("自分の投稿です。自分の投稿は、取り込みません。")
        elif old:
            v.append(f"取り込み済みです（状態: {old.get('state')}、取得元: {old.get('source') or '不明'}）。承認待ち・履歴を確認してください。")
        else:
            if directed:
                v.append("返信先は、自分です（自分宛ての返信）。メンション一覧か、返信の検索で、取れるはずの投稿です。")
            elif refs.get("replied_to"):
                parent = out.get("parent")
                v.append("返信先が自分ではありません" + (f"（返信先の投稿の作者ID: {parent['author_id']}）" if parent else "") +
                         "。自分の投稿のスレッド内で、他のユーザーに返した返信は、取得の対象外です。" +
                         ("" if at_me else "本文にも自分の@がありません。"))
            elif refs.get("quoted"):
                v.append("引用の投稿です。引用元の作者が自分なら、引用の取得（自投稿の確認の周期）で取れます。")
            if at_me:
                v.append("本文に自分の@があります。メンション一覧に出る条件を満たします。")
            elif directed:
                v.append("本文に自分の@はありません。メンション一覧には出ず、返信の検索（to:）だけが頼りです。")
            if cursor.isdigit() and int(cursor) > 0:
                newer = int(tid) > int(cursor)
                v.append(f"メンション一覧の取得位置（{out['mentions_cursor']['at']}）より、この投稿は{'新しい' if newer else '古い'}です。"
                         + ("" if newer else "差分取得（取得位置より新しいものだけ）の対象外です。"))
            age = (a.now() - snowflake_time(tid)).total_seconds() / 86400
            if age > SEARCH_WINDOW_DAYS:
                v.append(f"投稿から{age:.1f}日たっています。返信の検索は、直近7日が対象です。")
            if directed:
                v.append("次回のジョブの実行で取り込まれない場合は、Xのこの2つのAPIが、この投稿を返していません（Xの仕様や、返信の表示のフィルタ）。下の「取り込む」で、手動で取り込めます。")
        if not out["own_post"] and not old:
            cost += self._probe(out, token, tw, my_id, username, tid)
        if ingest and not old and not out["own_post"]:
            item = a.jobs.ingest_manual(tw)
            out["ingest_result"] = {"state": item.get("state") if item else "", "rule": (item or {}).get("rule_name", "")}
            v.append("手動で取り込みました。承認待ちか履歴に入っています。")
        self._done(out, reads, cost)
        return out

    def _probe(self, out: dict, token: str, tw: dict, my_id: str, username: str, tid: str) -> float:
        """この投稿が、Xの検索に出るかを調べる（狭い検索を2つ）。メンション一覧は、取得位置の関係で、ここでは調べられない。"""
        a = self.app
        age = (a.now() - snowflake_time(tid)).total_seconds() / 86400
        out["probes"] = []
        if age > SEARCH_WINDOW_DAYS:
            out["verdict"].append("検索の確認: 投稿から7日以上たっているため、行いませんでした（検索は直近7日だけ）。")
            return 0.0
        queries = []
        if username and tw.get("author_id"):
            queries.append(("投稿者 × 自分宛て", f"from:{tw['author_id']} to:{username}"))
        if tw.get("conversation_id"):
            queries.append(("同じ会話", f"conversation_id:{tw['conversation_id']}"))
        cost, hit, errors = 0.0, False, 0
        for label, q in queries:
            try:
                r = a.x.search_recent(token, q, 10)
                ids = [t["id"] for t in r["tweets"]]
                out["reads"] += r["reads"]; cost += COST_OTHER_READ * r["reads"]
                out["probes"].append({"label": label, "query": q, "count": len(ids), "contains": tid in ids})
                hit = hit or tid in ids
            except XError as e:
                errors += 1
                out["probes"].append({"label": label, "query": q, "error": str(e)})
        v = out["verdict"]
        if hit:
            v.append("検索の確認: Xの検索には、この投稿が出ます。返信の検索（to:）の条件や、取得の周期に、改善の余地があります。")
        elif queries and not errors:
            v.append("検索の確認: どの検索にも、この投稿は出ませんでした。メンション一覧にも出ていないため、Xが、この投稿を、一覧と検索から外している可能性があります"
                     "（スパム・品質のフィルタ、ブロック・ミュートの関係など。推測です）。アプリからは、自動で取得できません。必要なら、「取り込む」で、手動で取り込めます。")
        elif errors:
            v.append("検索の確認: 検索がエラーになりました。下の表のエラーを確認してください。")
        return cost

    def posts(self) -> dict:
        """自分の投稿ごとの、Xが数える返信数・引用数と、取り込み済みの件数。差があれば、取りこぼしの目安。"""
        xcfg, token = self._ready()
        a = self.app
        cfg = a.settings.get("job")
        start = (a.now() - timedelta(days=cfg["metrics_window_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = a.x.own_posts(token, xcfg["user_id"], start, cfg["metrics_max_posts"])
        items = a.repo.list("mentions")
        rows = []
        for p in res["tweets"]:
            m = p.get("public_metrics", {})
            rows.append({"id": p["id"], "posted_at": _jst(snowflake_time(p["id"])), "preview": (p.get("text") or "")[:40],
                         "x_replies": int(m.get("reply_count", 0)), "x_quotes": int(m.get("quote_count", 0)), "x_likes": int(m.get("like_count", 0)),
                         "got_replies": sum(1 for i in items if i.get("conversation_id") == p["id"] and i.get("kind") in ("reply_to_post", "reply_to_reply")),
                         "got_quotes": sum(1 for i in items if i.get("kind") == "quote" and i.get("parent_id") == p["id"])})
        self._done({}, res["reads"], COST_OWN_READ * res["reads"], what="自投稿の返信数の確認")
        return {"posts": rows, "reads": res["reads"], "window_days": cfg["metrics_window_days"]}

    def _done(self, out: dict, reads: int, cost: float, what: str = "取得の診断") -> None:
        self.app.logger.info("job", f"{what}: 読み取り{reads}件（約${cost:.3f}）" + (f"、投稿ID {out['id']}" if out.get("id") else ""))
