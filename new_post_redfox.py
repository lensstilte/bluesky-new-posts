
import os
import sys
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atproto import Client, models


# ============================================================
# CONFIGURATION
# ============================================================

ACCOUNT_NAME = "redheadheaven"

BLUESKY_HANDLE = os.getenv(
    "BLUESKY_HANDLE",
    "redheadheaven.bsky.social"
)
BLUESKY_PASSWORD = os.getenv("BLUESKY_PASSWORD")

POST_TEXT = os.getenv(
    "POST_TEXT",
    "♥️ @redfoxofficial.bsky.social ♥️\n"
    "🦊 onlyfans.com/redfoxofficial 🦊",
)

MENTION_HANDLE = "redfoxofficial.bsky.social"
MENTION_TEXT = "@redfoxofficial.bsky.social"

FEED_URI = (
    "at://did:plc:cxrt7ggxkamgzxa47cggtees/"
    "app.bsky.feed.generator/aaaoirmgh53zw"
)

FEED_LOOKBACK_HOURS = 5
FEED_PAGE_SIZE = 100
MAX_FEED_PAGES = 20

STATE_FILE = (
    Path(__file__).resolve().parent /
    "redfox_feed_state.json"
)

STATE_RETENTION_DAYS = 90

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4"}


# ============================================================
# GENERAL FUNCTIONS
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


def parse_timestamp(value):
    if not value:
        return None

    try:
        if isinstance(value, datetime):
            result = value
        else:
            result = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


# ============================================================
# MEDIA DETECTION
# ============================================================

def embed_has_media(embed):
    if not embed:
        return False

    # Direct image embed
    if getattr(embed, "images", None):
        return True

    # Direct video embed
    if getattr(embed, "video", None):
        return True

    # Quote with media
    nested_media = getattr(embed, "media", None)

    if nested_media:
        if getattr(nested_media, "images", None):
            return True

        if getattr(nested_media, "video", None):
            return True

    # Some embed models contain nested record/media objects
    record_with_media = getattr(embed, "record_with_media", None)

    if record_with_media:
        nested = getattr(record_with_media, "media", None)

        if nested:
            if getattr(nested, "images", None):
                return True

            if getattr(nested, "video", None):
                return True

    return False


def post_has_media(post):
    if embed_has_media(getattr(post, "embed", None)):
        return True

    record = getattr(post, "record", None)

    if record and embed_has_media(
        getattr(record, "embed", None)
    ):
        return True

    return False


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_feed_state():
    if not STATE_FILE.exists():
        print("📝 No previous feed state found.")
        state = {"processed": {}}

        # Create initial state file
        save_feed_state(state)

        return state

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("Invalid state format")

        if not isinstance(state.get("processed"), dict):
            raise ValueError("Missing processed entries")

        print(
            f"📝 Loaded {len(state['processed'])} "
            "previously processed items."
        )

        return state

    except Exception as exc:
        raise RuntimeError(
            f"Could not read feed state: {exc}"
        )


def save_feed_state(state):
    temporary_file = STATE_FILE.with_suffix(".tmp")

    cutoff = datetime.now(timezone.utc) - timedelta(
        days=STATE_RETENTION_DAYS
    )

    cleaned = {}

    for uri, processed_at in state["processed"].items():
        timestamp = parse_timestamp(processed_at)

        if timestamp and timestamp >= cutoff:
            cleaned[uri] = processed_at

    state["processed"] = cleaned

    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(
                state,
                file,
                indent=2,
                ensure_ascii=False
            )

        os.replace(temporary_file, STATE_FILE)

    except Exception as exc:
        raise RuntimeError(
            f"Could not save feed state: {exc}"
        )


def mark_feed_item_processed(state, uri):
    state["processed"][uri] = (
        datetime.now(timezone.utc).isoformat()
    )

    save_feed_state(state)


# ============================================================
# CHECK EXISTING REPOST
# ============================================================

def already_reposted_by_us(client, uri, cid, our_did):
    cursor = None

    while True:
        response = client.get_reposted_by(
            uri=uri,
            cid=cid,
            cursor=cursor,
            limit=100
        )

        for actor in response.reposted_by:
            if actor.did == our_did:
                return True

        cursor = getattr(response, "cursor", None)

        if not cursor:
            break

    return False


# ============================================================
# CHECK EXISTING LIKE
# ============================================================

def already_liked_by_us(client, uri, cid, our_did):
    cursor = None

    while True:
        response = client.get_likes(
            uri=uri,
            cid=cid,
            cursor=cursor,
            limit=100
        )

        for like in response.likes:
            actor = getattr(like, "actor", None)

            if actor and actor.did == our_did:
                return True

        cursor = getattr(response, "cursor", None)

        if not cursor:
            break

    return False


