import pytest

from xronix.classify import CAUTION, FAN, OTHER, QUESTION, SPAM, classify
from xronix.common import Lease, has_url, weighted_len
from xronix.http import NetworkError, Response, read_with_backoff
from xronix.settings import ValidationError
from xronix.xclient import AuthRequired, CreateError, LiveX, authorize_url, make_pkce


@pytest.mark.parametrize("text,cat", [
    ("@example_account いつも可愛い投稿ありがとうございます！", FAN),
    ("@example_account この写真はどこですか？", QUESTION),
    ("副業で稼げます", SPAM),
    ("見て https://example.com/a", SPAM),
    ("check my profile", SPAM),
    ("DM送ってもいいですか？", CAUTION),
    ("きもい", CAUTION),
    ("今日は雨ですね", OTHER),
])
def test_classify(text, cat):
    assert classify(text) == cat


def test_text_helpers():
    assert has_url("see example.com now") and has_url("http://a") and not has_url("ありがとう！")
    assert weighted_len("abc") == 3 and weighted_len("あい") == 4


def test_settings_validation(app):
    s = app.settings
    assert s.update("job", {"interval_minutes": 1})["interval_minutes"] == 1
    assert s.update("job", {"interval_minutes": 999})["interval_minutes"] == 999
    for bad in (0, 1000, -5):
        with pytest.raises(ValidationError):
            s.update("job", {"interval_minutes": bad})
    assert s.update("job", {"jitter_minutes": 99})["jitter_minutes"] == 99
    assert s.update("job", {"jitter_minutes": 0})["jitter_minutes"] == 0     # 0はずらさない
    for bad in (-1, 100):
        with pytest.raises(ValidationError):
            s.update("job", {"jitter_minutes": bad})
    with pytest.raises(ValidationError):
        s.update("job", {"enabled": "yes"})
    with pytest.raises(ValidationError):
        s.update("job", {"interval_minutes": "30"})
    with pytest.raises(ValidationError):
        s.update("ai", {"provider": "grok"})
    with pytest.raises(ValidationError):
        s.update("ai", {"bedrock_region": "Tokyo"})
    assert s.update("ai", {"bedrock_model": ""})["bedrock_model"] == ""        # 空は自動選択
    with pytest.raises(ValidationError):
        s.update("ai", {"bedrock_model": "a b"})
    assert s.update("job", {"metrics_interval_minutes": 5, "metrics_window_days": 30, "metrics_max_posts": 100})["metrics_window_days"] == 30
    for bad in ({"metrics_interval_minutes": 4}, {"metrics_window_days": 31}, {"metrics_max_posts": 4}, {"metrics_max_posts": 101}):
        with pytest.raises(ValidationError):
            s.update("job", bad)
    assert s.update("safety", {"like_daily_limit": 0, "auto_daily_limit": 0})["auto_daily_limit"] == 0
    with pytest.raises(ValidationError):
        s.update("safety", {"auto_daily_limit": 101})
    with pytest.raises(ValidationError):
        s.update("safety", {"daily_limit": 1000})
    with pytest.raises(ValidationError):
        s.update("x", {"redirect_uri": "http://evil.example/cb"})
    with pytest.raises(ValidationError):
        s.update("notify", {"telegram_chat_id": "not an id"})
    with pytest.raises(ValidationError):
        s.update("notify", {"site_url": "http://insecure.example"})
    assert s.update("notify", {"telegram_chat_id": "-1001234567890"})["telegram_chat_id"] == "-1001234567890"
    assert s.update("x", {"client_id": "abc", "evil": "x"}).get("evil") is None


def test_ai_defaults_to_bedrock_and_old_values_fall_back():
    from conftest import FakeSecrets
    from xronix.repo import SqliteRepo
    from xronix.settings import Settings
    st = Settings(SqliteRepo(":memory:"))
    ai = st.get("ai")
    assert ai["provider"] == "bedrock" and ai["bedrock_model"] == "" and ai["bedrock_region"] == "ap-northeast-1"
    st.repo.put("settings", "ai", {"provider": "grok"})    # 廃止した選択肢が保存されていても既定に戻る
    assert st.get("ai")["provider"] == "bedrock"


def test_lease_exclusive(app, clock):
    a, b = Lease(app.repo, "t", clock), Lease(app.repo, "t", clock)
    assert a.acquire("A", 60) is True
    assert b.acquire("B", 60) is False
    clock.advance(seconds=61)
    assert b.acquire("B", 60) is True  # 期限切れは奪える
    a.release("A")                      # 他人のリースは解放できない
    assert Lease(app.repo, "t", clock).acquire("C", 60) is False


