"""JioSaavn full-song source.

JioSaavn's public web API (the same one jiosaavn.com itself uses) serves the
complete song — Western hits included — as AAC/MP4, and unlike YouTube it does
not block datacenter IPs (Render etc.). No cookies, no PO tokens, no ffmpeg
downloading tricks: decrypt the URL and fetch it from the CDN.

Media URLs come DES-encrypted in the API payload; the key is the long-public
"38346591". DES with an 8-byte key equals 3DES(K,K,K), so we build a 24-byte
key to keep every supported `cryptography` release quiet.
"""
import asyncio
import base64
import html
import logging
import os
import re
import shutil
import subprocess
import uuid
from difflib import SequenceMatcher

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES

log = logging.getLogger("musicbot.saavn")

_API = "https://www.jiosaavn.com/api.php"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
}

_client = httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEADERS)

# (search URL decrypt key), ECB mode, PKCS7 padding — matches the site itself.
_MEDIA_KEY24 = b"38346591" * 3

TEMP = os.path.join(os.path.dirname(__file__), "_downloads")
os.makedirs(TEMP, exist_ok=True)


def _clean(text: str) -> str:
    """API values are HTML-escaped; decode for display."""
    return html.unescape(re.sub(r"\s+", " ", str(text))).strip() if text else ""


