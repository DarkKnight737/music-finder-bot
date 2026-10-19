"""Full-song fetchers.

YouTube via yt-dlp. In 2026 YouTube aggressively blocks datacenter IPs, so the
scan tries several player clients and can use a local bgutil PO-token
provider (see Dockerfile) before giving up.
"""
import asyncio
import logging
import os
import shutil
import time
import uuid

log = logging.getLogger("musicbot.dl")

TEMP = os.path.join(os.path.dirname(__file__), "_downloads")
os.makedirs(TEMP, exist_ok=True)

_MIN_DUR = 40
_MAX_DUR = 20 * 60
_TIME_BUDGET = 150  # seconds for the whole search+download effort
_SEARCH_CLIENTS = ("default",)
# Client chains tried in order. NOTE: the "android" client is broken in
# yt-dlp >= 2025 and "ios" requires PO tokens it can't always get, so they
# waste the whole time budget on Render. These chains are all viable as of
# yt-dlp 2026.08 on datacenter IPs (bgutil PO provider covers the web-based
# clients, and web_safari/tv work without tokens).
_DL_CLIENT_CHAIN = (
    None,                       # yt-dlp's defaults (visionos + web)
    ("web_safari",),
    ("tv", "tv_simply"),
    ("mweb",),
)

import yt_dlp  # noqa: E402  (after env prep)

_ydl_base = {
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 20,
    "retries": 3,
}


def _search_sync(query: str, source: str = "youtube") -> list[dict]:
    """Blocking: flat search returning candidates."""
    if source == "youtube":
        target = f"ytsearch5:{query}"
    else:
        target = f"scsearch5:{query}"
    opts = {**_ydl_base, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    out = []
    for e in info.get("entries") or []:
        if not e:
            continue
        dur = e.get("duration") or 0
        if dur and not (_MIN_DUR <= dur <= _MAX_DUR):
            continue
        out.append({
            "title": e.get("title") or "",
            "url": e.get("url") or (f"https://www.youtube.com/watch?v={e.get('id')}" if e.get("id") else ""),
            "duration": int(dur or 0),
            "uploader": e.get("uploader") or "",
        })
    return out


async def find_track(query: str, source: str = "youtube") -> list[dict]:
    return await asyncio.to_thread(_search_sync, query, source)


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or ""


def _download_one_sync(url: str, clients) -> tuple[str, dict]:
    out = os.path.join(TEMP, f"song_{uuid.uuid4().hex[:12]}.%(ext)s")
    path_base = out.rsplit(".", 1)[0]
    opts = {
        **_ydl_base,
        "format": "bestaudio/best",
        "outtmpl": out,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    if clients:
        opts["extractor_args"] = {
            "youtube": {"player_client": list(clients)},
        }
    if _ffmpeg():
        opts["ffmpeg_location"] = _ffmpeg()
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    result = path_base + ".mp3"
    if not os.path.exists(result):
        raise FileNotFoundError("post-processing did not produce MP3")
    return result, info


def _short_err(e: Exception) -> str:
    for line in str(e).splitlines():
        s = line.strip()
        if s:
            return s[:160]
    return "unknown error"


async def download_best(query: str, notify=None) -> tuple[str, dict]:
    """Search YouTube (then SoundCloud), try each candidate across client chains."""
    if notify:
        await notify("🔎 Searching for the full track…")
    cands: list[dict] = []
    try:
        cands = await find_track(query)
    except Exception as e:
        # A failed search must not abort everything — SoundCloud is next.
        log.warning("youtube search failed: %s", e)
    if not cands:
        try:
            cands = await find_track(query, source="soundcloud")
        except Exception as e:
            log.warning("soundcloud search failed: %s", e)
    if not cands:
        raise RuntimeError("no candidates found")

    deadline = time.monotonic() + _TIME_BUDGET
    errors: list[str] = []
    for c in cands[:3]:
        for clients in _DL_CLIENT_CHAIN:
            if time.monotonic() > deadline:
                errors.append("time budget exceeded")
                break
            label = c["title"][:60] or c["url"]
            if notify:
                tag = f" (client: {','.join(clients)})" if clients else ""
                await notify(f"⬇️ Downloading: {label}{tag}")
            try:
                return await asyncio.to_thread(_download_one_sync, c["url"], clients)
            except Exception as e:
                errors.append(_short_err(e))
    raise RuntimeError("; ".join(errors[-2:]) or "download failed")


def cleanup(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
