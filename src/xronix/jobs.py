"""定期処理。取得→保存→分類→返信案→通知。EventBridgeは短い間隔で tick() を呼ぶだけで、実行間隔は設定で決まる。"""
from __future__ import annotations

import random
from datetime import timedelta

from .classify import OTHER
from .events import METRIC_EVENTS, classify_event
from .common import Lease, iso, new_id, parse_iso, utcnow
from .xclient import AuthRequired, XError

COST_READ = 0.001
COST_THREAD_READ = 0.005  # スレッドの文脈で読む他人の投稿（上限の見積もり）
MIN_LEAD_SECONDS = 15        # 次回の実行は、登録時点から最低この秒数あとにする
EARLY_TOLERANCE_SECONDS = 2  # 起動がわずかに早い場合の許容
WATCHDOG_GRACE_SECONDS = 120 # 見守り（15分ごと）は、予定からこれ以上遅れているときだけ実行する


class JobRunner:
    def __init__(self, repo, settings, tokens, x, ai, templates, notifier, logger, now=utcnow, scheduler=None, rng=None,
                 rules=None, engine=None):
        self.repo, self.settings, self.tokens, self.x = repo, settings, tokens, x
        self.ai, self.templates, self.notifier, self.log, self.now = ai, templates, notifier, logger, now
        self.lease = Lease(repo, "job", now)
        from .scheduler import NullScheduler
        self.scheduler = scheduler or NullScheduler()
        self.rng = rng or random.SystemRandom()
        self.rules, self.engine = rules, engine

    # --- 状態 ---
    def state(self) -> dict:
        return {"last_started_at": "", "last_status": "", "next_run_at": "", "schedule_name": "", **(self.repo.get("state", "job") or {})}

    def next_due(self) -> str:
        cfg = self.settings.get("job")
        return self.state()["next_run_at"] if cfg["enabled"] and not self.settings.get("safety")["kill_switch"] else ""

    def _save(self, **values) -> None:
        self.repo.put("state", "job", {**self.state(), **values})

    # --- 次回実行の計画 ---
    def plan(self, reason: str = "") -> str:
        """次回の実行時刻を決めて登録する。ずらし幅があれば、±の範囲で秒単位にランダムにずらす。"""
        cfg, st = self.settings.get("job"), self.state()
        old = st["schedule_name"]
        new_name, next_at = "", ""
        if cfg["enabled"] and not self.settings.get("safety")["kill_switch"]:
            now = self.now()
            if st["last_started_at"]:
                delay = cfg["interval_minutes"] * 60
                j = cfg["jitter_minutes"] * 60
                if j:
                    delay += self.rng.randint(-j, j)  # 秒まで含めてずらす
                nxt = max(parse_iso(st["last_started_at"]) + timedelta(seconds=delay), now + timedelta(seconds=MIN_LEAD_SECONDS))
            else:
                nxt = now + timedelta(seconds=MIN_LEAD_SECONDS)  # 初めて有効にしたときは、すぐ実行する
            next_at = iso(nxt)
            try:
                new_name = self.scheduler.arm(nxt)
            except Exception as e:  # noqa: BLE001  登録に失敗しても、見守りが予定の遅れを見て実行する
                self.log.error("job", f"次回実行の登録に失敗しました（見守りが代わりに実行します）: {type(e).__name__}")
        self._save(next_run_at=next_at, schedule_name=new_name)
        if old and old != new_name:
            try:
                self.scheduler.cancel(old)
            except Exception as e:  # noqa: BLE001
                self.log.warn("job", f"古い実行予定の削除に失敗しました: {type(e).__name__}")
        return next_at

    # --- 入口 ---
    def tick(self, source: str = "schedule") -> dict | None:
        """定期起動の入口。source=schedule は予定時刻に、watchdog は15分ごとの見守りから呼ばれる。"""
        cfg = self.settings.get("job")
        if not cfg["enabled"] or self.settings.get("safety")["kill_switch"]:
            st = self.state()
            if st["next_run_at"] or st["schedule_name"]:
                self.plan("disabled")  # 予定を取り消す
            return None
        nxt = self.state()["next_run_at"]
        if not nxt:
            self.plan("init")
            return None
        due = parse_iso(nxt)
        ready = self.now() >= due + timedelta(seconds=WATCHDOG_GRACE_SECONDS) if source == "watchdog" \
            else self.now() >= due - timedelta(seconds=EARLY_TOLERANCE_SECONDS)
        return self.run(source) if ready else None

    def run(self, trigger: str = "manual") -> dict:
        t0 = self.now()
        run = {"id": iso(t0) + "-" + new_id()[:4], "trigger": trigger, "started_at": iso(t0), "finished_at": "",
               "status": "", "message": "", "fetched": 0, "new": 0, "reads": 0, "thread_reads": 0, "by_category": {}, "drafted": 0,
               "actions": 0, "ai_calls": 0, "est_cost_usd": 0.0}
        if self.settings.get("safety")["kill_switch"]:
            return self._finish(run, "skipped", "緊急停止中のため実行しませんでした", record=False)
        holder = new_id()
        if not self.lease.acquire(holder, ttl_seconds=600):
            return self._finish(run, "skipped", "別の実行が進行中です", record=False)
        try:
            self._save(last_started_at=run["started_at"])
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
        run["est_cost_usd"] = round(run["reads"] * COST_READ + run["thread_reads"] * COST_THREAD_READ, 4)
        if record:
            self.repo.put("runs", run["id"], run)
            self._save(last_status=status)
            (self.log.info if status == "ok" else self.log.error)("job", f"ジョブ{ {'ok': '成功', 'error': '失敗', 'skipped': 'スキップ'}[status] }: {message}", run_id=run["id"], trigger=run["trigger"])
            self.plan("after_run")
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
        for tw in reversed(res["tweets"]):  # 古い順
            self._ingest(tw, run, xcfg["user_id"])
        if res["newest_id"]:
            self.repo.put("state", "cursor", {"since_id": res["newest_id"]})
        if res.get("truncated"):
            run["message"] = "新着が多く、一部は次回に持ち越しました"
        self._check_metrics(run, token, xcfg["user_id"])
        pending = len(self.pending_ids())
        if run["new"] and pending:
            run["notified"] = self.notifier.notify_pending(pending)

    def _check_metrics(self, run: dict, token: str, my_id: str) -> None:
        """自投稿の数値（いいね数など）が基準以上になったものに、ルールを実行する。確認は設定の間隔ごと。"""
        rules = self.rules.metric_rules() if self.rules else []
        if not rules:
            return
        cfg, st, now = self.settings.get("job"), self.state(), self.now()
        last = st.get("last_metrics_at", "")
        if last and parse_iso(last) + timedelta(minutes=cfg["metrics_interval_minutes"]) > now:
            return
        start = (now - timedelta(days=cfg["metrics_window_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = self.x.own_posts(token, my_id, start, cfg["metrics_max_posts"])
        run["reads"] += res["reads"]
        for tw in res["tweets"]:
            metrics = tw.get("public_metrics", {})
            for rule in rules:
                key, label = METRIC_EVENTS[rule["events"][0]]
                value = int(metrics.get(key, 0))
                if value < rule["threshold"]:
                    continue
                stamp = iso(now)
                item = {"id": f"metric_{tw['id']}_{rule['id']}", "author_id": my_id, "text": tw.get("text", ""), "created_at": tw.get("created_at", ""),
                        "conversation_id": tw["id"], "kind": rule["events"][0], "target_tweet_id": tw["id"], "parent_id": "",
                        "metric": {"key": key, "label": label, "value": value, "threshold": rule["threshold"]},
                        "category": OTHER, "state": "new", "draft_text": "", "draft_template_id": "", "final_text": "", "sent_tweet_id": "",
                        "error": "", "classified_by": "", "actions": [], "rule_id": "", "rule_name": "", "fetched_at": stamp, "updated_at": stamp}
                if self.repo.put_if_absent("mentions", item["id"], item):  # 各投稿で、基準を初めて超えたときに1回だけ
                    run["new"] += 1
                    self.engine.run(item, rule, run)
        self._save(last_metrics_at=iso(now))

    def pending_ids(self) -> list[str]:
        return [m["id"] for m in self.repo.list("mentions") if m["state"] in ("pending", "review")]

    def _ingest(self, tw: dict, run: dict, my_id: str) -> dict | None:
        if tw.get("author_id") == my_id:
            return None  # 自分の投稿は扱わない
        now = iso(self.now())
        kind = classify_event(tw, my_id)
        refs = {r.get("type"): r.get("id") for r in tw.get("referenced_tweets", [])}
        item = {
            "id": tw["id"], "author_id": tw.get("author_id", ""), "text": tw.get("text", ""),
            "created_at": tw.get("created_at", ""), "conversation_id": tw.get("conversation_id", ""),
            "kind": kind, "target_tweet_id": tw["id"], "parent_id": refs.get("replied_to") or refs.get("quoted") or "",
            "category": OTHER, "state": "new", "draft_text": "", "draft_template_id": "", "final_text": "",
            "sent_tweet_id": "", "error": "", "classified_by": "", "actions": [], "rule_id": "", "rule_name": "",
            "fetched_at": now, "updated_at": now,
        }
        if not self.repo.put_if_absent("mentions", tw["id"], item):
            return None
        run["new"] += 1
        if self.repo.get("blocklist", item["author_id"]) is not None:
            item.update(category=self.ai_rule_only(item["text"]), state="blocked", classified_by="rule")
            self.repo.put("mentions", item["id"], item)
            return item
        cat, by = self.ai.classify(item["text"])
        if by == "ai":
            run["ai_calls"] += 1
        item.update(category=cat, classified_by=by)
        run["by_category"][cat] = run["by_category"].get(cat, 0) + 1
        rule = self.rules.match(kind, cat) if self.rules else None
        if rule:
            return self.engine.run(item, rule, run)
        item.update(state="review", actions=[{"type": "none", "label": "該当するルールなし", "ok": True, "detail": "該当するルールが無いため、人が確認します"}])
        self.repo.put("mentions", item["id"], item)
        return item

    @staticmethod
    def ai_rule_only(text: str) -> str:
        from .classify import classify
        return classify(text)
