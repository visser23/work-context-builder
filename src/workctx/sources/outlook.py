"""Outlook mail and calendar source adapters (Outlook REST API, browser-session token).

Both adapters share :class:`OutlookClient`, which calls the Outlook REST API
(``https://outlook.office.com/api/v2.0``) with the access token that Outlook on
the web itself uses (see :mod:`workctx.auth.webtokens`). Everything is read-only:
only ``GET`` requests are made, nothing is moved, flagged or marked as read.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from workctx.auth import webtokens
from workctx.auth.webtokens import WebSessionExpiredError, WebToken
from workctx.config import CalendarSource, MailSource, SharePointSource
from workctx.models import ChangeAction, DiscoveredChange, SourceType, SyncCheckpoint
from workctx.normalise.outlook import (
    day_of,
    display_name,
    format_address,
    limit_chars,
    one_line,
    parse_dt,
    render_event_markdown,
    render_mail_markdown,
    trim_invite_boilerplate,
    trim_quoted_reply,
)
from workctx.sources.base import Source
from workctx.state import StateDB

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 60.0
PAGE_SIZE = 200
MAX_RETRIES = 8
MAX_RETRY_WAIT_SECONDS = 60.0
WELL_KNOWN_FOLDERS = {
    "inbox": "Inbox",
    "sentitems": "Sent Items",
    "drafts": "Drafts",
    "archive": "Archive",
    "deleteditems": "Deleted Items",
    "junkemail": "Junk Email",
}
# ImmutableId keeps a message's id stable when it is moved between folders.
_PREFER = 'outlook.body-content-type="text", IdType="ImmutableId", outlook.timezone="UTC"'

_MAIL_LIST_FIELDS = (
    "Id,Subject,From,Sender,ToRecipients,CcRecipients,ReceivedDateTime,SentDateTime,"
    "HasAttachments,Importance,WebLink,IsDraft"
)
_EVENT_LIST_FIELDS = (
    "Id,Subject,Start,End,IsAllDay,Location,Organizer,Attendees,IsCancelled,ShowAs,Type,"
    "ResponseStatus,OnlineMeeting,OnlineMeetingUrl,Categories,WebLink,ChangeKey,"
    "LastModifiedDateTime"
)


def _short_id(source_id: str) -> str:
    return hashlib.sha1(source_id.encode("utf-8")).hexdigest()[:8]


class OutlookClient:
    """Minimal read-only Outlook REST client with throttling back-off and token refresh."""

    def __init__(
        self,
        *,
        profile: str,
        mailbox_url: str,
        api_base: str,
        token_provider: Callable[[bool], WebToken] | None = None,
    ) -> None:
        parsed = urlparse(api_base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"Outlook api_base must be a plain https URL: {api_base!r}")
        self._api_base = api_base.rstrip("/")
        self._host = parsed.hostname.lower()
        self._profile = profile
        self._mailbox_url = mailbox_url
        self._token_provider = token_provider or self._default_token
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()
        self._token: WebToken | None = None
        self._pause_until = 0.0  # shared back-off: one 429 slows every worker thread

    def _default_token(self, force: bool) -> WebToken:
        return webtokens.get_outlook_token(self._profile, self._mailbox_url, force=force)

    # -- plumbing ----------------------------------------------------------------

    def _http(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _bearer(self, force: bool = False) -> str:
        with self._lock:
            if force or self._token is None or not self._token.valid_for(60):
                self._token = self._token_provider(force)
            return self._token.value

    def _check_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != self._host:
            raise ValueError("refusing to send the Outlook token to an unrelated host")

    def get(self, path_or_url: str, params: dict[str, str] | None = None) -> httpx.Response:
        """GET ``path_or_url`` (relative to the API base, or an absolute nextLink)."""
        url = path_or_url if path_or_url.startswith("https://") else self._api_base + path_or_url
        self._check_url(url)
        refreshed = False
        resp: httpx.Response | None = None
        for attempt in range(MAX_RETRIES):
            pause = self._pause_until - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            headers = {
                "Authorization": f"Bearer {self._bearer()}",
                "Accept": "application/json",
                "Prefer": _PREFER,
            }
            try:
                resp = self._http().get(url, params=params, headers=headers)
            except httpx.TransportError as exc:  # read timeouts etc. are transient
                if attempt == MAX_RETRIES - 1:
                    raise
                wait = min(2.0 ** (attempt + 1), MAX_RETRY_WAIT_SECONDS)
                logger.warning(
                    "Outlook request failed (%s), retrying in %.0fs", type(exc).__name__, wait
                )
                self._pause_until = max(self._pause_until, time.monotonic() + wait)
                continue
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._bearer(force=True)
                continue
            if resp.status_code in (429, 503, 504):
                wait = _retry_after(resp, default=2.0 ** (attempt + 1))
                logger.warning("Outlook throttled (HTTP %d), waiting %.0fs", resp.status_code, wait)
                self._pause_until = max(self._pause_until, time.monotonic() + wait)
                continue
            break
        assert resp is not None
        if resp.status_code in (401, 301, 302, 303, 307, 308):
            raise WebSessionExpiredError(
                "Outlook rejected the browser session token. "
                "Run: uv run workctx auth login-web --source <name>"
            )
        return resp

    def get_json(self, path_or_url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        resp = self.get(path_or_url, params)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Outlook API {urlparse(str(resp.url)).path} failed: HTTP {resp.status_code} "
                f"{resp.text[:200]}"
            )
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def pages(
        self, path: str, params: dict[str, str], *, max_items: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of a paged collection (follows ``@odata.nextLink``)."""
        count = 0
        next_url: str | None = path
        next_params: dict[str, str] | None = params
        while next_url:
            data = self.get_json(next_url, next_params)
            for item in data.get("value", []):
                yield item
                count += 1
                if max_items is not None and count >= max_items:
                    return
            next_url = data.get("@odata.nextLink")
            next_params = None


