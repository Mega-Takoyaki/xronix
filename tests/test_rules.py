import pytest

from xronix.api import Api
from xronix.rules import validate_rule
from xronix.settings import ValidationError


def call(api, method, path, body=None):
    st, payload, _ = api.handle(method, path, {}, body or {})
    return st, payload


def rule(**kw):
    base = {"name": "テスト", "events": ["reply_to_post"], "categories": [], "actions": [{"type": "ignore"}]}
    base.update(kw)
    return base


def test_default_rules_are_seeded(app):
    rules = app.rules.list()
    assert [r["order"] for r in rules] == list(range(len(rules)))
    enabled = [r for r in rules if r["enabled"]]
    assert len(enabled) == 4 and len(rules) == 6                     # 従来の動き（4件）＋使い方の見本（無効2件）
    assert any(r["events"] == ["metric_likes"] and not r["enabled"] for r in rules)
    app.rules.seed_if_empty()
    assert len(app.rules.list()) == 6                                 # 2回目は増やさない


@pytest.mark.parametrize("bad,msg", [
    ({"name": ""}, "ルール名"),
    ({"name": "あ" * 61}, "ルール名"),
    ({"events": []}, "反応の種類"),
    ({"events": ["nope"]}, "反応の種類"),
    ({"events": ["reply_to_post", "metric_likes"]}, "同じルールにまとめられません"),
    ({"categories": ["nope"]}, "分類"),
    ({"actions": []}, "アクションは1"),
    ({"actions": [{"type": "ignore"}] * 6}, "アクションは1"),
    ({"actions": [{"type": "explode"}]}, "不明なアクション"),
    ({"actions": [{"type": "queue_reply"}, {"type": "send_reply"}]}, "返信のアクションは、1つ"),
    ({"actions": [{"type": "send_reply", "source": "manual"}]}, "作り方"),
    ({"actions": [{"type": "queue_reply", "source": "ai", "prompt": "  "}]}, "プロンプト"),
    ({"actions": [{"type": "queue_reply", "source": "ai", "prompt": "x" * 2001}]}, "2000"),
    ({"actions": [{"type": "queue_reply", "source": "ai", "prompt": "x", "context": "all"}]}, "文脈"),
    ({"actions": [{"type": "queue_reply", "source": "ai", "prompt": "x", "thread_depth": 11}]}, "1〜10"),
    ({"actions": [{"type": "queue_reply", "source": "template", "template_category": "spam"}]}, "定型文"),
    ({"events": ["metric_likes"], "threshold": 0}, "基準"),
    ({"events": ["metric_likes", "metric_reposts"], "threshold": 5}, "1つの種類ごと"),
    ({"events": ["metric_likes"], "threshold": 5, "actions": [{"type": "like"}]}, "使えません"),
    ({"events": ["metric_likes"], "threshold": 5, "actions": [{"type": "blocklist"}]}, "使えません"),
])
def test_rule_validation(bad, msg):
    with pytest.raises(ValidationError, match=msg):
        validate_rule(rule(**bad))


def test_valid_rules_are_normalized():
    r = validate_rule(rule(events=["metric_likes"], categories=["fan"], threshold=100,
                           actions=[{"type": "queue_reply", "source": "ai", "prompt": " お礼を書く ", "context": "thread", "thread_depth": 4}]))
    assert r["categories"] == [] and r["threshold"] == 100                      # 数値のルールに、分類は無い
    assert r["actions"][0]["prompt"] == "お礼を書く" and r["actions"][0]["thread_depth"] == 4
    r = validate_rule(rule(actions=[{"type": "like", "evil": 1}, {"type": "send_reply", "source": "template"}]))
    assert r["actions"][0] == {"type": "like"} and r["threshold"] == 0


def test_first_match_wins_and_filters(app):
    for r in app.rules.list():
        app.rules.delete(r["id"])
    a = app.rules.create(rule(name="A: ポジティブ反応の返信", events=["reply_to_post"], categories=["fan"], actions=[{"type": "like"}]))
    b = app.rules.create(rule(name="B: 返信すべて", events=["reply_to_post", "reply_to_reply"], actions=[{"type": "ignore"}]))
    off = app.rules.create(rule(name="C: 無効", enabled=False, events=["mention"], actions=[{"type": "ignore"}]))
    assert app.rules.match("reply_to_post", "fan")["id"] == a["id"]            # 上から見て、最初に合致したもの
    assert app.rules.match("reply_to_post", "question")["id"] == b["id"]       # 分類が合わなければ、次へ
    assert app.rules.match("reply_to_reply", "fan")["id"] == b["id"]
    assert app.rules.match("mention", "fan") is None                           # 無効なルールは使わない
    assert app.rules.match("quote", "fan") is None


def test_rules_api_crud_move_and_simulate(app):
    api = Api(app)
    st, body = call(api, "GET", "/api/rules")
    assert st == 200 and len(body["items"]) == 6 and {a["key"] for a in body["meta"]["actions"]} >= {"like", "queue_reply", "send_reply"}
    assert any(e["metric"] for e in body["meta"]["events"]) and body["meta"]["sources"]["send_reply"] == ["template", "ai"]
    st, created = call(api, "POST", "/api/rules", rule(name="新規", events=["quote"], actions=[{"type": "like"}]))
    assert st == 201 and created["order"] == 6
    assert call(api, "POST", "/api/rules", rule(name=""))[0] == 400
    assert call(api, "PUT", f"/api/rules/{created['id']}", {"enabled": False})[1]["enabled"] is False
    assert call(api, "PUT", f"/api/rules/{created['id']}", {"actions": [{"type": "explode"}]})[0] == 400
    assert call(api, "PUT", "/api/rules/none", {"enabled": False})[0] == 404
    call(api, "POST", f"/api/rules/{created['id']}/move", {"direction": "up"})
    ids = [r["id"] for r in app.rules.list()]
    assert ids.index(created["id"]) == 5 and [r["order"] for r in app.rules.list()] == list(range(7))
    call(api, "POST", f"/api/rules/{ids[0]}/move", {"direction": "up"})         # 端は何も起きない
    assert [r["id"] for r in app.rules.list()] == ids
    sim = call(api, "POST", "/api/rules/simulate", {"event": "reply_to_post", "category": "fan"})[1]
    assert sim["rule"]["name"].startswith("ポジティブ反応") and sim["rule"]["actions"] == ["返信案を作り、承認待ちに入れる"]
    assert call(api, "POST", "/api/rules/simulate", {"event": "metric_likes"})[0] == 400
    assert call(api, "DELETE", f"/api/rules/{created['id']}")[0] == 200
    assert len(app.rules.list()) == 6
