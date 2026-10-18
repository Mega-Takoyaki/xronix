"""Telegramの承認ボタン: 全文つきの通知、ボタンの操作（承認・却下）、結果の表示、操作できる人の限定。"""
import json

import pytest

from conftest import FakeHttp  # noqa: F401
from test_actions import connect_and_start, only_rule
from xronix.api import Api
from xronix.settings import ValidationError
from xronix.telegram_bot import WEBHOOK_SECRET

TOKEN = "123:BOT-TOKEN"
SECRET = "s3cret-token"


def calls(app, method=None):
    out = []
    for c in app.http.calls:
        if "api.telegram.org" in c["url"]:
            name = c["url"].rsplit("/", 1)[-1]
            if method is None or name == method:
                out.append((name, json.loads(c["body"] or b"{}")))
    return out


def ok_reply(app, result=None):
    app.http.push(200, {"ok": True, "result": result or {"message_id": 777}})


def setup(app, chat="42", user_id="", actions=True):
    app.secrets.put("telegram_bot_token", TOKEN)
    app.settings.update("notify", {"telegram_enabled": True, "telegram_chat_id": chat, "site_url": "https://x.example.net",
                                   "telegram_user_id": user_id, "telegram_actions": actions})
    if actions:
        app.secrets.put(WEBHOOK_SECRET, SECRET)
    connect_and_start(app)
    only_rule(app, categories=["fan"], actions=[{"type": "queue_reply", "source": "template", "template_category": "fan"}])


def make_pending(app, text="可愛い投稿ありがとうございます！応援しています", author="1000"):
    app.x.add_mentions([text], [author])
    ok_reply(app)  # sendMessage
    app.jobs.run("manual")
    return [m for m in app.repo.list("mentions") if m["id"].isdigit()][-1]