def _retry_after(resp: httpx.Response, *, default: float) -> float:
    try:
        wait = float(resp.headers.get("Retry-After", default))
    except ValueError:
        wait = default
    return max(0.0, min(wait, MAX_RETRY_WAIT_SECONDS))


class _OutlookSource(Source):
    """Shared plumbing for the mail and calendar adapters."""

    def __init__(
        self,
        config: MailSource | CalendarSource,
        *,
        sharepoint_sources: dict[str, SharePointSource] | None = None,
        max_workers: int = 4,
        client: OutlookClient | None = None,
    ) -> None:
        self.config = config
        self._max_workers = max(1, max_workers)
        self._config_error: str | None = None
        self.client: OutlookClient | None = client
        if client is None:
            try:
                self.client = OutlookClient(
                    profile=config.profile_name(sharepoint_sources),
                    mailbox_url=config.mailbox_url,
                    api_base=config.api_base,
                )
            except ValueError as exc:
                self._config_error = f"{config.name}: {exc}"

    @property
    def name(self) -> str:
        return self.config.name

    def validate(self) -> list[str]:
        return [self._config_error] if self._config_error else []

    def _require_client(self) -> OutlookClient:
        if self._config_error or self.client is None:
            raise RuntimeError(self._config_error or f"{self.name}: not configured")
        return self.client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


