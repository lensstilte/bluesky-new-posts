import os
import sys
from pathlib import Path

from atproto import Client, models


# ============================================================
# CONFIG
# ============================================================

ACCOUNT_NAME = os.getenv(
    "ACCOUNT_NAME",
    "Beautyfan"
)

BLUESKY_HANDLE = os.getenv(
    "BLUESKY_HANDLE",
    "beautyfan.bsky.social"
)

BLUESKY_PASSWORD = os.getenv(
    "BLUESKY_PASSWORD"
)


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
    print("       BLUESKY NEW POST")
    print("=" * 60)
    print()

    print(f"Account : {ACCOUNT_NAME}")
    print(f"Handle  : {BLUESKY_HANDLE}")

    # --------------------------------------------------------
    # CHECK PASSWORD
    # --------------------------------------------------------

    if not BLUESKY_PASSWORD:
        fail(
            "BLUESKY_PASSWORD is not set."
        )

    # --------------------------------------------------------
    # GET MEDIA FROM GOOGLE DRIVE
    # --------------------------------------------------------

    print()
    print("🔎 Checking Google Drive...")

    from photo_manager import get_photo

    photo, local_path = get_photo(
        ACCOUNT_NAME
    )

    # --------------------------------------------------------
    # NOTHING TO POST
    # --------------------------------------------------------

    if not photo:
        print()
        print("📭 No media found.")
        print("⏭️ Skip — nothing to post.")
        return

    local_path = Path(local_path)

    filename = photo["name"]
    media_type = get_media_type(filename)

    print()
    print(f"📁 File : {filename}")
    print(f"📦 Type : {media_type}")
    print(f"📏 Size : {photo['size']} bytes")

    if media_type is None:
        print()
        print("⚠️ Unsupported file type.")
        print("⏭️ Skip — file remains in ready.")
        return

    # --------------------------------------------------------
    # VERIFY LOCAL FILE
    # --------------------------------------------------------

    if not local_path.exists():
        fail(
            f"Downloaded file not found: "
            f"{local_path}"
        )

    local_size = local_path.stat().st_size

    if local_size != photo["size"]:
        fail(
            "Downloaded file size does not match "
            "the original Drive file."
        )

    print()
    print("✅ Original file verified")
    print(f"   Size: {local_size} bytes")

    # --------------------------------------------------------
    # GET ORIGINAL IMAGE DIMENSIONS
    # --------------------------------------------------------

    image_width = None
    image_height = None

    if media_type == "image":

        try:
            from PIL import Image

            with Image.open(local_path) as image:
                image_width, image_height = image.size

        except Exception as exc:
            fail(
                f"Could not read image dimensions: {exc}"
            )

        print()
        print("📐 Original image dimensions")
        print(f"   Width : {image_width}px")
        print(f"   Height: {image_height}px")

    # --------------------------------------------------------
    # LOGIN
    # --------------------------------------------------------

    print()
    print("🔐 Logging into Bluesky...")

    client = Client()

    try:
        client.login(
            BLUESKY_HANDLE,
            BLUESKY_PASSWORD
        )
    except Exception as exc:
        fail(
            f"Bluesky login failed: {exc}"
        )

    print("✅ Bluesky login successful")

    # --------------------------------------------------------
    # POST MEDIA
    # --------------------------------------------------------

    print()
    print("📤 Posting media...")

    try:

        with open(
            local_path,
            "rb"
        ) as media_file:

            media_data = media_file.read()

        # ====================================================
        # IMAGE
        # ====================================================

        if media_type == "image":

            uploaded = client.upload_blob(
                media_data
            ).blob

            new_image = (
                models.AppBskyEmbedImages.Image(
                    alt="",
                    image=uploaded,
                    aspect_ratio={
                        "width": image_width,
                        "height": image_height,
                    }
                )
            )

            new_embed = (
                models.AppBskyEmbedImages.Main(
                    images=[new_image]
                )
            )

            response = client.send_post(
                text="",
                embed=new_embed
            )

        # ====================================================
        # VIDEO
        # ====================================================

        elif media_type == "video":

            uploaded = client.upload_blob(
                media_data
            ).blob

            # Voor video gebruiken we dezelfde Bluesky
            # embed-methode als in het werkende
            # WomenWorld-script.
            #
            # De aspect ratio wordt hier alleen ingevuld
            # wanneer we die betrouwbaar kunnen bepalen.

            from PIL import Image

            # Video-afmetingen worden niet uit het bestand
            # gehaald met Pillow. Daarom gebruiken we voor
            # video's geen verzonnen aspect ratio.

            new_embed = (
                models.AppBskyEmbedVideo.Main(
                    video=uploaded,
                    alt=None
                )
            )

            response = client.send_post(
                text="",
                embed=new_embed
            )

        else:

            fail(
                "Unknown media type."
            )

    except Exception as exc:

        print()
        print(
            f"❌ Bluesky post failed: {exc}"
        )

        print()
        print(
            "⚠️ Media remains in "
            f"{ACCOUNT_NAME}/ready"
        )

        sys.exit(1)

    # --------------------------------------------------------
    # VERIFY SUCCESS
    # --------------------------------------------------------

    if not response:

        print()
        print(
            "❌ Bluesky returned no response."
        )

        print(
            "⚠️ Media remains in ready."
        )

        sys.exit(1)

    print()
    print("✅ Bluesky post successful")

    if hasattr(response, "uri"):
        print(
            f"🔗 URI: {response.uri}"
        )

    # --------------------------------------------------------
    # MOVE TO USED
    # --------------------------------------------------------

    print()
    print("📦 Moving media to used...")

    try:

        from photo_manager import mark_photo_used

        mark_photo_used(photo)

    except Exception as exc:

        print()
        print(
            f"❌ Could not move media to used: {exc}"
        )

        print()
        print("⚠️ IMPORTANT:")
        print(
            "   The Bluesky post WAS successful."
        )
        print(
            "   The media is still in ready."
        )

        sys.exit(1)

    # --------------------------------------------------------
    # COMPLETE
    # --------------------------------------------------------

    print()
    print("🎉 NEW POST COMPLETE!")
    print(
        f"   {ACCOUNT_NAME}/ready → "
        f"{ACCOUNT_NAME}/used"
    )
    print()
    print("=" * 60)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()