# ============================================================
# REDFOX MENTIONS FEED
# ============================================================

def repost_redfox_feed(client):

    print()
    print("=" * 60)
    print("       REDFOX MENTIONS FEED")
    print("=" * 60)

    try:
        state = load_feed_state()

    except Exception as exc:
        print(f"❌ State error: {exc}")
        print("⚠️ Feed processing stopped to prevent duplicates.")
        return

    our_did = getattr(client.me, "did", None)

    if not our_did:
        print("❌ Could not determine RedheadHeaven DID.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=FEED_LOOKBACK_HOURS
    )

    print(f"🔎 Required mention : {MENTION_TEXT}")
    print(f"🕒 Lookback         : {FEED_LOOKBACK_HOURS} hours")
    print("📥 Fetching feed...")

    candidates = {}

    cursor = None
    pages_read = 0

    try:
        while pages_read < MAX_FEED_PAGES:

            params = {
                "feed": FEED_URI,
                "limit": FEED_PAGE_SIZE
            }

            if cursor:
                params["cursor"] = cursor

            response = client.app.bsky.feed.get_feed(params)

            pages_read += 1

            for item in response.feed:

                post = item.post

                record = getattr(post, "record", None)

                if not record:
                    continue

                # Include posts AND replies.
                # No author filter: this is a mentions feed.

                text = getattr(record, "text", "") or ""

                # Exact Bluesky handle must appear in text
                if MENTION_TEXT.lower() not in text.lower():
                    continue

                # Only posts containing actual media
                if not post_has_media(post):
                    continue

                timestamp = parse_timestamp(
                    getattr(record, "created_at", None)
                )

                if not timestamp:
                    timestamp = parse_timestamp(
                        getattr(post, "indexed_at", None)
                    )

                if not timestamp:
                    continue

                if timestamp < cutoff:
                    continue

                candidates[post.uri] = {
                    "uri": post.uri,
                    "cid": post.cid,
                    "timestamp": timestamp,
                    "text": text
                }

            cursor = getattr(response, "cursor", None)

            if not cursor:
                break

        print(f"📄 Feed pages read : {pages_read}")
        print(f"🎯 Matching media  : {len(candidates)}")

        if pages_read >= MAX_FEED_PAGES and cursor:
            print(
                "⚠️ Page limit reached. "
                "Some older feed items may not have been checked."
            )

    except Exception as exc:
        print(f"❌ Feed retrieval failed: {exc}")
        print("⚠️ Continuing with Google Drive media.")
        return

    # Oldest first, newest last
    ordered_posts = sorted(
        candidates.values(),
        key=lambda item: item["timestamp"]
    )

    reposted_count = 0
    liked_count = 0
    skipped_count = 0
    error_count = 0

    for item in ordered_posts:

        uri = item["uri"]
        cid = item["cid"]

        # Already processed in a previous workflow run
        if uri in state["processed"]:
            print(f"⏭️ Already in state: {uri}")
            skipped_count += 1
            continue

        print()
        print(f"🔎 Processing: {uri}")
        print(f"📝 Text: {item['text'][:120]}")

        try:

            # --------------------------------------------
            # REPOST
            # --------------------------------------------

            if already_reposted_by_us(
                client,
                uri,
                cid,
                our_did
            ):
                print("⏭️ Already reposted by RedheadHeaven.")

            else:
                client.repost(uri, cid)

                reposted_count += 1

                print("✅ Reposted successfully.")

            # --------------------------------------------
            # LIKE
            # --------------------------------------------

            if already_liked_by_us(
                client,
                uri,
                cid,
                our_did
            ):
                print("⏭️ Already liked by RedheadHeaven.")

            else:
                client.like(uri, cid)

                liked_count += 1

                print("❤️ Liked successfully.")

            # --------------------------------------------
            # SAVE STATE
            # --------------------------------------------

            mark_feed_item_processed(state, uri)

            print("💾 Saved to feed state.")

        except Exception as exc:

            error_count += 1

            print(f"❌ Processing failed: {exc}")
            print("⚠️ Item not marked as processed.")
            print("   It can be retried during the next run.")

    print()
    print("=" * 60)
    print("       REDFOX FEED SUMMARY")
    print("=" * 60)

    print(f"   Matching media : {len(candidates)}")
    print(f"   New reposts    : {reposted_count}")
    print(f"   New likes      : {liked_count}")
    print(f"   Already known  : {skipped_count}")
    print(f"   Errors         : {error_count}")
    print(f"   State file     : {STATE_FILE.name}")

    print("=" * 60)


# ============================================================
# CLICKABLE POST FACETS
# ============================================================

