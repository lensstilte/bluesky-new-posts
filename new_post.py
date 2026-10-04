import os
import sys
from pathlib import Path

from atproto import Client, Request
from httpx import Timeout

from photo_manager import get_photo, mark_photo_used


# ============================================================
# CONFIG
# ============================================================

ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "Beautyfan")
BLUESKY_HANDLE = os.getenv(
    "BLUESKY_HANDLE",
    "beautyfan.bsky.social"
)
BLUESKY_PASSWORD = os.getenv("BLUESKY_PASSWORD")

DOWNLOAD_DIR = Path("downloads")

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
}

VIDEO_EXTENSIONS = {
    ".mp4",
}


# ============================================================
# HELPERS
# ============================================================

def fail(message):
    print(f"❌ {message}")
    sys.exit(1)


def get_media_type(filename):
    extension = Path(filename).suffix.lower()

    if extension in IMAGE_EXTENSIONS:
        return "image"

    if extension in VIDEO_EXTENSIONS:
        return "video"

    return None


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("BLUESKY NEW POST")
    print("=" * 60)

    print(f"Account : {ACCOUNT_NAME}")
    print(f"Handle  : {BLUESKY_HANDLE}")

    if not BLUESKY_PASSWORD:
        fail("BLUESKY_PASSWORD is not set.")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------
    # FIND MEDIA IN GOOGLE DRIVE
    # --------------------------------------------------------

    print()
    print("🔎 Looking for media in Google Drive...")

    photo = get_photo(ACCOUNT_NAME)

    if not photo:
        print("📭 No media found in ready.")
        print("⏭️ Nothing to post. Skipping.")
        return

    filename = photo["name"]
    media_type = get_media_type(filename)

    print(f"📁 File    : {filename}")
    print(f"📦 Type    : {media_type or 'unsupported'}")
    print(f"📏 Size    : {photo['size']} bytes")

    if media_type is None:
        print("⚠️ Unsupported file type.")
        print("⏭️ Skipping without moving the file.")
        return

    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    print()
    print("⬇️ Downloading original media...")

    local_path = photo["local_path"]

    if not local_path:
        fail("Photo Manager did not return a local file.")

    local_path = Path(local_path)

    if not local_path.exists():
        fail(f"Downloaded file does not exist: {local_path}")

    local_size = local_path.stat().st_size

    if local_size != photo["size"]:
        fail(
            f"File size mismatch! "
            f"Drive={photo['size']} "
            f"Local={local_size}"
        )

    print("✅ Original file downloaded")
    print(f"   Local size: {local_size} bytes")

    # --------------------------------------------------------
    # BLUESKY LOGIN
    # --------------------------------------------------------

    print()
    print("🔐 Logging into Bluesky...")

    # Longer timeout because video uploads can take longer.
    request = Request(
        timeout=Timeout(
            connect=30.0,
            read=300.0,
            write=300.0,
            pool=30.0,
        )
    )

    client = Client(request=request)

    try:
        client.login(
            BLUESKY_HANDLE,
            BLUESKY_PASSWORD
        )
    except Exception as exc:
        fail(f"Bluesky login failed: {exc}")

    print("✅ Bluesky login successful")

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    print()
    print("📤 Posting media to Bluesky...")

    try:

        with open(local_path, "rb") as media_file:
            media_data = media_file.read()

        if media_type == "image":

            response = client.send_image(
                text="",
                image=media_data,
                image_alt="",
            )

        elif media_type == "video":

            response = client.send_video(
                text="",
                video=media_data,
                video_alt="",
            )

        else:
            fail("Unknown media type.")

    except Exception as exc:
        print(f"❌ Bluesky post failed: {exc}")
        print()
        print("⚠️ IMPORTANT:")
        print("   Media stays in Google Drive/ready.")
        print("   It will NOT be moved to used.")
        sys.exit(1)

    # --------------------------------------------------------
    # POST SUCCESS
    # --------------------------------------------------------

    if not response:
        print("❌ Bluesky returned no post response.")
        print("⚠️ Media remains in ready.")
        sys.exit(1)

    print("✅ Bluesky post successful")

    if hasattr(response, "uri"):
        print(f"🔗 URI: {response.uri}")

    # --------------------------------------------------------
    # MOVE TO USED
    # --------------------------------------------------------

    print()
    print("📦 Moving media to used...")

    try:
        mark_photo_used(photo)
    except Exception as exc:
        print(f"❌ Could not move media to used: {exc}")
        print()
        print("⚠️ The Bluesky post WAS successful.")
        print("⚠️ The file remains in ready until the move is fixed.")
        sys.exit(1)

    print("✅ Media moved successfully")
    print(f"   {ACCOUNT_NAME}/ready → {ACCOUNT_NAME}/used")

    print()
    print("🎉 NEW POST COMPLETE!")
    print("=" * 60)


if __name__ == "__main__":
    main()
