"""Tests for the Teams transcripts source, against a fake SharePoint tenant.

No network, no browser, no real credentials: ``httpx`` is wired to an in-memory
``MockTransport`` (see ``conftest.tenant``) and cookie acquisition is stubbed.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.fake_sharepoint import (
    LIST_ID,
    ME,
    MY,
    OTHER_PERSONAL,
    PERSONAL,
    SITE_ID,
    TEAM,
    WEB_ID,
    make_source,
    uid,
)
from workctx.auth.sharepoint import (
    SessionExpiredError,
    get_valid_cookies,
    onedrive_secret_ref,
    tenant_hosts,
)
from workctx.config import AuthConfig, TranscriptsSource
from workctx.models import ChangeAction, SourceObject, SourceType
from workctx.sources import teams_transcripts as tt
from workctx.sources.teams_transcripts import RecordingHit, TeamsTranscriptSource, _drive_id
from workctx.state import StateDB


@pytest.fixture
def db(tmp_path):
    database = StateDB(tmp_path / "state.sqlite")
    yield database
    database.close()


# ----------------------------------------------------------------------- helpers
class TestHelpers:
    def test_tenant_hosts(self):
        assert tenant_hosts("https://contoso.sharepoint.com/sites/x") == (TEAM, MY)
        assert tenant_hosts("https://contoso-my.sharepoint.com/personal/a_b") == (TEAM, MY)
        assert tenant_hosts("https://Contoso.SharePoint.us") == (
            "https://contoso.sharepoint.us",
            "https://contoso-my.sharepoint.us",
        )

    @pytest.mark.parametrize("url", ["", "https://example.com/x", "not a url", "https://localhost"])
    def test_tenant_hosts_rejects_non_sharepoint(self, url):
        with pytest.raises(ValueError):
            tenant_hosts(url)

    def test_onedrive_secret_ref(self):
        assert onedrive_secret_ref("abc") == "abc-my"

    def test_drive_id_matches_graph_format(self):
        drive = _drive_id(SITE_ID, WEB_ID, LIST_ID)
        assert drive and drive.startswith("b!")
        raw = base64.urlsafe_b64decode(drive[2:] + "=" * (-len(drive[2:]) % 4))
        assert len(raw) == 48
        assert uuid.UUID(bytes_le=raw[:16]) == uuid.UUID(SITE_ID)
        assert uuid.UUID(bytes_le=raw[16:32]) == uuid.UUID(WEB_ID)
        assert uuid.UUID(bytes_le=raw[32:]) == uuid.UUID(LIST_ID)

    def test_drive_id_invalid_guids(self):
        assert _drive_id("x", "y", "z") is None
        assert _drive_id("", "", "") is None

    def test_recording_hit_api_bases_fall_back_to_default_drive(self):
        hit = RecordingHit(uid(1), "T", PERSONAL, "bad", WEB_ID, LIST_ID, None, None)
        assert hit.api_bases() == [f"{PERSONAL}/_api/v2.1/drive/items/{uid(1)}"]

    def test_get_valid_cookies_uses_cache_then_refreshes(self, monkeypatch):
        import workctx.auth.sharepoint as auth

        monkeypatch.setattr(auth, "load_cookies", lambda ref: {"rtFa": "a", "FedAuth": "b"})
        monkeypatch.setattr(auth, "_http_test_cookies", lambda url, c: True)
        assert get_valid_cookies(MY, "p", "ref") == {"rtFa": "a", "FedAuth": "b"}

        refreshed = {"rtFa": "new", "FedAuth": "new"}
        monkeypatch.setattr(auth, "_http_test_cookies", lambda url, c: False)
        monkeypatch.setattr(auth, "keepalive_and_extract", lambda *a, **k: refreshed)
        assert get_valid_cookies(MY, "p", "ref") == refreshed

        monkeypatch.setattr(auth, "load_cookies", lambda ref: None)
        assert get_valid_cookies(MY, "p", "ref") == refreshed


# -------------------------------------------------------------------- validation
class TestValidation:
    def test_valid_reused_login(self):
        assert make_source().validate() == []

    def test_unknown_sharepoint_source(self):
        cfg = TranscriptsSource(name="t", sharepoint_source="missing")
        src = TeamsTranscriptSource(cfg, sharepoint_sources={})
        assert any("not found" in i for i in src.validate())

    def test_standalone_mode(self):
        cfg = TranscriptsSource(
            name="t", site_url=f"{TEAM}/sites/x", auth=AuthConfig(mode="browser", secret_ref="r")
        )
        src = TeamsTranscriptSource(cfg)
        assert src.validate() == []
        assert src.name == "t"
        assert src.source_type == SourceType.TRANSCRIPT

    def test_non_sharepoint_url(self):
        cfg = TranscriptsSource(
            name="t",
            site_url="https://example.com",
            auth=AuthConfig(mode="browser", secret_ref="r"),
        )
        assert TeamsTranscriptSource(cfg).validate()

    def test_site_on_other_tenant_flagged(self):
        src = make_source(sites=["https://fabrikam.sharepoint.com/sites/x"])
        assert any("not on this SharePoint tenant" in i for i in src.validate())

    def test_nothing_enabled(self):
        src = make_source(include_own=False, include_shared=False)
        assert any("nothing to sync" in i for i in src.validate())

    def test_discover_with_bad_config_raises(self, db):
        cfg = TranscriptsSource(name="t", sharepoint_source="missing")
        src = TeamsTranscriptSource(cfg, sharepoint_sources={})
        with pytest.raises(RuntimeError, match="not found"):
            src.discover_changes(db, None)


# --------------------------------------------------------------------- discovery
class TestDiscovery:
    def test_finds_own_and_shared_transcripts(self, tenant, db):
        tenant.add(uid(1), "Weekly sync")
        tenant.add(uid(2), "Customer call", web=OTHER_PERSONAL, scope="shared")
        src = make_source()

        changes = {c.source_id: c for c in src.discover_changes(db, None)}

        assert set(changes) == {uid(1), uid(2)}
        c = changes[uid(1)]
        assert c.action == ChangeAction.ADD
        assert c.title == "Weekly sync"
        assert c.source_version == f"ctag-{uid(1)[:4]}-1"
        assert c.source_updated_at == datetime(2026, 6, 25, 10, 32, 9, tzinfo=UTC)
        assert c.metadata["meeting_date"] == "2026-06-25"
        assert c.metadata["start_time"] == "10:16"
        assert c.metadata["front_matter"] == {"meeting_date": "2026-06-25"}
        assert c.metadata["recorded_by"] == "Ada Example"
        assert c.source_url.startswith(PERSONAL)
        assert "/_api/v2.1/drives/b!" in c.metadata["item_api"]

    def test_search_queries_scoped_to_identity(self, tenant, db):
        tenant.add(uid(1), "Mine")
        make_source().discover_changes(db, None)
        joined = "\n".join(tenant.search_queries)
        assert f'path:"{PERSONAL}"' in joined
        assert f'SharedWithUsersOWSUSER:"{ME}"' in joined
        assert all(q.startswith("ProgId:Media.Meeting") for q in tenant.search_queries)

    def test_scope_flags_and_extra_sites(self, tenant, db):
        tenant.add(uid(1), "Mine")
        tenant.add(uid(2), "Channel", web=f"{TEAM}/sites/Proj", scope="site")
        src = make_source(include_own=False, include_shared=False, sites=[f"{TEAM}/sites/Proj/"])
        changes = src.discover_changes(db, None)
        assert [c.source_id for c in changes] == [uid(2)]
        assert len(tenant.search_queries) == 1

    def test_duplicate_hits_across_queries_deduplicated(self, tenant, db):
        # Same recording appears for both the "own" and "shared" queries.
        tenant.add(uid(1), "Both")
        tenant.recordings[uid(1)]["scope"] = "own"
        orig = tenant._search

        def both(qs):
            resp = orig(qs)
            if "SharedWithUsersOWSUSER" in qs["querytext"][0]:
                tenant.recordings[uid(1)]["scope"] = "shared"
                resp = orig(qs)
                tenant.recordings[uid(1)]["scope"] = "own"
            return resp

        tenant._search = both  # type: ignore[method-assign]
        changes = make_source().discover_changes(db, None)
        assert [c.source_id for c in changes] == [uid(1)]

    def test_recordings_without_transcripts_are_skipped(self, tenant, db):
        tenant.add(uid(1), "Has one")
        tenant.add(uid(2), "No transcript", transcripts=[])
        changes = make_source().discover_changes(db, None)
        assert [c.source_id for c in changes] == [uid(1)]

    @pytest.mark.parametrize("status", [403, 404, 410, 423])
    def test_inaccessible_items_skipped_quietly(self, tenant, db, status, caplog):
        tenant.add(uid(1), "OK")
        tenant.add(uid(2), "Locked owner", status=status)
        with caplog.at_level("WARNING"):
            changes = make_source().discover_changes(db, None)
        assert [c.source_id for c in changes] == [uid(1)]
        assert "failed" not in caplog.text

    def test_transient_probe_failure_is_isolated(self, tenant, db, caplog):
        tenant.add(uid(1), "OK")
        tenant.add(uid(2), "Server error", status=500)
        with caplog.at_level("WARNING"):
            changes = make_source().discover_changes(db, None)
        assert [c.source_id for c in changes] == [uid(1)]
        assert "probing" in caplog.text

    def test_all_probes_failing_is_an_error(self, tenant, db):
        tenant.add(uid(1), "A", status=500)
        tenant.add(uid(2), "B", status=500)
        with pytest.raises(RuntimeError, match="all 2 recording checks failed"):
            make_source().discover_changes(db, None)

    def test_since_days_filter(self, tenant, db):
        now = datetime.now(UTC)
        tenant.add(uid(1), "Recent", modified=now - timedelta(days=2))
        tenant.add(uid(2), "Old", modified=now - timedelta(days=90))
        changes = make_source(since_days=30).discover_changes(db, None)
        assert [c.source_id for c in changes] == [uid(1)]

    def test_exclude_titles_case_insensitive_glob(self, tenant, db):
        tenant.add(uid(1), "Team standup")
        tenant.add(uid(2), "1:1 with manager")
        tenant.add(uid(3), "Private - HR chat")
        changes = make_source(exclude_titles=["1:1*", "PRIVATE*"]).discover_changes(db, None)
        assert [c.source_id for c in changes] == [uid(1)]

    def test_search_paging(self, tenant, db, monkeypatch):
        monkeypatch.setattr(tt, "SEARCH_PAGE_SIZE", 2)
        for n in range(5):
            tenant.add(uid(n), f"Meeting {n}", modified=datetime(2026, 1, 1 + n, tzinfo=UTC))
        changes = make_source(include_shared=False).discover_changes(db, None)
        assert len(changes) == 5

    def test_title_falls_back_to_file_name(self, tenant, db):
        tenant.add(uid(1), "", file_name="Budget review-20260301_090000UTC-Meeting Recording.mp4")
        change = make_source().discover_changes(db, None)[0]
        assert change.title == "Budget review"
        assert change.metadata["start_time_is_utc"] is True

    def test_service_account_not_reported_as_recorder(self, tenant, db):
        tenant.add(uid(1), "T")
        orig = tenant._item

        def item(u, rest, qs):
            resp = orig(u, rest, qs)
            if rest == "":
                data = json.loads(resp.content)
                data["createdBy"] = {"user": {"displayName": "SharePoint App"}}
                return httpx.Response(200, json=data)
            return resp

        tenant._item = item  # type: ignore[method-assign]
        assert make_source().discover_changes(db, None)[0].metadata["recorded_by"] is None

    def test_prefers_default_visible_transcript(self, tenant, db):
        tenant.add(
            uid(1),
            "Multi",
            transcripts=[
                {"id": "t-fr", "cTag": "c1", "isDefault": False, "isVisible": True},
                {"id": "t-en", "cTag": "c2", "isDefault": True, "isVisible": True},
            ],
        )
        change = make_source().discover_changes(db, None)[0]
        assert change.metadata["transcript_id"] == "t-en"

    def test_throttling_is_retried(self, tenant, db):
        tenant.add(uid(1), "Throttled")
        tenant.throttle_once.add(uid(1))
        assert len(make_source().discover_changes(db, None)) == 1

    def test_auth_rejection_raises_session_expired(self, tenant, db):
        tenant.auth_status = 401
        with pytest.raises(SessionExpiredError):
            make_source().discover_changes(db, None)

    def test_login_redirect_raises_session_expired(self, tenant, db):
        tenant.auth_status = 302
        with pytest.raises(SessionExpiredError):
            make_source().discover_changes(db, None)

    def test_search_failure_raises(self, tenant, db):
        tenant.search_status = 500
        with pytest.raises(RuntimeError, match="search failed"):
            make_source().discover_changes(db, None)


# ------------------------------------------------------------ invited scope
THIRD_PERSONAL = f"{MY}/personal/pat_third_contoso_com"
MEMBER_SITE = f"{TEAM}/sites/Project"
READONLY_SITE = f"{TEAM}/sites/AllStaff"
TEAMS_SITE = f"{TEAM}/sites/msteams_abc123"


class TestInvitedScope:
    """``include_invited`` finds every meeting the user can open and was invited to."""

    def _ids(self, src, db):
        return {c.source_id for c in src.discover_changes(db, None)}

    def test_other_peoples_onedrive_recordings_are_included(self, tenant, db):
        tenant.add(uid(1), "Mine", web=PERSONAL, scope="own")
        # Not in "shared with me" search results, but visible => invited.
        tenant.add(uid(2), "Hosted by Sam", web=OTHER_PERSONAL, scope="hidden")
        tenant.add(uid(3), "Hosted by Pat", web=THIRD_PERSONAL, scope="hidden")
        assert self._ids(make_source(include_invited=True), db) == {uid(1), uid(2), uid(3)}

    def test_disabled_keeps_only_own_and_shared(self, tenant, db):
        tenant.add(uid(1), "Mine", web=PERSONAL, scope="own")
        tenant.add(uid(2), "Shared", web=OTHER_PERSONAL, scope="shared")
        tenant.add(uid(3), "Hosted by Pat", web=THIRD_PERSONAL, scope="hidden")
        assert self._ids(make_source(include_invited=False), db) == {uid(1), uid(2)}

    def test_site_recordings_need_membership_or_a_teams_site(self, tenant, db):
        tenant.contributor_webs.add(MEMBER_SITE)
        tenant.add(uid(1), "Project sync", web=MEMBER_SITE, scope="hidden")
        tenant.add(uid(2), "Channel meeting", web=TEAMS_SITE, scope="hidden")
        tenant.add(uid(3), "All staff webinar", web=READONLY_SITE, scope="hidden")
        assert self._ids(make_source(include_invited=True), db) == {uid(1), uid(2)}

    def test_include_all_sites_adds_read_only_sites(self, tenant, db):
        tenant.add(uid(1), "All staff webinar", web=READONLY_SITE, scope="hidden")
        tenant.add(uid(2), "Hosted by Pat", web=THIRD_PERSONAL, scope="hidden")
        src = make_source(include_invited=False, include_all_sites=True)
        # all-sites does not pull in other people's OneDrives
        assert self._ids(src, db) == {uid(1)}
        src = make_source(include_invited=True, include_all_sites=True)
        assert self._ids(src, db) == {uid(1), uid(2)}

    def test_permission_check_inaccessible_site_is_not_invited(self, tenant, db):
        tenant.permission_status = 403
        tenant.add(uid(1), "Locked site", web=MEMBER_SITE, scope="hidden")
        assert self._ids(make_source(include_invited=True), db) == set()

    def test_permission_check_server_error_aborts_instead_of_dropping(self, tenant, db):
        tenant.permission_status = 500
        tenant.add(uid(1), "Project sync", web=MEMBER_SITE, scope="hidden")
        src = make_source(include_invited=True)
        with pytest.raises(RuntimeError, match="permission check failed"):
            src.discover_changes(db, None)
        with pytest.raises(RuntimeError, match="permission check failed"):
            src.get_current_ids()

    def test_current_ids_matches_discovery_scope(self, tenant):
        tenant.add(uid(1), "Hosted by Pat", web=THIRD_PERSONAL, scope="hidden")
        tenant.add(uid(2), "All staff webinar", web=READONLY_SITE, scope="hidden")
        assert make_source(include_invited=True).get_current_ids() == {uid(1)}

    def test_title_filters_still_apply(self, tenant, db):
        tenant.add(uid(1), "Hosted by Pat", web=THIRD_PERSONAL, scope="hidden")
        tenant.add(uid(2), "1:1 with Pat", web=THIRD_PERSONAL, scope="hidden")
        src = make_source(include_invited=True, exclude_titles=["1:1*"])
        assert self._ids(src, db) == {uid(1)}

    def test_default_config_is_invited(self):
        cfg = TranscriptsSource(name="t", sharepoint_source="sp")
        assert cfg.include_invited is True and cfg.include_all_sites is False

    def test_helpers(self):
        assert tt._is_teams_site(f"{TEAM}/sites/msteams_8f21aa")
        assert not tt._is_teams_site(f"{TEAM}/sites/Project")
        src = make_source()
        assert src._is_personal(PERSONAL)
        assert not src._is_personal(f"{TEAM}/personal/x")  # wrong host
        assert not src._is_personal(f"{MY}/sites/x")


# --------------------------------------------------------- incremental behaviour
def stored(source_id: str, *, version: str, updated: datetime, error: str | None = None):
    return SourceObject(
        source_name="teams",
        source_type=SourceType.TRANSCRIPT,
        source_id=source_id,
        source_version=version,
        source_updated_at=updated,
        output_path="transcripts/teams/x.md",
        last_error=error,
    )


class TestIncremental:
    def test_unchanged_transcript_not_returned(self, tenant, db):
        mod = datetime.now(UTC) - timedelta(days=2)
        tenant.add(uid(1), "Same", modified=mod)
        db.upsert_object(stored(uid(1), version=f"ctag-{uid(1)[:4]}-1", updated=mod))
        assert make_source().discover_changes(db, None) == []

    def test_edited_transcript_returned_as_update(self, tenant, db):
        mod = datetime.now(UTC) - timedelta(days=3)
        tenant.add(uid(1), "Edited", modified=mod)
        db.upsert_object(stored(uid(1), version="old-ctag", updated=mod))
        (change,) = make_source().discover_changes(db, None)
        assert change.action == ChangeAction.UPDATE

    def test_previously_failed_object_retried(self, tenant, db):
        mod = datetime.now(UTC) - timedelta(days=60)
        tenant.add(uid(1), "Retry", modified=mod)
        db.upsert_object(
            stored(uid(1), version=f"ctag-{uid(1)[:4]}-1", updated=mod, error="stub:x")
        )
        assert len(make_source().discover_changes(db, None)) == 1

    def test_old_unchanged_recordings_not_even_probed(self, tenant, db):
        mod = datetime.now(UTC) - timedelta(days=100)
        tenant.add(uid(1), "Ancient", modified=mod)
        db.upsert_object(stored(uid(1), version="whatever", updated=mod))
        assert make_source().discover_changes(db, None) == []
        assert not [r for r in tenant.requests if "/media/transcripts" in r.url.path]

    def test_recent_recordings_always_rechecked(self, tenant, db):
        mod = datetime.now(UTC) - timedelta(days=1)
        tenant.add(uid(1), "Fresh", modified=mod)
        db.upsert_object(stored(uid(1), version="stale-ctag", updated=mod))
        assert len(make_source().discover_changes(db, None)) == 1

    def test_full_ignores_known_versions(self, tenant, db):
        mod = datetime.now(UTC) - timedelta(days=100)
        tenant.add(uid(1), "Ancient", modified=mod)
        db.upsert_object(stored(uid(1), version=f"ctag-{uid(1)[:4]}-1", updated=mod))
        (change,) = make_source().discover_changes(db, None, full=True)
        assert change.action == ChangeAction.UPDATE


# ------------------------------------------------------------------ reconciliation
class TestCurrentIds:
    def test_returns_all_visible_ids(self, tenant):
        tenant.add(uid(1), "A")
        tenant.add(uid(2), "B", transcripts=[])
        assert make_source().get_current_ids() == {uid(1), uid(2)}

    def test_never_returns_partial_set_on_failure(self, tenant):
        tenant.add(uid(1), "A")
        tenant.search_status = 503
        with pytest.raises(RuntimeError):
            make_source().get_current_ids()

    def test_title_exclusion_removes_from_current_ids(self, tenant):
        tenant.add(uid(1), "Keep")
        tenant.add(uid(2), "Secret board")
        assert make_source(exclude_titles=["secret*"]).get_current_ids() == {uid(1)}


# ------------------------------------------------------------------------ render
class TestRender:
    def _change(self, tenant, db, **add_kwargs):
        tenant.add(uid(1), "Weekly sync", **add_kwargs)
        src = make_source()
        (change,) = src.discover_changes(db, None)
        return src, change

    def test_renders_markdown_and_front_matter(self, tenant, db):
        src, change = self._change(tenant, db)
        md = src.render_content(change)
        assert md and md.startswith("# Weekly sync")
        assert "[00:00:01] **Ada Example:** Hello team." in md
        assert "[00:00:04] **Bob Example:** Morning." in md
        assert "- **Meeting date**: 2026-06-25, starting 10:16" in md
        assert "- **Speakers**: Ada Example; Bob Example" in md
        assert change.metadata["front_matter"]["participants"] == ["Ada Example", "Bob Example"]
        assert change.metadata["front_matter"]["duration_minutes"] == 30

    def test_falls_back_to_vtt_when_json_unavailable(self, tenant, db):
        src, change = self._change(tenant, db, json_status=406)
        md = src.render_content(change)
        assert "**Vee Fallback:** From vtt" in md

    def test_download_failure_raises(self, tenant, db):
        src, change = self._change(tenant, db)
        tenant.recordings[uid(1)]["json_status"] = 500
        orig = tenant._item

        def item(u, rest, qs):
            if rest.endswith("/streamContent"):
                return httpx.Response(500)
            return orig(u, rest, qs)

        tenant._item = item  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="download failed"):
            src.render_content(change)


# ---------------------------------------------------------------------- security
class TestCredentialSafety:
    def test_cookies_only_sent_to_tenant_hosts(self, tenant, db):
        tenant.add(uid(1), "Mine")
        make_source().discover_changes(db, None)
        assert set(tenant.cookies_seen) <= {TEAM, MY}
        # each host gets its own cookie jar (per-host secret refs)
        assert any("rt-sp-cookies-my" in c for c in tenant.cookies_seen[MY])

    def test_refuses_unrelated_hosts(self):
        src = make_source()
        for url in (
            "https://evil.example.com/x",
            "http://contoso.sharepoint.com/x",
            "https://contoso.sharepoint.com.evil.example/x",
            "file:///etc/passwd",
        ):
            with pytest.raises(ValueError, match="refusing"):
                src._client_for(url)

    def test_hits_with_foreign_web_url_dropped(self, tenant, db):
        tenant.add(uid(1), "Mine")
        tenant.add(uid(2), "Spoofed", web="https://evil.example.com/personal/x")
        changes = make_source().discover_changes(db, None)
        assert [c.source_id for c in changes] == [uid(1)]
        assert not [r for r in tenant.requests if r.url.host == "evil.example.com"]

    def test_query_values_are_escaped(self):
        assert tt._kql_quote("o'brien@contoso.com") == "o''brien@contoso.com"
        assert '"' not in tt._kql_quote('a"b')


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://contoso.sharepoint.com/x", TEAM),
        ("https://CONTOSO.sharepoint.com:443/x", TEAM),
        ("https://user@contoso.sharepoint.com/x", ""),
        ("https://user:pw@contoso.sharepoint.com/x", ""),
        ("https://contoso.sharepoint.com:8443/x", ""),
        ("https://contoso.sharepoint.com:notaport/x", ""),
        ("http://contoso.sharepoint.com/x", ""),
        ("", ""),
    ],
)
def test_host_root_is_strict(url, expected):
    assert TeamsTranscriptSource._host_root(url) == expected
