"""永続化層。コレクション＋IDの単純なキー値モデルで、ローカル（SQLite）とAWS（DynamoDB）を差し替える。"""
from __future__ import annotations

import json
import sqlite3
import threading
from decimal import Decimal
from typing import Any, Protocol


class Repo(Protocol):
    def get(self, coll: str, id: str) -> dict | None: ...
    def put(self, coll: str, id: str, item: dict) -> None: ...
    def put_if_absent(self, coll: str, id: str, item: dict) -> bool: ...
    def delete(self, coll: str, id: str) -> None: ...
    def list(self, coll: str, limit: int | None = None, reverse: bool = False) -> list[dict]: ...
    def update_if(self, coll: str, id: str, updates: dict, expect: dict | None = None) -> dict | None: ...
    def incr(self, coll: str, id: str, field: str, by: int = 1, max_value: int | None = None) -> int | None: ...


class SqliteRepo:
    """ローカル用。1接続を排他ロックで共有する。"""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.RLock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS kv (coll TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (coll, id))"
        )

    def get(self, coll, id):
        with self._lock:
            row = self._db.execute("SELECT data FROM kv WHERE coll=? AND id=?", (coll, id)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, coll, id, item):
        with self._lock:
            self._db.execute(
                "INSERT INTO kv (coll, id, data) VALUES (?,?,?) ON CONFLICT(coll,id) DO UPDATE SET data=excluded.data",
                (coll, id, json.dumps(item, ensure_ascii=False)),
            )

    def put_if_absent(self, coll, id, item):
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO kv (coll, id, data) VALUES (?,?,?)",
                (coll, id, json.dumps(item, ensure_ascii=False)),
            )
            return cur.rowcount == 1

    def delete(self, coll, id):
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE coll=? AND id=?", (coll, id))

    def list(self, coll, limit=None, reverse=False):
        sql = "SELECT data FROM kv WHERE coll=? ORDER BY id " + ("DESC" if reverse else "ASC")
        args: list[Any] = [coll]
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def update_if(self, coll, id, updates, expect=None):
        with self._lock:
            cur = self.get(coll, id)
            if cur is None:
                return None
            if any(cur.get(k) != v for k, v in (expect or {}).items()):
                return None
            cur.update(updates)
            self.put(coll, id, cur)
            return cur

    def incr(self, coll, id, field, by=1, max_value=None):
        with self._lock:
            cur = self.get(coll, id) or {}
            new = int(cur.get(field, 0)) + by
            if max_value is not None and new > max_value:
                return None
            cur[field] = new
            self.put(coll, id, cur)
            return new


def _to_ddb(v: Any) -> Any:
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_ddb(x) for x in v]
    return v


def _from_ddb(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _from_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_from_ddb(x) for x in v]
    return v


class DynamoRepo:
    """AWS用。単一テーブル（pk=コレクション、sk=ID）。条件付き書き込みで排他を実現する。"""

    def __init__(self, table_name: str, resource=None):
        import boto3
        from botocore.exceptions import ClientError

        self._ClientError = ClientError
        self._t = (resource or boto3.resource("dynamodb")).Table(table_name)

    def _clean(self, item: dict | None) -> dict | None:
        if not item:
            return None
        item = _from_ddb(item)
        item.pop("pk", None)
        item.pop("sk", None)
        return item

    def get(self, coll, id):
        return self._clean(self._t.get_item(Key={"pk": coll, "sk": id}, ConsistentRead=True).get("Item"))

    def put(self, coll, id, item):
        self._t.put_item(Item={**_to_ddb(item), "pk": coll, "sk": id})

    def put_if_absent(self, coll, id, item):
        try:
            self._t.put_item(
                Item={**_to_ddb(item), "pk": coll, "sk": id},
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except self._ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def delete(self, coll, id):
        self._t.delete_item(Key={"pk": coll, "sk": id})

    def list(self, coll, limit=None, reverse=False):
        from boto3.dynamodb.conditions import Key

        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(coll),
            "ScanIndexForward": not reverse,
            "ConsistentRead": True,
        }
        if limit:
            kwargs["Limit"] = limit
        while True:
            resp = self._t.query(**kwargs)
            items.extend(resp.get("Items", []))
            if limit and len(items) >= limit:
                return [self._clean(i) for i in items[:limit]]
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return [self._clean(i) for i in items]

    def update_if(self, coll, id, updates, expect=None):
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        sets = []
        for i, (k, v) in enumerate(updates.items()):
            names[f"#u{i}"] = k
            values[f":u{i}"] = _to_ddb(v)
            sets.append(f"#u{i} = :u{i}")
        conds = ["attribute_exists(pk)"]
        for i, (k, v) in enumerate((expect or {}).items()):
            names[f"#e{i}"] = k
            values[f":e{i}"] = _to_ddb(v)
            conds.append(f"#e{i} = :e{i}")
        if not sets:
            return self.get(coll, id)
        try:
            resp = self._t.update_item(
                Key={"pk": coll, "sk": id},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=" AND ".join(conds),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            return self._clean(resp["Attributes"])
        except self._ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def incr(self, coll, id, field, by=1, max_value=None):
        kwargs: dict[str, Any] = {
            "Key": {"pk": coll, "sk": id},
            "UpdateExpression": "ADD #f :by",
            "ExpressionAttributeNames": {"#f": field},
            "ExpressionAttributeValues": {":by": by},
            "ReturnValues": "UPDATED_NEW",
        }
        if max_value is not None:
            kwargs["ConditionExpression"] = "attribute_not_exists(#f) OR #f <= :lim"
            kwargs["ExpressionAttributeValues"][":lim"] = max_value - by
        try:
            return int(self._t.update_item(**kwargs)["Attributes"][field])
        except self._ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
