"""A tiny in-memory SharePoint document library for ``SharePointWebSource`` tests.

Answers the REST calls the adapter makes (folder ``/Files`` + ``/Folders`` listings and
file downloads) and can misbehave on demand: throttle, reset connections, return 5xx,
refuse a folder. All names are synthetic.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx

SITE = "https://contoso.sharepoint.com/sites/team"
BASE = "/sites/team/Shared Documents/Root"


class FakeLibrary:
    def __init__(self, base: str = BASE) -> None:
        self.base = base
        self.files: dict[str, dict[str, Any]] = {}  # server url -> metadata
        self.folders: set[str] = {base}
        self.requests: list[httpx.Request] = []
        self.auth_ok = True  # False => every request is 401
        self.forbidden: set[str] = set()  # folder urls answering 403
        self.faults: dict[str, deque[Any]] = defaultdict(deque)  # url fragment -> queue
        self.session_probe_status = 200

    # -- data --------------------------------------------------------------
    def add(
        self,
        rel: str,
        content: bytes = b"hello",
        modified: str = "2026-10-01T10:00:00Z",
    ) -> str:
        url = f"{self.base}/{rel}"
        parent = url.rsplit("/", 1)[0]
        while parent.startswith(self.base) and parent not in self.folders:
            self.folders.add(parent)
            parent = parent.rsplit("/", 1)[0]
        self.files[url] = {
            "Name": url.rsplit("/", 1)[1],
            "ServerRelativeUrl": url,
            "TimeLastModified": modified,
            "Length": str(len(content)),
            "UniqueId": f"uid-{len(self.files)}",
            "_content": content,
        }
        return url

    def remove(self, rel: str) -> None:
        self.files.pop(f"{self.base}/{rel}", None)

    def fail(self, fragment: str, *outcomes: Any) -> None:
        """Queue outcomes for requests whose path contains ``fragment``.

        An int is an HTTP status, ``"reset"`` raises a connection reset.
        """
        self.faults[fragment].extend(outcomes)

    # -- transport -----------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.path)
        query = parse_qs(request.url.query.decode())

        if not self.auth_ok:
            return httpx.Response(401)
        for fragment, queue in self.faults.items():
            if queue and fragment in path + "?" + request.url.query.decode():
                outcome = queue.popleft()
                if outcome == "reset":
                    raise httpx.ReadError("Connection reset by peer", request=request)
                return httpx.Response(int(outcome), headers={"Retry-After": "1"})

        if path.endswith("/_api/web/title"):
            return httpx.Response(self.session_probe_status, json={"d": {"Title": "Team"}})

        folder = self._path_arg(path, query, "GetFolderByServerRelativeUrl")
        if folder is not None:
            return self._folder(folder, path.rsplit("/", 1)[1])

        file_path = self._path_arg(path, query, "GetFileByServerRelativeUrl")
        if file_path is not None:
            meta = self.files.get(file_path)
            if meta is None:
                return httpx.Response(404)
            return httpx.Response(200, content=meta["_content"])
        return httpx.Response(404, json={"error": "unhandled " + path})

    @staticmethod
    def _path_arg(path: str, query: dict[str, list[str]], method: str) -> str | None:
        if f"/{method}(" not in path:
            return None
        m = re.search(rf"{method}\('(?P<p>.*)'\)", path)
        if m:
            return m["p"].replace("''", "'")
        if "@path" in query:
            return query["@path"][0].strip("'")
        return None

    def _folder(self, folder: str, kind: str) -> httpx.Response:
        if folder in self.forbidden:
            return httpx.Response(403)
        if folder not in self.folders:
            return httpx.Response(404)
        if kind == "Files":
            results = [
                {k: v for k, v in meta.items() if not k.startswith("_")}
                for url, meta in self.files.items()
                if url.rsplit("/", 1)[0] == folder
            ]
        else:
            results = [
                {"Name": f.rsplit("/", 1)[1], "ServerRelativeUrl": f}
                for f in sorted(self.folders)
                if f != folder and f.rsplit("/", 1)[0] == folder
            ]
        return httpx.Response(200, json={"d": {"results": results}})
