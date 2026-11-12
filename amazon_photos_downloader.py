#!/usr/bin/env python3
"""
Download every image and video from a public Amazon Photos shared album
and upload the original bytes to a Google Drive folder.

Required environment variables:
  AMAZON_PHOTOS_URL
  GOOGLE_SERVICE_ACCOUNT_JSON   (service-account JSON, as a GitHub secret)
  GOOGLE_DRIVE_FOLDER_ID

The Amazon /drive/v1 endpoints are undocumented and may change.
"""

import io
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from playwright.sync_api import sync_playwright

DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]
PAGE_SIZE = 200
PAUSE_SECONDS = 0.8
SIZE_TOLERANCE = 0.02


def log(message: str) -> None:
    print(f"[amazon-photos] {message}", flush=True)


def safe_filename(name: str, fallback: str) -> str:
    name = (name or fallback).replace("\\", "_").replace("/", "_").strip()
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name)
    return name or fallback


def load_drive_service():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing GitHub secret GOOGLE_SERVICE_ACCOUNT_JSON")

    # Accept either the full JSON value or a base64-encoded JSON value.
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        import base64
        info = json.loads(base64.b64decode(raw).decode("utf-8"))

    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=DRIVE_SCOPE
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def drive_file_exists(service, folder_id: str, node_id: str) -> bool:
    query = (
        f"'{folder_id}' in parents and "
        f"appProperties has {{ key='amazonNodeId' and value='{node_id}' }} "
        "and trashed=false"
    )
    result = service.files().list(
        q=query,
        spaces="drive",
        pageSize=10,
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return bool(result.get("files"))


def upload_to_drive(service, folder_id: str, node_id: str, name: str,
                    content_type: str, data: bytes) -> str:
    metadata = {
        "name": name,
        "mimeType": content_type or "application/octet-stream",
        "parents": [folder_id],
        "appProperties": {"amazonNodeId": str(node_id)},
    }
    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype=content_type or "application/octet-stream",
        resumable=True,
    )
    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,size",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


def main() -> int:
    album_url = os.environ.get("AMAZON_PHOTOS_URL", "").strip()
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()

    parsed = urlparse(album_url)
    match = re.search(r"/photos/share/([^/?#]+)", parsed.path or "")
    if not parsed.scheme.startswith("http") or not parsed.netloc or not match:
        raise RuntimeError("AMAZON_PHOTOS_URL is not a valid Amazon Photos share URL")
    if not folder_id:
        raise RuntimeError("Missing GOOGLE_DRIVE_FOLDER_ID")

    share_id = match.group(1)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    common = f"shareId={share_id}&resourceVersion=V2&ContentType=JSON"

    drive = load_drive_service()
    log(f"Starting public album share {share_id}")
    log(f"Google Drive destination folder: {folder_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-GB")
        page = context.new_page()

        response = page.goto(album_url, wait_until="domcontentloaded", timeout=90000)
        if response is not None and response.status >= 400:
            raise RuntimeError(f"Amazon album page returned HTTP {response.status}")
        page.wait_for_timeout(3000)

        def api(path: str):
            result = page.evaluate(
                """async (url) => {
                    const response = await fetch(url, {
                        headers: { Accept: "application/json" },
                        credentials: "include"
                    });
                    return { status: response.status, body: await response.text() };
                }""",
                f"{origin}{path}",
            )
            if result["status"] != 200:
                raise RuntimeError(
                    f"Amazon API HTTP {result['status']} for {path}: "
                    f"{result['body'][:300]}"
                )
            try:
                return json.loads(result["body"])
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Amazon API returned non-JSON for {path}") from exc

        share_data = api(f"/drive/v1/shares/{share_id}?{common}")
        root_id = (share_data.get("nodeInfo") or {}).get("id")
        if not root_id:
            raise RuntimeError("Could not find the shared album root node")

        root_children = api(
            f"/drive/v1/nodes/{root_id}/children"
            f"?asset=ALL&limit=1&searchOnFamily=true&tempLink=true&{common}&offset=0"
        )
        root_data = root_children.get("data") or []
        if not root_data:
            raise RuntimeError("The shared album contains no accessible album folder")
        album_id = root_data[0].get("id")
        if not album_id:
            raise RuntimeError("Amazon did not return an album folder ID")

        filters = (
            "kind:(FILE* OR FOLDER*) AND "
            "contentProperties.contentType:(image* OR video*) AND "
            "status:(AVAILABLE*)"
        )
        from urllib.parse import quote
        encoded_filters = quote(filters, safe="")

        # Read every page. Offset pagination is used by the current Amazon web API.
        items = []
        offset = 0
        while True:
            page_data = api(
                f"/drive/v1/nodes/{album_id}/children"
                f"?asset=ALL&filters={encoded_filters}&limit={PAGE_SIZE}"
                f"&searchOnFamily=true&tempLink=true&{common}&offset={offset}"
            )
            batch = page_data.get("data") or []
            items.extend(batch)
            log(f"Discovered {len(items)} media items so far")
            if len(batch) < PAGE_SIZE:
                break
            offset += len(batch)

        # Keep only actual image/video nodes.
        items = [
            item for item in items
            if str((item.get("contentProperties") or {}).get("contentType", ""))
            .lower().startswith(("image", "video"))
        ]
        if not items:
            raise RuntimeError(
                "No images or videos were returned. The album may be empty, "
                "private, or Amazon may have changed its API."
            )

        log(f"Album contains {len(items)} image/video items")
        downloaded = uploaded = skipped = failed = 0

        for index, item in enumerate(items, start=1):
            node_id = str(item.get("id") or "")
            props = item.get("contentProperties") or {}
            expected_size = int(props.get("size") or 0)
            content_type = str(props.get("contentType") or "application/octet-stream")
            name = safe_filename(item.get("name"), f"{node_id or index}.bin")

            if not node_id:
                failed += 1
                log(f"[{index}/{len(items)}] Missing node ID; skipped")
                continue

            try:
                if drive_file_exists(drive, folder_id, node_id):
                    skipped += 1
                    log(f"[{index}/{len(items)}] Already in Drive: {name}")
                    continue

                download_url = (
                    f"{origin}/drive/v1/nodes/{node_id}/contentRedirection"
                    f"?download=true&{common}"
                )
                media_response = context.request.get(download_url, timeout=120000)
                if not media_response.ok:
                    raise RuntimeError(
                        f"Download returned HTTP {media_response.status}"
                    )
                data = media_response.body()
                actual_size = len(data)

                if expected_size and abs(actual_size - expected_size) > max(
                    1, expected_size * SIZE_TOLERANCE
                ):
                    raise RuntimeError(
                        f"Size verification failed: received {actual_size} bytes, "
                        f"Amazon listed {expected_size}"
                    )

                file_id = upload_to_drive(
                    drive, folder_id, node_id, name, content_type, data
                )
                downloaded += 1
                uploaded += 1
                log(
                    f"[{index}/{len(items)}] Uploaded {name} "
                    f"({actual_size} bytes, Drive ID {file_id})"
                )
            except Exception as exc:
                failed += 1
                log(f"[{index}/{len(items)}] FAILED {name}: {exc}")

            time.sleep(PAUSE_SECONDS)

        log(
            f"Finished. Total={len(items)}, uploaded={uploaded}, "
            f"already_present={skipped}, failed={failed}"
        )
        browser.close()

        if failed:
            return 1
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        log(f"ERROR: {error}")
        sys.exit(1)
