import os
import sys
from pathlib import Path

from atproto import Client, models


ACCOUNT_NAME = "redheadheaven"
BLUESKY_HANDLE = os.getenv("BLUESKY_HANDLE", "redheadheaven.bsky.social")
BLUESKY_PASSWORD = os.getenv("BLUESKY_PASSWORD")
POST_TEXT = os.getenv(
    "POST_TEXT",
    "♥️ @redfoxofficial.bsky.social ♥️",
)
MENTION_HANDLE = "redfoxofficial.bsky.social"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4"}


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


def build_post_facets(client, text):
    """Make the RedFox handle a clickable Bluesky mention when present."""
    token = f"@{MENTION_HANDLE}"
    start_char = text.find(token)
    if start_char < 0:
        return None

    try:
        identity = client.com.atproto.identity.resolve_handle(
            {"handle": MENTION_HANDLE}
        )
        did = identity.did
    except Exception as exc:
        print(f"⚠️ Could not resolve mention handle; posting text without clickable facet: {exc}")
        return None

    byte_start = len(text[:start_char].encode("utf-8"))
    byte_end = byte_start + len(token.encode("utf-8"))
    mention = models.AppBskyRichtextFacet.Mention(did=did)
    facet = models.AppBskyRichtextFacet.Main(
        index=models.AppBskyRichtextFacet.ByteSlice(
            byte_start=byte_start,
            byte_end=byte_end,
        ),
        features=[mention],
    )
    return [facet]


def main():
    print("=" * 60)
    print("       REDFOX NEW POST")
    print("=" * 60)
    print()
    print(f"Account : {ACCOUNT_NAME}")
    print(f"Handle  : {BLUESKY_HANDLE}")

    if not BLUESKY_PASSWORD:
        fail("BLUESKY_PASSWORD is not set.")
    if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
        fail("GOOGLE_SERVICE_ACCOUNT_JSON is not set.")

    print()
    print("🔎 Checking Redfox Google Drive folder...")
    from redfox_photo_manager import get_photo, mark_photo_used

    photo, local_path = get_photo(ACCOUNT_NAME)
    if not photo:
        print("📭 No media found. Skip — nothing to post.")
        return

    local_path = Path(local_path)
    media_type = get_media_type(photo["name"])
    print()
    print(f"📁 File : {photo['name']}")
    print(f"📦 Type : {media_type}")
    print(f"📏 Size : {photo['size']} bytes")

    if media_type is None:
        fail("Unsupported file type. Media remains in Ready.")
    if not local_path.exists():
        fail(f"Downloaded file not found: {local_path}")
    if local_path.stat().st_size != photo["size"]:
        fail("Downloaded file size does not match the original Drive file.")

    image_width = image_height = None
    if media_type == "image":
        try:
            from PIL import Image
            with Image.open(local_path) as image:
                image_width, image_height = image.size
        except Exception as exc:
            fail(f"Could not read image dimensions: {exc}")

    print()
    print("🔐 Logging into Bluesky...")
    client = Client()
    try:
        client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
    except Exception as exc:
        fail(f"Bluesky login failed: {exc}")
    print("✅ Bluesky login successful")

    print()
    print("📤 Posting Redfox media...")
    try:
        with local_path.open("rb") as media_file:
            media_data = media_file.read()
        uploaded = client.upload_blob(media_data).blob
        facets = build_post_facets(client, POST_TEXT)

        if media_type == "image":
            image = models.AppBskyEmbedImages.Image(
                alt="",
                image=uploaded,
                aspect_ratio={"width": image_width, "height": image_height},
            )
            embed = models.AppBskyEmbedImages.Main(images=[image])
        else:
            embed = models.AppBskyEmbedVideo.Main(video=uploaded, alt=None)

        post_kwargs = {"text": POST_TEXT, "embed": embed}
        if facets:
            post_kwargs["facets"] = facets
        response = client.send_post(**post_kwargs)
    except Exception as exc:
        print(f"❌ Bluesky post failed: {exc}")
        print("⚠️ Media remains in Redfox/Ready.")
        sys.exit(1)

    if not response:
        fail("Bluesky returned no response. Media remains in Ready.")

    print("✅ Bluesky post successful")
    if hasattr(response, "uri"):
        print(f"🔗 URI: {response.uri}")

    print()
    print("📦 Moving media to Redfox/Used...")
    try:
        mark_photo_used(photo)
    except Exception as exc:
        print(f"❌ Post succeeded, but moving media failed: {exc}")
        print("⚠️ The media may still be in Redfox/Ready.")
        sys.exit(1)

    print()
    print("🎉 REDFOX NEW POST COMPLETE!")
    print("   redheadheaven/Redfox/Ready → redheadheaven/Redfox/Used")


if __name__ == "__main__":
    main()
