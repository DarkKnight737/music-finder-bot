"""YouTube via Invidious mirrors (no cookies, no bot checks).

When YouTube itself blocks the server's datacenter IP ("Sign in to confirm
you're not a bot"), the request is re-routed: a public Invidious instance
(which talks to YouTube from ITS IP) does the search and format extraction,
and we then download the youtube-videoplayback stream directly. Those media
URLs are served cross-IP by Google's CDN, so the bot block doesn't apply.

Videos are metadata-verified before download — exactly like the Audius and
SoundCloud fallbacks — so a candidate only stands in for the requested track
when it is a faithful re-upload: matching title plus corroborating
duration/artist, and never a remix/cover/lyrics upload.

Instances rotate on failure. `local` proxied URLs are NOT used: instances
front those with Anubis JS proof-of-work pages that we can't answer.
"""
import asyncio
import logging
import os
import re
import time
import uuid

import httpx

from audius_source import _clean, _convert_sync, _norm, _ok, _sniff, TEMP

log = logging.getLogger("musicbot.ytmirror")

# Tried in order; the first one that works is remembered per process.
# Public alternative-front-end instances rotate constantly — see README.
_INSTANCES = [
    "https://invidious.f5.si",
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
]

# Optional user-supplied instances first: comma-separated base URLs, e.g.
# INVIDIOUS_INSTANCES=https://invidious.mydomain.tld (self-hosted ones are
# by far the most reliable — no rate limits, no outdated software).
_SELF_HOSTED = [i.strip() for i in (os.getenv("INVIDIOUS_INSTANCES") or "")
                .split(",") if i.strip()]

# The official registry (api.invidious.io) — refreshed at most every 6 h so
# a dead instance never lingers at the top of the rotation for weeks.
_REGISTRY_TTL = 6 * 3600
_registry_since = [0.0]          # list = mutable module state
_registry_known: list[str] = []

