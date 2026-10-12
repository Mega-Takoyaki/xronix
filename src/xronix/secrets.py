"""秘密情報の保管。ローカルはファイル、AWSはSSM Parameter Store（SecureString）。値は画面に返さない。"""
from __future__ import annotations

import json
import os
import threading
from typing import Protocol

NAMES = ("x_client_secret", "x_access_token", "x_refresh_token", "ai_api_key", "discord_webhook")


class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...
    def put(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class FileSecrets:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._path)

    def get(self, name):
        return self._load().get(name)

    def put(self, name, value):
        with self._lock:
            data = self._load()
            data[name] = value
            self._save(data)

    def delete(self, name):
        with self._lock:
            data = self._load()
            data.pop(name, None)
            self._save(data)


class SsmSecrets:
    def __init__(self, prefix: str = "/xronix/", client=None):
        import boto3

        self._ssm = client or boto3.client("ssm")
        self._prefix = prefix

    def get(self, name):
        try:
            return self._ssm.get_parameter(Name=self._prefix + name, WithDecryption=True)["Parameter"]["Value"]
        except self._ssm.exceptions.ParameterNotFound:
            return None

    def put(self, name, value):
        self._ssm.put_parameter(Name=self._prefix + name, Value=value, Type="SecureString", Overwrite=True)

    def delete(self, name):
        try:
            self._ssm.delete_parameter(Name=self._prefix + name)
        except self._ssm.exceptions.ParameterNotFound:
            pass