def _norm(s: str) -> str:
    """Lowercase, alphanumeric-only, for similarity checks."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def decrypt_media_url(enc: str) -> str:
    """Decrypt `encrypted_media_url` -> plain CDN URL (e.g. …_96.mp4)."""
    data = base64.b64decode(enc + "=" * (-len(enc) % 4))
    dec = Cipher(TripleDES(_MEDIA_KEY24), modes.ECB()).decryptor()
    padded = dec.update(data) + dec.finalize()
    unp = PKCS7(64).unpadder()
    return (unp.update(padded) + unp.finalize()).decode("utf-8", "replace")


async def search(query: str, limit: int = 10) -> list[dict]:
    """Search JioSaavn: [{title, artist, album, duration, url, has_320}]"""
    try:
        r = await _client.get(_API, params={
            "__call": "search.getResults", "q": query, "_format": "json",
            "_marker": "0", "api_version": "4", "ctx": "web6dot0",
            "n": str(limit)})
        data = r.json()
    except Exception as e:
        log.warning("saavn search failed: %s", e)
        return []

    out = []
    for s in data.get("results") or []:
        m = s.get("more_info") or {}
        enc = m.get("encrypted_media_url") or ""
        if not enc:
            continue
        try:
            url = decrypt_media_url(enc)
        except Exception as e:
            log.debug("media url decrypt failed: %s", e)
            continue
        if not url.startswith("http"):
            continue

        amap = m.get("artistMap") or {}
        names: list[str] = []
        for artists in (amap.get("primary_artists") or []):
            if artists.get("name"):
                names.append(_clean(artists["name"]))
        primary = list(names)                      # performers only (official uploads)
        names += [_clean(m.get("album") or ""), _clean(m.get("music") or "")]
        for artists in (amap.get("artists") or [])[:4]:
            if artists.get("name"):
                names.append(_clean(artists["name"]))

        out.append({
            "title": _clean(s.get("title")),
            "subtitle": _clean(s.get("subtitle")),
            "artist": ", ".join(dict.fromkeys(n for n in names if n))[:80],
            "primary": ", ".join(dict.fromkeys(n for n in primary if n))[:80],
            "album": _clean(m.get("album")),
            "duration": int(m.get("duration") or 0),
            "url": url,
            "has_320": str(m.get("320kbps", "")).lower() == "true",
            "language": s.get("language"),
            "image": _clean(s.get("image")),
        })
    return out


def _artist_hit(candidate_artists: str, artist: str) -> float:
    """1.0 exact-ish artist match, 0.5 partial token overlap, 0 no match."""
    if not candidate_artists or not artist:
        return 0.0
    cand = _norm(candidate_artists)
    want = _norm(artist)
    if not want or not cand:
        return 0.0
    if want in cand:
        # "theweeknd" inside "theweeknd, maxmartin" or a featured string
        return 1.0
    tokens = [t for t in re.split(r"[^a-z0-9]+", (artist or "").lower()) if t]
    overlapping = sum(1 for t in tokens if t and t in cand)
    if tokens and overlapping >= min(2, len(tokens)):
        return 0.5
    if tokens and len(tokens) == 1 and overlapping == 1 and len(tokens[0]) >= 4:
        return 0.5
    return 0.0


# Common re-upload variant labels. When the *requested* title doesn't contain
# them but a catalog candidate does, it's a cover/instrumental/sped-up version
# of the song — not the full official track.
_VARIANT_RE = re.compile(
    r"(piano|cover|karaoke|tribute|instrumental|8\s?d|8\s?bit|sped|slowed|"
    r"reverb|lo-?fi|nightcore|ringtone|whatsapp|status|workout|relaxing|"
    r"loops?-|chill\s+version|sad\s+version|edit|rolloff)")


def _score(c: dict, title: str, artist: str, duration) -> float:
    """Higher is better. 0 means unacceptable."""
    t = _norm(c.get("title"))
    want_t = _norm(title)
    t_sim = SequenceMatcher(None, t, want_t).ratio() if t and want_t else 0.0
    if t_sim < 0.55:
        return 0.0
    if _VARIANT_RE.search(t) and not _VARIANT_RE.search(want_t):
        return 0.0
    # Only the *primary* (performing) artists can vouch for a track. Covers,
    # karaoke albums and knock-off re-uploads list the real artist as
    # composer/songwriter in secondary fields — those must never score.
    a = _artist_hit(c.get("primary") or c.get("artist", ""), artist) \
        if artist else 0.5
    if artist and a == 0.0:
        return 0.0
    d = 0.0
    if duration and c.get("duration"):
        diff = abs(int(c["duration"]) - int(duration))
        d = max(0.0, 1.0 - diff / 90)          # exact=1, ±90s off → 0
        if d < 0.25:                            # > ~67s out → too far off
            return 0.0
    elif duration:
        d = 0.4                                 # unverified duration – mild
    q = 0.15 if c.get("has_320") else 0.0
    return t_sim * 100 + a * 30 + d * 20 + q


async def pick(query: str, *, title: str, artist: str, duration=None):
    """Best catalog match for (title, artist) — like-for-like or None.

    Returns dict {title, artist, album, duration, media_url} or None.
    """
    for q in (f"{artist} {title}" if artist else title, title):
        cands = await search(q, limit=10)
        if not cands:
            continue
        scored = sorted(
            ((_score(c, title, artist, duration), c) for c in cands),
            key=lambda x: x[0], reverse=True)
        best_s, best = scored[0] if scored else (0.0, None)
        if best_s >= 55.0:
            return best
        break  # second query only when the first returned nothing at all
    return None


async def resolve_media_url(c: dict) -> str | None:
    """Upgrade the 96 kbps URL to the 320 kbps file when available."""
    if not c.get("has_320"):
        return c["url"]
    url320 = c["url"].replace("_96.mp4", "_320.mp4")
    try:
        rh = await _client.head(url320)
        if rh.status_code == 200 and \
                int(rh.headers.get("content-length") or 0) > 200_000:
            return url320
    except Exception:
        pass
    return c["url"]


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or ""


def _convert_sync(src: str, dst: str) -> str:
    ff = _ffmpeg()
    if ff:
        r = subprocess.run(
            [ff, "-y", "-loglevel", "error", "-i", src,
             "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", dst],
            capture_output=True, timeout=180)
        if r.returncode == 0 and os.path.exists(dst):
            return dst
        raise RuntimeError(f"ffmpeg failed: {r.stderr.decode(errors='replace')[-200:]}")
    # No ffmpeg → ship the original AAC file as-is (Telegram plays mp4)
    shutil.copyfile(src, dst)
    return dst


async def fetch(query: str, *, title: str, artist: str, duration=None,
                notify=None) -> tuple[str, dict] | None:
    """Full download for a track, or None when the catalog can't confirm it.

    Returns (mp3_path, info) with info keys title / artist / duration / source.
    """
    if notify:
        await notify(f"🔎 Checking the catalog for “{_clean(title)[:40]}”…")
    best = None
    try:
        best = await pick(query, title=title, artist=artist, duration=duration)
    except Exception as e:
        log.warning("saavn pick failed: %s", e)
        return None
    if not best:
        return None

    if notify:
        await notify(f"⬇️ Downloading: {best['title'][:60]}"
                     f"{' — ' + best['artist'][:30] if best.get('artist') else ''}")
    try:
        url = await resolve_media_url(best)
    except Exception as e:
        log.warning("saavn url resolve failed: %s", e)
        return None

    src = os.path.join(TEMP, f"saavn_{uuid.uuid4().hex[:12]}.m4a")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as dl:
            async with dl.stream("GET", url) as resp:
                resp.raise_for_status()
                total = 0
                with open(src, "wb") as fh:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        fh.write(chunk)
                        total += len(chunk)
        if total < 100_000:
            raise RuntimeError(f"tiny download ({total} bytes)")
    except Exception as e:
        log.warning("saavn download failed: %s", e)
        try:
            os.remove(src)
        except OSError:
            pass
        return None

    if notify:
        await notify("🎚 Converting to MP3…")
    dst = src[:-4] + ".mp3"
    try:
        result = await asyncio.to_thread(_convert_sync, src, dst)
    except Exception as e:
        log.warning("mp3 conversion failed: %s", e)
        for p in (src, dst):
            try:
                os.remove(p)
            except OSError:
                pass
        return None
    finally:
        try:
            os.remove(src)
        except OSError:
            pass

    info = {"title": best["title"], "artist": best.get("artist") or artist,
            "duration": best.get("duration") or int(duration or 0),
            "source": "jiosaavn"}
    return result, info

