"""Audius full-song source (datacenter-IP friendly, no cookies).

Audius streams user-uploaded full tracks from its own global CDN. There is
no API key, and — unlike YouTube — music CDNs are not blocked from hosted
servers (Render etc.), so it works with no cookies, no PO tokens.

The catalog is mostly indie artists and remixes/edits of mainstream songs,
so — like the SoundCloud fallback — a candidate may only stand in for the
requested track when it is metadata-verified: a faithful re-upload with the
same title and a corroborating duration/artist. Remixes, covers, bootlegs
and "Lyrics" uploads of the same song never qualify, so you never get the
wrong track.

Audius requires every API request to carry an app_name identifier.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import uuid
from difflib import SequenceMatcher

import httpx

log = logging.getLogger("musicbot.audius")

_DISCOVERY = "https://api.audius.co"
_APP = "musicfinderbot"          # required identifier sent with every request
_FALLBACK_HOST = "https://api.audius.co"

TEMP = os.path.join(os.path.dirname(__file__), "_downloads")
os.makedirs(TEMP, exist_ok=True)

_client = httpx.AsyncClient(timeout=30, follow_redirects=True)
_host: str | None = None


def _norm(s: str) -> str:
    """Lowercase, alphanumeric-only, for similarity checks."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


# Re-upload variant labels. A candidate whose title adds a variant label that
# the requested title doesn't have is a remix/cover/mash-up — not the track.
# Word boundaries keep substring lookalikes ("Mediterranean") out of it.
_VARIANT_RE = re.compile(
    r"\b(remix|rework|bootleg|bootie|mix|mash\s?up|edit|flip|cover|"
    r"sing\s?off|competition|lyrics|lyric\s+video|snippet|"
    r"nightcore|sped\s?up|slowed|reverb|8\s?d|8\s?bit|instrumental|"
    r"karaoke|tribute|live\s+(at|session)|acoustic(\s+version)?|"
    r"piano\s+version|dub|loop|cut|sample)\b",
    re.IGNORECASE)


def _variant_taint(title: str) -> bool:
    """True when any variant label appears in the title."""
    return bool(_VARIANT_RE.search(title or ""))


async def _base_url() -> str:
    """Resolve one API host; the discovery endpoint lists a few."""
    global _host
    if _host:
        return _host
    try:
        r = await _client.get(_DISCOVERY, params={"app_name": _APP})
        hosts = (r.json().get("data") or [])
        _host = hosts[0] if hosts else _FALLBACK_HOST
    except Exception as e:
        log.warning("audius discovery failed (%s) — using default host", e)
        _host = _FALLBACK_HOST
    return _host


async def search(query: str, limit: int = 10) -> list[dict]:
    """Search Audius: [{title, artist, duration, id}]"""
    try:
        h = await _base_url()
        r = await _client.get(f"{h}/v1/tracks/search",
                              params={"query": query, "app_name": _APP})
        data = r.json()
    except Exception as e:
        log.warning("audius search failed: %s", e)
        return []
    out = []
    for t in (data.get("data") or [])[:limit]:
        if t.get("id") is None:
            continue
        out.append({
            "title": _clean(t.get("title")),
            "artist": _clean((t.get("user") or {}).get("name")),
            "duration": int(t.get("duration") or 0),
            "id": t.get("id"),
            "genre": _clean(t.get("genre")),
        })
    return out


def _ok(c: dict, title: str, artist: str, duration) -> bool:
    """Metadata-verified re-upload rule (same strictness as SoundCloud):
    title must match, and duration and/or artist name corroborates it.
    Variant labels anywhere in the candidate disqualify it, unless the
    *requested* title itself contains the same label."""
    t, want_t = _norm(c.get("title")), _norm(title)
    if not t or not want_t:
        return False
    if t == want_t or t in want_t or want_t in t:
        pass                              # trivially same/re-subset title
    elif SequenceMatcher(None, t, want_t).ratio() < 0.6:
        return False
    if _variant_taint(c.get("title")) and not _variant_taint(title):
        return False
    if duration and c.get("duration"):
        if abs(c["duration"] - int(duration)) > 40:
            return False
    elif artist:
        if _norm(artist) not in _norm(c.get("artist")):
            return False
    return True


def _sniff(buf: bytes) -> bool:
    """Plausibly an MP3/AAC stream rather than a JSON error page."""
    return buf[:3] == b"ID3" or (buf[:1] == b"\xff" and
                                 (buf[1] & 0xE0) == 0xE0)


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
    shutil.copyfile(src, dst)
    return dst


async def fetch(query: str, *, title: str, artist: str, duration=None,
                notify=None) -> tuple[str, dict] | None:
    """Full download for a verified Audius track, or None.

    Returns (mp3_path, info) with info keys title / artist / duration.
    """
    if not title:
        return None
    if notify:
        await notify("🔎 Checking Audius for the full track…")
    try:
        cands = await search(query)
    except Exception as e:
        log.warning("audius search failed: %s", e)
        return None
    verified = [c for c in cands if _ok(c, title, artist, duration)]
    if not verified:
        return None
    if duration:
        verified.sort(key=lambda c: abs(
            (c.get("duration") or 10**6) - int(duration)))

    for c in verified[:2]:
        if notify:
            await notify(
                f"⬇️ Downloading (Audius): {_clean(c['title'])[:60]}")
        src = os.path.join(TEMP, f"audius_{uuid.uuid4().hex[:12]}.mp3")
        try:
            h = await _base_url()
            async with _client.stream(
                    "GET", f"{h}/v1/tracks/{c['id']}/stream",
                    params={"app_name": _APP}) as resp:
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "")
                if "json" in ctype or "html" in ctype or \
                        "text/plain" in ctype:
                    continue                     # dead track, not audio
                total = 0
                head = b""
                with open(src, "wb") as fh:
                    first = True
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        if first:
                            head = chunk[:4]
                            first = False
                        fh.write(chunk)
                        total += len(chunk)
            if total < 100_000 or (head and not _sniff(head)):
                raise RuntimeError(f"bad stream ({total} bytes, {ctype})")
        except Exception as e:
            log.warning("audius download failed: %s", e)
            try:
                os.remove(src)
            except OSError:
                pass
            continue

        if notify:
            await notify("🎚 Converting to MP3…")
        dst = src[:-4] + "_aud.mp3"
        try:
            result = await asyncio.to_thread(_convert_sync, src, dst)
        except Exception as e:
            log.warning("audius mp3 conversion failed: %s", e)
            for p in (src, dst):
                try:
                    os.remove(p)
                except OSError:
                    pass
            continue
        finally:
            try:
                os.remove(src)
            except OSError:
                pass

        return result, {"title": _clean(c["title"]) or title,
                        "artist": artist or _clean(c.get("artist")),
                        "duration": duration or c.get("duration"),
                        "source": "audius"}
    return None
