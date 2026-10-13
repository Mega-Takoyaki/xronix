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


# ---------- 利用可否の確認と、拒否されたときの自動切り替え ----------
from xronix.ai import ranked  # noqa: E402


def by_id(app, **kw):
    return {i["id"]: i for i in app.ai.list_models(**kw)["items"]}


def test_availability_flags_and_reasons(app, control):
    control.availability = {
        "anthropic.claude-haiku-x": {"entitlementAvailability": "NOT_AVAILABLE"},
        "amazon.nova-micro-v1:0": {"authorizationStatus": "NOT_AUTHORIZED"},
        "anthropic.claude-big-x": {"regionAvailability": "NOT_AVAILABLE"},
    }
    got = by_id(app)
    assert got["anthropic.claude-haiku-x"]["available"] is False and "契約" in got["anthropic.claude-haiku-x"]["reason"]
    assert got["amazon.nova-micro-v1:0"]["available"] is False and "有効化" in got["amazon.nova-micro-v1:0"]["reason"]
    assert got["apac.anthropic.claude-big-x"]["available"] is False and "リージョン" in got["apac.anthropic.claude-big-x"]["reason"]   # プロファイルは、元のモデルに従う


@pytest.mark.parametrize("override,expected", [
    ({"agreementAvailability": {"status": "PENDING"}}, False),
    ({"agreementAvailability": {"status": "NOT_AVAILABLE"}}, False),
    ({"agreementAvailability": {"status": "AVAILABLE"}}, True),
    ({}, True),
])
def test_availability_agreement_states(app, control, override, expected):
    control.availability = {"amazon.nova-micro-v1:0": override}
    assert by_id(app)["amazon.nova-micro-v1:0"]["available"] is expected


def test_availability_api_failure_means_unknown_not_excluded(app, control):
    control.availability_error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetFoundationModelAvailability")
    got = by_id(app)
    assert got["amazon.nova-micro-v1:0"]["available"] is None                # 不明。一覧からは除かない
    assert app.ai.resolve_model(app.settings.get("ai")) == "anthropic.claude-haiku-x"


def test_auto_pick_skips_models_this_account_cannot_use(app, control):
    # 利用者の状況: GPT-6 Luna は一覧に出るが、このアカウントでは使えない
    control.models.append({"modelId": "openai.gpt-6-luna", "modelName": "GPT-6 Luna", "providerName": "OpenAI",
                           "inferenceTypesSupported": ["ON_DEMAND"], "modelLifecycle": {"status": "ACTIVE"}})
    control.availability = {"openai.gpt-6-luna": {"entitlementAvailability": "NOT_AVAILABLE"}}
    assert by_id(app)["openai.gpt-6-luna"]["available"] is False
    assert app.ai.list_models()["default"] == "anthropic.claude-haiku-x"      # 次の候補が選ばれる
    control.availability = {}
    assert app.ai.list_models(refresh=True)["default"] == "openai.gpt-6-luna"  # 使えるなら、最優先で選ばれる


def test_ranked_excludes_unavailable_and_prefers_confirmed():
    its = [
        {"id": "a.haiku", "label": "haiku", "available": None},
        {"id": "b.haiku", "label": "haiku", "available": True},
        {"id": "c.luna", "label": "luna", "available": False},
        {"id": "d.other", "label": "other", "available": True},
    ]
    assert ranked(its) == ["b.haiku", "a.haiku", "d.other"]               # 同じ名前の中では、利用可と確認できたものが先。使えないものは除く
    assert pick_default([{"id": "x", "label": "x", "available": False}]) is None


def make_bedrock_fail_first(bedrock, control, failing):
    """ranked順の先頭から、failing個を拒否し、そのあとは成功させる。"""
    for _ in range(failing):
        bedrock.push_error("AccessDeniedException", "openai.x is not available for this account")
    bedrock.push_text('{"category":"fan"}')


