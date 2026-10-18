"""アクションの実行。ルールに合致した反応に対して、設定されたアクションを順に実行する。

安全のための決まり:
- 承認なしの送信（send_reply）は、1日の自動送信の上限・全体の送信上限・緊急停止・ブロック対象・リンク禁止の検査を通る。
  上限に達した場合や、返信文を作れなかった場合は、送らずに承認待ちに回す。
- いいねは、1日のいいね上限を超えない。
- 投稿作成は再試行しない（sender）。
"""
from __future__ import annotations

from .classify import FAN, LABELS as CATEGORY_LABELS, QUESTION
from .common import iso, jst_date, utcnow
from . import limits
from .events import LABELS as EVENT_LABELS
from .rules import ACTION_LABELS, REPLY_ACTIONS
from .sender import SendRefused


def _res(t: str, ok: bool, detail: str) -> dict:
    return {"type": t, "label": ACTION_LABELS.get(t, t), "ok": ok, "detail": detail}


class ActionEngine:
    def __init__(self, repo, settings, x, tokens, ai, templates, notifier, sender, logger, now=utcnow, sendq=None):
        self.repo, self.settings, self.x, self.tokens, self.sendq = repo, settings, x, tokens, sendq
        self.ai, self.templates, self.notifier, self.sender, self.log, self.now = ai, templates, notifier, sender, logger, now

    def _count(self, name: str) -> str:
        return f"{name}#{jst_date(self.now())}"

    def run(self, item: dict, rule: dict, run: dict | None = None) -> dict:
        """ルールのアクションを順に実行し、結果を記録して保存する。1つが失敗しても、残りは続ける。"""
        results: list[dict] = []
        item.update(rule_id=rule["id"], rule_name=rule["name"], state="done")
        for a in rule["actions"]:
            t = a["type"]
            try:
                if t == "ignore":
                    res = _res(t, True, "履歴に記録しました")
                elif t == "notify":
                    res = self._notify(item)
                elif t == "like":
                    res = self._like(item)
                elif t == "blocklist":
                    res = self._block(item)
                else:
                    res = self._reply(item, a, run)
            except Exception as e:  # noqa: BLE001  予期しない失敗も、記録して残りを続ける
                self.log.error("action", f"アクション「{ACTION_LABELS.get(t, t)}」で予期しないエラー: {type(e).__name__}", mention_id=item["id"])
                res = _res(t, False, f"予期しないエラー（{type(e).__name__}）")
            results.append(res)
            if run is not None:
                run["actions"] = run.get("actions", 0) + 1
        item["actions"] = results
        if item["state"] == "done" and item.get("category") == "spam":
            item["state"] = "spam"
        item["updated_at"] = iso(self.now())
        self.repo.put("mentions", item["id"], item)
        return item

    # --- 個別のアクション ---
    def _notify(self, item: dict) -> dict:
        label = EVENT_LABELS.get(item.get("kind", ""), "反応")
        cat = CATEGORY_LABELS.get(item.get("category", ""), "")
        ok = self.notifier.notify_text(f"XroniX: {label}" + (f"（{cat}）" if cat and not item.get("kind", "").startswith("metric") else "") + "がありました。")
        return _res("notify", ok, "通知しました" if ok else "通知しませんでした（Telegramの通知が無効か、設定が未完了です）")

    def _like(self, item: dict) -> dict:
        limit = self.settings.get("safety")["like_daily_limit"]
        if self.repo.incr("state", self._count("likes"), "count", 1, max_value=limit) is None:
            return _res("like", False, f"1日のいいね上限（{limit}件）に達したため、付けませんでした")
        try:
            self.x.like(self.tokens.get_access_token(), self.settings.get("x")["user_id"], item["target_tweet_id"])
        except Exception as e:  # noqa: BLE001
            self.repo.incr("state", self._count("likes"), "count", -1)
            self.log.error("action", f"いいねに失敗しました: {e}", mention_id=item["id"])
            return _res("like", False, f"いいねに失敗しました: {e}")
        return _res("like", True, "いいねを付けました")

    def _block(self, item: dict) -> dict:
        aid = item.get("author_id", "")
        if not aid:
            return _res("blocklist", False, "ユーザーIDが分かりません")
        self.repo.put("blocklist", aid, {"id": aid, "note": "ルールにより追加", "created_at": iso(self.now())})
        return _res("blocklist", True, "ブロック対象に追加しました")

    # --- 返信（承認待ち／自動送信） ---
    def _draft(self, item: dict, a: dict, run: dict | None) -> tuple[str, str, str]:
        """(返信文, 使った定型文のID, 失敗の理由)。作れなければ返信文は空。"""
        src = a.get("source", "template")
        if src == "manual":
            return "", "", ""
        if src == "template":
            cat = a.get("template_category", "auto")
            if cat == "auto":
                cat = item.get("category") if item.get("category") in (FAN, QUESTION) else FAN
            t = self.templates.pick(cat)
            return (t["text"], t["id"], "") if t else ("", "", "有効な定型文がありません")
        # AI
        thread = self._thread(item, a["thread_depth"], run) if a.get("context") == "thread" else None
        facts = ""
        if item.get("metric"):
            m = item["metric"]
            facts = f"{m['label']}が{m['value']}件になりました（基準は{m['threshold']}件）。これは、あなた自身の投稿です。"
        try:
            return self.ai.generate_reply(a.get("prompt", ""), item.get("text", ""), thread, facts), "", ""
        except Exception as e:  # noqa: BLE001  AIErrorを含む。作れなければ、人が書く
            self.log.warn("action", f"AIで返信文を作れませんでした: {e}", mention_id=item["id"])
            return "", "", f"AIで返信文を作れませんでした: {e}"

    def _thread(self, item: dict, depth: int, run: dict | None) -> list[str]:
        """返信先をたどって、スレッドの流れ（古い順）を取る。取得した件数ぶん、読み取り料金がかかる。"""
        me = self.settings.get("x")["user_id"]
        token = self.tokens.get_access_token()
        lines: list[str] = []
        tid = item.get("parent_id", "")
        for _ in range(depth):
            if not tid:
                break
            got = self.x.tweets(token, [tid])
            if run is not None:
                run["thread_reads"] = run.get("thread_reads", 0) + got["reads"]
            if not got["tweets"]:
                break
            tw = got["tweets"][0]
            lines.append(("あなた: " if tw.get("author_id") == me else "相手: ") + tw.get("text", ""))
            refs = {r.get("type"): r.get("id") for r in tw.get("referenced_tweets", [])}
            tid = refs.get("replied_to", "")
        return list(reversed(lines))

    def _reply(self, item: dict, a: dict, run: dict | None) -> dict:
        t = a["type"]
        stop = limits.reached(self.repo, self.settings, self.now(), item)  # 会話が終わらなくなることを避ける（返信案も作らない）
        if stop:
            item.update(draft_text="", draft_template_id="", state="done", limit_reached=True)
            self.log.info("action", stop, mention_id=item["id"])
            return _res(t, True, stop)
        draft, tpl_id, why = self._draft(item, a, run)
        if run is not None and draft:
            run["drafted"] = run.get("drafted", 0) + 1
        item.update(draft_text=draft, draft_template_id=tpl_id, state="pending" if draft else "review")
        if t == "queue_reply" or not draft:
            note = "返信案を、承認待ちに入れました" if draft else "返信文は空のまま、承認待ちに入れました（人が書きます）"
            return _res(t, True, note + (f"。{why}" if why else ""))
        # 承認なしで送信する
        limit = self.settings.get("safety")["auto_daily_limit"]
        key = self._count("autosent")
        if self.repo.incr("state", key, "count", 1, max_value=limit) is None:
            return _res(t, True, f"1日の自動送信の上限（{limit}件）に達したため、承認待ちに入れました")
        self.repo.put("mentions", item["id"], item)  # 送信の前に、状態を保存する
        try:
            # すぐには送らず、送信キューに入れる。間隔（秒、ランダムな幅つき）をあけて、1件ずつ送る
            self.sendq.enqueue(item["id"], draft, auto=True, autosent_key=key)
        except SendRefused as e:
            self.repo.incr("state", key, "count", -1)
            item.update(self.repo.get("mentions", item["id"]) or {})
            return _res(t, False, f"送信キューに入れられなかったため、承認待ちに入れました: {e}")
        item.update(self.repo.get("mentions", item["id"]) or {})
        item["auto_queued"] = True
        return _res(t, True, "送信キューに入れました（間隔をあけて、順に送信します）")
