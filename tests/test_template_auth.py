import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
T = (ROOT / "template.yaml").read_text(encoding="utf-8")


def test_basic_auth_is_gone():
    assert "BasicAuth" not in T and "BASIC_HASH" not in T and "www-authenticate" not in T.lower()
    assert "FunctionAssociations" not in T


def test_login_uses_cognito_admin_only_with_email_recovery():
    assert "AllowAdminCreateUserOnly: true" in T
    assert "UsernameAttributes: [email]" in T
    assert "verified_email" in T and "CONFIRM_WITH_CODE" in T
    assert "PreventUserExistenceErrors: ENABLED" in T
    assert "GenerateSecret: false" in T
    assert "MinimumLength: 12" in T


def test_api_requires_jwt_except_two_public_routes():
    assert "DefaultAuthorizer: Jwt" in T
    assert "issuer: !Sub https://cognito-idp.${AWS::Region}.amazonaws.com/${UserPool}" in T
    assert "audience:\n                - !Ref UserPoolClient" in T
    public = re.findall(r"Path: (/api/[\w/{}+-]+), Method: GET, Auth: \{Authorizer: NONE\}", T)
    assert sorted(public) == ["/api/auth-config", "/api/x/callback"]
    assert "Authorizer: NONE" not in T.replace("Auth: {Authorizer: NONE}", "")  # それ以外に認証なしの経路は作らない


def test_admin_email_is_a_parameter_not_a_literal():
    assert re.search(r"AdminEmail:\n    Type: String", T)
    assert not re.search(r"[\w.+-]+@(?!example\.com)[\w-]+\.[\w.]+", re.sub(r"\[\^@[^\n]*", "", T))  # 実在のメールアドレスを書かない


def test_deploy_workflow_does_not_leak_or_skip_email():
    wf = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "ADMIN_EMAIL" in wf and "BASIC_AUTH" not in wf
    assert re.search(r"AdminEmail=\$ADMIN_EMAIL", wf)
