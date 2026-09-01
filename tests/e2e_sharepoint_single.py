"""Single-file E2E test against live SharePoint.

Tests the full pipeline: cookie refresh -> list folder -> pick one file ->
download -> convert -> verify output. No DB writes.

Usage:
    uv run python tests/e2e_sharepoint_single.py
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from workctx.auth.sharepoint import keepalive_and_extract, load_cookies
from workctx.config import load_config
from workctx.normalise.office import can_handle as office_handles
from workctx.normalise.office import convert_office
from workctx.normalise.pdf import can_handle as pdf_handles
from workctx.normalise.pdf import convert_pdf


def main() -> None:
    config_path = Path("workctx.yaml")
    if not config_path.exists():
        print("ERROR: workctx.yaml not found in current directory")
        sys.exit(1)

    cfg = load_config(config_path)
    sp_configs = [s for s in cfg.sources.sharepoint if s.mode == "browser"]
    if not sp_configs:
        print("ERROR: No browser-mode SharePoint sources in config")
        sys.exit(1)

    sp = sp_configs[0]
    site_url = (sp.site_url or "").rstrip("/")
    secret_ref = sp.auth.secret_ref if sp.auth else ""
    server_path = sp.server_relative_path or f"{site_url}/{sp.doc_library}"

    print(f"Source: {sp.name}")
    print(f"Site:   {site_url}")
    print(f"Path:   {server_path}")
    print()

    # Step 1: Cookie refresh (keep-alive)
    print("1. Refreshing cookies via keep-alive...")
    try:
        cookies = keepalive_and_extract(site_url, sp.name, secret_ref)
        print(f"   OK — got {len(cookies)} cookies: {', '.join(cookies.keys())}")
    except Exception as e:
        print(f"   Keep-alive failed, trying cached: {e}")
        cookies = load_cookies(secret_ref)
        if not cookies:
            print("   FAIL — no cached cookies either")
            sys.exit(1)
        print(f"   Using cached cookies: {', '.join(cookies.keys())}")

    # Step 2: Build HTTP client
    print("\n2. Building HTTP client...")
    import httpx

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    client = httpx.Client(
        base_url=site_url,
        timeout=120.0,
        headers={
            "Cookie": cookie_header,
            "Accept": "application/json;odata=verbose",
            "User-Agent": "WorkContextMirror/E2ETest",
        },
        follow_redirects=False,
    )

    # Step 3: Validate session
    print("   Validating session...")
    resp = client.get("/_api/web/title")
    if resp.status_code != 200:
        print(f"   FAIL — HTTP {resp.status_code}: {resp.text[:200]}")
        sys.exit(1)
    title_data = resp.json()
    web_title = title_data.get("d", {}).get("Title", "unknown")
    print(f"   OK — web title: {web_title}")

    # Step 4: List files in target folder
    print(f"\n3. Listing files in: {server_path}")
    encoded = quote(server_path, safe="/")
    resp = client.get(
        f"/_api/web/GetFolderByServerRelativeUrl('{encoded}')/Files",
        params={"$select": "Name,ServerRelativeUrl,TimeLastModified,Length"},
    )
    if resp.status_code == 400:
        print("   Initial URL encoding failed (400), trying @path fallback...")
        encoded_full = quote(server_path, safe="")
        resp = client.get(
            "/_api/web/GetFolderByServerRelativeUrl(@path)/Files",
            params={
                "@path": f"'{encoded_full}'",
                "$select": "Name,ServerRelativeUrl,TimeLastModified,Length",
            },
        )

    if resp.status_code != 200:
        print(f"   FAIL — HTTP {resp.status_code}: {resp.text[:300]}")
        print("\n   Trying subfolders instead...")
        resp = client.get(
            f"/_api/web/GetFolderByServerRelativeUrl('{encoded}')/Folders",
            params={"$select": "Name,ServerRelativeUrl"},
        )
        if resp.status_code == 200:
            folders = resp.json().get("d", {}).get("results", [])
            print(f"   Found {len(folders)} subfolders:")
            for f in folders[:10]:
                print(f"     {f.get('Name')}")
        sys.exit(1)

    files = resp.json().get("d", {}).get("results", [])
    print(f"   Found {len(files)} files")
    if not files:
        print("   No files in this folder. Trying subfolders...")
        resp2 = client.get(
            f"/_api/web/GetFolderByServerRelativeUrl('{encoded}')/Folders",
            params={"$select": "Name,ServerRelativeUrl"},
        )
        if resp2.status_code == 200:
            folders = resp2.json().get("d", {}).get("results", [])
            for f in folders[:5]:
                sub_path = f.get("ServerRelativeUrl", "")
                sub_encoded = quote(sub_path, safe="/")
                sub_resp = client.get(
                    f"/_api/web/GetFolderByServerRelativeUrl('{sub_encoded}')/Files",
                    params={"$select": "Name,ServerRelativeUrl,TimeLastModified,Length"},
                )
                if sub_resp.status_code == 200:
                    sub_files = sub_resp.json().get("d", {}).get("results", [])
                    if sub_files:
                        files = sub_files
                        print(f"   Found {len(files)} files in subfolder: {f.get('Name')}")
                        break

    if not files:
        print("   FAIL — no files found anywhere")
        sys.exit(1)

    # Pick a good test file (prefer docx/xlsx/pdf, then any)
    preferred = [".docx", ".xlsx", ".pdf", ".pptx"]
    chosen = None
    for ext in preferred:
        for f in files:
            if f.get("Name", "").lower().endswith(ext):
                chosen = f
                break
        if chosen:
            break
    if not chosen:
        chosen = files[0]

    fname = chosen["Name"]
    furl = chosen["ServerRelativeUrl"]
    fsize = chosen.get("Length", "?")
    fmod = chosen.get("TimeLastModified", "?")
    print("\n4. Selected test file:")
    print(f"   Name: {fname}")
    print(f"   URL:  {furl}")
    print(f"   Size: {fsize} bytes")
    print(f"   Modified: {fmod}")

    # Step 5: Download
    print("\n5. Downloading...")
    encoded_file = quote(furl, safe="/")
    resp = client.get(
        f"/_api/web/GetFileByServerRelativeUrl('{encoded_file}')/$value",
    )
    if resp.status_code == 400:
        encoded_full = quote(furl, safe="")
        resp = client.get(
            "/_api/web/GetFileByServerRelativeUrl(@path)/$value",
            params={"@path": f"'{encoded_full}'"},
        )

    if resp.status_code != 200:
        print(f"   FAIL — HTTP {resp.status_code}")
        sys.exit(1)

    suffix = Path(fname).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(resp.content)
        tmp_path = Path(tmp.name)
    print(f"   OK — {len(resp.content):,} bytes -> {tmp_path}")

    # Step 6: Convert
    print("\n6. Converting to Markdown...")
    md_content = None
    try:
        if pdf_handles(tmp_path):
            md_content = convert_pdf(tmp_path)
            print("   Converter: PyMuPDF4LLM (PDF)")
        elif office_handles(tmp_path):
            md_content = convert_office(tmp_path)
            print("   Converter: MarkItDown (Office)")
        else:
            md_content = tmp_path.read_text(encoding="utf-8", errors="replace")
            print("   Converter: raw text")
    except Exception as e:
        print(f"   FAIL — conversion error: {e}")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    if md_content:
        print(f"   OK — {len(md_content):,} chars of Markdown")
        print("\n7. Preview (first 500 chars):")
        print("   " + "-" * 60)
        preview = md_content[:500].replace("\n", "\n   ")
        print(f"   {preview}")
        print("   " + "-" * 60)
    else:
        print("   FAIL — no content produced")
        sys.exit(1)

    # Step 7: Verify idempotency
    print("\n8. Idempotency check...")
    print("   Re-downloading same file...")
    resp2 = client.get(
        f"/_api/web/GetFileByServerRelativeUrl('{encoded_file}')/$value",
    )
    if resp2.status_code == 200 and resp2.content == resp.content:
        print("   OK — same content on re-download (idempotent)")
    else:
        print(f"   WARN — different content or status {resp2.status_code}")

    client.close()
    print("\n✓ E2E test PASSED")


if __name__ == "__main__":
    main()
