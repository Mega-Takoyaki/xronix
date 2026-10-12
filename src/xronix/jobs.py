"""定期処理。取得→保存→分類→返信案→通知。EventBridgeは短い間隔で tick() を呼ぶだけで、実行間隔は設定で決まる。"""
from __future__ import annotations

from datetime import timedelta

from .classify import CAUTION, FAN, OTHER, QUESTION, SPAM
from .common import Lease, iso, new_id, parse_iso, utcnow
from .xclient import AuthRequired, XError

COST_READ = 0.001


class JobRunner:
    def __init__(self, repo, settings, tokens, x, ai, templates, notifier, logger, now=utcnow):
        self.repo, self.settings, self.tokens, self.x = repo, settings, tokens, x
        self.ai, self.templates, self.notifier, self.log, self.now = ai, templates, notifier, logger, now
        self.lease = Lease(repo, "job", now)

    # --- 状態 ---
    def state(self) -> dict:
        return {"last_started_at": "", "last_status": "", **(self.repo.get("state", "job") or {})}

    def next_due(self) -> str:
        cfg, st = self.settings.get("job"), self.state()
        if not cfg["enabled"] or not st["last_started_at"]:
            return ""
        return iso(parse_iso(st["last_started_at"]) + timedelta(minutes=cfg["interval_minutes"]))

    # --- 入口 ---
    def tick(self) -> dict | None:
        """定期起動の入口。有効で、停止中でなく、時刻が来ていれば実行する。"""
        cfg = self.settings.get("job")
        if not cfg["enabled"] or self.settings.get("safety")["kill_switch"]:
            return None
        st = self.state()
        if st["last_started_at"] and parse_iso(st["last_started_at"]) + timedelta(minutes=cfg["interval_minutes"]) > self.now():
            return None
        return self.run("schedule")

    def run(self, trigger: str = "manual") -> dict:
        t0 = self.now()
        run = {"id": iso(t0) + "-" + new_id()[:4], "trigger": trigger, "started_at": iso(t0), "finished_at": "",
               "status": "", "message": "", "fetched": 0, "new": 0, "reads": 0, "by_category": {}, "drafted": 0,
               "ai_calls": 0, "est_cost_usd": 0.0}
        if self.settings.get("safety")["kill_switch"]:
            return self._finish(run, "skipped", "緊急停止中のため実行しませんでした", record=False)
        holder = new_id()
        if not self.lease.acquire(holder, ttl_seconds=600):
            return self._finish(run, "skipped", "別の実行が進行中です", record=False)
        try:
            self.repo.put("state", "job", {**self.state(), "last_started_at": run["started_at"]})
            try:
                self._execute(run)
                return self._finish(run, "ok", run["message"] or "完了")
            except AuthRequired as e:
                return self._finish(run, "error", f"Xの認可エラー: {e}")
            except XError as e:
                return self._finish(run, "error", str(e))
            except Exception as e:  # noqa: BLE001  予期しない例外も記録して継続可能にする
                self.log.error("job", f"予期しないエラー: {type(e).__name__}: {e}")
                return self._finish(run, "error", f"予期しないエラー: {type(e).__name__}")
        finally:
            self.lease.release(holder)

    def _finish(self, run: dict, status: str, message: str, record: bool = True) -> dict:
        run.update(status=status, message=message, finished_at=iso(self.now()))
        run["est_cost_usd"] = round(run["reads"] * COST_READ, 4)
        if record:
            self.repo.put("runs", run["id"], run)
            self.repo.put("state", "job", {**self.state(), "last_status": status})
            (self.log.info if status == "ok" else self.log.error)("job", f"ジョブ{ {'ok': '成功', 'error': '失敗', 'skipped': 'スキップ'}[status] }: {message}", run_id=run["id"], trigger=run["trigger"])
        return run

    # --- 本体 ---
    def _execute(self, run: dict) -> None:
        cfg = self.settings.get("job")
        xcfg = self.settings.get("x")
        if not xcfg["user_id"]:
            raise XError("Xに接続されていません")
        token = self.tokens.get_access_token()
        cursor = (self.repo.get("state", "cursor") or {}).get("since_id")
        first = cursor is None
        res = self.x.mentions(token, xcfg["user_id"], cursor, max_pages=1 if first and cfg["initial_mode"] == "from_now" else cfg["max_pages"],
                              max_results=5 if first and cfg["initial_mode"] == "from_now" else 100)
        run["fetched"], run["reads"] = len(res["tweets"]), res["reads"]
        if first and cfg["initial_mode"] == "from_now":
            # 現在地点から始める: 過去分は取り込まず、カーソルだけ進める
            if res["newest_id"]:
                self.repo.put("state", "cursor", {"since_id": res["newest_id"]})
            else:
                self.repo.put("state", "cursor", {"since_id": "0"})
            run["message"] = "初回のため現在地点から開始しました（過去分は取り込みません）"
            return
        new_pending = 0
        for tw in reversed(res["tweets"]):  # 古い順
            m = self._ingest(tw, run)
            if m and m["state"] in ("pending", "review"):
                new_pending += 1
        if res["newest_id"]:
            self.repo.put("state", "cursor", {"since_id": res["newest_id"]})
        if res.get("truncated"):
            run["message"] = "新着が多く、一部は次回に持ち越しました"
        if new_pending:
            run["notified"] = self.notifier.notify_pending(len(self.pending_ids()))

    def pending_ids(self) -> list[str]:
        return [m["id"] for m in self.repo.list("mentions") if m["state"] in ("pending", "review")]

    def _ingest(self, tw: dict, run: dict) -> dict | None:
        now = iso(self.now())
        item = {
            "id": tw["id"], "author_id": tw.get("author_id", ""), "text": tw.get("text", ""),
            "created_at": tw.get("created_at", ""), "conversation_id": tw.get("conversation_id", ""),
            "category": OTHER, "state": "new", "draft_text": "", "draft_template_id": "", "final_text": "",
            "sent_tweet_id": "", "error": "", "classified_by": "", "fetched_at": now, "updated_at": now,
        }
        if not self.repo.put_if_absent("mentions", tw["id"], item):
            return None
        run["new"] += 1
        blocked = self.repo.get("blocklist", item["author_id"]) is not None
        if blocked:
            cat, draft, by = self.ai_rule_only(item["text"]), "", "rule"
        else:
            cat, draft, by = self.ai.classify(item["text"])
            if by == "ai":
                run["ai_calls"] += 1
        tpl_id = ""
        if not blocked and cat in (FAN, QUESTION) and not draft:
            t = self.templates.pick(cat)
            if t:
                draft, tpl_id = t["text"], t["id"]
        state = "blocked" if blocked else "spam" if cat == SPAM else "review" if (cat in (CAUTION, OTHER) or not draft) else "pending"
        item.update(category=cat, state=state, draft_text=draft, draft_template_id=tpl_id, classified_by=by, updated_at=now)
        self.repo.put("mentions", item["id"], item)
        if draft:
            run["drafted"] += 1
        run["by_category"][cat] = run["by_category"].get(cat, 0) + 1
        return item

    @staticmethod
    def ai_rule_only(text: str) -> str:
        from .classify import classify
        return classify(text)
