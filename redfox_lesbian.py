#!/usr/bin/env python3
"""
RedFox Lesbian
- Selects exactly one random image/video from the Google Drive RedFox folder.
- Automatically discovers source folders (all immediate subfolders except "used").
- Special caption for "ready"; other folders use their folder name + clickable RedFox mention.
- Converts images with Pillow and videos with FFmpeg to Bluesky-friendly formats.
- After a successful post, moves the original Drive file into used/<source-folder>/.
- Keeps a 14-day posting history. After 14 days, a previously posted file becomes eligible again.
"""

import io
import json
import os
import random
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atproto import Client, models
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


# ============================================================
# CONFIGURATION
# ============================================================

BLUESKY_HANDLE = os.getenv("BLUESKY_HANDLE", "sappicheaven.bsky.social")
BLUESKY_PASSWORD = os.getenv("BLUESKY_PASSWORD")

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# This is the main Google Drive folder that contains the account folders.
DRIVE_MAIN_FOLDER_ID = os.getenv(
    "DRIVE_MAIN_FOLDER_ID",
    "1Sv-eqhefclMV25cCH8HFsOjV9BTYa-ag",
)

# The script locates the folder named "redfox" (case-insensitive)
# directly inside DRIVE_MAIN_FOLDER_ID.
SOURCE_ROOT_FOLDER_NAME = os.getenv("SOURCE_ROOT_FOLDER_NAME", "redfox")

STATE_FILE = Path("redfox_lesbian_state.json")
STATE_RETENTION_DAYS = 14

REDFOX_HANDLE = "redfoxofficial.bsky.social"
READY_POST_TEXT = (
    "♥️ @redfoxofficial.bsky.social ♥️\n"
    "🦊 onlyfans.com/redfoxofficial 🦊"
)

IMAGE_MAX_BYTES = 1_900_000
IMAGE_MAX_DIMENSION = 2400

# Bluesky video upload safety target; FFmpeg lowers bitrate when needed.
VIDEO_TARGET_BYTES = 95 * 1024 * 1024
VIDEO_MAX_SECONDS = 180

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv",
    ".flv", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts", ".ogv"
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def log(message):
    print(f"[RedFox Lesbian] {message}", flush=True)


