
#!/usr/bin/env python3

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

RESOLVE_URL = (
    "https://public.api.bsky.app/xrpc/"
    "com.atproto.identity.resolveHandle"
)

PLC_DIRECTORY = "https://plc.directory"

LOGIN_URL = (
    "https://bsky.social/xrpc/"
    "com.atproto.server.createSession"
)

STATE_FILE = Path("redfoxdownload_state.json")
TEMP_DIR = Path("redfoxdownload_temp")

LOOKBACK_HOURS = 24
PAGE_LIMIT = 100
MAX_PAGES = 10
REQUEST_TIMEOUT = 60

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
            raise ValueError("Ongeldig statebestand")

        data.setdefault("downloaded", {})
        return data

    except Exception as exc:
        log.warning("Statebestand niet leesbaar: %s", exc)
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
        "User-Agent": "RedFoxMediaDownloader/4.0"
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
    """
    Haalt het DID-document rechtstreeks op bij plc.directory.
    Zo vermijden we de 501-fout van public.api.bsky.app.
    """

    if did.startswith("did:plc:"):
        url = f"{PLC_DIRECTORY}/{did}"

    elif did.startswith("did:web:"):
        domain = did[len("did:web:"):].replace("%3A", ":")
        url = f"https://{domain}/.well-known/did.json"

    else:
        raise RuntimeError(
            f"Niet-ondersteund DID-type: {did}"
        )

    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    did_document = response.json()

    for service in did_document.get("service", []):
        service_id = service.get("id", "")

        if service_id.endswith("#atproto_pds"):
            endpoint = service.get("serviceEndpoint")

            if endpoint:
                endpoint = endpoint.rstrip("/")
                log.info("PDS gevonden voor %s: %s", did, endpoint)
                return endpoint

    raise RuntimeError(
        f"Geen #atproto_pds gevonden in DID-document van {did}"
    )


def create_bluesky_search_session():
    username = os.environ.get(
        "DOWNLOAD_BSKY_USERNAME", ""
    ).strip()

    password = os.environ.get(
        "DOWNLOAD_BSKY_APP_PASSWORD", ""
    ).strip()

    if not username or not password:
        raise RuntimeError(
            "DOWNLOAD_BSKY_USERNAME of "
            "DOWNLOAD_BSKY_APP_PASSWORD ontbreekt."
        )

    response = requests.post(
        LOGIN_URL,
        json={
            "identifier": username,
            "password": password,
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    login = response.json()

    access_jwt = login["accessJwt"]
    did_doc = login.get("didDoc") or {}

    pds = None

    for service in did_doc.get("service", []):
        if service.get("id", "").endswith("#atproto_pds"):
            pds = service.get("serviceEndpoint")
            break

    if not pds:
        raise RuntimeError(
            "PDS-adres ontbreekt in Bluesky-login."
        )

    search_session = requests.Session()

    search_session.headers.update({
        "User-Agent": "RedFoxMediaDownloader/4.0",
        "Authorization": f"Bearer {access_jwt}",
        "atproto-proxy": "did:web:api.bsky.app#bsky_appview",
    })

    log.info("Bluesky-login gelukt.")
    log.info("Zoekverbinding via PDS: %s", pds)

    return search_session, pds.rstrip("/")


def search_recent_posts(session, pds, since_iso):
    endpoint = (
        pds
        + "/xrpc/app.bsky.feed.searchPosts"
    )

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

        response = session.get(
            endpoint,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        payload = response.json()
        posts = payload.get("posts", []) or []

        log.info(
            "Zoekpagina %s: %s resultaten",
            page + 1,
            len(posts),
        )

        if not posts:
            break

        results.extend(posts)
        cursor = payload.get("cursor")

        if not cursor:
            break

    return results


def contains_redfox_mention(post, redfox_did):
    record = post.get("record") or {}
    text = record.get("text", "")

    if HANDLE.lower() in text.lower():
        return True

    for facet in record.get("facets", []) or []:
        for feature in facet.get("features", []) or []:
            if (
                feature.get("$type", "").endswith("#mention")
                and feature.get("did") == redfox_did
            ):
                return True

    return False


def find_blob_cid(blob):
    if not isinstance(blob, dict):
        return None

    ref = blob.get("ref")

    if isinstance(ref, dict):
        return ref.get("$link")

    if isinstance(ref, str):
        return ref

    return blob.get("cid")


def unwrap_media_embed(embed):
    if not isinstance(embed, dict):
        return {}

    embed_type = embed.get("$type", "")

    if "recordWithMedia" in embed_type:
        return embed.get("media") or {}

    return embed


def media_items(post):
    found = []

    record = post.get("record") or {}

    raw_embed = unwrap_media_embed(
        record.get("embed") or {}
    )

    view_embed = unwrap_media_embed(
        post.get("embed") or {}
    )

    raw_type = raw_embed.get("$type", "")
    view_type = view_embed.get("$type", "")

    # Afbeeldingen
    if "images" in raw_type or "images" in view_type:
        raw_images = raw_embed.get("images", []) or []
        view_images = view_embed.get("images", []) or []

        count = max(
            len(raw_images),
            len(view_images)
        )

        for index in range(count):
            raw_image = (
                raw_images[index]
                if index < len(raw_images)
                else {}
            )

            view_image = (
                view_images[index]
                if index < len(view_images)
                else {}
            )

            blob = raw_image.get("image") or {}

            cid = find_blob_cid(blob)

            url = (
                view_image.get("fullsize")
                or view_image.get("thumb")
            )

            mime = blob.get(
                "mimeType",
                "image/jpeg"
            )

            if cid or url:
                found.append({
                    "cid": cid,
                    "url": url,
                    "mime": mime,
                    "kind": "image",
                })

    # Video
    if "video" in raw_type or "video" in view_type:
        video = raw_embed.get("video") or {}

        cid = find_blob_cid(video)

        mime = video.get(
            "mimeType",
            "video/mp4"
        )

        if cid:
            found.append({
                "cid": cid,
                "url": None,
                "mime": mime,
                "kind": "video",
            })

    if not found:
        log.debug(
            "Geen media herkend: record=%s view=%s",
            raw_type or "geen",
            view_type or "geen",
        )

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

    if kind == "video":
        return ".mp4"

    return ".jpg"


def download_media(
    session,
    pds,
    did,
    item,
    destination,
):
    cid = item.get("cid")

    if cid:
        endpoint = (
            pds
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

    else:
        url = item.get("url")

        if not url:
            raise RuntimeError(
                "Geen media-CID of URL beschikbaar."
            )

        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
        )

    response.raise_for_status()

    if not response.content:
        raise RuntimeError("Leeg mediabestand ontvangen.")

    destination.write_bytes(response.content)

    return len(response.content)


def create_drive_service():
    raw = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_JSON", ""
    ).strip()

    if not raw:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON ontbreekt."
        )

    info = json.loads(raw)

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
    mime,
):
    metadata = {
        "name": filename,
        "parents": [folder_id],
        "description": f"Bluesky media van @{HANDLE}",
    }

    media = MediaFileUpload(
        str(file_path),
        mimetype=mime or "application/octet-stream",
        resumable=True,
    )

    return (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,size",
            supportsAllDrives=True,
        )
        .execute()
    )


