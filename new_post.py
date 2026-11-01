
import os
import sys
import subprocess
import shutil
from pathlib import Path

from atproto import Client, models

ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "Beautyfan")
BLUESKY_HANDLE = os.getenv("BLUESKY_HANDLE", "beautyfan.bsky.social")
BLUESKY_PASSWORD = os.getenv("BLUESKY_PASSWORD")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4"}
GIF_EXTENSIONS = {".gif"}
CONVERTED_DIR = Path("photo_downloads/converted")

REBOOST_COUNT = 3


def fail(message):
    print(f"❌ {message}", flush=True)
    sys.exit(1)


def reboost_last_posts(client):
    """Repost de laatste 3 eigen posts, van oudste naar nieuwste."""

    print("\n" + "=" * 60)
    print("       REBOOST LAATSTE 3 EIGEN POSTS")
    print("=" * 60)

    try:
        profile = client.app.bsky.actor.get_profile({
            "actor": BLUESKY_HANDLE
        })

        account_did = profile.did

        response = client.app.bsky.feed.get_author_feed({
            "actor": account_did,
            "limit": 50,
            "filter": "posts_no_replies",
        })

        posts = []

        for item in response.feed:
            post = item.post

            # Alleen eigen originele posts.
            if post.author.did != account_did:
                continue

            # Geen reposts van andere accounts.
            if item.reason is not None:
                continue

            if not post.record:
                continue

            posts.append(post)

            if len(posts) >= REBOOST_COUNT:
                break

        if not posts:
            print("📭 Geen eigen posts gevonden.")
            return

        # Feed is nieuwste eerst.
        # Omkeren zodat we oudste eerst reposteren.
        posts.reverse()

        print(f"📋 {len(posts)} eigen posts gevonden.")
        print("📅 Volgorde: oudste → nieuwste")

        for number, post in enumerate(posts, start=1):

            print(f"\n🔄 Post {number}/{len(posts)}")
            print(f"URI: {post.uri}")

            # Controleren of de post al gerepost is.
            try:
                existing = client.app.bsky.feed.get_reposted_by({
                    "uri": post.uri,
                    "cid": post.cid,
                    "limit": 100,
                })

                already_reposted = any(
                    actor.did == account_did
                    for actor in existing.reposted_by
                )

                if already_reposted:
                    print("↩️ Bestaande repost verwijderen...")

                    client.delete_repost(
                        post.uri,
                        post.cid
                    )

                    print("✅ Oude repost verwijderd.")

            except Exception as exc:
                print(f"⚠️ Controle bestaande repost: {exc}")

            # Opnieuw reposteren.
            try:
                client.repost(
                    post.uri,
                    post.cid
                )

                print("✅ Opnieuw gerepost.")

            except Exception as exc:
                print(f"⚠️ Repost mislukt: {exc}")

        print("\n✅ REBOOST AFGEROND.")

    except Exception as exc:
        print(f"⚠️ Reboost-module mislukt: {exc}")
        print("➡️ Nieuwe post wordt gewoon voortgezet.")


def run_ffmpeg(args):
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg is niet geïnstalleerd. Voeg aan de workflow toe: "
            "sudo apt-get install -y ffmpeg"
        )

    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        detail = (
            result.stderr or result.stdout or "Onbekende FFmpeg-fout"
        ).strip()
        raise RuntimeError(detail[-2500:])


