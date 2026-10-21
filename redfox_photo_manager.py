import os
import json
import random
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


# Separate manager for Redfox only. Existing photo_manager.py is untouched.
# Parent folder contains the exact subfolders "Ready" and "Used".
REDFOX_FOLDER_ID = "1io9JflAesjxaTXtCSZbQJeY-UUfmF-yb"
SCOPES = ["https://www.googleapis.com/auth/drive"]
DOWNLOAD_DIR = Path("photo_downloads")


def get_drive():
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set.")

    service_account_info = json.loads(raw_json)
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def find_child_folder(drive, folder_name, parent_id):
    escaped_name = folder_name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{escaped_name}' "
        f"and '{parent_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    result = drive.files().list(
        q=query, fields="files(id,name)", pageSize=100
    ).execute()
    folders = result.get("files", [])
    return folders[0] if folders else None


def find_ready_media(drive):
    ready_folder = find_child_folder(drive, "Ready", REDFOX_FOLDER_ID)
    if not ready_folder:
        raise RuntimeError(
            "Could not find the 'Ready' folder inside the configured Redfox folder. "
            "Check the folder ID and service-account access."
        )

    query = (
        f"'{ready_folder['id']}' in parents "
        "and trashed = false "
        "and (mimeType contains 'image/' or mimeType = 'video/mp4')"
    )

    files = []
    page_token = None
    while True:
        result = drive.files().list(
            q=query,
            fields="nextPageToken,files(id,name,mimeType,size,createdTime)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        files.extend(result.get("files", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    if not files:
        return None

    selected = random.choice(files)
    return {
        "account": "Redfox",
        "file_id": selected["id"],
        "name": selected["name"],
        "mime_type": selected["mimeType"],
        "size": int(selected.get("size", 0)),
        "created_time": selected.get("createdTime"),
        "ready_folder_id": ready_folder["id"],
        "account_folder_id": REDFOX_FOLDER_ID,
    }


def download_media(drive, media):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = DOWNLOAD_DIR / Path(media["name"]).name

    if destination.exists():
        destination.unlink()

    request = drive.files().get_media(fileId=media["file_id"])
    with destination.open("wb") as file_handle:
        downloader = MediaIoBaseDownload(
            file_handle, request, chunksize=1024 * 1024
        )
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"   Download: {int(status.progress() * 100)}%")

    actual_size = destination.stat().st_size
    if actual_size != media["size"]:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"Downloaded size mismatch: Drive={media['size']} bytes, "
            f"downloaded={actual_size} bytes."
        )

    print("✅ Redfox original media downloaded and verified")
    print(f"   File: {destination}")
    print(f"   Size: {actual_size} bytes")
    return destination


def get_photo(account_name="redheadheaven"):
    """Return (media_metadata, local_path), compatible with new_post.py."""
    drive = get_drive()
    print("🔎 Looking for media: Redfox/Ready")

    media = find_ready_media(drive)
    if not media:
        print("⚠️ No image or MP4 video found in Redfox/Ready")
        return None, None

    print("📸 Redfox media found")
    print(f"   Name: {media['name']}")
    print(f"   Type: {media['mime_type']}")
    print(f"   Size: {media['size']} bytes")

    local_path = download_media(drive, media)
    return media, local_path


def mark_photo_used(media):
    """Move media from Redfox/Ready to Redfox/Used after a successful post."""
    if not media:
        raise RuntimeError("Cannot mark Redfox media as used: no media supplied.")

    drive = get_drive()
    used_folder = find_child_folder(drive, "Used", REDFOX_FOLDER_ID)
    if not used_folder:
        raise RuntimeError(
            "Could not find the 'Used' folder inside the configured Redfox folder."
        )

    print()
    print("📦 Moving Redfox media to Used...")
    print(f"   File: {media['name']}")

    drive.files().update(
        fileId=media["file_id"],
        addParents=used_folder["id"],
        removeParents=media["ready_folder_id"],
        fields="id,name,parents",
    ).execute()

    print("✅ Redfox media moved: Ready → Used")


if __name__ == "__main__":
    selected_media, selected_path = get_photo()
    if selected_media and selected_path:
        print()
        print("✅ Download test completed. Media was NOT moved.")
        print("ℹ️ It remains in Redfox/Ready until mark_photo_used() is called.")
