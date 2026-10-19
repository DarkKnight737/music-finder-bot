"""Full-song search & download via yt-dlp (YouTube audio)."""
import asyncio
import os
import shutil
import uuid

TEMP = os.path.join(os.path.dirname(__file__), "_downloads")
os.makedirs(TEMP, exist_ok=True)

_MIN_DUR = 40       # ignore results shorter than this (karaoke clips, intros…)
_MAX_DUR = 20 * 60  # ignore 1-hour mixes

_ydl_base = {
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 20,
    "retries": 3,
}

def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or ""


def _download_sync(url: str) -> tuple[str, dict]:
    """Blocking: download best audio as MP3, return (path, metadata)."""
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
    if _ffmpeg():
        opts["ffmpeg_location"] = _ffmpeg()
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    result = path_base + ".mp3"
    if not os.path.exists(result):
        raise FileNotFoundError("post-processing did not produce MP3")
    return result, info


def _search_sync(query: str) -> list[dict]:
    """Blocking: quick flat search, returns candidates [{title,url,duration}]"""
    import yt_dlp
    opts = {**_ydl_base, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch5:{query}", download=False)
    out = []
    for e in info.get("entries") or []:
        if not e:
            continue
        dur = e.get("duration") or 0
        if dur and not (_MIN_DUR <= dur <= _MAX_DUR):
            continue
        out.append({
            "title": e.get("title") or "",
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
            "duration": int(dur or 0),
            "uploader": e.get("uploader") or "",
        })
    return out


async def find_track(query: str) -> list[dict]:
    """Async wrapper: candidates for a query."""
    return await asyncio.to_thread(_search_sync, query)


async def download_best(query: str, notify=None) -> tuple[str, dict]:
    """Search, pick best candidate, download. Caller must delete the file."""
    if notify:
        await notify("🔎 Searching for the full track…")
    cands = await find_track(query)
    if not cands:
        raise RuntimeError("no candidates")
    errors = []
    for c in cands[:3]:
        if notify:
            await notify(f"⬇️ Downloading: {c['title'][:60]}…")
        try:
            return await asyncio.to_thread(_download_sync, c["url"])
        except Exception as e:
            errors.append(str(e))
    raise RuntimeError(" · ".join(errors[-1:] or ["download failed"]))


def cleanup(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
