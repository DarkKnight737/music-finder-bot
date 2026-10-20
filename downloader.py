"""Full-song fetchers.

Primary w/o cookies: JioSaavn — official full songs (strong for
Bollywood/regional; Western covers are auto-rejected) straight from its CDN,
and unlike YouTube it does not block datacenter IPs. No cookies, no tokens.

Final fallbacks: SoundCloud and Audius (both datacenter-IP friendly;
metadata-verified re-uploads only) and YouTube via yt-dlp — In 2026 YouTube
aggressively blocks datacenter IPs, so the scan tries several player
clients, can use a local bgutil PO-token provider (see Dockerfile) and
benefits enormously from a cookies.txt. With a cookies.txt YouTube is tried
first again (full quality).
"""
import asyncio
import base64
import logging
import os
import shutil
import time
import uuid
from difflib import SequenceMatcher
import re

log = logging.getLogger("musicbot.dl")

TEMP = os.path.join(os.path.dirname(__file__), "_downloads")
os.makedirs(TEMP, exist_ok=True)

# YouTube hard-blocks datacenter IPs (Render etc.) unless the requests carry
# authenticated cookies. Sources, in order of convenience:
#   • YTDLP_COOKIES env var: a path to a Netscape cookies.txt, the file
#     content pasted straight in, or that content base64-encoded
#     (env-var dashboards dislike newlines).
#   • a Render "Secret File" mounted at /etc/secrets/cookies.txt
#   • ./cookies.txt next to downloader.py
# Any optional YouTube (-only) HTTP/SOCKS proxy can be set with YTDLP_PROXY.


def _log(msg: str, *a) -> None:
    log.info(msg, *a)


def _looks_netscape(text: str) -> bool:
    if "# Netscape" in text or "# HTTP Cookie File" in text:
        return True
    rows = [ln for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]
    return bool(rows) and all(ln.count("\t") >= 5 for ln in rows[:5])


def _resolve_cookies() -> str:
    """Returns a cookies.txt path usable by yt-dlp, or "" if none found."""
    env = (os.getenv("YTDLP_COOKIES") or "").strip()
    if env and os.path.isfile(env):                      # direct path
        return env
    for p in ("/etc/secrets/cookies.txt",
              os.path.join(os.path.dirname(__file__), "cookies.txt")):
        if os.path.isfile(p):
            return p
    if env:                                              # inline content?
        raw = env
        if not _looks_netscape(raw):                     # try base64 decode
            try:
                decoded = base64.b64decode(raw, validate=True).decode(
                    "utf-8", "replace")
            except Exception:
                decoded = ""
            if _looks_netscape(decoded):
                raw = decoded
        if _looks_netscape(raw):
            path = os.path.join(TEMP, "cookies_from_env.txt")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(raw if raw.endswith("\n") else raw + "\n")
            _log("Using YouTube cookies from YTDLP_COOKIES (inline content)")
            return path
        _log("YTDLP_COOKIES set but not a valid cookies.txt path, content "
             "or base64 — ignoring it")
    return ""


COOKIES_FILE = _resolve_cookies()
if COOKIES_FILE:
    _log("Using YouTube cookies: %s", COOKIES_FILE)

# Route all yt-dlp traffic through a proxy when provided (the classic fix
# for datacenter-IP blocks when a cookies.txt can't be maintained).
PROXY = (os.getenv("YTDLP_PROXY") or "").strip()
if PROXY:
    _log("Routing yt-dlp via YTDLP_PROXY (%s)", PROXY)

# yt-dlp's YouTube extraction needs a JS runtime for the player/cookie
# challenges ("The page needs to be reloaded" without one). Only deno is
# enabled by default, so point it at whichever runtime actually exists.
# The server image ships Node (for the PO-token provider).
_JS_RUNTIME = shutil.which("deno") or shutil.which("node") or ""

_MIN_DUR = 40
_MAX_DUR = 20 * 60
_TIME_BUDGET = 150  # seconds for the whole search+download effort
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
    "progress": False,
    "socket_timeout": 20,
    "retries": 3,
    "nocheckcertificate": True,
}
if COOKIES_FILE:
    _ydl_base["cookiefile"] = COOKIES_FILE
