"""Slack source adapter: per-conversation, per-day digests via your browser session.

Uses the ``xoxc-`` client tokens and ``d`` cookie of a logged-in Slack web client
(see :mod:`workctx.auth.webtokens`) to call the same Web API the client uses.
Strictly read-only: only ``users.conversations``, ``conversations.history`` /
``replies`` and ``users.info`` are called, so nothing is posted or marked as read.

Output: one Markdown file per conversation per UTC day,
``slack/<source>/<workspace>/<conversation>-<id>/<YYYY-MM-DD>.md``.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from workctx.auth import webtokens
from workctx.auth.webtokens import SlackSession, SlackTeam
from workctx.config import SharePointSource, SlackSource
from workctx.models import ChangeAction, DiscoveredChange, SourceType, SyncCheckpoint
from workctx.normalise.slack import (
    SKIPPED_SUBTYPES,
    SlackMessage,
    convert_mrkdwn,
    mentioned_user_ids,
    message_fingerprint,
    render_day_markdown,
    thread_preview,
    ts_to_datetime,
    ts_to_day,
    version_for,
)
from workctx.sources.base import Source
from workctx.state import StateDB

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 60.0
PAGE_LIMIT = 200
MAX_RETRIES = 5
MAX_RETRY_WAIT_SECONDS = 60.0
# Slack errors that mean "this credential is no good" (vs. a problem with one channel).
_AUTH_ERRORS = frozenset(
    {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}
)
# Per-conversation errors that simply mean "skip this one".
_SKIPPABLE_ERRORS = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "missing_scope",
        "is_archived",
        "restricted_action",
        "access_denied",
        "team_access_not_granted",
        "enterprise_is_restricted",
        "user_not_found",
        "user_not_visible",
        "thread_not_found",
        "no_permission",
    }
)


class SlackApiError(RuntimeError):
    def __init__(self, method: str, error: str) -> None:
        super().__init__(f"Slack {method} failed: {error}")
        self.error = error


class SlackAuthError(SlackApiError):
    """The token/cookie was rejected."""


class SlackClient:
    """Read-only Slack Web API client for one workspace."""

    def __init__(self, team: SlackTeam, cookie_header: str, *, client: httpx.Client | None = None):
        parsed = urlparse(team.url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host.endswith(".slack.com") or parsed.username:
            raise ValueError(f"refusing to send Slack credentials to {team.url!r}")
        self.team = team
        self._base = f"https://{host}/api"
        self._cookie = cookie_header
        self._http = client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False)
        self._owns_http = client is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def call(self, method: str, **params: str) -> dict[str, Any]:
        """POST a Web API method; raises :class:`SlackApiError` on ``ok: false``."""
        headers = {"Authorization": f"Bearer {self.team.token}", "Cookie": self._cookie}
        for attempt in range(MAX_RETRIES):
            resp = self._http.post(f"{self._base}/{method}", headers=headers, data=params)
            if resp.status_code == 429:
                wait = _retry_after(resp, default=2.0 ** (attempt + 1))
                logger.warning("Slack %s throttled, waiting %.0fs", method, wait)
                time.sleep(wait)
                continue
            if resp.status_code in (301, 302, 303, 307, 308, 401, 403):
                raise SlackAuthError(method, f"http_{resp.status_code}")
            if resp.status_code >= 500:
                time.sleep(min(2.0 ** (attempt + 1), 10.0))
                continue
            try:
                data = resp.json()
            except ValueError:
                time.sleep(min(2.0 ** (attempt + 1), 10.0))
                continue
            if not isinstance(data, dict):
                raise SlackApiError(method, "bad_response")
            if data.get("ok"):
                return data
            error = str(data.get("error") or "unknown_error")
            if error == "ratelimited":
                time.sleep(_retry_after(resp, default=2.0 ** (attempt + 1)))
                continue
            if error in _AUTH_ERRORS:
                raise SlackAuthError(method, error)
            raise SlackApiError(method, error)
        raise SlackApiError(method, "retries_exhausted")

    def paged(self, method: str, key: str, **params: str) -> list[dict[str, Any]]:
        """Collect every item under ``key`` across cursor pages."""
        items: list[dict[str, Any]] = []
        cursor = ""
        while True:
            page_params = {**params, "limit": str(PAGE_LIMIT)}
            if cursor:
                page_params["cursor"] = cursor
            data = self.call(method, **page_params)
            items.extend(data.get(key) or [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return items


def _retry_after(resp: httpx.Response, *, default: float) -> float:
    try:
        wait = float(resp.headers.get("Retry-After", default))
    except ValueError:
        wait = default
    return max(1.0, min(wait, MAX_RETRY_WAIT_SECONDS))


def slugify_segment(value: str, max_length: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:max_length].strip("-") or "x"


def day_start(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)


class _Conversation:
    """A channel / DM / group DM with its display title and storage directory."""

    def __init__(self, team: SlackTeam, raw: dict[str, Any], title: str, kind: str) -> None:
        self.team = team
        self.id: str = raw["id"]
        self.title = title
        self.kind = kind
        self.directory = f"{slugify_segment(title)}-{self.id}"

    @property
    def workspace_dir(self) -> str:
        return slugify_segment(self.team.domain or self.team.name or self.team.id)


class SlackAdapter(Source):
    """Slack conversations as per-day Markdown digests."""

    def __init__(
        self,
        config: SlackSource,
        *,
        sharepoint_sources: dict[str, SharePointSource] | None = None,
        max_workers: int = 4,
        session_provider: Any = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._max_workers = max(1, max_workers)
        self._profile = config.profile_name(sharepoint_sources)
        self._session_provider = session_provider or self._default_session
        self._http_client = http_client
        self._clients: dict[str, SlackClient] = {}
        self._users: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def source_type(self) -> SourceType:
        return SourceType.SLACK

    def retention_cutoff(self) -> datetime | None:
        return datetime.now(UTC) - timedelta(days=self.config.since_days)

    def reconcile_supported(self) -> bool:
        # Finding deleted days means re-reading every thread in the window; Slack
        # digests are a rolling skim, so older deletions are intentionally not mirrored.
        return False

    def get_current_ids(self) -> set[str]:
        return set()

    def _default_session(self, force: bool) -> SlackSession:
        return webtokens.get_slack_session(
            self._profile,
            self.config.client_url,
            signin_url=self.config.signin_url,
            sso_button_text=self.config.sso_button_text,
            force=force,
        )

    def _client(self, team: SlackTeam, session: SlackSession) -> SlackClient:
        with self._lock:
            client = self._clients.get(team.id)
            if client is None:
                client = SlackClient(team, session.cookie_header(), client=self._http_client)
                self._clients[team.id] = client
            return client

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()

    def _user_name(self, client: SlackClient, user_id: str) -> str:
        key = (client.team.id, user_id)
        with self._lock:
            if key in self._users:
                return self._users[key]
        name = user_id
        try:
            info = client.call("users.info", user=user_id).get("user") or {}
            profile = info.get("profile") or {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or info.get("real_name")
                or info.get("name")
                or user_id
            )
        except SlackAuthError:
            raise
        except SlackApiError as exc:
            logger.debug("Slack users.info %s failed: %s", user_id, exc.error)
        with self._lock:
            self._users[key] = " ".join(str(name).split())
            return self._users[key]

    # ----------------------------------------------------------------- discovery

    def discover_changes(
        self,
        db: StateDB,
        checkpoint: SyncCheckpoint | None,
        *,
        full: bool = False,
    ) -> list[DiscoveredChange]:
        stored = {o.source_id: o for o in db.get_objects_for_source(self.name)}
        try:
            return self._discover(stored, full=full, force_session=False)
        except SlackAuthError:
            logger.info("Slack/%s: credentials rejected, refreshing the browser session", self.name)
            webtokens.clear_caches()
            for client in list(self._clients.values()):
                client.close()
            self._clients.clear()
            return self._discover(stored, full=full, force_session=True)

    def _window_start(self) -> datetime:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return today - timedelta(days=self.config.since_days)

    def _latest_stored_days(self, stored: dict[str, Any]) -> dict[tuple[str, str], str]:
        latest: dict[tuple[str, str], str] = {}
        for source_id in stored:
            parts = source_id.split(":")
            if len(parts) != 3:
                continue
            team_id, channel_id, day = parts
            if latest.get((team_id, channel_id), "") < day:
                latest[(team_id, channel_id)] = day
        return latest

    def _discover(
        self, stored: dict[str, Any], *, full: bool, force_session: bool
    ) -> list[DiscoveredChange]:
        session: SlackSession = self._session_provider(force_session)
        teams = webtokens.select_slack_teams(session.teams, self.config.workspaces)
        if not teams:
            raise RuntimeError(f"{self.name}: no Slack workspace matched in the browser session")
        latest = {} if full else self._latest_stored_days(stored)
        window_start = self._window_start()

        jobs: list[tuple[SlackClient, _Conversation, datetime]] = []
        for team in teams:
            client = self._client(team, session)
            for conv in self._conversations(client):
                last_day = latest.get((team.id, conv.id))
                oldest = window_start
                if last_day:
                    oldest = max(window_start, day_start(last_day) - timedelta(days=1))
                jobs.append((client, conv, oldest))

        changes: list[DiscoveredChange] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(self._conversation_changes, client, conv, oldest, stored, full): conv
                for client, conv, oldest in jobs
            }
            for future in as_completed(futures):
                try:
                    changes.extend(future.result())
                except SlackAuthError:
                    raise
                except Exception as exc:
                    errors.append(f"{futures[future].title}: {exc}")
        if errors:
            logger.warning(
                "Slack/%s: %d conversation(s) failed (retried next run): %s",
                self.name,
                len(errors),
                "; ".join(errors[:3]),
            )
        if jobs and len(errors) == len(jobs):
            raise RuntimeError(f"{self.name}: all {len(jobs)} Slack conversations failed")
        logger.info(
            "Slack/%s: %d workspace(s), %d conversations, %d new/changed day digests",
            self.name,
            len(teams),
            len(jobs),
            len(changes),
        )
        return changes

    # -- conversations -------------------------------------------------------------

    def _conversations(self, client: SlackClient) -> list[_Conversation]:
        raw = client.paged(
            "users.conversations",
            "channels",
            types=",".join(self.config.conversation_types),
            exclude_archived="true",
        )
        convs: list[_Conversation] = []
        for item in raw:
            conv = self._to_conversation(client, item)
            if conv and self._selected(conv, item):
                convs.append(conv)
        if len(convs) > self.config.max_channels:
            logger.warning(
                "Slack/%s: %d conversations; limiting to max_channels=%d",
                self.name,
                len(convs),
                self.config.max_channels,
            )
            convs = convs[: self.config.max_channels]
        return convs

    def _to_conversation(self, client: SlackClient, item: dict[str, Any]) -> _Conversation | None:
        if not item.get("id"):
            return None
        if item.get("is_im"):
            other = item.get("user") or ""
            name = self._user_name(client, other) if other else "unknown"
            return _Conversation(client.team, item, f"DM with {name}", "im")
        if item.get("is_mpim"):
            handles = re.sub(r"^mpdm-|-\d+$", "", str(item.get("name") or "")).split("--")
            title = "Group DM: " + ", ".join(h for h in handles if h)
            return _Conversation(client.team, item, title, "mpim")
        name = item.get("name") or item["id"]
        kind = "private_channel" if item.get("is_private") else "public_channel"
        return _Conversation(client.team, item, f"#{name}", kind)

    def _selected(self, conv: _Conversation, item: dict[str, Any]) -> bool:
        names = {conv.title.lower().lstrip("#"), str(item.get("name") or "").lower()}
        if self.config.include_channels and not any(
            fnmatch.fnmatch(n, p.lower()) for n in names for p in self.config.include_channels
        ):
            return False
        return not any(
            fnmatch.fnmatch(n, p.lower()) for n in names for p in self.config.exclude_channels
        )

    def _conversation_changes(
        self,
        client: SlackClient,
        conv: _Conversation,
        oldest: datetime,
        stored: dict[str, Any],
        full: bool,
    ) -> list[DiscoveredChange]:
        try:
            raw_messages = self._fetch_messages(client, conv, oldest)
        except SlackApiError as exc:
            if isinstance(exc, SlackAuthError):
                raise
            if exc.error in _SKIPPABLE_ERRORS:
                logger.debug("Slack/%s: skipping %s (%s)", self.name, conv.title, exc.error)
                return []
            raise
        if not raw_messages:
            return []

        by_day: dict[str, list[dict[str, Any]]] = {}
        for raw in raw_messages:
            by_day.setdefault(ts_to_day(raw["ts"]), []).append(raw)

        # Resolve every person mentioned or speaking once, before rendering.
        for raw in raw_messages:
            for user_id in mentioned_user_ids(raw.get("text") or ""):
                self._user_name(client, user_id)

        changes: list[DiscoveredChange] = []
        for day, day_raw in sorted(by_day.items()):
            source_id = f"{conv.team.id}:{conv.id}:{day}"
            version = version_for([message_fingerprint(r) for r in day_raw])
            existing = None if full else stored.get(source_id)
            if existing and not existing.last_error and existing.source_version == version:
                continue
            messages = self._to_messages(client, day_raw, raw_messages)
            if not messages:
                continue
            last_ts = max(r["ts"] for r in day_raw)
            first_ts = min(day_raw, key=lambda r: float(r["ts"]))["ts"]
            speakers = sorted({m.user for m in messages if m.user})
            body = render_day_markdown(
                conversation=conv.title,
                workspace=conv.team.name,
                day=day,
                messages=messages,
            )
            changes.append(
                DiscoveredChange(
                    source_id=source_id,
                    source_key=f"{conv.title} {day}",
                    title=f"{conv.title} - {day}",
                    source_url=(f"{conv.team.url}/archives/{conv.id}/p{first_ts.replace('.', '')}"),
                    source_version=version,
                    source_updated_at=ts_to_datetime(last_ts),
                    action=ChangeAction.UPDATE if source_id in stored else ChangeAction.ADD,
                    content_text=body,
                    metadata={
                        "occurred_on": day,
                        "subpath": f"{conv.workspace_dir}/{conv.directory}",
                        "front_matter": {
                            "workspace": conv.team.name,
                            "channel": conv.title,
                            "message_count": len(messages),
                            "participants": speakers,
                        },
                    },
                )
            )
        return changes

    # -- messages ------------------------------------------------------------------

    def _fetch_messages(
        self, client: SlackClient, conv: _Conversation, oldest: datetime
    ) -> list[dict[str, Any]]:
        """Top-level messages since ``oldest`` plus (optionally) their thread replies."""
        oldest_ts = f"{oldest.timestamp():.6f}"
        history = client.paged(
            "conversations.history", "messages", channel=conv.id, oldest=oldest_ts
        )
        by_ts: dict[str, dict[str, Any]] = {}
        for raw in history:
            if self._skip(raw):
                continue
            by_ts[raw["ts"]] = raw
            if self.config.include_threads and raw.get("reply_count") and raw.get("ts"):
                try:
                    replies = client.paged(
                        "conversations.replies",
                        "messages",
                        channel=conv.id,
                        ts=raw["ts"],
                        oldest=oldest_ts,
                    )
                except SlackAuthError:
                    raise
                except SlackApiError as exc:
                    logger.debug("Slack/%s: replies for %s failed: %s", self.name, raw["ts"], exc)
                    continue
                for reply in replies:
                    if reply.get("ts") != raw["ts"] and not self._skip(reply):
                        reply.setdefault("thread_ts", raw["ts"])
                        by_ts[reply["ts"]] = reply
        return sorted(by_ts.values(), key=lambda r: float(r["ts"]))

    @staticmethod
    def _skip(raw: dict[str, Any]) -> bool:
        return (
            not raw.get("ts") or raw.get("subtype") in SKIPPED_SUBTYPES or bool(raw.get("hidden"))
        )

    def _to_messages(
        self,
        client: SlackClient,
        day_raw: list[dict[str, Any]],
        all_raw: list[dict[str, Any]],
    ) -> list[SlackMessage]:
        parents = {r["ts"]: r for r in all_raw}
        resolve_user = lambda uid: self._user_name(client, uid)  # noqa: E731
        out: list[SlackMessage] = []
        for raw in day_raw:
            thread_ts = raw.get("thread_ts")
            is_reply = bool(thread_ts and thread_ts != raw["ts"])
            preview = ""
            if is_reply and thread_ts in parents:
                preview = thread_preview(
                    convert_mrkdwn(parents[thread_ts].get("text") or "", resolve_user)
                )
            text = convert_mrkdwn(raw.get("text") or "", resolve_user)
            if not text.strip():
                text = _attachment_text(raw, resolve_user)
            files = [str(f.get("name") or f.get("title") or "file") for f in raw.get("files") or []]
            if not text.strip() and not files:
                continue
            user_id = raw.get("user")
            bot = bool(raw.get("bot_id")) and not user_id
            name = (
                self._user_name(client, user_id)
                if user_id
                else str(raw.get("username") or (raw.get("bot_profile") or {}).get("name") or "app")
            )
            out.append(
                SlackMessage(
                    ts=raw["ts"],
                    user=name,
                    text=text,
                    is_reply=is_reply,
                    thread_preview=preview,
                    files=files,
                    reactions=[
                        (str(r.get("name")), int(r.get("count") or 0))
                        for r in raw.get("reactions") or []
                    ],
                    edited=bool(raw.get("edited")),
                    bot=bot,
                )
            )
        return out


def _attachment_text(raw: dict[str, Any], resolve_user: Any) -> str:
    """Text from legacy attachments / blocks when a message has no plain ``text``."""
    parts: list[str] = []
    for att in raw.get("attachments") or []:
        piece = att.get("text") or att.get("fallback") or att.get("title") or ""
        if piece:
            parts.append(convert_mrkdwn(str(piece), resolve_user))
    return "\n".join(parts)