class MailAdapter(_OutlookSource):
    """Email from Outlook / Exchange Online, one Markdown file per message."""

    config: MailSource

    @property
    def source_type(self) -> SourceType:
        return SourceType.EMAIL

    def retention_cutoff(self) -> datetime | None:
        return datetime.now(UTC) - timedelta(days=self.config.since_days)

    # -- discovery ---------------------------------------------------------------

    def _folder_ids(self) -> list[tuple[str, str]]:
        """``(api folder id, display name)`` for every configured folder."""
        client = self._require_client()
        resolved: list[tuple[str, str]] = []
        custom: dict[str, tuple[str, str]] | None = None
        for raw in self.config.folders:
            key = raw.strip().lower()
            if key in WELL_KNOWN_FOLDERS:
                resolved.append((key, WELL_KNOWN_FOLDERS[key]))
                continue
            if custom is None:
                custom = {}
                for folder in client.pages(
                    "/me/mailfolders", {"$select": "Id,DisplayName", "$top": "100"}
                ):
                    custom[one_line(folder.get("DisplayName")).lower()] = (
                        folder["Id"],
                        one_line(folder.get("DisplayName")),
                    )
            if key not in custom:
                raise RuntimeError(
                    f"{self.name}: mail folder '{raw}' not found "
                    f"(available: {', '.join(sorted(v[1] for v in custom.values()))})"
                )
            resolved.append(custom[key])
        return resolved

    def _list_folder(self, folder_id: str, *, select: str) -> Iterator[dict[str, Any]]:
        client = self._require_client()
        cutoff = datetime.now(UTC) - timedelta(days=self.config.since_days)
        params = {
            "$filter": f"ReceivedDateTime ge {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "ReceivedDateTime desc",
            "$top": str(PAGE_SIZE),
            "$select": select,
        }
        yield from client.pages(
            f"/me/mailfolders/{folder_id}/messages", params, max_items=self.config.max_messages
        )

    def _excluded(self, message: dict[str, Any]) -> bool:
        entry = message.get("From") or message.get("Sender")
        address = ((entry or {}).get("EmailAddress") or {}).get("Address") or ""
        # Match on the bare address, the display name and "Name <address>".
        senders = {address.lower(), display_name(entry).lower(), format_address(entry).lower()}
        subject = one_line(message.get("Subject")).lower()
        if any(
            fnmatch.fnmatch(sender, p.lower())
            for sender in senders
            if sender
            for p in self.config.exclude_senders
        ):
            return True
        return any(fnmatch.fnmatch(subject, p.lower()) for p in self.config.exclude_subjects)

    def discover_changes(
        self,
        db: StateDB,
        checkpoint: SyncCheckpoint | None,
        *,
        full: bool = False,
    ) -> list[DiscoveredChange]:
        known = {} if full else {o.source_id: o for o in db.get_objects_for_source(self.name)}
        known_all = {o.source_id for o in db.get_objects_for_source(self.name)}
        changes: list[DiscoveredChange] = []
        seen = 0
        for folder_id, folder_name in self._folder_ids():
            for message in self._list_folder(folder_id, select=_MAIL_LIST_FIELDS):
                seen += 1
                message_id = message.get("Id")
                if not message_id or message.get("IsDraft") or self._excluded(message):
                    continue
                existing = known.get(message_id)
                # Delivered mail never changes; flag/read-state edits are not content.
                if existing and not existing.last_error:
                    continue
                received = parse_dt(message.get("ReceivedDateTime") or message.get("SentDateTime"))
                sender_name = display_name(message.get("From") or message.get("Sender"))
                changes.append(
                    DiscoveredChange(
                        source_id=message_id,
                        source_key=message.get("InternetMessageId"),
                        title=one_line(message.get("Subject")) or "(no subject)",
                        source_url=message.get("WebLink"),
                        source_version=f"r:{message.get('ReceivedDateTime')}",
                        source_updated_at=received,
                        action=ChangeAction.UPDATE if message_id in known_all else ChangeAction.ADD,
                        metadata={
                            "occurred_on": day_of(message.get("ReceivedDateTime")),
                            "message": message,
                            "folder": folder_name,
                            "front_matter": {
                                "sender": sender_name,
                                "folder": folder_name,
                                "participants": _participants(message),
                            },
                        },
                    )
                )
        logger.info("Mail/%s: %d messages in window, %d new/changed", self.name, seen, len(changes))
        return changes

    def get_current_ids(self) -> set[str]:
        """Ids of every message currently in the window. Raises on any failure."""
        ids: set[str] = set()
        for folder_id, _ in self._folder_ids():
            for message in self._list_folder(folder_id, select="Id,From,Sender,Subject,IsDraft"):
                if message.get("Id") and not message.get("IsDraft") and not self._excluded(message):
                    ids.add(message["Id"])
        return ids

    # -- content -----------------------------------------------------------------

    def render_content(self, change: DiscoveredChange) -> str | None:
        client = self._require_client()
        message: dict[str, Any] = change.metadata["message"]
        data = client.get_json(
            f"/me/messages/{_quote_id(change.source_id)}", {"$select": "Body,BodyPreview"}
        )
        raw_body = str((data.get("Body") or {}).get("Content") or data.get("BodyPreview") or "")
        trimmed = False
        if self.config.trim_quoted_replies:
            body, trimmed = trim_quoted_reply(raw_body)
        else:
            body = raw_body.strip()
        body = limit_chars(body, self.config.max_body_chars)
        attachments: list[dict[str, Any]] = []
        if message.get("HasAttachments"):
            try:
                attachments = list(
                    client.pages(
                        f"/me/messages/{_quote_id(change.source_id)}/attachments",
                        {"$select": "Name,Size,ContentType", "$top": "50"},
                        max_items=50,
                    )
                )
            except RuntimeError as exc:
                logger.debug("Mail/%s: attachment listing failed: %s", self.name, exc)
        return render_mail_markdown(
            message,
            body,
            folder=str(change.metadata.get("folder") or ""),
            attachments=attachments,
            trimmed=trimmed,
        )