def main():
    folder_id = os.environ.get(
        "DRIVE_FOLDER_ID", ""
    ).strip()

    if not folder_id:
        raise RuntimeError("DRIVE_FOLDER_ID ontbreekt.")

    TEMP_DIR.mkdir(exist_ok=True)

    state = load_state()
    downloaded = state["downloaded"]

    session = get_session()

    redfox_did = resolve_did(session)

    search_session, search_pds = (
        create_bluesky_search_session()
    )

    drive = create_drive_service()

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

    log.info("Zoekresultaten totaal: %s", len(posts))

    mention_count = 0
    media_count = 0
    eligible = []

    seen_posts = set()
    pds_cache = {}

    for post in posts:
        uri = post.get("uri")

        if not uri or uri in seen_posts:
            continue

        seen_posts.add(uri)

        indexed_at = post.get("indexedAt", "")

        if indexed_at and indexed_at < since_iso:
            continue

        if not contains_redfox_mention(post, redfox_did):
            continue

        mention_count += 1

        items = media_items(post)

        if items:
            media_count += len(items)
            eligible.append((post, items))

    log.info(
        "Berichten met RedFox-vermelding: %s",
        mention_count,
    )

    log.info("Media gevonden: %s", media_count)

    log.info(
        "Berichten met vermelding en media: %s",
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

        author_did = post["author"]["did"]
        rkey = uri.rsplit("/", 1)[-1]

        try:
            if author_did not in pds_cache:
                pds_cache[author_did] = get_pds(
                    session,
                    author_did,
                )

            author_pds = pds_cache[author_did]

        except Exception as exc:
            failed_count += len(items)

            log.exception(
                "PDS ophalen mislukt voor %s (%s): %s",
                author,
                author_did,
                exc,
            )
            continue

        for index, item in enumerate(items, start=1):
            cid = item.get("cid")

            unique_key = (
                f"{uri}|{cid}"
                if cid
                else f"{uri}|{item.get('url')}"
            )

            if unique_key in downloaded:
                skipped_count += 1
                continue

            suffix = extension_for(
                item["mime"],
                item["kind"],
            )

            safe_author = "".join(
                c if c.isalnum() or c in "-_" else "_"
                for c in author
            )

            filename = (
                f"{safe_author}_{rkey}_{index}{suffix}"
            )

            local_path = TEMP_DIR / filename

            try:
                size = download_media(
                    session,
                    author_pds,
                    author_did,
                    item,
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
                    "GEUPLOAD: %s (%s bytes)",
                    filename,
                    size,
                )

            except Exception as exc:
                failed_count += 1

                log.exception(
                    "Download/upload mislukt: %s | %s",
                    filename,
                    exc,
                )

            finally:
                local_path.unlink(missing_ok=True)

            time.sleep(0.5)

    save_state(state)

    log.info(
        "KLAAR | geüpload: %s | al bekend: %s | fouten: %s",
        uploaded_count,
        skipped_count,
        failed_count,
    )

    if failed_count:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        log.exception("Downloader gestopt: %s", exc)
        sys.exit(1)
