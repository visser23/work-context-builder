"""In-memory fakes of the Outlook REST API and the Slack Web API (httpx.MockTransport).

Both fakes only accept the expected credentials, record every request (so tests can
assert the sources are strictly read-only) and use made-up ``example.com`` data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

_REAL_CLIENT = httpx.Client  # tests monkeypatch httpx.Client; keep the real one
OUTLOOK_HOST = "outlook.example.com"
OUTLOOK_TOKEN = "outlook-good-token"
SLACK_TOKEN = "xoxc-test-token"
SLACK_COOKIE = "d=test-d-cookie"


def addr(name: str, address: str) -> dict[str, Any]:
    return {"EmailAddress": {"Name": name, "Address": address}}


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(days: float, hour: int = 10) -> datetime:
    base = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return base - timedelta(days=days)


class FakeOutlook:
    """Mailbox + calendar. Mutate ``messages`` / ``events`` between syncs."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}  # id -> message (with _folder, _body)
        self.events: dict[str, dict[str, Any]] = {}  # id -> event (with _body)
        self.custom_folders: dict[str, str] = {}  # display name -> id
        self.valid_token = OUTLOOK_TOKEN
        self.requests: list[httpx.Request] = []
        self.page_size = 50
        self.unauthorised_once = 0
        self.throttle_once = 0
        self.attachments: dict[str, list[dict[str, Any]]] = {}
        self.fail_attachments = False

    # -- data helpers
    def add_message(
        self,
        mid: str,
        subject: str,
        received: datetime,
        *,
        folder: str = "inbox",
        body: str = "Hello there, this is the message body.",
        sender: tuple[str, str] = ("Alice Example", "alice@example.com"),
        to: tuple[str, str] = ("Bob Example", "bob@example.com"),
        **extra: Any,
    ) -> None:
        self.messages[mid] = {
            "Id": mid,
            "Subject": subject,
            "From": addr(*sender),
            "Sender": addr(*sender),
            "ToRecipients": [addr(*to)],
            "CcRecipients": [],
            "ReceivedDateTime": iso(received),
            "SentDateTime": iso(received),
            "HasAttachments": False,
            "Importance": "Normal",
            "WebLink": f"https://{OUTLOOK_HOST}/owa/?ItemID={mid}",
            "IsDraft": False,
            "InternetMessageId": f"<{mid}@example.com>",
            "_folder": folder,
            "_body": body,
            **extra,
        }

    def add_event(
        self,
        eid: str,
        subject: str,
        start: datetime,
        *,
        minutes: int = 30,
        body: str = "Agenda: discuss things",
        change_key: str = "ck1",
        **extra: Any,
    ) -> None:
        self.events[eid] = {
            "Id": eid,
            "Subject": subject,
            "Start": {"DateTime": start.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "TimeZone": "UTC"},
            "End": {
                "DateTime": (start + timedelta(minutes=minutes)).strftime(
                    "%Y-%m-%dT%H:%M:%S.0000000"
                ),
                "TimeZone": "UTC",
            },
            "IsAllDay": False,
            "IsCancelled": False,
            "Location": {"DisplayName": "Room 1"},
            "Organizer": addr("Alice Example", "alice@example.com"),
            "Attendees": [
                {**addr("Bob Example", "bob@example.com"), "Status": {"Response": "Accepted"}}
            ],
            "ChangeKey": change_key,
            "WebLink": f"https://{OUTLOOK_HOST}/owa/?itemid={eid}",
            "_body": body,
            **extra,
        }

    # -- transport
    def client_factory(self):
        real = _REAL_CLIENT

        def make(**kwargs):
            return real(transport=httpx.MockTransport(self.handler), **kwargs)

        return make

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _page(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        skip = int(query.get("skip", ["0"])[0])
        chunk = items[skip : skip + self.page_size]
        body: dict[str, Any] = {"value": [self._public(i) for i in chunk]}
        if skip + self.page_size < len(items):
            nxt = request.url.copy_set_param("skip", str(skip + self.page_size))
            body["@odata.nextLink"] = str(nxt)
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != OUTLOOK_HOST:
            return httpx.Response(404)
        if request.headers.get("Authorization") != f"Bearer {self.valid_token}":
            return httpx.Response(401)
        if self.unauthorised_once:
            self.unauthorised_once -= 1
            return httpx.Response(401)
        if self.throttle_once:
            self.throttle_once -= 1
            return httpx.Response(429, headers={"Retry-After": "0"})
        path = unquote(request.url.path).removeprefix("/api/v2.0")
        query = parse_qs(request.url.query.decode())
        if path == "/me":
            return httpx.Response(200, json={"DisplayName": "Bob Example"})
        if path == "/me/mailfolders":
            value = [{"Id": fid, "DisplayName": name} for name, fid in self.custom_folders.items()]
            return httpx.Response(200, json={"value": value})
        if path.startswith("/me/mailfolders/") and path.endswith("/messages"):
            folder = path.split("/")[3]
            fid_to_name = {v: v for v in self.custom_folders.values()}
            items = [
                m
                for m in self.messages.values()
                if m["_folder"] in (folder, fid_to_name.get(folder))
            ]
            flt = query.get("$filter", [""])[0]
            if "ReceivedDateTime ge " in flt:
                cutoff = flt.split("ReceivedDateTime ge ")[1].strip()
                items = [m for m in items if m["ReceivedDateTime"] >= cutoff]
            items.sort(key=lambda m: m["ReceivedDateTime"], reverse=True)
            return self._page(request, items)
        if path.startswith("/me/messages/"):
            rest = path.removeprefix("/me/messages/")
            if rest.endswith("/attachments"):
                if self.fail_attachments:
                    return httpx.Response(500)
                return httpx.Response(200, json={"value": self.attachments.get(rest[:-12], [])})
            message = self.messages.get(rest)
            if not message:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"Body": {"ContentType": "Text", "Content": message["_body"]}}
            )
        if path == "/me/calendarview":
            start = query["startDateTime"][0].replace("Z", "")
            end = query["endDateTime"][0].replace("Z", "")
            items = [e for e in self.events.values() if start <= e["Start"]["DateTime"][:19] <= end]
            items.sort(key=lambda e: e["Start"]["DateTime"])
            return self._page(request, items)
        if path.startswith("/me/events/"):
            event = self.events.get(path.removeprefix("/me/events/"))
            if not event:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"Body": {"ContentType": "Text", "Content": event["_body"]}}
            )
        return httpx.Response(404, json={"error": path})

    def write_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]


