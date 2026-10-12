"""template.yaml 内のCloudFront Function（Basic認証）を、取り出してNodeで実行して検証する。"""
import base64
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from xronix.lambda_handlers import basic_hash

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="nodeが無い")


def function_code(hash_value: str) -> str:
    text = (Path(__file__).parent.parent / "template.yaml").read_text(encoding="utf-8")
    m = re.search(r"FunctionCode: !Sub \|\n(.*?)\n\n  Distribution:", text, re.S)
    return textwrap.dedent(m.group(1)).replace("${BasicAuthHash}", hash_value)


def run(auth_header, hash_value):
    harness = function_code(hash_value) + "\nconst h = " + json.dumps({} if auth_header is None else {"authorization": {"value": auth_header}}) + \
        ";\nconsole.log(JSON.stringify(handler({request: {uri: '/', headers: h}})));"
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def b64(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_accepts_correct_credentials_including_japanese():
    for user, pw in (("admin", "s3cret-長いパスワード"), ("u", "p")):
        out = run(b64(user, pw), basic_hash(user, pw))
        assert out.get("uri") == "/"          # リクエストがそのまま通る


@pytest.mark.parametrize("header", [None, "Bearer abc", "Basic !!!", b64("admin", "wrong"), b64("other", "s3cret")])
def test_rejects_everything_else(header):
    out = run(header, basic_hash("admin", "s3cret"))
    assert out["statusCode"] == 401 and "www-authenticate" in out["headers"]
