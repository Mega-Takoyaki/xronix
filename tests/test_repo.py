import boto3
import pytest
from moto import mock_aws

from xronix.repo import DynamoRepo, SqliteRepo


@pytest.fixture(params=["sqlite", "dynamo"])
def repo(request):
    if request.param == "sqlite":
        yield SqliteRepo(":memory:")
        return
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="ap-northeast-1")
        ddb.create_table(TableName="t", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
        yield DynamoRepo("t", ddb)


def test_put_get_delete(repo):
    repo.put("c", "1", {"a": 1, "b": "x", "f": 0.5, "n": {"z": [1, 2]}})
    assert repo.get("c", "1") == {"a": 1, "b": "x", "f": 0.5, "n": {"z": [1, 2]}}
    repo.delete("c", "1")
    assert repo.get("c", "1") is None


def test_put_if_absent(repo):
    assert repo.put_if_absent("c", "1", {"v": 1}) is True
    assert repo.put_if_absent("c", "1", {"v": 2}) is False
    assert repo.get("c", "1") == {"v": 1}


def test_list_order_limit_isolated_by_collection(repo):
    for i in ("2", "1", "3"):
        repo.put("c", i, {"i": i})
    repo.put("other", "9", {"i": "9"})
    assert [x["i"] for x in repo.list("c")] == ["1", "2", "3"]
    assert [x["i"] for x in repo.list("c", reverse=True)] == ["3", "2", "1"]
    assert [x["i"] for x in repo.list("c", limit=2)] == ["1", "2"]


def test_update_if(repo):
    repo.put("c", "1", {"state": "a", "n": 1})
    assert repo.update_if("c", "1", {"state": "b"}, {"state": "a"})["state"] == "b"
    assert repo.update_if("c", "1", {"state": "c"}, {"state": "a"}) is None  # 期待値が違う
    assert repo.get("c", "1")["state"] == "b"
    assert repo.update_if("c", "missing", {"state": "x"}) is None


def test_incr_with_max(repo):
    assert repo.incr("s", "d", "count", 1, max_value=2) == 1
    assert repo.incr("s", "d", "count", 1, max_value=2) == 2
    assert repo.incr("s", "d", "count", 1, max_value=2) is None
    assert repo.incr("s", "d", "count", -1) == 1
    assert repo.incr("s", "d", "count", 1, max_value=2) == 2