if PROXY:
    _ydl_base["proxy"] = PROXY
if _JS_RUNTIME:
    _name = os.path.splitext(os.path.basename(_JS_RUNTIME))[0].lower()
    _ydl_base["js_runtimes"] = {_name: {}}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _title_sim(a: str, b: str) -> float:
    an, bn = _norm(a), _norm(b)
    if not an or not bn:
        return 0.0
    if an == bn or an in bn or bn in an:
        return 1.0
    return SequenceMatcher(None, an, bn).ratio()


def _search_sync(query: str, source: str = "youtube") -> list[dict]:
    """Blocking: flat search returning candidates."""
    if source == "youtube":
        target = f"ytsearch5:{query}"
    else:
        target = f"scsearch8:{query}"
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


def _download_one_sync(url: str, clients,
                       source: str = "youtube") -> tuple[str, dict]:
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
    if source == "youtube":
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


# YouTube's "your IP is not trusted" signature. When it appears once, every
# other player client / candidate will hit it too — so detect it and move on
# to SoundCloud instead of grinding through the whole matrix (~60–90s wasted).
_BLOCK_SIGS = (
    "sign in to confirm", "confirm you're not a bot",
    "confirm you are not a bot", "use --cookies",
    "cookies for the authentication", "pass cookies",
)


def _ip_blocked(e: Exception) -> bool:
    s = str(e).lower()
    return any(sig in s for sig in _BLOCK_SIGS)


# ---------------- YouTube (primary) ----------------

async def _try_youtube(query: str, notify=None) -> tuple[str, dict] | list[str]:
    """Returns (path, info) or the list of accumulated error strings."""
    if notify:
        await notify("🔎 Searching YouTube for the full track…")
    try:
        cands = await find_track(query)
    except Exception as e:
        log.warning("youtube search failed: %s", e)
        return [f"search failed: {_short_err(e)}"]
    if not cands:
        return ["no YouTube results"]

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
                return await asyncio.to_thread(
                    _download_one_sync, c["url"], clients, "youtube")
            except Exception as e:
                errors.append(_short_err(e))
                if _ip_blocked(e):
                    # Every player client will hit the same wall — don't burn
                    # the whole matrix on it (Render without cookies).
                    if notify:
                        await notify(
                            "🛑 YouTube refuses this server's IP — "
                            "switching sources…")
                    return errors
    return errors


# ---------------- JioSaavn (official catalog, no IP blocks) ----------------

async def _try_saavn(query: str, notify=None, *, title: str = "",
                     artist: str = "", duration=None
                     ) -> tuple[str, dict] | list[str]:
    import saavn_source  # local import: needs `cryptography` optional dep

    if not title:
        return ["no metadata for saavn catalog"]
    try:
        got = await saavn_source.fetch(
            query, title=title, artist=artist or "", duration=duration,
            notify=notify)
    except Exception as e:
        log.warning("saavn attempt failed: %s", e)
        got = None
    if got:
        return got
    return ["no official match on jiosaavn"]


# ---------------- SoundCloud (datacenter-IP friendly fallback) ----------------

def _sc_filter_ok(c: dict, title: str, artist: str, duration) -> bool:
    """A re-upload may only stand in for the real thing when it's
    metadata-verified: same title, and artist or duration corroborates it."""
    if _title_sim(c.get("title"), title) < 0.6:
        return False
    if duration:
        if c.get("duration"):
            if abs(c["duration"] - int(duration)) > 40:
                return False
        elif artist:
            return _title_sim(c.get("uploader"), artist) >= 0.6
    return True


