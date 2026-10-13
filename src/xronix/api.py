"""APIルーター。ローカルサーバとLambdaの両方から同じ関数を呼ぶ。認証は呼び出し側（Cognito／ローカル限定）の責務。"""
from __future__ import annotations

import re
from datetime import timedelta

from . import stats as stats_mod
from .ai import AIError
from .app import App
from .classify import CATEGORIES, LABELS
from .events import ALL_EVENTS, LABELS as EVENT_LABELS, is_metric
from .rules import ACTION_LABELS, METRIC_ACTIONS, REPLY_ACTIONS, SOURCES
from .common import iso, jst_date, new_id, parse_iso
from .notify import NotifyError
from .sender import SendRefused
from .settings import ValidationError
from .xclient import AuthRequired, XError, authorize_url, make_pkce
from .secrets import NAMES

Result = tuple[int, dict, dict]  # status, body, headers
EDITABLE_SECRETS = ("x_client_secret", "openai_api_key", "telegram_bot_token")


def _ok(body=None, status=200, headers=None) -> Result:
    return status, body if body is not None else {"ok": True}, headers or {}


def _err(message: str, status: int = 400) -> Result:
    return status, {"error": message}, {}


class Api:
    def __init__(self, app: App):
        self.app = app
        self.routes: list[tuple[str, re.Pattern, callable]] = []
        for method, pattern, fn in [
            ("GET", r"/api/status", self.status),
            ("GET", r"/api/settings", self.get_settings),
            ("PUT", r"/api/settings/(?P<section>\w+)", self.put_settings),
            ("PUT", r"/api/secrets/(?P<name>\w+)", self.put_secret),
            ("DELETE", r"/api/secrets/(?P<name>\w+)", self.delete_secret),
            ("POST", r"/api/x/connect", self.x_connect),
            ("GET", r"/api/x/callback", self.x_callback),
            ("POST", r"/api/x/disconnect", self.x_disconnect),
            ("POST", r"/api/x/test", self.x_test),
            ("POST", r"/api/ai/test", self.ai_test),
            ("GET", r"/api/ai/models", self.ai_models),
            ("GET", r"/api/rules", self.list_rules),
            ("POST", r"/api/rules", self.create_rule),
            ("POST", r"/api/rules/simulate", self.simulate_rule),
            ("PUT", r"/api/rules/(?P<id>\w+)", self.update_rule),
            ("DELETE", r"/api/rules/(?P<id>\w+)", self.delete_rule),
            ("POST", r"/api/rules/(?P<id>\w+)/move", self.move_rule),
            ("POST", r"/api/dev/mock-posts", self.mock_posts),
            ("POST", r"/api/notify/test", self.notify_test),
            ("POST", r"/api/notify/detect-chat", self.notify_detect_chat),
            ("POST", r"/api/kill", self.kill),
            ("GET", r"/api/mentions", self.list_mentions),
            ("POST", r"/api/mentions/(?P<id>\w+)/reply", self.reply),
            ("POST", r"/api/mentions/(?P<id>\w+)/reject", self.reject),
            ("POST", r"/api/mentions/(?P<id>\w+)/redraft", self.redraft),
            ("POST", r"/api/mentions/(?P<id>\w+)/resolve", self.resolve),
            ("POST", r"/api/mentions/(?P<id>\w+)/block", self.block_author),
            ("GET", r"/api/templates", self.list_templates),
            ("POST", r"/api/templates", self.create_template),
            ("PUT", r"/api/templates/(?P<id>\w+)", self.update_template),
            ("DELETE", r"/api/templates/(?P<id>\w+)", self.delete_template),
            ("GET", r"/api/blocklist", self.list_block),
            ("POST", r"/api/blocklist", self.add_block),
            ("DELETE", r"/api/blocklist/(?P<id>\w+)", self.del_block),
            ("GET", r"/api/jobs", self.jobs),
            ("POST", r"/api/jobs/run", self.run_job),
            ("GET", r"/api/stats", self.stats),
            ("GET", r"/api/logs", self.logs),
            ("POST", r"/api/dev/mock-mentions", self.mock_mentions),
        ]:
            self.routes.append((method, re.compile(f"^{pattern}$"), fn))

    def handle(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> Result:
        path_matched = False
        for m, rx, fn in self.routes:
            match = rx.match(path)
            if not match:
                continue
            path_matched = True
            if m != method:
                continue
            try:
                return fn(query or {}, body or {}, **match.groupdict())
            except ValidationError as e:
                return _err(str(e))
            except SendRefused as e:
                return _err(str(e), 409)
            except AuthRequired as e:
                return _err(str(e), 400)
            except (XError, AIError, NotifyError) as e:
                return _err(str(e), 502)
        return _err("見つかりません", 405 if path_matched else 404)

    # --- 状態 ---
    def status(self, q, b) -> Result:
        a = self.app
        safety, job = a.settings.get("safety"), a.settings.get("job")
        mentions = a.repo.list("mentions")
        return _ok({
            "mode": a.x.mode,
            "kill_switch": safety["kill_switch"],
            "x_connected": a.tokens.connected() and bool(a.settings.get("x")["user_id"]),
            "username": a.settings.get("x")["username"],
            "auth_error": (a.repo.get("state", "token") or {}).get("auth_error", ""),
            "pending": sum(1 for m in mentions if m["state"] in ("pending", "review")),
            "unknown": sum(1 for m in mentions if m["state"] == "unknown"),
            "sent_today": a.sender.sent_today(), "daily_limit": safety["daily_limit"],
            "job_enabled": job["enabled"], "next_due": a.jobs.next_due(), "last_status": a.jobs.state()["last_status"],
        })

    # --- 設定 ---
    def get_settings(self, q, b) -> Result:
        a = self.app
        s = a.settings.all()
        s["secrets"] = {n: bool(a.secrets.get(n)) for n in NAMES}
        s["mode"] = a.x.mode
        return _ok(s)

    def put_settings(self, q, b, section) -> Result:
        cur = self.app.settings.update(section, b)
        if section in ("job", "safety"):
            self.app.jobs.plan("settings")  # 間隔・ずらし幅・有効/無効の変更を、次回の実行予定に反映する
        return _ok(cur)

    def put_secret(self, q, b, name) -> Result:
        if name not in EDITABLE_SECRETS:
            return _err("この項目は画面から設定できません", 404)
        value = str(b.get("value", "")).strip()
        if not value:
            return _err("値を入力してください")
        self.app.secrets.put(name, value)
        self.app.logger.info("settings", f"{name} を更新しました")
        return _ok()

    def delete_secret(self, q, b, name) -> Result:
        if name not in EDITABLE_SECRETS:
            return _err("この項目は画面から削除できません", 404)
        self.app.secrets.delete(name)
        self.app.logger.info("settings", f"{name} を削除しました")
        return _ok()

    # --- X接続 ---
    def x_connect(self, q, b) -> Result:
        a = self.app
        if a.mock:
            a.tokens.save_tokens(a.x.exchange_code())
            me = a.x.me("mock")
            a.settings.set_internal("x", user_id=me["id"], username=me["username"], connected_at=iso(a.now()))
            a.logger.info("x", "モックで接続しました")
            return _ok({"mock": True})
        cfg = a.settings.get("x")
        if not cfg["client_id"] or not a.secrets.get("x_client_secret"):
            return _err("先にClient IDとClient Secretを保存してください")
        if not cfg["redirect_uri"]:
            return _err("先にコールバックURLを保存してください")
        verifier, challenge = make_pkce()
        state = new_id() + new_id()
        a.repo.put("oauth", state, {"id": state, "verifier": verifier, "created_at": iso(a.now())})
        return _ok({"url": authorize_url(cfg["client_id"], cfg["redirect_uri"], state, challenge)})

    def x_callback(self, q, b) -> Result:
        a = self.app
        back = "/?tab=settings&x="
        if q.get("error"):
            return 302, {}, {"Location": back + "denied"}
        st = a.repo.get("oauth", q.get("state", ""))
        if not st or parse_iso(st["created_at"]) < a.now() - timedelta(minutes=15):
            return 302, {}, {"Location": back + "invalid"}
        a.repo.delete("oauth", st["id"])
        cfg = a.settings.get("x")
        try:
            data = a.x.exchange_code(cfg["client_id"], a.secrets.get("x_client_secret") or "", q.get("code", ""), cfg["redirect_uri"], st["verifier"])
            a.tokens.save_tokens(data)
            me = a.x.me(data["access_token"])
        except (XError, ValueError, KeyError) as e:
            a.logger.error("x", f"接続に失敗しました: {e}")
            return 302, {}, {"Location": back + "failed"}
        a.settings.set_internal("x", user_id=me["id"], username=me["username"], connected_at=iso(a.now()))
        a.logger.info("x", f"@{me['username']} と接続しました")
        return 302, {}, {"Location": back + "ok"}

    def x_disconnect(self, q, b) -> Result:
        self.app.tokens.clear()
        self.app.settings.set_internal("x", user_id="", username="", connected_at="")
        self.app.logger.info("x", "Xとの接続を解除しました")
        return _ok()

    def x_test(self, q, b) -> Result:
        a = self.app
        me = a.x.me(a.tokens.get_access_token())
        return _ok({"username": me["username"], "id": me["id"]})

    def ai_test(self, q, b) -> Result:
        return _ok(self.app.ai.test())

    def ai_models(self, q, b) -> Result:
        """Bedrockで、いま使えるモデルの一覧。選択中のモデルと、自動で選ばれるモデルも返す。"""
        out = self.app.ai.list_models(refresh=q.get("refresh") == "1")
        out["selected"] = self.app.settings.get("ai")["bedrock_model"]
        return _ok(out)

    # --- ルール ---
    def list_rules(self, q, b) -> Result:
        meta = {
            "events": [{"key": e, "label": EVENT_LABELS[e], "metric": is_metric(e)} for e in ALL_EVENTS],
            "actions": [{"key": k, "label": v, "metric_ok": k in METRIC_ACTIONS, "reply": k in REPLY_ACTIONS} for k, v in ACTION_LABELS.items()],
            "sources": {k: list(v) for k, v in SOURCES.items()},
            "categories": [{"key": k, "label": LABELS[k]} for k in CATEGORIES],
        }
        return _ok({"items": self.app.rules.list(), "meta": meta})

    def create_rule(self, q, b) -> Result:
        return _ok(self.app.rules.create(b), 201)

    def update_rule(self, q, b, id) -> Result:
        r = self.app.rules.update(id, b)
        return _ok(r) if r else _err("見つかりません", 404)

    def delete_rule(self, q, b, id) -> Result:
        self.app.rules.delete(id)
        return _ok()

    def move_rule(self, q, b, id) -> Result:
        self.app.rules.move(id, str(b.get("direction", "")))
        return _ok()

    def simulate_rule(self, q, b) -> Result:
        """設定の確認用。反応の種類と分類を指定すると、どのルールが働くかを返す（何も実行しない）。"""
        kind, cat = b.get("event"), b.get("category", "other")
        if kind not in ALL_EVENTS or is_metric(kind):
            return _err("返信・引用・メンションの種類を指定してください")
        if cat not in CATEGORIES:
            return _err("分類の指定が不正です")
        r = self.app.rules.match(kind, cat)
        return _ok({"rule": None if not r else {"id": r["id"], "name": r["name"], "actions": [ACTION_LABELS[a["type"]] for a in r["actions"]]}})

    def notify_test(self, q, b) -> Result:
        ok, reason = self.app.notifier.send("XroniX: 通知のテストです。")
        return _ok({"sent": ok, "reason": reason})

    def notify_detect_chat(self, q, b) -> Result:
        """ボットに届いたメッセージから、チャットIDを探す。先にボットへメッセージを送っておく。"""
        chats = self.app.notifier.detect_chats()
        return _ok({"chats": chats})

    def kill(self, q, b) -> Result:
        on = bool(b.get("on"))
        self.app.settings.update("safety", {"kill_switch": on})
        self.app.jobs.plan("kill")  # 停止中は予定を取り消し、解除すると再登録する
        self.app.logger.warn("safety", "緊急停止を" + ("有効にしました" if on else "解除しました"))
        return _ok({"kill_switch": on})

    # --- メンション ---
    def list_mentions(self, q, b) -> Result:
        view = q.get("view", "queue")
        rows = self.app.repo.list("mentions", reverse=True)
        if view == "queue":
            rows = [m for m in rows if m["state"] in ("pending", "review", "failed", "unknown")]
        else:
            rows = [m for m in rows if m["state"] not in ("pending", "review")]
        blocked = {x["id"] for x in self.app.repo.list("blocklist")}
        rows.sort(key=lambda m: m["fetched_at"], reverse=True)
        for m in rows[:200]:
            m["label"] = LABELS.get(m["category"], m["category"])
            m["kind_label"] = EVENT_LABELS.get(m.get("kind", ""), "")
            m["author_blocked"] = m["author_id"] in blocked
        return _ok({"items": rows[:200]})

    def reply(self, q, b, id) -> Result:
        m = self.app.sender.send(id, str(b.get("text", "")))
        return _ok(m)

    def _get_mention(self, id):
        return self.app.repo.get("mentions", id)

    def reject(self, q, b, id) -> Result:
        m = self._get_mention(id)
        if not m or m["state"] not in ("pending", "review", "failed"):
            return _err("この状態では却下できません", 409)
        self.app.repo.update_if("mentions", id, {"state": "rejected", "updated_at": iso(self.app.now())}, {"state": m["state"]})
        return _ok()

    def resolve(self, q, b, id) -> Result:
        """送信結果が不明(unknown)のものを、Xで確認した運用者が記録して解決する。"""
        m = self._get_mention(id)
        if not m or m["state"] != "unknown":
            return _err("結果不明の項目ではありません", 409)
        sent = b.get("outcome") == "sent"
        if b.get("outcome") not in ("sent", "not_sent"):
            return _err("outcomeは sent か not_sent を指定してください")
        if not sent:
            self.app.repo.incr("state", "sent#" + jst_date(parse_iso(m["updated_at"])), "count", -1)
        self.app.repo.update_if("mentions", id, {"state": "sent" if sent else "failed", "updated_at": iso(self.app.now()),
                                                 "sent_at": iso(self.app.now()) if sent else "", "error": "" if sent else "送信されていないことを確認済み"},
                                {"state": "unknown"})
        self.app.logger.info("send", "結果不明の送信を記録しました: " + ("送信済み" if sent else "未送信"), mention_id=id)
        return _ok()

    def redraft(self, q, b, id) -> Result:
        m = self._get_mention(id)
        if not m or m["state"] not in ("pending", "review", "failed"):
            return _err("この状態では差し替えできません", 409)
        cat = b.get("category") if b.get("category") in CATEGORIES else m["category"]
        t = self.app.templates.pick(cat, exclude_id=m["draft_template_id"])
        if not t:
            return _err("このカテゴリの有効な定型文がありません")
        new = self.app.repo.update_if("mentions", id, {"draft_text": t["text"], "draft_template_id": t["id"], "category": cat,
                                                       "state": "pending", "updated_at": iso(self.app.now())})
        new["label"] = LABELS.get(cat, cat)
        return _ok(new)

    def block_author(self, q, b, id) -> Result:
        m = self._get_mention(id)
        if not m:
            return _err("見つかりません", 404)
        self.app.repo.put("blocklist", m["author_id"], {"id": m["author_id"], "note": str(b.get("note", ""))[:100], "created_at": iso(self.app.now())})
        for x in self.app.repo.list("mentions"):
            if x["author_id"] == m["author_id"] and x["state"] in ("pending", "review", "failed"):
                self.app.repo.update_if("mentions", x["id"], {"state": "blocked", "updated_at": iso(self.app.now())})
        self.app.logger.info("blocklist", "ブロック対象に追加しました", author_id=m["author_id"])
        return _ok()

    # --- 定型文 ---
    def list_templates(self, q, b) -> Result:
        return _ok({"items": self.app.templates.list(), "categories": [{"key": k, "label": LABELS[k]} for k in ("fan", "question")]})

    def _check_template(self, b, partial=False) -> dict:
        out = {}
        if "category" in b or not partial:
            if b.get("category") not in ("fan", "question"):
                raise ValidationError("カテゴリは「ポジティブ反応」「質問」から選んでください")
            out["category"] = b["category"]
        if "text" in b or not partial:
            out["text"] = self.app.sender.check_text(str(b.get("text", "")))
        if "enabled" in b:
            out["enabled"] = bool(b["enabled"])
        return out

    def create_template(self, q, b) -> Result:
        try:
            v = self._check_template(b)
        except SendRefused as e:
            raise ValidationError(str(e)) from e
        return _ok(self.app.templates.create(v["category"], v["text"], v.get("enabled", True)), 201)

    def update_template(self, q, b, id) -> Result:
        try:
            v = self._check_template(b, partial=True)
        except SendRefused as e:
            raise ValidationError(str(e)) from e
        t = self.app.templates.update(id, **v)
        return _ok(t) if t else _err("見つかりません", 404)

    def delete_template(self, q, b, id) -> Result:
        self.app.templates.delete(id)
        return _ok()

    # --- ブロック対象 ---
    def list_block(self, q, b) -> Result:
        return _ok({"items": self.app.repo.list("blocklist")})

    def add_block(self, q, b) -> Result:
        aid = str(b.get("author_id", "")).strip()
        if not re.fullmatch(r"\d{1,20}", aid):
            return _err("ユーザーIDは数字で入力してください")
        self.app.repo.put("blocklist", aid, {"id": aid, "note": str(b.get("note", ""))[:100], "created_at": iso(self.app.now())})
        return _ok(status=201)

    def del_block(self, q, b, id) -> Result:
        self.app.repo.delete("blocklist", id)
        return _ok()

    # --- ジョブ ---
    def jobs(self, q, b) -> Result:
        a = self.app
        return _ok({"settings": a.settings.get("job"), "state": a.jobs.state(), "next_due": a.jobs.next_due(),
                    "runs": a.repo.list("runs", limit=30, reverse=True)})

    def run_job(self, q, b) -> Result:
        run = self.app.jobs.run("manual")
        return _ok(run)

    # --- 統計・ログ ---
    def stats(self, q, b) -> Result:
        return _ok(stats_mod.build(self.app.repo, self.app.now))

    def logs(self, q, b) -> Result:
        level = q.get("level", "")
        rows = self.app.repo.list("logs", limit=300, reverse=True)
        if level:
            rows = [r for r in rows if r["level"] == level]
        for r in rows:
            r.pop("ttl", None)
        return _ok({"items": rows})

    # --- 開発用（モックのみ） ---
    def mock_mentions(self, q, b) -> Result:
        if not self.app.mock:
            return _err("モックモードでのみ使えます", 404)
        samples = [
            ("いつも可愛い投稿ありがとうございます！応援してます😍", "reply_to_post"),
            ("この写真はどこで撮影したんですか？", "reply_to_reply"),
            ("副業で月100万稼げる方法教えます https://example.com/xxx", "mention"),
            ("DM送ってもいいですか？会いたいです", "reply_to_post"),
            ("今日もお疲れさまです", "reply_to_reply"),
            ("新作楽しみにしてます！最高です", "quote"),
        ]
        import random
        picks = random.sample(samples, k=min(len(samples), int(b.get("count", 4))))
        added = self.app.x.add_mentions([p[0] for p in picks], [str(2000 + i) for i in range(len(picks))], [p[1] for p in picks])
        return _ok({"added": len(added)})

    def mock_posts(self, q, b) -> Result:
        if not self.app.mock:
            return _err("モックモードでのみ使えます", 404)
        p = self.app.x.add_post("（サンプル）今日の写真です📷", likes=int(b.get("likes", 120)), reposts=int(b.get("reposts", 5)), impressions=int(b.get("impressions", 3000)))
        return _ok({"id": p["id"]})
