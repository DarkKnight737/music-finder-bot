"""Search helper for free music-metadata APIs (Deezer / iTunes)."""
import asyncio
import html
import re
from urllib.parse import quote

import httpx

_client = httpx.AsyncClient(timeout=10, follow_redirects=True)


def _clean(text: str) -> str:
    """Deezer returns HTML-escaped strings; decode before sending to Telegram."""
    if not text:
        return ""
    return html.unescape(re.sub(r"\s+", " ", str(text))).strip()


async def search_deezer(query: str, limit: int = 10, index: int = 0) -> list[dict]:
    """Return tracks: [{title, artist, album, duration, preview, cover, link}]"""
    url = (f"https://api.deezer.com/search?q={quote(query)}"
           f"&limit={limit}&index={max(0, index)}")
    try:
        r = await _client.get(url)
        data = r.json()
    except Exception:
        return []
    out = []
    for t in data.get("data", []):
        out.append(
            {
                "title": _clean(t.get("title")),
                "artist": _clean(t.get("artist", {}).get("name")),
                "album": _clean(t.get("album", {}).get("title")),
                "duration": t.get("duration", 0),
                "preview": t.get("preview") or "",
                "cover": (t.get("album", {}).get("cover_big")
                          or t.get("album", {}).get("cover_medium") or ""),
                "thumb": (t.get("album", {}).get("cover_medium")
                          or t.get("album", {}).get("cover_medium") or ""),
                "link": t.get("link") or "",
            }
        )
    return out


async def search_itunes(query: str, limit=10, offset: int = 0) -> list[dict]:
    url = (f"https://itunes.apple.com/search?term={quote(query)}"
           f"&media=music&entity=song&limit={limit}&offset={max(0, offset)}")
    try:
        r = await _client.get(url)
        data = r.json()
    except Exception:
        return []
    out = []
    for t in data.get("results", []):
        out.append(
            {
                "title": _clean(t.get("trackName")),
                "artist": _clean(t.get("artistName")),
                "album": _clean(t.get("collectionName")),
                "duration": int(t.get("trackTimeMillis", 0) / 1000),
                "preview": t.get("previewUrl") or "",
                "cover": (t.get("artworkUrl100") or "").replace("100x100", "600x600"),
                # small artwork for the audio thumbnail (Telegram caps
                # thumbnails at 200 KB; 600x600 JPEGs regularly exceed that)
                "thumb": (t.get("artworkUrl100") or "").replace("100x100", "200x200"),
                "link": t.get("trackViewUrl") or "",
            }
        )
    return out


async def search_all(query: str, limit_per_source: int = 10,
                     index: int = 0) -> list[dict]:
    """Merge Deezer + iTunes results, drop duplicates by (artist,title)."""
    dz, it = await asyncio.gather(
        search_deezer(query, limit_per_source, index),
        search_itunes(query, limit_per_source, index),
    )
    seen, merged = set(), []
    for t in dz + it:
        key = (t["artist"].lower(), t["title"].lower())
        if key in seen or not t["title"]:
            continue
        seen.add(key)
        merged.append(t)
    return merged


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


async def download_file(url: str) -> bytes | None:
    """Download an audio preview file, return bytes or None on failure."""
    if not url:
        return None
    try:
        r = await _client.get(url)
        r.raise_for_status()
        return r.content
    except Exception:
        return None