def _participants(message: dict[str, Any]) -> list[str]:
    people = [display_name(r) for r in (message.get("ToRecipients") or [])]
    people += [display_name(r) for r in (message.get("CcRecipients") or [])]
    return [p for p in dict.fromkeys(people) if p]


def _quote_id(message_id: str) -> str:
    return quote(message_id, safe="")


class CalendarAdapter(_OutlookSource):
    """Outlook calendar events, one Markdown file per event occurrence."""

    config: CalendarSource

    @property
    def source_type(self) -> SourceType:
        return SourceType.CALENDAR

    def retention_cutoff(self) -> datetime | None:
        return datetime.now(UTC) - timedelta(days=self.config.past_days)

    def _window(self) -> tuple[datetime, datetime]:
        now = datetime.now(UTC)
        return (
            now - timedelta(days=self.config.past_days),
            now + timedelta(days=self.config.future_days),
        )

    def _events(self, select: str) -> Iterator[dict[str, Any]]:
        client = self._require_client()
        start, end = self._window()
        params = {
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$orderby": "Start/DateTime",
            "$top": "50",
            "$select": select,
        }
        for event in client.pages("/me/calendarview", params):
            if event.get("IsCancelled") and not self.config.include_cancelled:
                continue
            title = one_line(event.get("Subject")).lower()
            if any(fnmatch.fnmatch(title, p.lower()) for p in self.config.exclude_titles):
                continue
            yield event

    def discover_changes(
        self,
        db: StateDB,
        checkpoint: SyncCheckpoint | None,
        *,
        full: bool = False,
    ) -> list[DiscoveredChange]:
        stored = {o.source_id: o for o in db.get_objects_for_source(self.name)}
        known = {} if full else stored
        changes: list[DiscoveredChange] = []
        seen = 0
        for event in self._events(_EVENT_LIST_FIELDS):
            seen += 1
            event_id = event.get("Id")
            if not event_id:
                continue
            version = f"ck:{event.get('ChangeKey') or event.get('LastModifiedDateTime')}"
            existing = known.get(event_id)
            if existing and not existing.last_error and existing.source_version == version:
                continue
            start = parse_dt((event.get("Start") or {}).get("DateTime"))
            changes.append(
                DiscoveredChange(
                    source_id=event_id,
                    title=one_line(event.get("Subject")) or "(no title)",
                    source_url=event.get("WebLink"),
                    source_version=version,
                    source_updated_at=start,
                    action=ChangeAction.UPDATE if event_id in stored else ChangeAction.ADD,
                    metadata={
                        "occurred_on": day_of((event.get("Start") or {}).get("DateTime")),
                        "event": event,
                        "front_matter": {
                            "start_at": (start.isoformat() if start else None),
                            "end_at": _iso((event.get("End") or {}).get("DateTime")),
                            "location": one_line((event.get("Location") or {}).get("DisplayName")),
                            "organizer": display_name(event.get("Organizer")),
                            "participants": [
                                n
                                for n in (display_name(a) for a in event.get("Attendees") or [])
                                if n
                            ],
                        },
                    },
                )
            )
        logger.info(
            "Calendar/%s: %d events in window, %d new/changed", self.name, seen, len(changes)
        )
        return changes

    def get_current_ids(self) -> set[str]:
        return {e["Id"] for e in self._events("Id,Subject,IsCancelled") if e.get("Id")}

    def render_content(self, change: DiscoveredChange) -> str | None:
        client = self._require_client()
        event: dict[str, Any] = change.metadata["event"]
        body = ""
        if self.config.max_body_chars:
            data = client.get_json(
                f"/me/events/{_quote_id(change.source_id)}", {"$select": "Body,BodyPreview"}
            )
            raw = str((data.get("Body") or {}).get("Content") or data.get("BodyPreview") or "")
            body = limit_chars(trim_invite_boilerplate(raw), self.config.max_body_chars)
        return render_event_markdown(event, body)


def _iso(value: str | None) -> str | None:
    parsed = parse_dt(value)
    return parsed.isoformat() if parsed else None
