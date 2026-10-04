import os
import json
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


# ============================================================
# CONFIG
# ============================================================

MAIN_FOLDER_ID = "1JQx_1SBOETCSMlmlQSIFOjSBuL4op5KA"

SCOPES = [
    "https://www.googleapis.com/auth/drive"
]

DOWNLOAD_DIR = Path("photo_downloads")


# ============================================================
# GOOGLE DRIVE
# ============================================================

def get_drive():
    raw_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

    service_account_info = json.loads(raw_json)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False
    )


# ============================================================
# FIND FOLDER
# ============================================================

def find_folder(drive, name, parent_id):
    query = (
        f"name = '{name}' "
        f"and '{parent_id}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )

    result = drive.files().list(
        q=query,
        fields="files(id,name)",
        orderBy="name"
    ).execute()

    folders = result.get("files", [])

    if not folders:
        return None

    return folders[0]


# ============================================================
# FIND READY MEDIA
# ============================================================

def find_ready_photo(drive, account_name):
    account_folder = find_folder(
        drive,
        account_name,
        MAIN_FOLDER_ID
    )

    if not account_folder:
        raise RuntimeError(
            f"Account folder not found: {account_name}"
        )

    ready_folder = find_folder(
        drive,
        "ready",
        account_folder["id"]
    )

    if not ready_folder:
        raise RuntimeError(
            f"ready folder not found for: {account_name}"
        )

    # Supports:
    # - JPG
    # - JPEG
    # - PNG
    # - WEBP
    # - MP4
    query = (
        f"'{ready_folder['id']}' in parents "
        f"and trashed = false "
        f"and (mimeType contains 'image/' "
        f"or mimeType = 'video/mp4')"
    )

    result = drive.files().list(
        q=query,
        fields="files(id,name,mimeType,size,createdTime)",
        orderBy="createdTime"
    ).execute()

    files = result.get("files", [])

    if not files:
        return None

    return {
        "account": account_name,
        "file_id": files[0]["id"],
        "name": files[0]["name"],
        "mime_type": files[0]["mimeType"],
        "size": int(files[0].get("size", 0)),
        "created_time": files[0].get("createdTime"),
        "ready_folder_id": ready_folder["id"],
        "account_folder_id": account_folder["id"],
    }


# ============================================================
# DOWNLOAD
# ============================================================

def download_photo(drive, photo):
    DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    destination = DOWNLOAD_DIR / photo["name"]

    if destination.exists():
        destination.unlink()

    request = drive.files().get_media(
        fileId=photo["file_id"]
    )

    with open(destination, "wb") as fh:
        downloader = MediaIoBaseDownload(
            fh,
            request,
            chunksize=1024 * 1024
        )

        done = False

        while not done:
            status, done = downloader.next_chunk()

            if status:
                progress = int(status.progress() * 100)
                print(f"   Download: {progress}%")

    downloaded_size = destination.stat().st_size

    if downloaded_size != photo["size"]:
        raise RuntimeError(
            f"Downloaded size mismatch! "
            f"Drive={photo['size']} bytes, "
            f"Downloaded={downloaded_size} bytes"
        )

    print("✅ Original file downloaded")
    print(f"   File: {destination}")
    print(f"   Size: {downloaded_size} bytes")

    return destination


# ============================================================
# MOVE TO USED
# ============================================================

def move_to_used(drive, photo):
    used_folder = find_folder(
        drive,
        "used",
        photo["account_folder_id"]
    )

    if not used_folder:
        raise RuntimeError(
            f"used folder not found for: {photo['account']}"
        )

    print()
    print(f"📦 Moving to {photo['account']}/used...")
    print(f"   File: {photo['name']}")

    drive.files().update(
        fileId=photo["file_id"],
        addParents=used_folder["id"],
        removeParents=photo["ready_folder_id"],
        fields="id,name,parents"
    ).execute()

    print("✅ Media moved successfully")
    print(
        f"   {photo['account']}/ready → "
       