def load_state():
    if not STATE_FILE.exists():
        return {"posted": {}}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {"posted": {}}
        posted = data.get("posted", {})
        if not isinstance(posted, dict):
            posted = {}
        return {"posted": posted}
    except (OSError, json.JSONDecodeError) as exc:
        log(f"WARNING: state file kon niet worden gelezen: {exc}")
        return {"posted": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
    temporary.replace(STATE_FILE)


def prune_state(state):
    cutoff = utc_now() - timedelta(days=STATE_RETENTION_DAYS)
    retained = {}

    for file_id, timestamp in state.get("posted", {}).items():
        try:
            posted_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
            if posted_at >= cutoff:
                retained[file_id] = posted_at.isoformat()
        except (TypeError, ValueError, AttributeError):
            continue

    state["posted"] = retained
    return state


def posted_within_retention(state, file_id):
    timestamp = state.get("posted", {}).get(file_id)
    if not timestamp:
        return False

    try:
        posted_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        return utc_now() - posted_at < timedelta(days=STATE_RETENTION_DAYS)
    except (TypeError, ValueError, AttributeError):
        return False


# ============================================================
# GOOGLE DRIVE
# ============================================================

def create_drive_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GitHub Secret GOOGLE_SERVICE_ACCOUNT_JSON ontbreekt.")

    credentials_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=DRIVE_SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def escape_drive_query(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_children(service, parent_id):
    results = []
    page_token = None

    while True:
        response = service.files().list(
            q=f"'{parent_id}' in parents and trashed = false",
            fields="nextPageToken, files(id,name,mimeType,size,parents,webViewLink)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


def find_named_folder(service, parent_id, folder_name):
    wanted = folder_name.strip().lower()
    for item in list_children(service, parent_id):
        if item.get("mimeType") == "application/vnd.google-apps.folder":
            if item.get("name", "").strip().lower() == wanted:
                return item
    return None


def find_redfox_root(service):
    folder = find_named_folder(service, DRIVE_MAIN_FOLDER_ID, SOURCE_ROOT_FOLDER_NAME)
    if not folder:
        names = [
            item.get("name", "")
            for item in list_children(service, DRIVE_MAIN_FOLDER_ID)
            if item.get("mimeType") == "application/vnd.google-apps.folder"
        ]
        raise RuntimeError(
            f"Map '{SOURCE_ROOT_FOLDER_NAME}' niet gevonden direct onder de hoofdmap. "
            f"Gevonden mappen: {names}"
        )
    return folder


def ensure_folder(service, parent_id, folder_name):
    existing = find_named_folder(service, parent_id, folder_name)
    if existing:
        return existing["id"]

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = service.files().create(
        body=metadata,
        fields="id,name",
        supportsAllDrives=True,
    ).execute()
    log(f"Drive-map aangemaakt: used/{folder_name}")
    return created["id"]


def download_drive_file(service, file_id):
    request = service.files().get_media(
        fileId=file_id,
        supportsAllDrives=True,
    )
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    return buffer.getvalue()


def move_file_to_used(service, file_id, source_folder_id, used_category_folder_id):
    service.files().update(
        fileId=file_id,
        addParents=used_category_folder_id,
        removeParents=source_folder_id,
        fields="id,name,parents",
        supportsAllDrives=True,
    ).execute()


def move_file_within_used(service, file_id, current_parent_id, used_category_folder_id):
    if current_parent_id == used_category_folder_id:
        return

    service.files().update(
        fileId=file_id,
        addParents=used_category_folder_id,
        removeParents=current_parent_id,
        fields="id,name,parents",
        supportsAllDrives=True,
    ).execute()


def recycle_expired_used_files(service, redfox_root, state):
    """Return expired archived media to its category source folder, then drop its state entry."""
    cutoff = utc_now() - timedelta(days=STATE_RETENTION_DAYS)
    posted = state.get("posted", {})
    if not posted:
        return

    used_root = find_named_folder(service, redfox_root["id"], "used")
    if not used_root:
        return

    used_categories = [
        item for item in list_children(service, used_root["id"])
        if item.get("mimeType") == "application/vnd.google-apps.folder"
    ]
    used_by_id = {}
    for category_folder in used_categories:
        for item in list_children(service, category_folder["id"]):
            used_by_id[item["id"]] = (item, category_folder)

    for file_id, timestamp in list(posted.items()):
        try:
            posted_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, AttributeError):
            posted.pop(file_id, None)
            continue

        if posted_at >= cutoff:
            continue

        archived = used_by_id.get(file_id)
        if archived:
            file_item, category_folder = archived
            source_folder_id = ensure_folder(
                service,
                redfox_root["id"],
                category_folder["name"],
            )
            service.files().update(
                fileId=file_id,
                addParents=source_folder_id,
                removeParents=category_folder["id"],
                fields="id,name,parents",
                supportsAllDrives=True,
            ).execute()
            log(
                f"14 dagen verstreken: '{file_item['name']}' teruggezet naar "
                f"'{category_folder['name']}' en opnieuw beschikbaar."
            )

        # Expired history is removed from the JSON state.
        posted.pop(file_id, None)


def discover_candidates(service, redfox_root, state):
    children = list_children(service, redfox_root["id"])
    folders = [
        item for item in children
        if item.get("mimeType") == "application/vnd.google-apps.folder"
        and item.get("name", "").strip().lower() != "used"
    ]

    candidates = []
    source_folder_ids = set()

    # New media from every source folder.
    for folder in folders:
        source_folder_ids.add(folder["id"])
        for item in list_children(service, folder["id"]):
            suffix = Path(item.get("name", "")).suffix.lower()
            if suffix not in MEDIA_EXTENSIONS:
                continue
            if posted_within_retention(state, item["id"]):
                continue

            candidates.append({
                "file": item,
                "category": folder["name"],
                "parent_id": folder["id"],
                "is_already_used": False,
            })

    return candidates


# ============================================================
# BLUESKY FACETS / POSTING
# ============================================================

def build_facets(client, text, include_onlyfans=False):
    facets = []
    encoded = text.encode("utf-8")

    mention = f"@{REDFOX_HANDLE}"
    mention_bytes = mention.encode("utf-8")
    mention_start = encoded.find(mention_bytes)

    if mention_start >= 0:
        profile = client.app.bsky.actor.get_profile({"actor": REDFOX_HANDLE})
        facets.append(
            models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(
                    byte_start=mention_start,
                    byte_end=mention_start + len(mention_bytes),
                ),
                features=[
                    models.AppBskyRichtextFacet.Mention(did=profile.did)
                ],
            )
        )

    if include_onlyfans:
        link_text = "onlyfans.com/redfoxofficial"
        link_bytes = link_text.encode("utf-8")
        link_start = encoded.find(link_bytes)
        if link_start >= 0:
            facets.append(
                models.AppBskyRichtextFacet.Main(
                    index=models.AppBskyRichtextFacet.ByteSlice(
                        byte_start=link_start,
                        byte_end=link_start + len(link_bytes),
                    ),
                    features=[
                        models.AppBskyRichtextFacet.Link(
                            uri="https://onlyfans.com/redfoxofficial"
                        )
                    ],
                )
            )

    return facets or None


def make_caption(folder_name):
    if folder_name.strip().lower() == "ready":
        return READY_POST_TEXT, True

    # Folder name is plain text; only the RedFox handle is a clickable mention.
    caption = f"♥️ {folder_name.strip()} @{REDFOX_HANDLE} ♥️"
    return caption, False


# ============================================================
# IMAGE PROCESSING (PILLOW)
# ============================================================

def prepare_image(data, original_name):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)

    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")

    image.thumbnail((IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION), Image.Resampling.LANCZOS)

    quality = 90
    while True:
        output = io.BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
        result = output.getvalue()

        if len(result) <= IMAGE_MAX_BYTES:
            return result, "image/jpeg"

        if quality > 45:
            quality -= 7
            continue

        # If quality alone is insufficient, reduce dimensions and retry.
        width, height = image.size
        if max(width, height) <= 800:
            raise RuntimeError("Afbeelding kan niet klein genoeg worden gemaakt.")
        image = image.resize(
            (max(1, int(width * 0.8)), max(1, int(height * 0.8))),
            Image.Resampling.LANCZOS,
        )
        quality = 82