_client_sync = httpx.Client(timeout=15, follow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0"})

_client = httpx.AsyncClient(timeout=30, follow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0"})
_active: str | None = None

_MAX_DUR = 1500          # ignore anything longer than 25 minutes
_MAX_BYTES = 25_000_000  # single-stream download safety cap
_MIN_BYTES = 300_000     # a real full song is never smaller than this


def _fetch_sync_registry() -> list[str]:
    """Blocking: live instance list from the official registry.

    Sorted so instances that actually deliver playback (the registry
    tracks a per-instance success ratio) come first, otherwise a dead
    extractor with a huge user count bubbles above a working one.
    Yggdrasil-only domains are skipped — unreachable from here."""
    r = _client_sync.get(
        "https://api.invidious.io/instances.json?sort_by=type,users")
    ranked = []
    for name, info in (r.json() or []):
        info = info if isinstance(info, dict) else {}
        uri = info.get("uri") or f"https://{name}"
        if info.get("type") != "https" or ".ygg" in uri:
            continue
        mon = info.get("monitor") or {}
        if mon and mon.get("down"):
            continue
        pb = (info.get("stats") or {}).get("playback") or {}
        ratio = float(pb.get("ratio") or 0.0)
        ranked.append((0 if ratio > 0 else 1, -ratio,
                       -int((info.get("stats") or {})
                            .get("usage", {}).get("users", {}).get("total", 0)
                        or 0), uri))
    ranked.sort()
    return [u for *_, u in ranked]


def _ordered_instances() -> list[str]:
    """Instances to try first→last: env-supplied, registry (cached),
    then the baked-in list. The registry refresh runs at most every 6 h
    and silently falls back to the static list when unreachable."""
    fresh = False
    if _registry_since[0] + _REGISTRY_TTL <= time.time():
        try:
            known = _fetch_sync_registry()
            if known:
                _registry_known.clear()
                _registry_known.extend(known)
            fresh = True
        except Exception as e:
            log.info("invidious registry fetch failed (%s) — "
                     "trying static list", e)
        _registry_since[0] = time.time()
    seen, order = set(), []
    for base in _SELF_HOSTED + _registry_known + _INSTANCES:
        if base not in seen:
            seen.add(base)
            order.append(base)
    if fresh:
        log.info("invidious registry: %d candidate instances", len(order))
    return order


# Instances that just failed a real request are avoided for 10 min — a
# health check can pass while actual video extraction is broken.
_DEAD_TTL = 600
_dead: dict[str, float] = {}


def _mark_dead(base: str) -> None:
    _dead[base] = time.time()
    globals()["_active"] = None


async def _find_instance() -> str:
    """Return a working instance base URL, rotating past failures."""
    global _active
    if _active:
        return _active
    for base in await asyncio.to_thread(_ordered_instances):
        if time.time() - _dead.get(base, 0) < _DEAD_TTL:
            continue
        try:
            r = await _client.get(f"{base}/api/v1/videos/dQw4w9WgXcQ",
                                  timeout=20)
            j = r.json()
            if isinstance(j, dict) and j.get("title") and \
                    not j.get("error"):
                _active = base
                log.info("invidious mirror up: %s", base)
                return base
        except Exception:
            continue
    raise RuntimeError("no invidious instance reachable")


async def search(query: str, limit: int = 10) -> list[dict]:
    """Search through the mirror: [{title, artist, duration, id}]"""
    data = None
    for _ in range(2):
        base = await _find_instance()
        try:
            r = await _client.get(f"{base}/api/v1/search",
                                  params={"q": query, "type": "video"},
                                  timeout=20)
            data = r.json()
            break
        except Exception as e:
            log.warning("mirror search failed on %s — rotating: %s",
                        base, e)
            _mark_dead(base)
    if not isinstance(data, list):
        return []
    out = []
    for it in (data if isinstance(data, list) else [])[:limit]:
        if not it.get("videoId"):
            continue
        out.append({
            "title": _clean(it.get("title")),
            "artist": _clean(it.get("author")),
            "duration": int(it.get("lengthSeconds") or 0),
            "id": it.get("videoId"),
        })
    return out


async def _streams(vid: str) -> list[dict]:
    """Audio-only format list for a video: [{url, kind}]

    Instance-aware: an extraction failure (broken companion, rate limit)
    kills the instance (10-min dead-list) and rotates; failures then cost
    the candidate loop nothing."""
    for _ in range(3):
        base = await _find_instance()
        try:
            r = await _client.get(f"{base}/api/v1/videos/{vid}",
                                  timeout=20)
            data = r.json()
        except Exception as e:
            log.warning("mirror extraction failed on %s — rotating: %s",
                        base, e)
            _mark_dead(base)
            continue
        if not isinstance(data, dict) or data.get("error"):
            _mark_dead(base)
            log.warning("mirror extraction failed on %s: %s", base,
                        str((data or {}).get("error"))[:80]
                        if isinstance(data, dict) else "bad payload")
            continue
        aud = []
        for f in (data.get("adaptiveFormats") or []):
            kind = str(f.get("type") or "")
            url = f.get("url")
            if not url or not kind.startswith("audio"):
                continue
            br = int(f.get("bitrate") or 0)
            if not (40_000 <= br <= 320_000):
                continue
            aud.append({"url": url, "kind": kind, "bitrate": br})
        aud.sort(key=lambda a: a["bitrate"], reverse=True)
        return aud
    raise RuntimeError("mirror extraction failed: no working instance")


async def _fetch_range(url: str, a: int, b: int) -> bytes:
    r = await _client.get(url, headers={"Range": f"bytes={a}-{b}"})
    if r.status_code != 206:
        raise RuntimeError(f"range request ignored ({r.status_code})")
    return r.content


async def _download(url: str, dst: str, workers: int = 4) -> str:
    """Download a media URL, using parallel ranges when the server honors
    them, otherwise one sequential stream. File is sniffed afterwards."""
    try:
        r = await _client.get(url, headers={"Range": "bytes=0-0"})
        total = int(r.headers.get("content-length") or
                    r.headers.get("content-range", "0/0").split("/")[-1] or 0)
    except Exception:
        total = 0
    buf = b""
    if total > _MIN_BYTES:
        step = total // workers
        spans = [(i * step, (total - 1) if i == workers - 1
                  else (i + 1) * step - 1) for i in range(workers)]
        try:
            parts = await asyncio.wait_for(
                asyncio.gather(*[_fetch_range(url, a, b) for a, b in spans]),
                timeout=180)
            if all(len(p) == (b - a + 1) for p, (a, b) in zip(parts, spans)):
                buf = b"".join(parts)
        except Exception as e:
            log.info("parallel ranges failed (%s) — falling back to "
                     "sequential stream", e)
            buf = b""
    if not buf:
        async with _client.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks = []
            got = 0
            async for chunk in resp.aiter_bytes(128 * 1024):
                chunks.append(chunk)
                got += len(chunk)
                if got > _MAX_BYTES:
                    raise RuntimeError("stream exceeds size cap")
            buf = b"".join(chunks)
    if len(buf) < _MIN_BYTES:
        raise RuntimeError(f"stream too small ({len(buf)} bytes)")
    if not _sniff(buf[:4]):
        raise RuntimeError("not an audio stream")
    with open(dst, "wb") as fh:
        fh.write(buf)
    return dst


async def fetch(query: str, *, title: str, artist: str, duration=None,
                notify=None) -> tuple[str, dict] | None:
    """Full download for a verified mirror video, or None."""
    if not title:
        return None
    if notify:
        await notify("🔎 Checking YouTube mirrors…")
    try:
        cands = await search(query)
    except Exception as e:
        log.warning("mirror search failed: %s", e)
        return None
    verified = [c for c in cands if _ok(c, title, artist, duration)
                and (c.get("duration") or 0) <= _MAX_DUR]
    if not verified:
        return None
    if duration:
        verified.sort(key=lambda c: abs(
            (c.get("duration") or 10**6) - int(duration)))

    for c in verified[:3]:
        if notify:
            await notify(
                f"⬇️ Downloading (mirror): {_clean(c['title'])[:60]}")
        # Fresh stream URLs are re-extracted per attempt: googlevideo
        # URLs carry an IP-bound PO token and can be refused (403) once
        # consumed, so a retry must come from a new extraction, not the
        # same URL.
        src = None
        for attempt in range(2):
            tmp = os.path.join(TEMP, f"yt_{uuid.uuid4().hex[:12]}.webm")
            try:
                aud = await _streams(c["id"])
                if not aud:
                    break
                await _download(aud[0]["url"], tmp)
                src = tmp
                break
            except Exception as e:
                log.warning("mirror download failed (try %d): %s",
                            attempt + 1, str(e)[:120])
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                continue
        if not src:
            continue

        if notify:
            await notify("🎚 Converting to MP3…")
        dst = src[:-5] + "_ytm.mp3"
        try:
            result = await asyncio.to_thread(_convert_sync, src, dst)
        except Exception as e:
            log.warning("mirror mp3 conversion failed: %s", e)
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
                        "source": "invidious"}
    return None
