#!/usr/bin/env python3
# redfoxdownload.py
#
# Zoekt Bluesky-posts en replies van de laatste 24 uur waarin
# @redfoxofficial.bsky.social wordt genoemd.
# Downloadt afbeeldingen en video's naar Google Drive.
#
# Benodigde GitHub Secrets:
# GOOGLE_SERVICE_ACCOUNT_JSON
# DRIVE_FOLDER_ID
# DOWNLOAD_BSKY_USERNAME
# DOWNLOAD_BSKY_APP_PASSWORD

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

HANDLE = "redfoxofficial.bsky.social"

SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
RESOLVE_URL = "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle"
DESCRIBE_URL = "https://public.api.bsky.app/xrpc/com.atproto.repo.describeRepo"

STATE_FILE = Path("redfoxdownload_state.json")
TEMP_DIR = Path("redfoxdownload_temp")

LOOKBACK_HOURS = 24
PAGE_LIMIT = 100
MAX_PAGES = 10
REQUEST_TIMEOUT = 45

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("redfoxdownload")


def load_state():
    if not STATE_FILE.exists():
        return {"downloaded": {}}

    try:
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(data, dict):
            raise ValueError("Ongeldig statusbestand")

        data.setdefault("downloaded", {})
        return data

    except Exception as exc:
        log.warning(
            "Statusbestand kon niet worden gelezen: %s",
            exc
        )
        return {"downloaded": {}}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8",
    )


def get_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": "RedFoxMediaDownloader/1.0"
    })

    return session