class FakeSlack:
    """One workspace. ``messages[channel]`` is a list of raw Slack message dicts."""

    def __init__(self) -> None:
        self.conversations: list[dict[str, Any]] = []
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, str] = {}  # channel id -> error for conversations.history
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.rate_limit_once = 0
        self.auth_fail_calls = 0
        self.history_page_size = 200
        self.token = SLACK_TOKEN

    def client(self) -> httpx.Client:
        return _REAL_CLIENT(transport=httpx.MockTransport(self.handler))

    def methods(self) -> list[str]:
        return [m for m, _ in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.removeprefix("/api/")
        params = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((method, params))
        if request.method != "POST" or not request.url.host.endswith(".slack.com"):
            return httpx.Response(404)
        if self.auth_fail_calls:
            self.auth_fail_calls -= 1
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        if request.headers.get(
            "Authorization"
        ) != f"Bearer {self.token}" or SLACK_COOKIE not in request.headers.get("Cookie", ""):
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        if self.rate_limit_once:
            self.rate_limit_once -= 1
            return httpx.Response(429, headers={"Retry-After": "0"})
        handler = getattr(self, "m_" + method.replace(".", "_"), None)
        if handler is None:
            return httpx.Response(200, json={"ok": False, "error": "unknown_method"})
        return httpx.Response(200, json=handler(params))

    # -- API methods
    def m_auth_test(self, p):
        return {"ok": True, "user": "me", "team": "Contoso"}

    def m_users_conversations(self, p):
        return {"ok": True, "channels": self.conversations}

    def m_users_info(self, p):
        user = self.users.get(p.get("user", ""))
        if not user:
            return {"ok": False, "error": "user_not_found"}
        return {"ok": True, "user": user}

    def m_conversations_history(self, p):
        channel = p["channel"]
        if channel in self.errors:
            return {"ok": False, "error": self.errors[channel]}
        oldest = float(p.get("oldest", "0"))
        items = [m for m in self.messages.get(channel, []) if float(m["ts"]) >= oldest]
        items.sort(key=lambda m: float(m["ts"]), reverse=True)
        top_level = [m for m in items if "thread_ts" not in m or m["thread_ts"] == m["ts"]]
        offset = int(p.get("cursor") or 0)
        chunk = top_level[offset : offset + self.history_page_size]
        meta = {}
        if offset + self.history_page_size < len(top_level):
            meta = {"next_cursor": str(offset + self.history_page_size)}
        return {"ok": True, "messages": chunk, "response_metadata": meta}

    def m_conversations_replies(self, p):
        thread = [
            m
            for m in self.messages.get(p["channel"], [])
            if m.get("thread_ts") == p["ts"] or m["ts"] == p["ts"]
        ]
        thread.sort(key=lambda m: float(m["ts"]))
        return {"ok": True, "messages": thread}


def slack_ts(days: int, hour: int, minute: int = 0, seq: int = 1) -> str:
    """Slack timestamp ``days`` UTC days ago at hour:minute (``seq`` = microsecond part)."""
    day = datetime.now(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(
        days=days
    )
    return f"{int(day.timestamp())}.{seq:06d}"


def dumps(value: Any) -> str:
    return json.dumps(value)