def test_auto_falls_back_to_next_candidate_when_rejected(app, bedrock, control):
    control.models.append({"modelId": "openai.gpt-6-luna", "modelName": "GPT-6 Luna", "providerName": "OpenAI",
                           "inferenceTypesSupported": ["ON_DEMAND"], "modelLifecycle": {"status": "ACTIVE"}})   # 利用可否のAPIは「利用可」と答えるが、実行すると拒否される
    Api(app).handle("PUT", "/api/settings/ai", {}, {"provider": "bedrock"})
    make_bedrock_fail_first(bedrock, control, 1)
    assert app.ai.classify("可愛い") == ("fan", "ai")
    assert [c["modelId"] for c in bedrock.calls] == ["openai.gpt-6-luna", "anthropic.claude-haiku-x"]     # 拒否 → 次の候補
    assert app.ai.last_model == "anthropic.claude-haiku-x"
    assert any("openai.gpt-6-luna" in l["message"] for l in app.repo.list("logs"))
    bedrock.calls.clear()
    bedrock.push_text('{"category":"fan"}')
    app.ai.classify("好きです")
    assert [c["modelId"] for c in bedrock.calls] == ["anthropic.claude-haiku-x"]                            # 外した候補は、以後は試さない
    app.ai.list_models(refresh=True)                                                                         # 一覧を更新すると、外した候補も戻る
    bedrock.calls.clear()
    bedrock.push_error("AccessDeniedException", "again")
    bedrock.push_text('{"category":"fan"}')
    app.ai.classify("最高です")
    assert bedrock.calls[0]["modelId"] == "openai.gpt-6-luna"


def test_auto_gives_up_after_three_rejections_with_clear_error(app, bedrock, control):
    Api(app).handle("PUT", "/api/settings/ai", {}, {"provider": "bedrock"})
    for _ in range(3):
        bedrock.push_error("AccessDeniedException", "nope")
    with pytest.raises(AIError, match="使えるモデルが見つかりませんでした"):
        app.ai._chat(app.settings.get("ai"), "s", "u", 10)
    assert len(bedrock.calls) == 3


def test_selected_model_is_never_silently_replaced(app, bedrock):
    Api(app).handle("PUT", "/api/settings/ai", {}, {"provider": "bedrock", "bedrock_model": "openai.gpt-6-luna"})
    bedrock.push_error("AccessDeniedException", "openai.gpt-6-luna is not available for this account")
    with pytest.raises(AIError) as e:
        app.ai._chat(app.settings.get("ai"), "s", "u", 10)
    assert len(bedrock.calls) == 1                                          # 選んだモデルは、勝手に差し替えない
    assert "AccessDeniedException" in str(e.value) and "「利用可」" in str(e.value)    # 日本語の案内が付く


def test_on_demand_throughput_error_hints_inference_profile(app, bedrock):
    Api(app).handle("PUT", "/api/settings/ai", {}, {"provider": "bedrock", "bedrock_model": "anthropic.claude-big-x"})
    bedrock.push_error("ValidationException", "Invocation of model ID with on-demand throughput isn't supported. Retry with an inference profile")
    with pytest.raises(AIError, match="推論プロファイル"):
        app.ai._chat(app.settings.get("ai"), "s", "u", 10)


def test_connection_test_reports_the_model_actually_used(app, bedrock, control):
    api = Api(app)
    api.handle("PUT", "/api/settings/ai", {}, {"provider": "bedrock"})
    bedrock.push_error("AccessDeniedException", "first not available")
    bedrock.push_text('{"category":"fan"}')
    st, body, _ = api.handle("POST", "/api/ai/test", {}, {})
    assert st == 200 and body["model"] == "amazon.nova-micro-v1:0"          # 先頭（Haiku）が拒否され、次の候補が使われた


def test_mock_mode_sample_list_includes_an_unavailable_model(app):
    app.ai.allow_external = False
    r = app.ai.list_models()
    assert [i["available"] for i in r["items"]] == [True, True, False] and r["default"] == "mock.large-model"   # 使えないサンプルは、選ばれない