def resolve_did(session):
    response = session.get(
        RESOLVE_URL,
        params={"handle": HANDLE},
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    did = response.json()["did"]

    log.info("RedFoxOfficial DID: %s", did)

    return did


def get_pds(session, did):
    response = session.get(
        DESCRIBE_URL,
        params={"repo": did},
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    services = (
        response.json()
        .get("didDoc", {})
        .get("service", [])
    )

    for service in services:
        if service.get("id", "").endswith("#atproto_pds"):
            return service.get("serviceEndpoint")

    return "https://bsky.social"


def create_bluesky_search_session():
    """
    Logt in met de aparte downloader-accountgegevens.
    De zoekopdracht wordt via de PDS naar de officiële AppView gestuurd.
    """

    username = os.environ.get(
        "DOWNLOAD_BSKY_USERNAME", ""
    ).strip()

    app_password = os.environ.get(
        "DOWNLOAD_BSKY_APP_PASSWORD", ""
    ).strip()

    if not username or not app_password:
        raise RuntimeError(
            "GitHub Secrets DOWNLOAD_BSKY_USERNAME "
            "en DOWNLOAD_BSKY_APP_PASSWORD ontbreken."
        )

    login_response = requests.post(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={
            "identifier": username,
            "password": app_password,
        },
        timeout=REQUEST_TIMEOUT,
    )

    login_response.raise_for_status()

    login = login_response.json()

    did = login["did"]
    access_jwt = login["accessJwt"]

    did_doc = login.get("didDoc") or {}

    pds_endpoint = None

    for service in did_doc.get("service", []) or []:
        if service.get("id", "").endswith("#atproto_pds"):
            pds_endpoint = service.get("serviceEndpoint")
            break

    pds_endpoint = (
        pds_endpoint or "https://bsky.social"
    ).rstrip("/")

    search_session = requests.Session()

    search_session.headers.update({
        "User-Agent": "RedFoxMediaDownloader/1.0",
        "Authorization": f"Bearer {access_jwt}",
        "atproto-proxy": "did:web:api.bsky.app#bsky_appview",
    })

    log.info(
        "Bluesky-login gelukt voor %s",
        username
    )

    log.info(
        "Zoekverbinding via PDS: %s",
        pds_endpoint
    )

    return search_session, pds_endpoint


def contains_redfox_mention(post, redfox_did):
    record = post.get("record", {})
    text = record.get("text", "")

    if HANDLE.lower() in text.lower():
        return True

    for facet in record.get("facets", []) or []:
        for feature in facet.get("features", []) or []:

            if (
                feature.get("$type")
                == "app.bsky.richtext.facet#mention"
            ):
                if feature.get("did") == redfox_did:
                    return True

    return False


def media_items(post):
    """
    Herkent afbeeldingen, video's en recordWithMedia-embeds.
    """

    found = []

    embed = post.get("embed") or {}
    embed_type = embed.get("$type", "")

    if embed_type.endswith("#recordWithMedia"):
        embed = embed.get("media") or {}
        embed_type = embed.get("$type", "")

    if embed_type.endswith("#images"):

        for image in embed.get("images", []) or []:
            image_obj = image.get("image") or {}

            cid = (
                image_obj.get("ref", {}).get("$link")
                or image_obj.get("cid")
            )

            mime = image_obj.get(
                "mimeType",
                "image/jpeg"
            )

            if cid:
                found.append({
                    "cid": cid,
                    "mime": mime,
                    "kind": "image",
                })

    elif embed_type.endswith("#video"):

        video = embed.get("video") or {}

        cid = (
            video.get("ref", {}).get("$link")
            or video.get("cid")
        )

        mime = video.get(
            "mimeType",
            "video/mp4"
        )

        if cid:
            found.append({
                "cid": cid,
                "mime": mime,
                "kind": "video",
            })

    return found


def extension_for(mime, kind):
    mime = (mime or "").lower()

    if "png" in mime:
        return ".png"

    if "webp" in mime:
        return ".webp"

    if "gif" in mime:
        return ".gif"

    if "jpeg" in mime or "jpg" in mime:
        return ".jpg"

    if "quicktime" in mime:
        return ".mov"

    if "mp4" in mime or kind == "video":
        return ".mp4"

    return ".jpg" if kind == "image" else ".mp4"


def download_blob(session, pds, did, cid, destination):
    endpoint = (
        pds.rstrip("/")
        + "/xrpc/com.atproto.sync.getBlob"
    )

    response = session.get(
        endpoint,
        params={
            "did": did,
            "cid": cid,
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    destination.write_bytes(response.content)

    if not response.content:
        raise RuntimeError(
            "Leeg mediabestand ontvangen"
        )

    return len(response.content)


def create_drive_service():
    raw = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_JSON", ""
    ).strip()

    if not raw:
        raise RuntimeError(
            "GitHub Secret GOOGLE_SERVICE_ACCOUNT_JSON ontbreekt."
        )

    try:
        info = json.loads(raw)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON bevat geen geldige JSON."
        ) from exc

    credentials = (
        service_account.Credentials
        .from_service_account_info(
            info,
            scopes=DRIVE_SCOPES,
        )
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def upload_to_drive(
    service,
    file_path,
    folder_id,
    filename,
    mime
):
    metadata = {
        "name": filename,
        "parents": [folder_id],
        "description": (
            f"Bluesky media van @{HANDLE}"
        ),
    }

    media = MediaFileUpload(
        str(file_path),
        mimetype=mime or "application/octet-stream",
        resumable=True,
    )

    result = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,size",
            supportsAllDrives=True,
        )
        .execute()
    )

    return result


def search_recent_posts(
    session,
    pds_endpoint,
    since_iso
):
    cursor = None
    results = []

    for page in range(MAX_PAGES):

        params = {
            "q": HANDLE,
            "sort": "latest",
            "limit": PAGE_LIMIT,
            "since": since_iso,
        }

        if cursor:
            params["cursor"] = cursor

        endpoint = (
            pds_endpoint.rstrip("/")
            + "/xrpc/app.bsky.feed.searchPosts"
        )

        response = session.get(
            endpoint,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        payload = response.json()
        posts = payload.get("posts", []) or []

        if not posts:
            break

        results.extend(posts)

        cursor = payload.get("cursor")

        log.info(
            "Zoekpagina %s: %s resultaten",
            page + 1,
            len(posts),
        )

        if not cursor:
            break

        oldest = min(
            (
                post.get("indexedAt", "")
                for post in posts
                if post.get("indexedAt")
            ),
            default="",
        )

        if oldest and oldest < since_iso:
            break

    return results


def main():

    folder_id = os.environ.get(
        "DRIVE_FOLDER_ID", ""
    ).strip()

    if not folder_id:
        raise RuntimeError(
            "GitHub Secret DRIVE_FOLDER_ID ontbreekt. "
            "Gebruik de ID van de map Shared/download."
        )

    TEMP_DIR.mkdir(exist_ok=True)

    state = load_state()
    downloaded = state["downloaded"]

    session = get_session()

    redfox_did = resolve_did(session)

    # Eerst de Bluesky-login uitvoeren.
    # Dit maakt search_session en search_pds aan.
    search_session, search_pds = (
        create_bluesky_search_session()
    )

    drive = create_drive_service()

    pds_cache = {}

    since = (
        datetime.now(timezone.utc)
        - timedelta(hours=LOOKBACK_HOURS)
    )

    since_iso = since.strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    log.info(
        "Zoeken vanaf %s (laatste %s uur)",
        since_iso,
        LOOKBACK_HOURS,
    )

    posts = search_recent_posts(
        search_session,
        search_pds,
        since_iso,
    )

    log.info(
        "Zoekresultaten totaal: %s",
        len(posts),
    )

    eligible = []
    seen_posts = set()

    for post in posts:

        uri = post.get("uri")
        indexed_at = post.get("indexedAt", "")

        if not uri or uri in seen_posts:
            continue

        seen_posts.add(uri)

        if indexed_at and indexed_at < since_iso:
            continue

        if not contains_redfox_mention(
            post,
            redfox_did
        ):
            continue

        items = media_items(post)

        if items:
            eligible.append((post, items))

    log.info(
        "Posts/replies met vermelding en media: %s",
        len(eligible),
    )

    uploaded_count = 0
    skipped_count = 0
    failed_count = 0

    for post, items in eligible:

        uri = post["uri"]

        author = (
            post.get("author", {})
            .get("handle", "unknown")
        )

        rkey = uri.rsplit("/", 1)[-1]

        for index, item in enumerate(
            items,
            start=1
        ):

            cid = item["cid"]
            unique_key = f"{uri}|{cid}"

            if unique_key in downloaded:
                skipped_count += 1
                continue

            suffix = extension_for(
                item["mime"],
                item["kind"],
            )

            safe_author = "".join(
                char
                if char.isalnum() or char in "-_"
                else "_"
                for char in author
            )

            filename = (
                f"{safe_author}_{rkey}_{index}{suffix}"
            )

            local_path = TEMP_DIR / filename

            try:

                author_did = post["author"]["did"]

                if author_did not in pds_cache:
                    pds_cache[author_did] = get_pds(
                        session,
                        author_did,
                    )

                size = download_blob(
                    session,
                    pds_cache[author_did],
                    author_did,
                    cid,
                    local_path,
                )

                result = upload_to_drive(
                    drive,
                    local_path,
                    folder_id,
                    filename,
                    item["mime"],
                )

                downloaded[unique_key] = {
                    "drive_file_id": result.get("id"),
                    "filename": filename,
                    "post_uri": uri,
                    "author": author,
                    "indexed_at": post.get("indexedAt"),
                    "downloaded_at": (
                        datetime.now(timezone.utc).isoformat()
                    ),
                    "bytes": size,
                }

                save_state(state)

                uploaded_count += 1

                log.info(
                    "Geüpload: %s (%s bytes)",
                    filename,
                    size,
                )

            except Exception as exc:

                failed_count += 1

                log.exception(
                    "Mislukt voor %s (%s): %s",
                    uri,
                    filename,
                    exc,
                )

            finally:

                try:
                    local_path.unlink(missing_ok=True)

                except Exception:
                    pass

            time.sleep(0.5)

    save_state(state)

    log.info(
        "Klaar | nieuw geüpload: %s | "
        "al bekend: %s | fouten: %s",
        uploaded_count,
        skipped_count,
        failed_count,
    )

    if failed_count:
        sys.exit(1)


if __name__ == "__main__":

    try:
        main()

    except Exception as error:

        log.exception(
            "Downloader gestopt: %s",
            error,
        )

        sys.exit(1)