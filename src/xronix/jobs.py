"""定期処理。取得→保存→分類→返信案→通知。EventBridgeは短い間隔で tick() を呼ぶだけで、実行間隔は設定で決まる。"""
from __future__ import annotations

import random
from datetime import timedelta

from .classify import OTHER
from .classify import LABELS as CAT_LABELS
from .events import LABELS, METRIC_EVENTS, classify_event
from .common import Lease, iso, new_id, parse_iso, utcnow
from .xclient import AuthRequired, XError

def cat_label(c: str) -> str:
    return CAT_LABELS.get(c, c)


MAX_CATCHUP_PAGES = 10  # 新着が多いときに、1回で追いつくページ数の上限（1ページ100件）
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

    def ingest_manual(self, tw: dict) -> dict | None:
        """診断画面から、指定した投稿を取り込む。通常の取り込みと同じ処理（ルールも動く）。"""
        t0 = self.now()
        run = {"id": iso(t0) + "-manual", "trigger": "manual", "started_at": iso(t0), "finished_at": "", "status": "", "message": "",
               "fetched": 1, "new": 0, "reads": 0, "thread_reads": 0, "by_category": {}, "drafted": 0, "actions": 0, "ai_calls": 0, "est_cost_usd": 0.0}
        return self._ingest(tw, run, self.settings.get("x")["user_id"], "手動の取り込み（診断）")

    def _dbg(self, run: dict, message: str, **extra) -> None:
        """処理の詳細ログ（設定でオフにできる）。投稿の本文やトークンは記録しない。"""
        if self.settings.get("job")["verbose_log"]:
            self.log.debug("job", message, run_id=run["id"], **extra)

    # --- 本体 ---
    def _execute(self, run: dict) -> None:
        cfg = self.settings.get("job")
        xcfg = self.settings.get("x")
        if not xcfg["user_id"]:
            raise XError("Xに接続されていません")
        token = self.tokens.get_access_token()
        cursor = (self.repo.get("state", "cursor") or {}).get("since_id")
        first = cursor is None
        enabled = {}
        for r in (self.rules.list() if self.rules else []):
            if r["enabled"]:
                for e in r["events"]:
                    enabled[e] = enabled.get(e, 0) + 1
        self._dbg(run, f"開始: 取得位置={cursor or 'なし（初回）'}、初回の動作={cfg['initial_mode']}、取得範囲の拡張={'オン' if cfg['wide_fetch'] else 'オフ'}、"
                       f"有効なルール（反応の種類ごと）={enabled or 'なし'}、アカウント=@{xcfg.get('username') or '未取得'}")
        res = self.x.mentions(token, xcfg["user_id"], cursor, max_pages=1 if first and cfg["initial_mode"] == "from_now" else max(cfg["max_pages"], MAX_CATCHUP_PAGES),
                              max_results=5 if first and cfg["initial_mode"] == "from_now" else 100)
        run["fetched"], run["reads"] = len(res["tweets"]), res["reads"]
        self._dbg(run, f"メンション一覧: {len(res['tweets'])}件を取得（最新ID={res['newest_id'] or 'なし'}、続きあり={'はい' if res.get('truncated') else 'いいえ'}）")
        if first and cfg["initial_mode"] == "from_now":
            # 現在地点から始める: 過去分は取り込まず、カーソルだけ進める
            if res["newest_id"]:
                self.repo.put("state", "cursor", {"since_id": res["newest_id"]})
            else:
                self.repo.put("state", "cursor", {"since_id": "0"})
            run["message"] = "初回のため現在地点から開始しました（過去分は取り込みません）"
            self._dbg(run, f"初回のため、取り込まずに取得位置だけを記録しました（位置={res['newest_id'] or '0'}）。返信の検索と引用の取得は、次回の実行から行います")
            return
        for tw in reversed(res["tweets"]):  # 古い順
            self._ingest(tw, run, xcfg["user_id"], "メンション一覧")
        if res["newest_id"]:
            self.repo.put("state", "cursor", {"since_id": res["newest_id"]})
        if res.get("truncated"):
            run["message"] = f"新着が非常に多く、古い分の一部は取り込めませんでした（1回の上限 {MAX_CATCHUP_PAGES * 100}件）"
            self.log.warn("job", run["message"], run_id=run["id"])
        self._wide_fetch(run, token, xcfg, fresh=first)
        self._check_metrics(run, token, xcfg["user_id"])
        pending = len(self.pending_ids())
        if run["new"] and pending:
            run["notified"] = self.notifier.notify_pending(pending)
        self._dbg(run, f"終了: 取得{run['fetched']}件、新規{run['new']}件、返信案{run['drafted']}件、承認待ち{pending}件、読み取り{run['reads']}件＋他人の投稿{run['thread_reads']}件")

    def _check_metrics(self, run: dict, token: str, my_id: str) -> None:
        """自投稿の数値（いいね数など）が基準以上になったものに、ルールを実行する。確認は設定の間隔ごと。"""
        rules = self.rules.metric_rules() if self.rules else []
        cfg, st, now = self.settings.get("job"), self.state(), self.now()
        need_quotes = bool(cfg["wide_fetch"] and self.rules and self.rules.reaction_rules("quote"))
        need_replies = bool(cfg["wide_fetch"] and self.rules and self.rules.reaction_rules("reply_to_post", "reply_to_reply"))
        if not rules and not need_quotes and not need_replies:
            self._dbg(run, "自投稿の確認: 数値のルールも引用のルールも無いため、行いません")
            return
        last = st.get("last_metrics_at", "")
        if last and parse_iso(last) + timedelta(minutes=cfg["metrics_interval_minutes"]) > now:
            self._dbg(run, f"自投稿の確認: 前回（{last}）から{cfg['metrics_interval_minutes']}分たっていないため、今回は行いません")
            return
        start = (now - timedelta(days=cfg["metrics_window_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = self.x.own_posts(token, my_id, start, cfg["metrics_max_posts"])
        run["reads"] += res["reads"]
        self._dbg(run, f"自投稿の確認: 自分の投稿{len(res['tweets'])}件を読みました（数値のルール{len(rules)}件、引用の確認={'あり' if need_quotes else 'なし'}、返信の補完={'あり' if need_replies else 'なし'}）")
        if cfg["verbose_log"]:
            mine = self.repo.list("mentions")
            for tw in res["tweets"]:
                pm = tw.get("public_metrics", {})
                got_r = sum(1 for m in mine if m.get("conversation_id") == tw["id"] and m.get("kind") in ("reply_to_post", "reply_to_reply"))
                got_q = sum(1 for m in mine if m.get("kind") == "quote" and m.get("parent_id") == tw["id"])
                self._dbg(run, f"自投稿 {tw['id']}（{tw.get('created_at', '')}）: Xの数 返信{pm.get('reply_count', 0)}・引用{pm.get('quote_count', 0)}・いいね{pm.get('like_count', 0)}"
                               f" ／ 取り込み済み 返信{got_r}・引用{got_q}")
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
        if need_quotes:
            self._fetch_quotes(run, token, my_id, res["tweets"])
        if need_replies:
            self._fetch_conversation_replies(run, token, my_id, res["tweets"])
        self._save(last_metrics_at=iso(now))

    def _wide_fetch(self, run: dict, token: str, xcfg: dict, fresh: bool) -> None:
        """メンション一覧に出ない返信（自分の@が無い返信）を、検索で補う。失敗しても、ジョブ全体は失敗にしない。"""
        cfg = self.settings.get("job")
        if not cfg["wide_fetch"] or not xcfg.get("username") or not self.rules:
            self._dbg(run, "返信の検索: 行いません（" + ("取得範囲の拡張がオフ" if not cfg["wide_fetch"] else "アカウント名が未取得") + "）")
            return
        if not self.rules.reaction_rules("reply_to_post", "reply_to_reply"):
            self._dbg(run, "返信の検索: 「自分の投稿への返信」「自分の返信への返信」を対象にする有効なルールが無いため、行いません")
            return
        try:
            cur = (self.repo.get("state", "reply_cursor") or {}).get("since_id")
            first = cur is None
            skip = first and fresh and cfg["initial_mode"] == "from_now"  # 取得を始めたばかりなら、過去分は取り込まない
            res = self.x.replies_to_me(token, xcfg["username"], cur, max_pages=1 if skip else MAX_CATCHUP_PAGES)
            run["thread_reads"] += res["reads"]
            self._dbg(run, f"返信の検索（to:@{xcfg['username']}）: {len(res['tweets'])}件を取得（取得位置={cur or 'なし'}、最新ID={res['newest_id'] or 'なし'}、続きあり={'はい' if res.get('truncated') else 'いいえ'}）")
            if skip:
                self.repo.put("state", "reply_cursor", {"since_id": res["newest_id"] or ""})  # 空なら、次回は直近7日をすべて読む（"0"は検索が拒否する）
                self._dbg(run, "返信の検索: 取得を始めたばかりのため、取り込まずに取得位置だけを記録しました")
                return
            for tw in reversed(res["tweets"]):
                self._ingest(tw, run, xcfg["user_id"], "返信の検索")
            if res["newest_id"]:
                self.repo.put("state", "reply_cursor", {"since_id": res["newest_id"]})
            elif first:
                self.repo.put("state", "reply_cursor", {"since_id": ""})
        except AuthRequired:
            raise
        except XError as e:
            self.log.warn("job", f"返信の検索に失敗しました（メンションの取得は完了）: {e}", run_id=run["id"], status=e.status)

    def _fetch_conversation_replies(self, run: dict, token: str, my_id: str, posts: list[dict]) -> None:
        """Xが数える返信数が増えた自分の投稿だけ、会話の検索（conversation_id:）で、自分宛ての返信を取る。
        メンション一覧と、返信の検索（to:）の取りこぼしを補う。実機の診断で、この検索には出ることを確認した。"""
        username = (self.settings.get("x").get("username") or "").lower()
        seen = self.repo.get("state", "reply_counts") or {}
        counts = dict(seen)
        self._dbg(run, f"返信の補完: 自分の投稿{len(posts)}件のうち、Xの返信数が増えたものだけ、会話の検索で取りに行きます")
        try:
            for p in posts:
                n = int(p.get("public_metrics", {}).get("reply_count", 0))
                if n <= int(seen.get(p["id"], 0)):
                    continue
                res = self.x.search_recent(token, f"conversation_id:{p['id']}", 100)
                run["thread_reads"] += res["reads"]
                mine = [t for t in res["tweets"] if t.get("author_id") != my_id
                        and (t.get("in_reply_to_user_id") == my_id or (username and "@" + username in (t.get("text") or "").lower()))]
                self._dbg(run, f"返信の補完: 投稿{p['id']}（Xの返信数 {seen.get(p['id'], 0)} → {n}）の会話から{len(res['tweets'])}件、うち自分宛て{len(mine)}件")
                for tw in sorted(mine, key=lambda t: int(t["id"])):
                    self._ingest(tw, run, my_id, "会話の検索")
                counts[p["id"]] = n
        except AuthRequired:
            raise
        except XError as e:
            self.log.warn("job", f"返信の補完（会話の検索）に失敗しました: {e}", run_id=run["id"], status=e.status)
        finally:
            self.repo.put("state", "reply_counts", counts)

    def _fetch_quotes(self, run: dict, token: str, my_id: str, posts: list[dict]) -> None:
        """引用の数が増えた自分の投稿だけ、引用を取りに行く。"""
        seen = self.repo.get("state", "quote_counts") or {}
        counts = dict(seen)
        self._dbg(run, f"引用の確認: 自分の投稿{len(posts)}件のうち、引用数が増えたものだけを取りに行きます")
        try:
            for p in posts:
                n = int(p.get("public_metrics", {}).get("quote_count", 0))
                if n <= int(seen.get(p["id"], 0)):
                    continue
                res = self.x.quotes_of(token, p["id"])
                run["thread_reads"] += res["reads"]
                self._dbg(run, f"引用の取得: 投稿{p['id']}（引用数 {seen.get(p['id'], 0)} → {n}）から{len(res['tweets'])}件を取得")
                for tw in reversed(res["tweets"]):
                    self._ingest(tw, run, my_id, "引用の取得")
                counts[p["id"]] = n
        except AuthRequired:
            raise
        except XError as e:
            self.log.warn("job", f"引用の取得に失敗しました: {e}", run_id=run["id"], status=e.status)
        finally:
            self.repo.put("state", "quote_counts", counts)

    def pending_ids(self) -> list[str]:
        return [m["id"] for m in self.repo.list("mentions") if m["state"] in ("pending", "review")]

    def _ingest(self, tw: dict, run: dict, my_id: str, source: str = "") -> dict | None:
        if tw.get("author_id") == my_id:
            self._dbg(run, f"スキップ: {tw.get('id')}（自分の投稿。取得元={source}）")
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
            "fetched_at": now, "updated_at": now, "source": source,
        }
        if not self.repo.put_if_absent("mentions", tw["id"], item):
            self._dbg(run, f"スキップ: {tw['id']}（取り込み済み。取得元={source}）")
            return None
        run["new"] += 1
        if self.repo.get("blocklist", item["author_id"]) is not None:
            item.update(category=self.ai_rule_only(item["text"]), state="blocked", classified_by="rule")
            self._dbg(run, f"取り込み: {tw['id']}（取得元={source}）→ ブロック対象の投稿者のため、処理しません")
            self.repo.put("mentions", item["id"], item)
            return item
        cat, by = self.ai.classify(item["text"])
        if by == "ai":
            run["ai_calls"] += 1
        item.update(category=cat, classified_by=by)
        run["by_category"][cat] = run["by_category"].get(cat, 0) + 1
        rule = self.rules.match(kind, cat) if self.rules else None
        self._dbg(run, f"取り込み: {tw['id']}（取得元={source}、種類={LABELS.get(kind, kind)}、分類={cat_label(cat)}（{by}）、投稿者ID={item['author_id'] or '不明'}、"
                       f"{len(item['text'])}文字）→ ルール: {rule['name'] if rule else '該当なし'}")
        if rule:
            return self.engine.run(item, rule, run)
        item.update(state="review", actions=[{"type": "none", "label": "該当するルールなし", "ok": True, "detail": "該当するルールが無いため、人が確認します"}])
        self.repo.put("mentions", item["id"], item)
        return item

    @staticmethod
    def ai_rule_only(text: str) -> str:
        from .classify import classify
        return classify(text)