# ============================================================
# VIDEO PROCESSING (FFMPEG)
# ============================================================

def require_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg/FFprobe niet gevonden. Installeer FFmpeg in de workflow.")


def probe_video(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError("Videoduur kon niet worden bepaald.")


def prepare_video(data, original_name):
    require_ffmpeg()

    suffix = Path(original_name).suffix.lower() or ".video"
    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / f"input{suffix}"
        output_path = Path(temp_dir) / "output.mp4"
        input_path.write_bytes(data)

        duration = probe_video(input_path)
        if duration <= 0:
            raise RuntimeError("Ongeldige videoduur.")
        if duration > VIDEO_MAX_SECONDS:
            raise RuntimeError(
                f"Video is {duration:.1f} seconden; maximaal {VIDEO_MAX_SECONDS} seconden toegestaan."
            )

        # Try progressively lower bitrates if the first MP4 is too large.
        audio_bitrate = "128k"
        video_bitrates = ["5000k", "3500k", "2500k", "1800k", "1200k", "800k", "500k"]

        for video_bitrate in video_bitrates:
            if output_path.exists():
                output_path.unlink()

            command = [
                "ffmpeg", "-y", "-i", str(input_path),
                "-map", "0:v:0", "-map", "0:a?",
                "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-b:v", video_bitrate,
                "-maxrate", video_bitrate, "-bufsize", "2M",
                "-c:a", "aac", "-b:a", audio_bitrate,
                "-movflags", "+faststart",
                "-t", str(VIDEO_MAX_SECONDS),
                str(output_path),
            ]

            subprocess.run(command, check=True, capture_output=True, text=True)
            if output_path.exists() and output_path.stat().st_size <= VIDEO_TARGET_BYTES:
                return output_path.read_bytes(), "video/mp4"

        raise RuntimeError(
            "Video blijft na conversie te groot voor de ingestelde uploadlimiet."
        )


# ============================================================
# BLUESKY MEDIA EMBEDS
# ============================================================

def create_image_embed(client, media_data):
    from PIL import Image

    uploaded = client.upload_blob(media_data).blob
    with Image.open(io.BytesIO(media_data)) as image:
        width, height = image.size

    image_item = models.AppBskyEmbedImages.Image(
        alt="",
        image=uploaded,
        aspect_ratio={"width": width, "height": height},
    )
    return models.AppBskyEmbedImages.Main(images=[image_item])


def create_video_embed(client, media_data):
    uploaded = client.upload_blob(media_data).blob
    return models.AppBskyEmbedVideo.Main(
        video=uploaded,
        alt=None,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BLUESKY_PASSWORD:
        raise RuntimeError("GitHub Secret BLUESKY_PASSWORD ontbreekt.")

    require_ffmpeg()

    state = load_state()

    drive = create_drive_service()
    redfox_root = find_redfox_root(drive)
    log(f"Google Drive-map gevonden: {redfox_root['name']} ({redfox_root['id']})")

    recycle_expired_used_files(drive, redfox_root, state)
    state = prune_state(state)
    save_state(state)

    candidates = discover_candidates(drive, redfox_root, state)
    if not candidates:
        log("Geen geschikte nieuwe media beschikbaar.")
        return

    # Choose a source category first, then one random media item from that category.
    categories = sorted({item["category"] for item in candidates}, key=str.lower)
    chosen_category = random.choice(categories)
    category_candidates = [
        item for item in candidates if item["category"] == chosen_category
    ]
    selected = random.choice(category_candidates)

    drive_file = selected["file"]
    file_name = drive_file["name"]
    file_id = drive_file["id"]
    log(f"Gekozen map: {chosen_category}")
    log(f"Gekozen bestand: {file_name}")

    original_data = download_drive_file(drive, file_id)
    suffix = Path(file_name).suffix.lower()

    if suffix in IMAGE_EXTENSIONS:
        media_data, mime_type = prepare_image(original_data, file_name)
        media_kind = "image"
    elif suffix in VIDEO_EXTENSIONS:
        media_data, mime_type = prepare_video(original_data, file_name)
        media_kind = "video"
    else:
        raise RuntimeError(f"Niet-ondersteund mediabestand: {file_name}")

    log(f"Media verwerkt: {media_kind}, {len(media_data):,} bytes, {mime_type}")

    client = Client()
    client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
    log(f"Ingelogd als {BLUESKY_HANDLE}")

    caption, is_ready = make_caption(chosen_category)
    facets = build_facets(client, caption, include_onlyfans=is_ready)

    if media_kind == "image":
        embed = create_image_embed(client, media_data)
    else:
        embed = create_video_embed(client, media_data)

    response = client.send_post(
        text=caption,
        embed=embed,
        facets=facets,
    )

    # Only archive and record the file after Bluesky confirms the post.
    used_root_id = ensure_folder(drive, redfox_root["id"], "used")
    used_category_id = ensure_folder(drive, used_root_id, chosen_category)

    if selected["is_already_used"]:
        move_file_within_used(
            drive,
            file_id,
            selected["parent_id"],
            used_category_id,
        )
    else:
        move_file_to_used(
            drive,
            file_id,
            selected["parent_id"],
            used_category_id,
        )

    state.setdefault("posted", {})[file_id] = iso_now()
    state = prune_state(state)
    save_state(state)

    log("Succesvol gepubliceerd op Bluesky.")
    log(f"Post URI: {getattr(response, 'uri', 'onbekend')}")
    log(f"Origineel bestand verplaatst naar used/{chosen_category}/")
    log(f"Dit bestand wordt na {STATE_RETENTION_DAYS} dagen opnieuw verkiesbaar.")


if __name__ == "__main__":
    main()
