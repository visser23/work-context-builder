"""SharePoint browser-mode discovery: full enumerate-and-diff, retries and safety nets.

Regression context (Oct 2026): the adapter relied on the SharePoint ``GetChanges`` feed,
resolved every change with a ``$select`` field the tenant rejects (HTTP 400), swallowed
the error and moved on — so no new or changed file was ever picked up after the first
sync, folders renamed on the server vanished, and a single connection reset failed the
whole run.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fake_sp_library import BASE, SITE
from workctx.auth.sharepoint import SessionExpiredError
from workctx.config import AuthConfig, SharePointSource
from workctx.models import ChangeAction, SourceObject, SourceType
from workctx.sources import sharepoint_web as sw
from workctx.sources.sharepoint_web import EnumerationIncompleteError, SharePointWebSource
from workctx.state import StateDB


@pytest.fixture
def db(tmp_path):
    state = StateDB(tmp_path / "state.sqlite")
    yield state
    state.close()


def make_source(**overrides) -> SharePointWebSource:
    cfg = {
        "name": "sp",
        "site_url": SITE,
        "mode": "browser",
        "doc_library": "Documents",
        "server_relative_path": BASE,
        "auth": AuthConfig(mode="browser", secret_ref="sp-cookies"),
        "exclude": ["**/~$*", "**/*.mp4"],
    }
    cfg.update(overrides)
    return SharePointWebSource(SharePointSource(**cfg), max_workers=2)


def store(db: StateDB, url: str, *, version: str, error: str | None = None, retries: int = 0):
    db.upsert_object(
        SourceObject(
            source_name="sp",
            source_type=SourceType.SHAREPOINT,
            source_id=url,
            title=url.rsplit("/", 1)[1],
            source_version=version,
            output_path="sharepoint/sp/x.md",
            last_error=error,
            retry_count=retries,
            last_processed_at=datetime.now(UTC),
        )
    )


def by_action(changes):
    out: dict[ChangeAction, list[str]] = {}
    for c in changes:
        out.setdefault(c.action, []).append(c.source_id.removeprefix(BASE + "/"))
    return {k: sorted(v) for k, v in out.items()}


class TestDiscovery:
    def test_first_sync_finds_every_file_in_every_folder(self, sp_library, db):
        sp_library.add("a.docx")
        sp_library.add("4. Remote Care/PHR/plan.docx")
        sp_library.add("4. Remote Care/1. Business cases/case.pdf")
        sp_library.add("deep/er/est/file.xlsx")

        changes = make_source().discover_changes(db, None)

        assert by_action(changes) == {
            ChangeAction.ADD: [
                "4. Remote Care/1. Business cases/case.pdf",
                "4. Remote Care/PHR/plan.docx",
                "a.docx",
                "deep/er/est/file.xlsx",
            ]
        }

    def test_new_folder_appearing_later_is_picked_up(self, sp_library, db):
        """A folder created (or renamed) after the first sync must not be lost."""
        url = sp_library.add("old/keep.docx")
        store(db, url, version="2026-10-01T10:00:00Z")
        sp_library.add("4. Remote Care/PHR/new.docx")

        changes = make_source().discover_changes(db, None)

        assert by_action(changes) == {ChangeAction.ADD: ["4. Remote Care/PHR/new.docx"]}

    def test_modified_file_is_updated_unchanged_file_is_not(self, sp_library, db):
        same = sp_library.add("same.docx", modified="2026-10-01T10:00:00Z")
        edited = sp_library.add("edited.docx", modified="2026-10-14T09:00:00Z")
        store(db, same, version="2026-10-01T10:00:00Z")
        store(db, edited, version="2026-09-01T10:00:00Z")

        changes = make_source().discover_changes(db, None)

        assert by_action(changes) == {ChangeAction.UPDATE: ["edited.docx"]}

    def test_output_location_is_relative_to_configured_folder(self, sp_library, db):
        sp_library.add("3. Pilot/plan.docx")

        (change,) = make_source().discover_changes(db, None)

        assert change.source_id == f"{BASE}/3. Pilot/plan.docx"
        assert change.metadata["relative_path"] == "3. Pilot/plan.docx"

    def test_excluded_and_unconvertible_files_are_skipped_with_a_reason(self, sp_library, db):
        sp_library.add("keep.docx")
        sp_library.add("clip.mp4")
        sp_library.add("photo.jpg")
        sp_library.add("~$lock.docx")

        changes = make_source().discover_changes(db, None)

        assert by_action(changes) == {ChangeAction.ADD: ["keep.docx"]}

    def test_names_with_ampersands_and_quotes_are_listed(self, sp_library, db):
        sp_library.add("R&D (Q3)/Bob's plan & notes.docx")

        changes = make_source().discover_changes(db, None)

        assert by_action(changes) == {ChangeAction.ADD: ["R&D (Q3)/Bob's plan & notes.docx"]}


class TestDeletions:
    def test_file_removed_on_server_is_deleted(self, sp_library, db):
        keep = sp_library.add("keep.docx")
        gone = f"{BASE}/gone.docx"
        store(db, keep, version="2026-10-01T10:00:00Z")
        store(db, gone, version="2026-10-01T10:00:00Z")

        changes = make_source().discover_changes(db, None)

        assert by_action(changes) == {ChangeAction.DELETE: ["gone.docx"]}

    def test_unreadable_folder_suppresses_deletions_but_not_other_work(self, sp_library, db):
        keep = sp_library.add("open/keep.docx")
        sp_library.add("open/new.docx")
        sp_library.add("locked/secret.docx")
        sp_library.forbidden.add(f"{BASE}/locked")
        store(db, keep, version="2026-10-01T10:00:00Z")
        store(db, f"{BASE}/locked/secret.docx", version="2026-10-01T10:00:00Z")
        store(db, f"{BASE}/open/removed.docx", version="2026-10-01T10:00:00Z")

        changes = make_source().discover_changes(db, None)

        assert by_action(changes) == {ChangeAction.ADD: ["open/new.docx"]}

    def test_empty_listing_with_stored_files_is_refused(self, sp_library, db):
        store(db, f"{BASE}/a.docx", version="v")

        with pytest.raises(EnumerationIncompleteError, match="mass deletion"):
            make_source().discover_changes(db, None)

    def test_mass_deletion_is_refused_unless_full(self, sp_library, db):
        sp_library.add("survivor.docx")
        for i in range(sw.MASS_DELETE_MIN + 10):
            store(db, f"{BASE}/lost/{i}.docx", version="v")

        refused = make_source().discover_changes(db, None)
        assert [c.action for c in refused] == [ChangeAction.ADD]  # survivor only, no deletes

        forced = make_source().discover_changes(db, None, full=True)
        deleted = [c for c in forced if c.action == ChangeAction.DELETE]
        assert len(deleted) == sw.MASS_DELETE_MIN + 10

    def test_small_cleanup_is_allowed(self, sp_library, db):
        for i in range(30):
            sp_library.add(f"f{i}.docx")
            store(db, f"{BASE}/f{i}.docx", version="2026-10-01T10:00:00Z")
        for i in range(5):
            store(db, f"{BASE}/old{i}.docx", version="v")

        changes = make_source().discover_changes(db, None)

        assert len([c for c in changes if c.action == ChangeAction.DELETE]) == 5


class TestStubRetries:
    def test_failed_conversion_retried_a_limited_number_of_runs(self, sp_library, db):
        url = sp_library.add("scan.pdf", modified="2026-10-01T10:00:00Z")
        store(db, url, version="2026-10-01T10:00:00Z", error="stub:conversion_failed", retries=1)
        assert len(make_source().discover_changes(db, None)) == 1

        store(
            db,
            url,
            version="2026-10-01T10:00:00Z",
            error="stub:conversion_failed",
            retries=sw.MAX_STUB_RETRIES,
        )
        assert make_source().discover_changes(db, None) == []

    def test_gave_up_file_is_retried_when_it_changes(self, sp_library, db):
        url = sp_library.add("scan.pdf", modified="2026-10-20T10:00:00Z")
        store(
            db,
            url,
            version="2026-10-01T10:00:00Z",
            error="stub:conversion_failed",
            retries=sw.MAX_STUB_RETRIES,
        )

        assert by_action(make_source().discover_changes(db, None)) == {
            ChangeAction.UPDATE: ["scan.pdf"]
        }


class TestResilience:
    def test_connection_reset_is_retried(self, sp_library, db):
        """The Oct 14 failure: one ECONNRESET used to fail the whole source."""
        sp_library.add("a/one.docx")
        sp_library.fail("/a')/Files", "reset", "reset")

        changes = make_source().discover_changes(db, None)

        assert by_action(changes) == {ChangeAction.ADD: ["a/one.docx"]}

    def test_stored_stub_for_a_file_type_now_skipped_is_removed(self, sp_library, db):
        url = sp_library.add("evidence.emf", b"\x01\x00")
        sp_library.add("keep.docx")
        db.upsert_object(
            SourceObject(
                source_name="sp",
                source_type=SourceType.SHAREPOINT,
                source_id=url,
                title="evidence",
                output_path="sharepoint/sp/evidence.emf.md",
                source_version="2026-10-01T10:00:00Z",
                last_error="stub:conversion_failed",
            )
        )

        changes = make_source().discover_changes(db, None)

        assert by_action(changes)[ChangeAction.DELETE] == ["evidence.emf"]

    def test_throttling_and_server_errors_are_retried(self, sp_library, db):
        sp_library.add("one.docx")
        sp_library.fail("/Files", 429, 503)

        assert by_action(make_source().discover_changes(db, None)) == {
            ChangeAction.ADD: ["one.docx"]
        }

    def test_persistent_failure_raises_instead_of_returning_a_partial_listing(self, sp_library, db):
        sp_library.add("a/one.docx")
        sp_library.add("b/two.docx")
        sp_library.fail("/b')/Files", *([503] * 20))

        with pytest.raises(EnumerationIncompleteError, match="incomplete"):
            make_source().discover_changes(db, None)

    def test_configured_root_not_readable_is_an_error(self, sp_library, db):
        source = make_source(server_relative_path=f"{BASE}/Nope")

        with pytest.raises(EnumerationIncompleteError, match="configured root"):
            source.discover_changes(db, None)

    def test_expired_session_raises_session_error_not_empty_result(
        self, sp_library, db, monkeypatch
    ):
        sp_library.add("one.docx")
        sp_library.auth_ok = False
        monkeypatch.setattr(sw, "cookie_status", lambda *a, **k: sw.CookieStatus.REJECTED)
        monkeypatch.setattr(sw, "keepalive_and_extract", lambda *a, **k: {"rtFa": "n"})

        with pytest.raises(SessionExpiredError):
            make_source().discover_changes(db, None)

    def test_session_refreshed_once_when_rejected_mid_run(self, sp_library, db, monkeypatch):
        sp_library.add("one.docx")
        sp_library.fail("/Files", 401)
        refreshed = []
        monkeypatch.setattr(
            sw, "keepalive_and_extract", lambda *a, **k: refreshed.append(1) or {"rtFa": "n"}
        )
        monkeypatch.setattr(sw, "cookie_status", lambda *a, **k: sw.CookieStatus.REJECTED)
        monkeypatch.setattr(sw, "load_cookies", lambda ref: None)

        changes = make_source().discover_changes(db, None)

        assert refreshed
        assert by_action(changes) == {ChangeAction.ADD: ["one.docx"]}

    def test_401_while_the_probe_says_valid_is_retried_without_refreshing(
        self, sp_library, db, monkeypatch
    ):
        sp_library.add("one.docx")
        sp_library.fail("/Files", 401, 401, 403)

        def boom(*a, **k):
            raise AssertionError("a valid session must never trigger a browser refresh")

        monkeypatch.setattr(sw, "keepalive_and_extract", boom)
        source = make_source()

        changes = source.discover_changes(db, None)

        assert by_action(changes) == {ChangeAction.ADD: ["one.docx"]}
        assert source._session_refreshes == 0

    def test_persistent_401_with_valid_session_fails_the_source_not_the_login(
        self, sp_library, db, monkeypatch
    ):
        sp_library.add("one.docx")
        sp_library.fail("/Files", *([401] * 30))
        monkeypatch.setattr(
            sw, "keepalive_and_extract", lambda *a, **k: pytest.fail("no refresh expected")
        )

        with pytest.raises(EnumerationIncompleteError):
            make_source().discover_changes(db, None)

    def test_unreachable_probe_keeps_cached_cookies_and_never_opens_a_browser(
        self, sp_library, monkeypatch
    ):
        monkeypatch.setattr(sw, "cookie_status", lambda *a, **k: sw.CookieStatus.UNREACHABLE)

        def boom(*a, **k):
            raise AssertionError("browser refresh must not run for a network blip")

        monkeypatch.setattr(sw, "keepalive_and_extract", boom)

        assert make_source()._get_cookies() == {"rtFa": "rt", "FedAuth": "fa"}

    def test_failed_browser_refresh_names_the_real_cause(self, sp_library, monkeypatch):
        monkeypatch.setattr(sw, "load_cookies", lambda ref: None)

        def crash(*a, **k):
            raise OSError("profile locked by another browser")

        monkeypatch.setattr(sw, "keepalive_and_extract", crash)

        with pytest.raises(SessionExpiredError, match="profile locked"):
            make_source()._get_cookies()


class TestCurrentIds:
    def test_returns_every_server_file(self, sp_library):
        a = sp_library.add("a.docx")
        b = sp_library.add("x/b.docx")

        assert make_source().get_current_ids() == {a, b}

    def test_reuses_the_listing_from_discovery(self, sp_library, db):
        sp_library.add("a.docx")
        source = make_source()
        source.discover_changes(db, None)
        calls = len(sp_library.requests)

        source.get_current_ids()

        assert len(sp_library.requests) == calls

    def test_raises_when_part_of_the_tree_is_unreadable(self, sp_library):
        sp_library.add("open/a.docx")
        sp_library.add("locked/b.docx")
        sp_library.forbidden.add(f"{BASE}/locked")

        with pytest.raises(EnumerationIncompleteError):
            make_source().get_current_ids()


class TestDownload:
    def test_downloads_file(self, sp_library):
        url = sp_library.add("doc.docx", content=b"payload")

        path = make_source().download_file(url)

        assert path is not None and path.read_bytes() == b"payload"
        path.unlink()

    def test_retries_reset_and_throttle(self, sp_library):
        url = sp_library.add("doc.docx", content=b"payload")
        sp_library.fail("GetFileByServerRelativeUrl", "reset", 429, 503)

        path = make_source().download_file(url)

        assert path is not None and path.read_bytes() == b"payload"
        path.unlink()

    def test_missing_file_returns_none(self, sp_library):
        assert make_source().download_file(f"{BASE}/nope.docx") is None

    def test_gives_up_after_repeated_failures(self, sp_library):
        url = sp_library.add("doc.docx")
        sp_library.fail("GetFileByServerRelativeUrl", *([503] * 30))

        assert make_source().download_file(url) is None


class TestRelativePath:
    def test_strips_configured_folder(self):
        source = make_source()
        assert source.relative_path(f"{BASE}/a/b.docx") == "a/b.docx"

    def test_case_insensitive_prefix(self):
        source = make_source()
        assert source.relative_path(BASE.upper() + "/a.docx") == "a.docx"

    def test_foreign_path_keeps_full_path_without_leading_slash(self):
        source = make_source()
        assert source.relative_path("/sites/other/Docs/a.docx") == "sites/other/Docs/a.docx"