def prepare_media(path):
    """Bereid afbeeldingen, GIF's en MP4-video's voor op Bluesky."""

    from PIL import Image, ImageOps

    path = Path(path)
    ext = path.suffix.lower()
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)

    # GIF: behoud de animatie door naar MP4 om te zetten.
    if ext in GIF_EXTENSIONS:
        output = CONVERTED_DIR / f"{path.stem}_animated.mp4"

        run_ffmpeg([
            "-i", str(path),
            "-vf",
            "scale=w='min(1920,iw)':h='min(1920,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2,"
            "fps=30,format=yuv420p",
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-movflags", "+faststart",
            str(output),
        ])

        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError(
                "GIF-conversie heeft geen MP4-bestand opgeleverd."
            )

        return output, "video", None, None

    # MP4: normaliseer naar een compatibel videoformaat.
    if ext in VIDEO_EXTENSIONS:
        output = CONVERTED_DIR / f"{path.stem}_compatible.mp4"

        run_ffmpeg([
            "-i", str(path),
            "-vf",
            "scale=w='min(1920,iw)':h='min(1920,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2,"
            "format=yuv420p",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output),
        ])

        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError(
                "Video-conversie heeft geen MP4-bestand opgeleverd."
            )

        return output, "video", None, None

    # Afbeeldingen: alleen converteren wanneer nodig.
    if ext in IMAGE_EXTENSIONS:
        with Image.open(path) as original:
            im = ImageOps.exif_transpose(original)
            width, height = im.size

            needs_convert = (
                ext == ".webp"
                or width > 2000
                or height > 2000
                or path.stat().st_size > 2 * 1024 * 1024
            )

            if not needs_convert:
                return path, "image", width, height

            im.thumbnail((2000, 2000), Image.Resampling.LANCZOS)

            if im.mode in ("RGBA", "LA") or "transparency" in im.info:
                rgba = im.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(
                    rgba,
                    mask=rgba.getchannel("A"),
                )
                im = background
            else:
                im = im.convert("RGB")

            output = CONVERTED_DIR / f"{path.stem}_converted.jpg"

            quality = 88

            while True:
                im.save(
                    output,
                    "JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )

                if output.stat().st_size <= 2 * 1024 * 1024:
                    break

                quality -= 7

                if quality < 35:
                    width, height = im.size

                    if max(width, height) <= 600:
                        raise RuntimeError(
                            "Afbeelding kon niet tot maximaal 2 MB worden verkleind."
                        )

                    im.thumbnail((
                        max(600, int(width * 0.8)),
                        max(600, int(height * 0.8)),
                    ), Image.Resampling.LANCZOS)

                    quality = 78

            with Image.open(output) as converted:
                width, height = converted.size

            print(
                f"Afbeelding geoptimaliseerd: "
                f"{path.stat().st_size} -> {output.stat().st_size} bytes"
            )

            return output, "image", width, height

    # Onbekend bestandstype: probeer het eerst als afbeelding te openen.
    try:
        with Image.open(path) as original:
            im = ImageOps.exif_transpose(original)
            im.thumbnail((2000, 2000), Image.Resampling.LANCZOS)

            if im.mode in ("RGBA", "LA") or "transparency" in im.info:
                rgba = im.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(
                    rgba,
                    mask=rgba.getchannel("A"),
                )
                im = background
            else:
                im = im.convert("RGB")

            output = CONVERTED_DIR / f"{path.stem}_converted.jpg"
            im.save(output, "JPEG", quality=88, optimize=True)

        with Image.open(output) as converted:
            width, height = converted.size

        return output, "image", width, height

    except Exception:
        raise RuntimeError(
            f"Niet-ondersteund of onleesbaar bestand: {path.name}"
        )


def move_to_used(photo, mark_photo_used, reason):
    """Verplaats het originele Drive-bestand naar used."""

    try:
        mark_photo_used(photo)
        print(f"✅ Bestand verplaatst naar used: {reason}")
        return True

    except Exception as exc:
        print(f"❌ Bestand kon niet naar used worden verplaatst: {exc}")
        return False


def main():
    print("=" * 60)
    print("       BLUESKY NEW POST")
    print("=" * 60)

    print(f"\nAccount : {ACCOUNT_NAME}")
    print(f"Handle  : {BLUESKY_HANDLE}")

    if not BLUESKY_PASSWORD:
        fail("BLUESKY_PASSWORD is niet ingesteld.")

    from photo_manager import get_photo, mark_photo_used

    print("\n🔎 Google Drive controleren...")

    photo, local_path = get_photo(ACCOUNT_NAME)

    if not photo:
        print("📭 Geen media gevonden in ready.")
        return

    local_path = Path(local_path)

    print(f"\n📁 Bestand: {photo['name']}")
    print(f"📦 MIME-type: {photo.get('mime_type', 'onbekend')}")
    print(f"📏 Grootte: {photo['size']} bytes")

    if not local_path.exists():
        fail(f"Gedownload bestand niet gevonden: {local_path}")

    if local_path.stat().st_size != photo["size"]:
        fail("Gedownloade bestandsgrootte komt niet overeen met Google Drive.")

    # Login eerst.
    print("\n🔐 Inloggen bij Bluesky...")

    client = Client()

    try:
        client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
    except Exception as exc:
        fail(f"Bluesky-login mislukt; bestand blijft in ready: {exc}")

    print("✅ Ingelogd")

    # Eerst de laatste 3 eigen posts opnieuw reposten.
    reboost_last_posts(client)

    # Bestaande media-voorbereiding.
    print("\n🛠️ Media voorbereiden...")

    try:
        upload_path, media_type, width, height = prepare_media(local_path)

    except Exception as exc:
        print(f"❌ Media voorbereiden mislukt: {exc}")
        print("📦 Bestand naar used verplaatsen om herhaling te voorkomen...")
        move_to_used(photo, mark_photo_used, "media voorbereiden mislukt")
        sys.exit(1)

    print(
        f"✅ Media klaar: {upload_path.name} "
        f"({upload_path.stat().st_size} bytes)"
    )

    print("\n📤 Media plaatsen op Bluesky...")

    try:
        with open(upload_path, "rb") as media_file:
            media_data = media_file.read()

        uploaded = client.upload_blob(media_data).blob

        if media_type == "image":
            image = models.AppBskyEmbedImages.Image(
                alt="",
                image=uploaded,
                aspect_ratio={
                    "width": width,
                    "height": height,
                },
            )

            embed = models.AppBskyEmbedImages.Main(images=[image])

        else:
            embed = models.AppBskyEmbedVideo.Main(
                video=uploaded,
                alt="",
            )

        response = client.send_post(text="", embed=embed)

    except Exception as exc:
        print(f"❌ Bluesky-post mislukt: {exc}")
        print("📦 Bestand naar used verplaatsen zodat het niet opnieuw wordt geprobeerd...")
        move_to_used(photo, mark_photo_used, "postpoging mislukt")
        sys.exit(1)

    if not response:
        print("❌ Bluesky gaf geen bevestiging terug.")
        move_to_used(photo, mark_photo_used, "geen bevestiging van Bluesky")
        sys.exit(1)

    print("\n✅ Bluesky-post succesvol")

    if hasattr(response, "uri"):
        print(f"🔗 URI: {response.uri}")

    print("\n📦 Origineel naar used verplaatsen...")

    if not move_to_used(photo, mark_photo_used, "post succesvol"):
        print(
            "⚠️ De post is geplaatst, maar het origineel staat mogelijk "
            "nog in ready. Controleer Google Drive om dubbel posten te voorkomen."
        )
        sys.exit(1)

    print("\n🎉 KLAAR!")
    print(f"   {ACCOUNT_NAME}/ready → {ACCOUNT_NAME}/used")
    print("=" * 60)


if __name__ == "__main__":
    main()
