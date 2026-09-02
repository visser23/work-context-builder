"""Tests for secret management."""

from workctx.secrets import _ref_to_env_key, get_secret


def test_ref_to_env_key():
    assert _ref_to_env_key("workctx/project/jira") == "WORKCTX_PROJECT_JIRA"
    assert _ref_to_env_key("workctx/my-project/confluence") == "WORKCTX_MY_PROJECT_CONFLUENCE"


def test_get_secret_from_env(monkeypatch):
    monkeypatch.setenv("WORKCTX_TEST_SECRET", "my-token")
    result = get_secret("workctx/test/secret")
    assert result == "my-token"


def test_get_secret_missing():
    result = get_secret("workctx/nonexistent/secret")
    assert result is None
