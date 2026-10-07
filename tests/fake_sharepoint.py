"""A tiny in-memory SharePoint/OneDrive used by the Teams transcript tests.

Answers just the calls ``TeamsTranscriptSource`` makes. All names, hosts and
identities are synthetic (contoso.com / example people).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote

import httpx

from workctx.config import AuthConfig, SharePointSource, TranscriptsSource
from workctx.sources.teams_transcripts import TeamsTranscriptSource

TEAM = "https://contoso.sharepoint.com"
MY = "https://contoso-my.sharepoint.com"
PERSONAL = f"{MY}/personal/alex_example_contoso_com"
OTHER_PERSONAL = f"{MY}/personal/sam_other_contoso_com"
ME = "alex.example@contoso.com"

SITE_ID, WEB_ID, LIST_ID = (str(uuid.UUID(int=i)) for i in (11, 22, 33))


class FakeTenant:
    """Minimal in-memory SharePoint/OneDrive that answers the calls the source makes."""

    def __init__(self) -> None:
        self.recordings: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.search_queries: list[str] = []
        self.search_status = 200
        self.auth_status: int | None = None
        self.throttle_once: set[str] = set()
        self.cookies_seen: dict[str, set[str]] = {}
        # Sites (web URLs) where the logged-in user holds "add items" rights.
        self.contributor_webs: set[str] = set()
        self.permission_status = 200

    def add(
        self,
        uid: str,
        title: str,
        *,
        web: str = PERSONAL,
        scope: str = "own",
        modified: datetime | None = None,
        transcripts: list[dict] | None = None,
        file_name: str | None = None,
        status: int = 200,
        entries: list[dict] | None = None,
        json_status: int = 200,
    ) -> None:
        self.recordings[uid] = {
            "uid": uid,
            "title": title,
            "web": web,
            "scope": scope,
            "modified": modified or datetime(2026, 6, 25, 10, 32, 9, tzinfo=UTC),
            "transcripts": transcripts
            if transcripts is not None
            else [
                {
                    "id": f"tr-{uid[:4]}",
                    "cTag": f"ctag-{uid[:4]}-1",
                    "size": 1234,
                    "isDefault": True,
                    "isVisible": True,
                }
            ],
            "file_name": file_name or f"{title}-20260625_101615-Meeting Transcript.mp4",
            "status": status,
            "entries": entries
            if entries is not None
            else [
                {
                    "text": "Hello team.",
                    "speakerDisplayName": "Ada Example",
                    "startOffset": "00:00:01",
                    "endOffset": "00:00:03",
                    "spokenLanguageTag": "en-gb",
                },
                {
                    "text": "Morning.",
                    "speakerDisplayName": "Bob Example",
                    "startOffset": "00:00:04",
                    "endOffset": "00:00:05",
                },
            ],
            "json_status": json_status,
        }

    # -- transport -----------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        host = f"https://{url.host}"
        self.cookies_seen.setdefault(host, set()).add(request.headers.get("cookie", ""))
        path = unquote(url.path)

        if self.auth_status:
            return httpx.Response(self.auth_status)

        if path.endswith("/_api/web/currentuser"):
            return httpx.Response(200, json={"Email": ME, "LoginName": f"i:0#.f|membership|{ME}"})
        if path.endswith("/GetMyProperties"):
            return httpx.Response(200, json={"PersonalUrl": PERSONAL + "/"})
        if path.endswith("/_api/web/effectivebasepermissions"):
            if self.permission_status != 200:
                return httpx.Response(self.permission_status)
            web = f"https://{url.host}{path[: -len('/_api/web/effectivebasepermissions')]}"
            low = 0x2 if web in self.contributor_webs else 0x1
            return httpx.Response(
                200, json={"EffectiveBasePermissions": {"Low": str(low), "High": "0"}}
            )
        if path.endswith("/_api/search/query"):
            return self._search(parse_qs(url.query.decode()))

        match = re.search(r"/items/(?P<uid>[0-9a-f-]{36})(?P<rest>/.*)?$", path)
        if match:
            return self._item(match["uid"], match["rest"] or "", dict(parse_qs(url.query.decode())))
        return httpx.Response(404, json={"error": "unhandled " + path})

    def _search(self, qs: dict[str, list[str]]) -> httpx.Response:
        if self.search_status != 200:
            return httpx.Response(self.search_status, text="boom")
        query = qs["querytext"][0].strip("'")
        self.search_queries.append(query)
        start, limit = int(qs.get("startrow", ["0"])[0]), int(qs["rowlimit"][0])
        if f'path:"{PERSONAL}"' in query:
            wanted = [r for r in self.recordings.values() if r["scope"] == "own"]
        elif "SharedWithUsersOWSUSER" in query:
            assert f'"{ME}"' in query
            wanted = [r for r in self.recordings.values() if r["scope"] == "shared"]
        elif "path:" not in query:
            # Tenant-wide query: SharePoint Search is security-trimmed, so every
            # recording the fake user can open is returned.
            wanted = list(self.recordings.values())
        else:
            m = re.search(r'path:"([^"]+)"', query)
            wanted = [r for r in self.recordings.values() if m and r["web"].startswith(m[1])]
        wanted.sort(key=lambda r: r["modified"], reverse=True)
        page = wanted[start : start + limit]
        rows = [
            {
                "Cells": [
                    {"Key": "Title", "Value": r["title"]},
                    {"Key": "UniqueId", "Value": "{" + r["uid"].upper() + "}"},
                    {"Key": "SPWebUrl", "Value": r["web"]},
                    {"Key": "SiteId", "Value": SITE_ID},
                    {"Key": "WebId", "Value": WEB_ID},
                    {"Key": "ListId", "Value": LIST_ID},
                    {
                        "Key": "LastModifiedTime",
                        "Value": r["modified"].strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                    },
                    {"Key": "Size", "Value": "251"},
                ]
            }
            for r in page
        ]
        return httpx.Response(
            200,
            json={
                "PrimaryQueryResult": {
                    "RelevantResults": {"TotalRows": len(wanted), "Table": {"Rows": rows}}
                }
            },
        )

    def _item(self, uid: str, rest: str, qs: dict[str, list[str]]) -> httpx.Response:
        rec = self.recordings.get(uid)
        if rec is None:
            return httpx.Response(404)
        if rec["status"] != 200:
            return httpx.Response(rec["status"], json={"error": {"code": "blocked"}})
        if uid in self.throttle_once:
            self.throttle_once.discard(uid)
            return httpx.Response(429, headers={"Retry-After": "1"})
        if rest == "/media/transcripts":
            return httpx.Response(200, json={"value": rec["transcripts"]})
        if rest.startswith("/media/transcripts/") and rest.endswith("/streamContent"):
            fmt = qs.get("format", [""])[0]
            if fmt == "json":
                if rec["json_status"] != 200:
                    return httpx.Response(rec["json_status"], json={"error": "nope"})
                return httpx.Response(
                    200,
                    json={
                        "version": "1.0.0",
                        "entries": rec["entries"],
                        "events": [{"eventType": "TranscriptStopped", "startOffset": "00:30:00"}],
                    },
                )
            return httpx.Response(
                200,
                text="WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Vee Fallback>From vtt</v>\n",
            )
        if rest == "":
            return httpx.Response(
                200,
                json={
                    "name": rec["file_name"],
                    "webUrl": f"{rec['web']}/Documents/Recordings/"
                    + rec["file_name"].replace(" ", "%20"),
                    "createdDateTime": "2026-06-25T10:32:09Z",
                    "createdBy": {"user": {"displayName": "Ada Example"}},
                },
            )
        return httpx.Response(404)


def make_source(**overrides) -> TeamsTranscriptSource:
    # Most tests exercise one scope at a time, so the tenant-wide "invited" scan
    # (the production default) is opt-in here.
    overrides.setdefault("include_invited", False)
    cfg = TranscriptsSource(name="teams", sharepoint_source="sp", **overrides)
    sp = SharePointSource(
        name="sp",
        site_url=f"{TEAM}/sites/Anything",
        mode="browser",
        auth=AuthConfig(mode="browser", secret_ref="sp-cookies"),
    )
    return TeamsTranscriptSource(cfg, sharepoint_sources={"sp": sp}, max_workers=4)


def uid(n: int) -> str:
    return str(uuid.UUID(f"{0xA000 + n:08x}-0000-4000-8000-000000000000"))