async def _try_soundcloud(query: str, notify=None, *, title: str = "",
                          artist: str = "", duration=None
                          ) -> tuple[str, dict] | list[str]:
    if not title:
        # without metadata we can't vouch for a re-upload at all
        return ["no metadata for soundcloud fallback"]
    errors: list[str] = []
    if notify:
        await notify("🔎 Checking SoundCloud for the full track…")
    try:
        cands = await find_track(query, source="soundcloud")
    except Exception as e:
        log.warning("soundcloud search failed: %s", e)
        return [f"soundcloud search failed: {_short_err(e)}"]
    verified = [c for c in cands
                if _sc_filter_ok(c, title, artist, duration)]
    if not verified:
        return ["no matching soundcloud re-upload"]
    if duration:
        verified.sort(key=lambda c: abs(
            (c.get("duration") or 10**6) - int(duration)))
    deadline = time.monotonic() + 60
    for c in verified[:3]:
        if time.monotonic() > deadline:
            errors.append("soundcloud time budget exceeded")
            break
        if notify:
            await notify(
                f"⬇️ Downloading: {(c['title'] or '')[:60]}"
                f"{' — ' + c.get('uploader', '')[:24] if c.get('uploader') else ''}")
        try:
            return await asyncio.to_thread(
                _download_one_sync, c["url"], None, "soundcloud")
        except Exception as e:
            errors.append(_short_err(e))
    return errors


# ---------------- Audius (metadata-verified re-uploads, no IP blocks) -------

async def _try_audius(query: str, notify=None, *, title: str = "",
                      artist: str = "", duration=None
                      ) -> tuple[str, dict] | list[str]:
    import audius_source

    if not title:
        return ["no metadata for audius fallback"]
    try:
        got = await audius_source.fetch(
            query, title=title, artist=artist or "", duration=duration,
            notify=notify)
    except Exception as e:
        log.warning("audius attempt failed: %s", e)
        got = None
    if got:
        return got
    return ["no faithful audius upload"]


# ---------------- entry point ----------------

async def download_best(query: str, notify=None, *, title: str | None = None,
                        artist: str | None = None,
                        duration: int | None = None) -> tuple[str, dict]:
    """Search+download a full song.

    Source order: without a cookies.txt, JioSaavn goes first — it hosts the
    official catalog outside YouTube's IP blocks (Western covers are
    rejected by metadata checks), then SoundCloud and Audius (both
    datacenter-IP friendly, metadata-verified re-uploads only), and YouTube
    last (it only ever answers "Sign in to confirm you're not a bot"
    without cookies, so we fast-bail on the first block error instead of
    grinding a 12-attempt matrix). With a cookies.txt, YouTube goes first
    for full quality, then Saavn, SoundCloud and Audius.
    """
    if notify:
        await notify("⏳ Getting the full song…\n<i>(usually 10–30 seconds)</i>")
    title = title or query

    order = ("youtube", "saavn", "soundcloud", "audius") if COOKIES_FILE \
        else ("saavn", "soundcloud", "audius", "youtube")
    if COOKIES_FILE and notify:
        await notify("🍪 Using YouTube cookies for the best quality…")

    attempts = {
        "saavn": lambda: _try_saavn(
            query, notify, title=title, artist=artist or "", duration=duration),
        "youtube": lambda: _try_youtube(query, notify),
        "soundcloud": lambda: _try_soundcloud(
            query, notify, title=title, artist=artist or "", duration=duration),
        "audius": lambda: _try_audius(
            query, notify, title=title, artist=artist or "", duration=duration),
    }
    errors: list[str] = []
    for name in order:
        try:
            got = await attempts[name]()
        except Exception as e:
            log.exception("%s attempt failed", name)
            errors.append(_short_err(e))
            continue
        if isinstance(got, tuple):
            return got
        errors.extend(got or [])

    msg = "; ".join(errors[-2:]) or "download failed"
    low = " ".join(e.lower() for e in errors)
    hit_yt_block = any(m in low for m in ("not a bot", "sign in", "cookies"))
    if not COOKIES_FILE and any(
            m in low for m in ("403", "rate limit", "blocked",
                               "not a bot", "sign in", "cookies")):
        msg += (" — YouTube is blocking this server's IP; add a YouTube "
                "cookies.txt (YTDLP_COOKIES env var or cookies.txt secret "
                "file) or set YTDLP_PROXY")
    elif COOKIES_FILE and hit_yt_block:
        msg += (" — YouTube refused the configured cookies; they may be "
                "expired — re-export a fresh cookies.txt")
    raise RuntimeError(msg)


def cleanup(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