def press(app, data, user="42", chat="42", secret=SECRET, message_id=777):
    upd = {"update_id": 1, "callback_query": {"id": "cb1", "from": {"id": int(user)}, "data": data,
                                              "message": {"message_id": message_id, "chat": {"id": int(chat)}}}}
    for _ in range(3):
        ok_reply(app, {})  # answerCallbackQuery / editMessageText
    return Api(app).handle("POST", "/api/telegram/webhook", {}, upd, {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {})


def test_pending_item_is_sent_with_full_text_and_buttons(app):
    setup(app)
    text = "可愛い投稿ありがとうございます！" + "応援しています。" * 25   # 長い本文でも、全文を送る
    m = make_pending(app, text)
    name, body = calls(app, "sendMessage")[-1]
    assert body["chat_id"] == "42" and text in body["text"] and m["draft_text"] in body["text"] and "ユーザーID: 1000" in body["text"]   # 全文
    kb = body["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["✅ 承認して送信", "❌ 却下"] and kb[0]["callback_data"].startswith("a:") and kb[1]["callback_data"].startswith("r:")
    assert all(len(b["callback_data"]) <= 64 for b in kb)
    fresh = app.repo.get("mentions", m["id"])
    assert fresh["tg_message_id"] == 777 and fresh["tg_key"] and fresh["tg_chat_id"] == "42"
    assert not any("承認待ちが" in b["text"] for _, b in calls(app, "sendMessage"))                      # 件数だけの通知は、送らない


def test_without_actions_only_the_count_is_sent(app):
    setup(app, actions=False)
    app.x.add_mentions(["可愛い投稿ありがとうございます！"])
    ok_reply(app)
    app.jobs.run("manual")
    _, body = calls(app, "sendMessage")[-1]
    assert "承認待ちが1件" in body["text"] and "可愛い" not in body["text"] and "reply_markup" not in body


def test_approve_button_sends_and_shows_result_on_the_message(app):
    setup(app)
    m = make_pending(app)
    key = app.repo.get("mentions", m["id"])["tg_key"]
    st, body, _ = press(app, f"a:{key}")
    assert st == 200
    assert app.repo.get("mentions", m["id"])["state"] == "sent" and app.x.sent[0]["in_reply_to"] == m["id"] and app.x.sent[0]["text"] == m["draft_text"]
    edit = calls(app, "editMessageText")[-1][1]
    assert "✅ 承認して送信しました" in edit["text"] and "【処理済み】" in edit["text"] and edit["reply_markup"] == {"inline_keyboard": []} and edit["message_id"] == 777
    ans = calls(app, "answerCallbackQuery")[-1][1]
    assert ans["callback_query_id"] == "cb1" and "承認して送信しました" in ans["text"]


def test_reject_button_rejects_and_shows_result(app):
    setup(app)
    m = make_pending(app)
    key = app.repo.get("mentions", m["id"])["tg_key"]
    press(app, f"r:{key}")
    assert app.repo.get("mentions", m["id"])["state"] == "rejected" and app.x.sent == []
    assert "❌ 却下しました" in calls(app, "editMessageText")[-1][1]["text"]


def test_pressing_twice_does_not_send_twice(app):
    setup(app)
    m = make_pending(app)
    key = app.repo.get("mentions", m["id"])["tg_key"]
    press(app, f"a:{key}"); press(app, f"a:{key}"); press(app, f"r:{key}")
    assert len(app.x.sent) == 1 and app.repo.get("mentions", m["id"])["state"] == "sent"
    assert "すでに処理済みです（送信済み）" in calls(app, "editMessageText")[-1][1]["text"]


def test_webhook_rejects_wrong_or_missing_secret(app):
    setup(app)
    m = make_pending(app)
    key = app.repo.get("mentions", m["id"])["tg_key"]
    for secret in ("wrong", "", None):
        n = len(app.http.calls)
        st, body, _ = press(app, f"a:{key}", secret=secret)
        assert st == 403 and len(app.http.calls) == n          # Telegramにも、Xにも、何も送らない
    assert app.x.sent == [] and app.repo.get("mentions", m["id"])["state"] == "pending"
    app.secrets.delete(WEBHOOK_SECRET)
    assert press(app, f"a:{key}", secret="")[0] == 403          # 合言葉が未設定（無効）のときも、全拒否


def test_only_the_configured_user_and_chat_can_operate(app):
    setup(app)
    m = make_pending(app)
    key = app.repo.get("mentions", m["id"])["tg_key"]
    assert press(app, f"a:{key}", user="999")[0] == 200            # 別のユーザー
    assert press(app, f"a:{key}", chat="555")[0] == 200            # 別のチャット
    assert app.x.sent == [] and app.repo.get("mentions", m["id"])["state"] == "pending"
    assert "許可されていません" in calls(app, "answerCallbackQuery")[-1][1]["text"]
    assert not calls(app, "editMessageText")                        # メッセージは変えない


def test_group_chat_needs_explicit_user_id(app):
    setup(app, chat="-100777", user_id="42")
    m = make_pending(app)
    key = app.repo.get("mentions", m["id"])["tg_key"]
    assert press(app, f"a:{key}", user="43", chat="-100777")[0] == 200 and app.x.sent == []   # グループの他の人は、不可
    press(app, f"a:{key}", user="42", chat="-100777")
    assert len(app.x.sent) == 1
    app.settings.update("notify", {"telegram_user_id": ""})
    assert Api(app).handle("POST", "/api/telegram/enable", {}, {})[0] == 502                   # グループで、ユーザーID未設定なら、有効にできない


def test_refused_send_keeps_buttons_and_explains(app):
    setup(app)
    app.settings.update("safety", {"daily_limit": 0})
    m = make_pending(app)
    key = app.repo.get("mentions", m["id"])["tg_key"]
    press(app, f"a:{key}")
    edit = calls(app, "editMessageText")[-1][1]
    assert "⚠️ 送信できませんでした" in edit["text"] and "上限" in edit["text"] and "reply_markup" not in edit        # ボタンは残す
    assert app.repo.get("mentions", m["id"])["state"] == "pending" and app.x.sent == []


def test_approve_goes_through_send_queue_when_enabled(app):
    setup(app)
    app.settings.update("send", {"manual_via_queue": True})
    m = make_pending(app)
    key = app.repo.get("mentions", m["id"])["tg_key"]
    press(app, f"a:{key}")
    assert app.repo.get("mentions", m["id"])["state"] == "send_queued" and app.x.sent == []
    assert "送信キューに入れました" in calls(app, "editMessageText")[-1][1]["text"]


def test_approving_in_the_app_updates_the_telegram_message(app):
    setup(app)
    m = make_pending(app)
    ok_reply(app, {})
    Api(app).handle("POST", f"/api/mentions/{m['id']}/reply", {}, {"text": "ありがとうございます"})
    edit = calls(app, "editMessageText")[-1][1]
    assert "✅ 画面から承認して送信しました" in edit["text"] and edit["reply_markup"] == {"inline_keyboard": []}
    m2 = make_pending(app, "可愛い！ありがとうございます", author="2000")
    ok_reply(app, {})
    Api(app).handle("POST", f"/api/mentions/{m2['id']}/reject", {}, {})
    assert "❌ 却下しました" in calls(app, "editMessageText")[-1][1]["text"]


def test_other_updates_are_ignored_and_unknown_key_is_harmless(app):
    setup(app)
    st, body, _ = Api(app).handle("POST", "/api/telegram/webhook", {}, {"update_id": 5, "message": {"text": "hi"}}, {"x-telegram-bot-api-secret-token": SECRET})
    assert st == 200 and app.x.sent == []
    press(app, "a:doesnotexist")
    assert "対象が見つかりません" in calls(app, "answerCallbackQuery")[-1][1]["text"] and app.x.sent == []


def test_cap_per_run_and_review_items_are_summarized(app):
    setup(app)
    app.x.add_mentions(["可愛い"] * 12, [str(2000 + i) for i in range(12)])
    for _ in range(40):
        ok_reply(app)
    app.jobs.run("manual")
    sends = calls(app, "sendMessage")
    items = [b for _, b in sends if "reply_markup" in b]
    assert len(items) == 10 and any("ほかに、承認待ちが2件" in b["text"] for _, b in sends)


def test_enable_and_disable_register_and_remove_webhook(app):
    app.secrets.put("telegram_bot_token", TOKEN)
    app.settings.update("notify", {"telegram_enabled": True, "telegram_chat_id": "42", "site_url": "https://x.example.net"})
    api = Api(app)
    ok_reply(app, True)
    st, body, _ = api.handle("POST", "/api/telegram/enable", {}, {})
    assert st == 200 and body["url"] == "https://x.example.net/api/telegram/webhook"
    name, payload = calls(app, "setWebhook")[-1]
    assert payload["url"] == body["url"] and payload["allowed_updates"] == ["callback_query"] and len(payload["secret_token"]) >= 32
    assert app.secrets.get(WEBHOOK_SECRET) == payload["secret_token"] and app.settings.get("notify")["telegram_actions"] is True
    assert TOKEN not in json.dumps(body)
    ok_reply(app, True)
    assert api.handle("POST", "/api/telegram/disable", {}, {})[0] == 200
    assert calls(app, "deleteWebhook") and app.secrets.get(WEBHOOK_SECRET) is None and app.settings.get("notify")["telegram_actions"] is False


def test_enable_requires_settings_and_reports_telegram_errors(app):
    api = Api(app)
    assert api.handle("POST", "/api/telegram/enable", {}, {})[0] == 502                       # トークン未設定
    app.secrets.put("telegram_bot_token", TOKEN)
    app.settings.update("notify", {"telegram_chat_id": "42"})
    st, body, _ = api.handle("POST", "/api/telegram/enable", {}, {})
    assert st == 502 and "画面のURL" in body["error"]
    app.settings.update("notify", {"site_url": "https://x.example.net"})
    app.http.push(400, {"ok": False, "description": "Bad Request: bad webhook"})
    st, body, _ = api.handle("POST", "/api/telegram/enable", {}, {})
    assert st == 502 and "bad webhook" in body["error"] and TOKEN not in body["error"]
    assert app.secrets.get(WEBHOOK_SECRET) is None and app.settings.get("notify")["telegram_actions"] is False


def test_user_id_validation(app):
    with pytest.raises(ValidationError):
        app.settings.update("notify", {"telegram_user_id": "abc"})
    assert app.settings.update("notify", {"telegram_user_id": "12345"})["telegram_user_id"] == "12345"