def build_post_facets(client, text):
    """Make the RedFox handle and OnlyFans URL clickable."""

    facets = []

    token = f"@{MENTION_HANDLE}"
    start_char = text.find(token)

    if start_char >= 0:
        try:
            identity = client.com.atproto.identity.resolve_handle(
                {"handle": MENTION_HANDLE}
            )

            did = identity.did

            byte_start = len(
                text[:start_char].encode("utf-8")
            )

            byte_end = byte_start + len(
                token.encode("utf-8")
            )

            mention = models.AppBskyRichtextFacet.Mention(
                did=did
            )

            facet = models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(
                    byte_start=byte_start,
                    byte_end=byte_end,
                ),
                features=[mention],
            )

            facets.append(facet)

        except Exception as exc:
            print(f"⚠️ Could not resolve mention handle: {exc}")

    url = "https://onlyfans.com/redfoxofficial"
    url_text = "onlyfans.com/redfoxofficial"
    url_start = text.find(url_text)

    if url_start >= 0:

        byte_start = len(
            text[:url_start].encode("utf-8")
        )

        byte_end = byte_start + len(
            url_text.encode("utf-8")
        )

        link = models.AppBskyRichtextFacet.Link(
            uri=url
        )

        facet = models.AppBskyRichtextFacet.Main(
            index=models.AppBskyRichtextFacet.ByteSlice(
                byte_start=byte_start,
                byte_end=byte_end,
            ),
            features=[link],
        )

        facets.append(facet)

    return facets or None


# ============================================================
# MAIN
# ============================================================

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

    # --------------------------------------------------------
    # 1. LOGIN
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
        fail(f"Bluesky login failed: {exc}")

    print("✅ Bluesky login successful")

    # --------------------------------------------------------
    # 2. REDFOX FEED REPOST + LIKE
    # --------------------------------------------------------

    repost_redfox_feed(client)

    # --------------------------------------------------------
    # 3. GOOGLE DRIVE MEDIA
    # --------------------------------------------------------

    print()
    print("🔎 Checking Redfox Google Drive folder...")

    from redfox_photo_manager import get_photo, mark_photo_used

    try:
        photo, local_path = get_photo(ACCOUNT_NAME)

    except Exception as exc:
        fail(f"Google Drive media retrieval failed: {exc}")

    if not photo:
        print("📭 No media found. Skip — nothing to post.")
        print("✅ RedFox feed processing has been completed.")
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
        fail(
            "Downloaded file size does not match "
            "the original Drive file."
        )

    # --------------------------------------------------------
    # 4. IMAGE DIMENSIONS
    # --------------------------------------------------------

    image_width = image_height = None

    if media_type == "image":

        try:
            from PIL import Image

            with Image.open(local_path) as image:
                image_width, image_height = image.size

        except Exception as exc:
            fail(f"Could not read image dimensions: {exc}")

    # --------------------------------------------------------
    # 5. POST NEW MEDIA
    # --------------------------------------------------------

    print()
    print("📤 Posting Redfox media...")

    try:

        with local_path.open("rb") as media_file:
            media_data = media_file.read()

        uploaded = client.upload_blob(media_data).blob

        facets = build_post_facets(
            client,
            POST_TEXT
        )

        if media_type == "image":

            image = models.AppBskyEmbedImages.Image(
                alt="",
                image=uploaded,
                aspect_ratio={
                    "width": image_width,
                    "height": image_height
                },
            )

            embed = models.AppBskyEmbedImages.Main(
                images=[image]
            )

        else:

            embed = models.AppBskyEmbedVideo.Main(
                video=uploaded,
                alt=None
            )

        post_kwargs = {
            "text": POST_TEXT,
            "embed": embed
        }

        if facets:
            post_kwargs["facets"] = facets

        response = client.send_post(**post_kwargs)

    except Exception as exc:

        print(f"❌ Bluesky post failed: {exc}")
        print("⚠️ Media remains in Redfox/Ready.")
        sys.exit(1)

    if not response:
        fail(
            "Bluesky returned no response. "
            "Media remains in Ready."
        )

    print("✅ Bluesky post successful")

    if hasattr(response, "uri"):
        print(f"🔗 URI: {response.uri}")

    # --------------------------------------------------------
    # 6. MOVE MEDIA TO USED
    # --------------------------------------------------------

    print()
    print("📦 Moving media to Redfox/Used...")

    try:
        mark_photo_used(photo)

    except Exception as exc:

        print(
            "❌ Post succeeded, but moving media failed: "
            f"{exc}"
        )

        print("⚠️ The media may still be in Redfox/Ready.")
        sys.exit(1)

    print()
    print("🎉 REDFOX NEW POST COMPLETE!")
    print(
        "   redheadheaven/Redfox/Ready "
        "→ redheadheaven/Redfox/Used"
    )


if __name__ == "__main__":
    main()
