"""次回実行の登録。EventBridge Schedulerの「一回限り」のスケジュール（秒単位の時刻指定）を、実行のたびに登録し直す。

15分ごとの固定起動では、1分刻みの間隔や、ランダムな秒での実行ができないため、この方式にしている。
"""
from __future__ import annotations

import json
import secrets as pysecrets
from datetime import datetime, timezone
from typing import Protocol


class JobScheduler(Protocol):
    def arm(self, when: datetime, payload: dict | None = None) -> str: ...
    def cancel(self, name: str) -> None: ...


class NullScheduler:
    """何も登録しない（ローカル。開発サーバの定期確認が時刻を見て実行する）。"""

    def arm(self, when: datetime, payload: dict | None = None) -> str:
        return ""

    def cancel(self, name: str) -> None:
        pass


class AwsScheduler:
    def __init__(self, job_arn: str, role_arn: str, prefix: str, client=None, group: str = "default"):
        import boto3

        self._c = client or boto3.client("scheduler")
        self.job_arn, self.role_arn, self.prefix, self.group = job_arn, role_arn, prefix, group

    def arm(self, when: datetime, payload: dict | None = None) -> str:
        """指定時刻（秒単位）に1回だけジョブを起動するスケジュールを作る。実行後は自動で削除される。"""
        when = when.astimezone(timezone.utc)
        name = f"{self.prefix}-next-{int(when.timestamp())}-{pysecrets.token_hex(2)}"
        self._c.create_schedule(
            Name=name, GroupName=self.group,
            ScheduleExpression=f"at({when.strftime('%Y-%m-%dT%H:%M:%S')})",
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE",
            Target={"Arn": self.job_arn, "RoleArn": self.role_arn, "Input": json.dumps(payload or {"source": "schedule"}),
                    "RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 60}},
        )
        return name

    def cancel(self, name: str) -> None:
        try:
            self._c.delete_schedule(Name=name, GroupName=self.group)
        except self._c.exceptions.ResourceNotFoundException:
            pass
