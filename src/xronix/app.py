"""サービスの組み立て。ローカル(SQLite＋ファイル)とAWS(DynamoDB＋SSM)を環境変数で切り替える。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .ai import AIService
from .common import Logger, utcnow
from .http import Http, UrllibHttp
from .jobs import JobRunner
from .notify import Notifier
from .sender import Sender
from .settings import Settings
from .templates import Templates
from .tokens import TokenManager
from .xclient import LiveX, MockX


@dataclass
class App:
    repo: object
    secrets: object
    settings: Settings
    x: object
    http: Http
    logger: Logger
    templates: Templates
    tokens: TokenManager
    ai: AIService
    notifier: Notifier
    jobs: JobRunner
    sender: Sender
    now: object = utcnow

    @property
    def mock(self) -> bool:
        return getattr(self.x, "mode", "") == "mock"


def build(repo, secrets, x, http: Http | None = None, now=utcnow, sleep=None) -> App:
    http = http or UrllibHttp()
    settings = Settings(repo)
    logger = Logger(repo, now)
    templates = Templates(repo, now)
    templates.seed_if_empty()
    kw = {"sleep": sleep} if sleep else {}
    tokens = TokenManager(repo, secrets, settings, x, logger, now, **kw)
    ai = AIService(settings, secrets, http, logger)
    notifier = Notifier(settings, secrets, http, logger)
    jobs = JobRunner(repo, settings, tokens, x, ai, templates, notifier, logger, now)
    sender = Sender(repo, settings, tokens, x, templates, logger, now)
    return App(repo, secrets, settings, x, http, logger, templates, tokens, ai, notifier, jobs, sender, now)


def build_local(data_dir: str = ".xronix", mode: str | None = None) -> App:
    from .repo import SqliteRepo
    from .secrets import FileSecrets

    os.makedirs(data_dir, exist_ok=True)
    mode = mode or os.environ.get("XRONIX_X_MODE", "mock")
    http = UrllibHttp()
    x = MockX() if mode == "mock" else LiveX(http)
    return build(SqliteRepo(os.path.join(data_dir, "xronix.db")), FileSecrets(os.path.join(data_dir, "secrets.json")), x, http)


def build_aws() -> App:
    from .repo import DynamoRepo
    from .secrets import SsmSecrets

    http = UrllibHttp()
    return build(DynamoRepo(os.environ["XRONIX_TABLE"]), SsmSecrets(os.environ.get("XRONIX_SSM_PREFIX", "/xronix/")), LiveX(http), http)
