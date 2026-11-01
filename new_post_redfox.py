
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

# RedFox custom feed
FEED_URI = (
    "at://did:plc:cxrt7ggxkamgzxa47cggtees/"
    "app.bsky.feed.generator/aaaoirmgh53zw"
)

FEED_AUTHOR = "redfoxofficial.bsky.social"
FEED_LOOKBACK_HOURS = 5
FEED_PAGE_SIZE = 100
MAX_FEED_PAGES = 20

# State file: stored alongside this script
STATE_FILE = Path(__file__).resolve().parent / "redfox_feed_state.json"

# Keep processed entries for 90 days
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
# STATE MANAGEMENT
# ============================================================

def load_feed_state():
    if not STATE_FILE.exists():
        print("📝 No previous feed state found. Starting fresh.")
        return {"processed": {}}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("Invalid state format")

        if not isinstance(state.get("processed"), dict):
            raise ValueError("Missing processed entries")

        print(
            f"📝 Loaded {len(state['processed'])} "
            "previously processed feed items."
        )

        return state

    except Exception as exc:
        raise RuntimeError(
            f"Could not read feed state: {exc}"
        )


def save_feed_state(state):
    temporary_file = STATE_FILE.with_suffix(".tmp")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=STATE_RETENTION_DAYS)

    # Remove old state entries
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
# CHECK WHETHER REDHEADHEAVEN ALREADY REPOSTED
# ============================================================

def already_reposted_by_us(client, uri, cid, our_did):
    """
    Check Bluesky directly to see whether the current account
    has already reposted this post.

    Pagination is used so that the account is not missed when
    a post has many reposts.
    """

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
# REDFOX FEED REPOSTER
# ============================================================

def repost_redfox_feed(client):
    print()
    print("=" * 60)
    print("       REDFOX FEED REPOSTER")
    print("=" * 60)

    try:
        state = load_feed_state()
    except Exception as exc:
        print(f"❌ Feed state error: {exc}")
        print("⚠️ Feed reposting skipped to avoid duplicates.")
        return

    our_did = getattr(client.me, "did", None)

    if not our_did:
        print("❌ Could not determine RedheadHeaven DID.")
        print("⚠️ Feed reposting skipped.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=FEED_LOOKBACK_HOURS
    )

    print(f"🔎 Feed author : @{FEED_AUTHOR}")
    print(f"🕒 Lookback    : {FEED_LOOKBACK_HOURS} hours")
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
                author = getattr(post, "author", None)

                if not author:
                    continue

                # Only RedFox's own posts and replies
                if author.handle.lower() != FEED_AUTHOR.lower():
                    continue

                record = getattr(post, "record", None)

                if not record:
                    continue

                # Replies are deliberately included.
                # We do not filter out records with a reply field.

                timestamp = parse_timestamp(
                    getattr(record, "created_at", None)
                )

                if not timestamp:
                    timestamp = parse_timestamp(
                        getattr(post, "indexed_at", None)
                    )

                if not timestamp or timestamp < cutoff:
                    continue

                candidates[post.uri] = {
                    "uri": post.uri,
                    "cid": post.cid,
                    "timestamp": timestamp
                }

            cursor = getattr(response, "cursor", None)

            if not cursor:
                break

        print(f"📄 Feed pages read : {pages_read}")
        print(f"🎯 RedFox items    : {len(candidates)}")

    except Exception as exc:
        print(f"❌ Could not retrieve RedFox feed: {exc}")
        print("⚠️ Continuing with the new media post.")
        return

    # Oldest first, newest last
    ordered_posts = sorted(
        candidates.values(),
        key=lambda item: item["timestamp"]
    )

    reposted = 0
    skipped = 0
    errors = 0

    for item in ordered_posts:
        uri = item["uri"]
        cid = item["cid"]

        # Already processed in an earlier workflow run
        if uri in state["processed"]:
            skipped += 1
            print(f"⏭️ Already processed: {uri}")
            continue

        try:
            # Check Bluesky itself as well as our state file
            if already_reposted_by_us(
                client,
                uri,
                cid,
                our_did
            ):
                print(f"⏭️ Already reposted by RedheadHeaven: {uri}")

                mark_feed_item_processed(state, uri)
                skipped += 1
                continue

            print(f"🔁 Reposting RedFox item: {uri}")

            client.repost(uri, cid)

            # Save immediately after each successful repost
            mark_feed_item_processed(state, uri)

            reposted += 1
            print("✅ Reposted successfully")

        except Exception as exc:
            errors += 1
            print(f"❌ Could not repost {uri}: {exc}")
            print("⚠️ Item was not marked as processed.")

    print()
    print("REDFOX FEED SUMMARY")
    print(f"   New reposts       : {reposted}")
    print(f"   Already processed : {skipped}")
    print(f"   Errors            : {errors}")
    print(f"   State file        : {STATE_FILE.name}")
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
    # 2. REPOST REDFOX FEED
    # --------------------------------------------------------

    repost_redfox_feed(client)

    # --------------------------------------------------------
    # 3. GET NEW GOOGLE DRIVE MEDIA
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
        print("✅ RedFox feed check has still been completed.")
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
    # 4. READ IMAGE DIMENSIONS
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
    print("   redheadheaven/Redfox/Ready → redheadheaven/Redfox/Used")


if __name__ == "__main__":
    main()
