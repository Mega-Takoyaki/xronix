import pytest
from botocore.exceptions import ClientError

from xronix.ai import AIError, pick_default
from xronix.api import Api


def items(*ids):
    return [{"id": i, "label": i, "provider": "", "kind": "model"} for i in ids]


def test_list_models_filters_to_usable_text_models(app, control):
    r = app.ai.list_models()
    ids = [i["id"] for i in r["items"]]
    assert "anthropic.claude-haiku-x" in ids and "amazon.nova-micro-v1:0" in ids
    assert "old.model" not in ids                         # 提供終了（LEGACY）は出さない
    assert "anthropic.claude-big-x" not in ids            # 推論プロファイルでしか呼べないモデルは、直接のIDでは出さない
    assert "apac.anthropic.claude-big-x" in ids           # 代わりに、プロファイルのIDを出す
    assert "apac.embed" not in ids                        # テキストを出力しないモデルのプロファイルは除く
    assert {i["kind"] for i in r["items"]} == {"model", "profile"} and r["region"] == "ap-northeast-1" and r["sample"] is False


def test_list_models_is_cached_and_refreshable(app, control, clock):
    app.ai.list_models(); app.ai.list_models()
    assert control.calls == 1 and app.ai.list_models()["cached"] is True
    app.ai.list_models(refresh=True)
    assert control.calls == 2
    clock.advance(hours=2)                                 # 1時間たつと、取り直す
    app.ai.list_models()
    assert control.calls == 3
    app.settings.update("ai", {"bedrock_region": "us-east-1"})   # リージョンを変えたら、取り直す
    app.ai.list_models()
    assert control.calls == 4


def test_list_models_error_is_reported(app, control):
    control.error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not allowed"}}, "ListFoundationModels")
    with pytest.raises(AIError, match="AccessDeniedException"):
        app.ai.list_models()
    st, body, _ = Api(app).handle("GET", "/api/ai/models", {}, {})
    assert st == 502 and "モデル一覧" in body["error"]


def test_pick_default_prefers_cheap_models_by_name():
    assert pick_default(items("anthropic.claude-haiku-x", "openai.gpt-6-luna", "x.big")) == "openai.gpt-6-luna"        # GPT-6 Luna があれば最優先
    assert pick_default(items("anthropic.claude-haiku-x", "someone.luna-1", "x.big")) == "someone.luna-1"             # 名前に luna を含むもの
    assert pick_default(items("anthropic.claude-haiku-x", "openai.gpt-5-nano", "x.big")) == "openai.gpt-5-nano"
    assert pick_default(items("x.big", "amazon.nova-micro-v1:0", "amazon.nova-lite-v1:0")) == "amazon.nova-micro-v1:0"
    assert pick_default(items("zzz.model", "aaa.model")) == "aaa.model"                                                  # 該当が無ければ先頭
    assert pick_default([]) is None


def test_pick_default_prefers_profile_of_the_region():
    ids = items("us.anthropic.claude-haiku-x", "anthropic.claude-haiku-x", "apac.anthropic.claude-haiku-x")
    assert pick_default(ids, "ap-northeast-1") == "apac.anthropic.claude-haiku-x"
    assert pick_default(ids, "us-east-1") == "us.anthropic.claude-haiku-x"
    assert pick_default(items("anthropic.claude-haiku-x", "us.anthropic.claude-haiku-x"), "ap-northeast-1") == "anthropic.claude-haiku-x"


def test_resolve_model_uses_selection_else_auto(app, control):
    assert app.ai.resolve_model({"bedrock_model": "my.model"}) == "my.model" and control.calls == 0
    assert app.ai.resolve_model(app.settings.get("ai")) == "anthropic.claude-haiku-x"
    control.models = []
    control.profiles = []
    app.ai.list_models(refresh=True)
    with pytest.raises(AIError, match="使えるモデルが見つかりません"):
        app.ai.resolve_model(app.settings.get("ai"))


def test_models_endpoint_returns_selection_and_default(app):
    api = Api(app)
    api.handle("PUT", "/api/settings/ai", {}, {"bedrock_model": "my.model"})
    st, body, _ = api.handle("GET", "/api/ai/models", {}, {})
    assert st == 200 and body["selected"] == "my.model" and body["default"] == "anthropic.claude-haiku-x" and body["items"]


def test_models_in_mock_mode_are_clearly_samples(app):
    app.ai.allow_external = False
    r = app.ai.list_models()
    assert r["sample"] is True and all("サンプル" in i["label"] for i in r["items"])