def test_backoff_retries_reads_only_on_retryable():
    seq = [Response(503, b"", {}), Response(429, b"", {}), Response(200, b"ok", {})]
    sleeps = []
    assert read_with_backoff(lambda: seq.pop(0), sleep=sleeps.append).status == 200
    assert sleeps == [1.0, 2.0]
    assert read_with_backoff(lambda: Response(404, b"", {}), sleep=sleeps.append).status == 404
    def boom(): raise NetworkError("x")
    with pytest.raises(NetworkError):
        read_with_backoff(boom, tries=2, sleep=lambda s: None)


def test_live_create_never_retries(app):
    from conftest import FakeHttp
    http = FakeHttp()
    x = LiveX(http, sleep=lambda s: None)
    http.push(503, {})
    with pytest.raises(CreateError) as e:
        x.create_reply("tok", "hi", "1")
    assert e.value.definite is False and len(http.calls) == 1   # 5xx: 不明・再試行なし
    http.push(403, {})
    with pytest.raises(CreateError) as e:
        x.create_reply("tok", "hi", "1")
    assert e.value.definite is True and len(http.calls) == 2
    http.push_error()
    with pytest.raises(CreateError) as e:
        x.create_reply("tok", "hi", "1")
    assert e.value.definite is False and len(http.calls) == 3
    http.push(201, {"data": {"id": "77"}})
    assert x.create_reply("tok", "hi", "1") == "77"
    import json
    assert json.loads(http.calls[-1]["body"]) == {"text": "hi", "reply": {"in_reply_to_tweet_id": "1"}}


def test_live_mentions_paging_and_params():
    from conftest import FakeHttp
    http = FakeHttp()
    x = LiveX(http, sleep=lambda s: None)
    http.push(200, {"data": [{"id": "3"}, {"id": "2"}], "meta": {"newest_id": "3", "next_token": "n"}})
    http.push(200, {"data": [{"id": "1"}], "meta": {"newest_id": "1"}})
    r = x.mentions("tok", "9", "0")
    assert [t["id"] for t in r["tweets"]] == ["3", "2", "1"] and r["newest_id"] == "3" and r["reads"] == 3
    assert "since_id=0" in http.calls[0]["url"] and "pagination_token=n" in http.calls[1]["url"]
    assert "expansions" not in http.calls[0]["url"]  # ユーザー展開は課金のため使わない


def test_live_retries_read_on_503():
    from conftest import FakeHttp
    http = FakeHttp()
    x = LiveX(http, sleep=lambda s: None)
    http.push(503, {})
    http.push(200, {"data": {"id": "1", "username": "u"}})
    assert x.me("t")["username"] == "u" and len(http.calls) == 2


def test_oauth_scope_and_pkce():
    v, c = make_pkce()
    url = authorize_url("cid", "http://127.0.0.1:8787/api/x/callback", "st", c)
    assert "offline.access" in url.replace("%20", " ").replace("+", " ") and "code_challenge_method=S256" in url
    from conftest import FakeHttp
    http = FakeHttp()
    http.push(400, {"error": "invalid_grant"})
    with pytest.raises(AuthRequired):
        LiveX(http).refresh("cid", "sec", "old")
    assert http.calls[0]["headers"]["Authorization"].startswith("Basic ")


def test_live_replies_to_me_and_quotes_requests():
    import urllib.parse
    from conftest import FakeHttp
    http = FakeHttp()
    x = LiveX(http, sleep=lambda s: None)
    http.push(200, {"data": [{"id": "5"}], "meta": {"newest_id": "5", "next_token": "NT"}})
    http.push(200, {"data": [{"id": "4"}], "meta": {"newest_id": "4"}})
    r = x.replies_to_me("tok", "example_account", "3", max_pages=3)
    assert [t["id"] for t in r["tweets"]] == ["5", "4"] and r["newest_id"] == "5" and r["truncated"] is False
    q1 = urllib.parse.parse_qs(urllib.parse.urlparse(http.calls[0]["url"]).query)
    assert "/2/tweets/search/recent" in http.calls[0]["url"] and q1["query"] == ["to:example_account -from:example_account"] and q1["since_id"] == ["3"]
    assert urllib.parse.parse_qs(urllib.parse.urlparse(http.calls[1]["url"]).query)["next_token"] == ["NT"]
    http.push(200, {"data": [{"id": "9"}]})
    assert x.quotes_of("tok", "77")["reads"] == 1 and "/2/tweets/77/quote_tweets" in http.calls[-1]["url"]
