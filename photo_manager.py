
import os
import json
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

MAIN_FOLDER_ID = "1JQx_1SBOETCSMlmlQSIFOjSBuL4op5KA"
SCOPES = ["https://www.googleapis.com/auth/drive"]
DOWNLOAD_DIR = Path("photo_downloads")


def get_drive():
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not raw_json:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is niet ingesteld."
        )

    service_account_info = json.loads(raw_json)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def find_folder(drive, name, parent_id):
    escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")

    query = (
        f"name = '{escaped_name}' "
        f"and '{parent_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )

    result = drive.files().list(
        q=query,
        fields="files(id,name)",
        orderBy="name",
        pageSize=100,
    ).execute()

    folders = result.get("files", [])
    return folders[0] if folders else None


def find_ready_photo(drive, account_name):
    account_folder = find_folder(
        drive,
        account_name,
        MAIN_FOLDER_ID,
    )

    if not account_folder:
        raise RuntimeError(
            f"Accountmap niet gevonden in Google Drive: {account_name}"
        )

    ready_folder = find_folder(
        drive,
        "ready",
        account_folder["id"],
    )

    if not ready_folder:
        raise RuntimeError(
            f"Map 'ready' niet gevonden voor {account_name}"
        )

    query = (
        f"'{ready_folder['id']}' in parents "
        "and trashed = false "
        "and mimeType != 'application/vnd.google-apps.folder' "
        "and mimeType != 'application/vnd.google-apps.shortcut'"
    )

    result = drive.files().list(
        q=query,
        fields="files(id,name,mimeType,size,createdTime)",
        orderBy="createdTime",
        pageSize=100,
    ).execute()

    files = result.get("files", [])

    if not files:
        return None

    # Oudste bestand eerst.
    first = files[0]

    return {
        "account": account_name,
        "file_id": first["id"],
        "name": first["name"],
        "mime_type": first.get(
            "mimeType",
            "application/octet-stream",
        ),
        "size": int(first.get("size", 0)),
        "created_time": first.get("createdTime"),
        "ready_folder_id": ready_folder["id"],
        "account_folder_id": account_folder["id"],
    }


def download_photo(drive, photo):
    DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    safe_name = Path(photo["name"]).name
    destination = DOWNLOAD_DIR / safe_name

    if destination.exists():
        destination.unlink()

    request = drive.files().get_media(
        fileId=photo["file_id"],
    )

    with open(destination, "wb") as file_handle:
        downloader = MediaIoBaseDownload(
            file_handle,
            request,
            chunksize=1024 * 1024,
        )

        done = False

        while not done:
            status, done = downloader.next_chunk()

            if status:
                print(
                    f"   Download: {int(status.progress() * 100)}%"
                )

    downloaded_size = destination.stat().st_size

    if downloaded_size != photo["size"]:
        raise RuntimeError(
            "Gedownloade bestandsgrootte klopt niet. "
            f"Drive: {photo['size']} bytes; "
            f"download: {downloaded_size} bytes."
        )

    print("✅ Origineel bestand gedownload")
    print(f"   Bestand: {destination}")
    print(f"   Grootte: {downloaded_size} bytes")

    return destination


def move_to_used(drive, photo):
    used_folder = find_folder(
        drive,
        "used",
        photo["account_folder_id"],
    )

    if not used_folder:
        raise RuntimeError(
            f"Map 'used' niet gevonden voor {photo['account']}. "
            "Maak de map 'used' aan naast 'ready'."
        )

    print(
        f"\n📦 Bestand verplaatsen naar "
        f"{photo['account']}/used..."
    )
    print(f"   Bestand: {photo['name']}")

    drive.files().update(
        fileId=photo["file_id"],
        addParents=used_folder["id"],
        removeParents=photo["ready_folder_id"],
        fields="id,name,parents",
    ).execute()

    print("✅ Bestand succesvol verplaatst")
    print(
        f"   {photo['account']}/ready → "
        f"{photo['account']}/used"
    )


def get_photo(account_name):
    drive = get_drive()

    print(
        f"🔎 Zoeken naar media in {account_name}/ready..."
    )

    photo = find_ready_photo(
        drive,
        account_name,
    )

    if not photo:
        print(
            f"⚠️ Geen bestanden gevonden in "
            f"{account_name}/ready"
        )
        return None, None

    print("\n📸 Bestand gevonden")
    print(f"   Account: {photo['account']}")
    print(f"   Naam: {photo['name']}")
    print(f"   Type: {photo['mime_type']}")
    print(f"   Grootte: {photo['size']} bytes")

    local_path = download_photo(
        drive,
        photo,
    )

    return photo, local_path


def mark_photo_used(photo):
    if not photo:
        raise RuntimeError(
            "Kan bestand niet verplaatsen: geen bestand geselecteerd."
        )

    drive = get_drive()
    move_to_used(
        drive,
        photo,
    )


if __name__ == "__main__":
    account = os.environ.get(
        "ACCOUNT_NAME",
        os.environ.get("PHOTO_ACCOUNT", "Beautyfan"),
    )

    print("=" * 45)
    print("       BLUESKY PHOTO MANAGER")
    print("=" * 45)
    print(f"\nAccount: {account}")

    photo, local_path = get_photo(account)

    if not photo:
        print("\n📭 Geen bestand om te verwerken.")
        raise SystemExit(0)

    print("\n✅ Download geslaagd.")
    print("ℹ️ Bestand staat nog in ready totdat new_post.py")
    print("   het succesvol of na een mislukte postpoging")
    print("   naar used verplaatst.")
