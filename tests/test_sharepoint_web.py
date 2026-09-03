"""Tests for SharePoint browser-mode adapter and cookie auth."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from workctx.auth.sharepoint import (
    _extract_sp_cookies,
    _is_login_redirect,
    _persist_cookies,
    load_cookies,
)
from workctx.config import AuthConfig, SharePointSource
from workctx.sources.sharepoint_web import SharePointWebSource, _parse_sp_datetime


class TestLoginRedirectDetection:
    def test_microsoft_online(self):
        assert _is_login_redirect("https://login.microsoftonline.com/common/oauth2")

    def test_adfs(self):
        assert _is_login_redirect("https://adfs.company.com/adfs/ls/?client-request-id=abc")

    def test_normal_sharepoint_url(self):
        assert not _is_login_redirect("https://company.sharepoint.com/sites/team")

    def test_login_live(self):
        assert _is_login_redirect("https://login.live.com/oauth")


class TestCookieExtraction:
    def test_extract_sp_cookies(self):
        mock_ctx = MagicMock()
        mock_ctx.cookies.return_value = [
            {"name": "rtFa", "value": "abc123", "domain": ".sharepoint.com"},
            {"name": "FedAuth", "value": "def456", "domain": ".sharepoint.com"},
            {"name": "other", "value": "xyz", "domain": ".sharepoint.com"},
        ]
        result = _extract_sp_cookies(mock_ctx, "https://company.sharepoint.com")
        assert result == {"rtFa": "abc123", "FedAuth": "def456"}

    def test_extract_missing_cookies(self):
        mock_ctx = MagicMock()
        mock_ctx.cookies.return_value = [
            {"name": "other", "value": "xyz", "domain": ".sharepoint.com"},
        ]
        result = _extract_sp_cookies(mock_ctx, "https://company.sharepoint.com")
        assert result == {}


class TestCookiePersistence:
    def test_persist_and_load(self):
        with (
            patch("workctx.auth.sharepoint.set_secret") as mock_set,
            patch("workctx.auth.sharepoint.get_secret") as mock_get,
        ):
            cookies = {"rtFa": "abc", "FedAuth": "def"}
            _persist_cookies("test-ref", cookies, "https://sp.com")

            stored = mock_set.call_args[0][1]
            data = json.loads(stored)
            assert data["cookies"] == cookies
            assert data["site_url"] == "https://sp.com"

            mock_get.return_value = stored
            loaded = load_cookies("test-ref")
            assert loaded == cookies

    def test_load_missing(self):
        with patch("workctx.auth.sharepoint.get_secret", return_value=None):
            assert load_cookies("missing") is None

    def test_load_invalid_json(self):
        with patch("workctx.auth.sharepoint.get_secret", return_value="not-json"):
            assert load_cookies("bad") is None

    def test_load_incomplete_cookies(self):
        data = json.dumps({"cookies": {"rtFa": "abc"}})
        with patch("workctx.auth.sharepoint.get_secret", return_value=data):
            assert load_cookies("partial") is None


class TestSharePointWebSourceConfig:
    def _make_source(self, **overrides) -> SharePointWebSource:
        defaults = {
            "name": "test-sp",
            "site_url": "https://company.sharepoint.com/sites/team",
            "mode": "browser",
            "doc_library": "Shared Documents",
            "auth": AuthConfig(mode="browser", secret_ref="sp-cookies"),
        }
        defaults.update(overrides)
        config = SharePointSource(**defaults)
        return SharePointWebSource(config)

    def test_validate_ok(self):
        source = self._make_source()
        assert source.validate() == []

    def test_validate_missing_site_url(self):
        source = self._make_source(site_url=None)
        issues = source.validate()
        assert any("site_url" in i for i in issues)

    def test_validate_missing_secret_ref(self):
        source = self._make_source(auth=AuthConfig(mode="browser"))
        issues = source.validate()
        assert any("secret_ref" in i for i in issues)

    def test_name_property(self):
        source = self._make_source()
        assert source.name == "test-sp"


class TestSPDateTimeParsing:
    def test_iso_format(self):
        dt = _parse_sp_datetime("2026-09-01T10:30:00Z")
        assert dt is not None
        assert dt.year == 2026

    def test_empty(self):
        assert _parse_sp_datetime("") is None
        assert _parse_sp_datetime(None) is None

    def test_invalid(self):
        assert _parse_sp_datetime("not-a-date") is None


class TestResolveDeletion:
    """_resolve_deletion should use exact sp_item_id lookup, not substring matching."""

    def _make_source(self) -> SharePointWebSource:
        config = SharePointSource(
            name="test-sp",
            site_url="https://sp.com/sites/team",
            mode="browser",
            doc_library="Docs",
            auth=AuthConfig(mode="browser", secret_ref="sp-cookies"),
        )
        return SharePointWebSource(config)

    def test_finds_object_by_sp_item_id(self, tmp_path):
        from workctx.models import SourceObject, SourceType
        from workctx.state import StateDB

        db = StateDB(tmp_path / "test.sqlite")
        obj = SourceObject(
            source_name="test-sp",
            source_type=SourceType.SHAREPOINT,
            source_id="/sites/team/Docs/report.docx",
            title="Report",
            sp_item_id=42,
        )
        db.upsert_object(obj)

        source = self._make_source()
        change = source._resolve_deletion(42, db)
        assert change is not None
        assert change.source_id == "/sites/team/Docs/report.docx"
        assert change.action.value == "delete"
        db.close()

    def test_returns_none_for_unknown_item_id(self, tmp_path):
        from workctx.state import StateDB

        db = StateDB(tmp_path / "test.sqlite")
        source = self._make_source()
        change = source._resolve_deletion(99999, db)
        assert change is None
        db.close()

    def test_no_false_positive_substring_match(self, tmp_path):
        """Item ID 42 must NOT match a GUID like 'a9f3e842-...' (old bug)."""
        from workctx.models import SourceObject, SourceType
        from workctx.state import StateDB

        db = StateDB(tmp_path / "test.sqlite")
        obj = SourceObject(
            source_name="test-sp",
            source_type=SourceType.SHAREPOINT,
            source_id="/sites/team/Docs/other.docx",
            source_key="a9f3e842-1234-5678-abcd-000000000000",
            title="Other",
            sp_item_id=999,
        )
        db.upsert_object(obj)

        source = self._make_source()
        change = source._resolve_deletion(42, db)
        assert change is None
        db.close()


class TestSharePointWebSourceExclude:
    def _make_source(self) -> SharePointWebSource:
        config = SharePointSource(
            name="test",
            site_url="https://sp.com/sites/team",
            mode="browser",
            doc_library="Docs",
            exclude=["**/~$*", "**/.DS_Store", "**/*.tmp"],
            auth=AuthConfig(mode="browser", secret_ref="sp-cookies"),
        )
        return SharePointWebSource(config)

    def test_excludes_temp_files(self):
        source = self._make_source()
        assert source._should_exclude("~$document.docx", "/sites/team/~$document.docx")

    def test_excludes_ds_store(self):
        source = self._make_source()
        assert source._should_exclude(".DS_Store", "/sites/team/.DS_Store")

    def test_allows_normal_files(self):
        source = self._make_source()
        assert not source._should_exclude("report.docx", "/sites/team/report.docx")
