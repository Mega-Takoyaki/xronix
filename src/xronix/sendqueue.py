"""送信キュー。自動送信（と、設定すれば承認からの送信）を、すぐには送らず、キューに入れて、間隔をあけて1件ずつ送る。

短時間に続けて送ると、スパムとみなされたり、表示が制限されたり（シャドウバン）する恐れがある。
間隔は、設定の秒数に、ランダムな幅（秒）を加えて決める。次の送信の時刻は、EventBridge Schedulerに、秒単位で登録する
（定期処理と同じ仕組み）。15分ごとの見守りも、予定が2分以上遅れていれば、代わりに送る。
"""
from __future__ import annotations

import random
from datetime import timedelta

from . import limits
from .common import Lease, iso, new_id, parse_iso, utcnow
from .sender import SENDABLE, SendRefused
from .xclient import AuthRequired

MIN_LEAD_SECONDS = 15
MIN_DELAY_SECONDS = 5
EARLY_TOLERANCE_SECONDS = 2
WATCHDOG_GRACE_SECONDS = 120
QUEUED = "send_queued"


class SendQueue:
    def __init__(self, repo, settings, sender, scheduler, logger, now=utcnow, rng=None):
        self.repo, self.settings, self.sender, self.scheduler, self.log, self.now = repo, settings, sender, scheduler, logger, now
        self.rng = rng or random.SystemRandom()
        self.lease = Lease(repo, "sendq", now)

    # --- 状態 ---
    def state(self) -> dict:
        return {"next_at": "", "schedule_name": "", "last_sent_at": "", **(self.repo.get("state", "sendq") or {})}

    def _save(self, **values) -> None:
        self.repo.put("state", "sendq", {**self.state(), **values})

    def queued(self) -> list[dict]:
        rows = [m for m in self.repo.list("mentions") if m.get("state") == QUEUED]
        rows.sort(key=lambda m: (m.get("queued_at", ""), m["id"]))
        return rows

    def next_at(self) -> str:
        return self.state()["next_at"] if self.queued() else ""

    def delay_seconds(self) -> int:
        s = self.settings.get("send")
        d = s["queue_interval_seconds"]
        j = s["queue_jitter_seconds"]
        if j:
            d += self.rng.randint(-j, j)  # 秒まで含めてずらす
        return max(MIN_DELAY_SECONDS, d)

    # --- キューへの出し入れ ---
    def enqueue(self, mention_id: str, text: str, auto: bool = False, autosent_key: str = "") -> dict:
        """送信待ちにする。送信前の検査（緊急停止・ブロック・文面）に通らなければ、SendRefused（Xには何も送らない）。"""
        if self.settings.get("safety")["kill_switch"]:
            raise SendRefused("緊急停止中のため送信できません")
        m = self.repo.get("mentions", mention_id)
        if not m:
            raise SendRefused("対象が見つかりません")
        if m["state"] not in SENDABLE and not (auto and m["state"] in ("pending", "review", "done")):
            raise SendRefused(f"この状態では送信できません（{m['state']}）")
        if self.repo.get("blocklist", m["author_id"]):
            raise SendRefused("ブロック対象のユーザーです")
        text = self.sender.check_text(text)
        cur = self.repo.update_if("mentions", mention_id, {"state": QUEUED, "queued_text": text, "queued_at": iso(self.now()), "queue_auto": auto,
                                                           "autosent_key": autosent_key, "error": "", "updated_at": iso(self.now())},
                                  {"state": m["state"]})
        if cur is None:
            raise SendRefused("他の操作で更新されました。画面を更新してください")
        self.log.info("send", "送信キューに入れました", mention_id=mention_id)
        self.plan()
        return self.repo.get("mentions", mention_id)

    def cancel(self, mention_id: str) -> dict:
        """キューから外して、承認待ち（要確認）に戻す。"""
        m = self.repo.get("mentions", mention_id)
        if not m or m["state"] != QUEUED:
            raise SendRefused("送信待ちではありません（すでに送信された可能性があります）")
        cur = self.repo.update_if("mentions", mention_id, {"state": "review", "draft_text": m.get("queued_text", ""), "updated_at": iso(self.now())}, {"state": QUEUED})
        if cur is None:
            raise SendRefused("すでに送信が始まっています")
        self._refund(m)
        self.log.info("send", "送信キューから外しました", mention_id=mention_id)
        self.plan()
        return self.repo.get("mentions", mention_id)

    def _refund(self, m: dict) -> None:
        if m.get("queue_auto") and m.get("autosent_key"):
            self.repo.incr("state", m["autosent_key"], "count", -1)

    # --- 次の送信の計画 ---
    def plan(self) -> None:
        st, queued, now = self.state(), self.queued(), self.now()
        old = st["schedule_name"]
        if not queued or self.settings.get("safety")["kill_switch"]:
            if old:
                self._cancel(old)
                self._save(schedule_name="", armed_for="")
            return
        if old and st.get("armed_for") and parse_iso(st["armed_for"]) >= now - timedelta(seconds=5):
            return  # 登録済み
        nxt = parse_iso(st["next_at"]) if st["next_at"] else None
        due = nxt if nxt and nxt > now + timedelta(seconds=MIN_LEAD_SECONDS) else now + timedelta(seconds=MIN_LEAD_SECONDS)
        name = ""
        try:
            name = self.scheduler.arm(due, {"source": "sendqueue"})
        except Exception as e:  # noqa: BLE001  登録に失敗しても、見守りが予定の遅れを見て送る
            self.log.error("send", f"送信の予定の登録に失敗しました（見守りが代わりに送ります）: {type(e).__name__}")
        self._save(next_at=iso(due), schedule_name=name, armed_for=iso(due))
        if old and old != name:
            self._cancel(old)

    def _cancel(self, name: str) -> None:
        try:
            self.scheduler.cancel(name)
        except Exception as e:  # noqa: BLE001
            self.log.warn("send", f"古い送信の予定の削除に失敗しました: {type(e).__name__}")

    # --- 入口 ---
    def tick(self, source: str = "schedule") -> dict | None:
        """予定時刻（schedule）か、15分ごとの見守り（watchdog）から呼ばれる。時刻が来ていれば、先頭の1件だけを送る。"""
        queued = self.queued()
        if not queued or self.settings.get("safety")["kill_switch"]:
            if self.state()["schedule_name"]:
                self.plan()
            return None
        st, now = self.state(), self.now()
        due = parse_iso(st["next_at"]) if st["next_at"] else now
        ready = now >= due + timedelta(seconds=WATCHDOG_GRACE_SECONDS) if source == "watchdog" else now >= due - timedelta(seconds=EARLY_TOLERANCE_SECONDS)
        if not ready:
            self.plan()
            return None
        holder = new_id()
        if not self.lease.acquire(holder, ttl_seconds=120):
            return None
        try:
            self._save(schedule_name="", armed_for="")  # 発火したスケジュールは、実行後に自動で消える
            item = self._send_one(self.queued()[0])
        finally:
            self.lease.release(holder)
        self.plan()
        return item

    def _send_one(self, m: dict) -> dict:
        mid, now = m["id"], self.now()
        stop = limits.reached(self.repo, self.settings, now, {**m, "id": mid}, include_queued=False) if m.get("queue_auto") else ""
        try:
            if stop:
                raise SendRefused(stop)
            sent = self.sender.send(mid, m.get("queued_text", ""), allow_queued=True)
            self._save(next_at=iso(now + timedelta(seconds=self.delay_seconds())), last_sent_at=iso(now))
            return sent
        except SendRefused as e:  # 送れなかった（Xには何も送っていない）。承認待ちに戻す。待たずに、次の1件へ
            self._back_to_review(m, str(e))
            self._save(next_at=iso(now))
        except AuthRequired:
            self._after_failure(m, now)
            raise
        except Exception:  # noqa: BLE001  senderが、失敗か結果不明かを記録済み
            self._after_failure(m, now)
        return self.repo.get("mentions", mid)

    def _after_failure(self, m: dict, now) -> None:
        cur = self.repo.get("mentions", m["id"]) or {}
        if cur.get("state") == "failed":  # 未投稿が確実なら、自動送信の枠を戻す
            self._refund(m)
        self._save(next_at=iso(now + timedelta(seconds=self.delay_seconds())))

    def _back_to_review(self, m: dict, why: str) -> None:
        self.repo.update_if("mentions", m["id"], {"state": "review", "draft_text": m.get("queued_text", ""), "error": f"送信キューから送れませんでした: {why}",
                                                  "updated_at": iso(self.now())}, {"state": QUEUED})
        self._refund(m)
        self.log.warn("send", f"送信キューから送れなかったため、承認待ちに戻しました: {why}", mention_id=m["id"])
