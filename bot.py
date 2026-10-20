"""Telegram music-finder bot: full MP3s in chat or via the Mini App."""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      MenuButtonWebApp, Message, ReplyKeyboardMarkup, Update,
                      WebAppInfo)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import config
import downloader
import music_service

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("musicbot")

STATIC_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "static"

WELCOME = (
    "👋 <b>Hi! I'm your Music Finder.</b>\n\n"
    "🔍 Type any <b>song name</b> — I'll send the <b>full song as MP3</b>.\n\n"
    "• ⬇️ Full MP3 with proper title/artist\n"
    "• 📱 Tap <b>Music App</b> (in the menu or the reply keyboard) for the Mini App"
)


def esc(s: str) -> str:
    return html.escape(s or "")


def clean_song_title(s: str) -> str:
    """Strip YouTube junk like '(Official Video)', '(Lyrics)' etc."""
    return re.sub(
        r"\s*\((?:official\s*)?(?:video|audio|lyrics?|music\s*video|mv|hd|hq|visualizer)[^)]*\)\s*",
        "", s or "", flags=re.IGNORECASE).strip()


# ---------------- health server (long-polling/cloud ping) ----------------

def _start_health_server(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"Music Finder bot is running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", port)


# ---------------- POT provider check (yt-dlp YouTube helper) ----------------

def check_pot_provider() -> None:
    """Log whether the local bgutil PO-token provider responded on 127.0.0.1:4416."""
    import httpx as _httpx

    def _run():
        for attempt in range(10):
            try:
                r = _httpx.get("http://127.0.0.1:4416/ping", timeout=3)
                data = r.json()
                log.info("POT provider OK (version %s)",
                         data.get("version", "unknown"))
                return
            except Exception as e:
                if attempt == 9:
                    log.warning(
                        "POT provider unreachable after retries: %s — "
                        "YouTube downloads on datacenter IPs may be blocked. "
                        "Consider adding a cookies.txt file.", e)
                    return
                time.sleep(3)

    threading.Thread(target=_run, daemon=True).start()


# ---------------- keyboards / texts ----------------

def list_kb(tracks: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, t in enumerate(tracks):
        label = f"{i + 1}. {t['title'][:26]} — {t['artist'][:20]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"track|{i}")])
    rows.append([
        InlineKeyboardButton("✖️ Close", callback_data="close"),
        InlineKeyboardButton("🔍 New search", callback_data="new"),
    ])
    return InlineKeyboardMarkup(rows)


def list_text(query: str, tracks: list[dict]) -> str:
    lines = [f"🎵 <b>Results for:</b> “{esc(query)}”", ""]
    for i, t in enumerate(tracks):
        d = music_service.fmt_duration(t["duration"])
        lines.append(f"<b>{i + 1}.</b> <b>{esc(t['title'])}</b> "
                     f"— {esc(t['artist'])} [{d}]")
    lines += ["", "<i>Tap a track to receive the full song as MP3 🎵</i>"]
    return "\n".join(lines)


def detail_kb(t: dict, idx: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⬇️ Full song (MP3)", callback_data=f"full|{idx}")],
    ]
    if t["link"]:
        rows.append([InlineKeyboardButton("🔗 Full track", url=t["link"])])
    rows.append([InlineKeyboardButton("◀️ Back to results", callback_data="back")])
    return InlineKeyboardMarkup(rows)


async def send_welcome(update, context):
    w = context.bot_data.get("webapp_url")
    kb = None
    if w:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("🎵 Music App", web_app=WebAppInfo(url=w))]],
            resize_keyboard=True)
    await update.effective_message.reply_text(
        WELCOME, parse_mode=ParseMode.HTML, reply_markup=kb,
        disable_web_page_preview=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_welcome(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_welcome(update, context)


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.effective_message.text or "").strip()
    if not query:
        return
    msg = await update.effective_message.reply_text("🔎 Searching…")

    tracks = await music_service.search_all(query, config.RESULTS_LIMIT)
    if not tracks:
        await msg.edit_text(
            "😕 No results found.\n"
            "Try the exact song title, or add the artist name.")
        return

    context.chat_data["tracks"] = tracks
    context.chat_data["query"] = query
    await msg.edit_text(list_text(query, tracks), parse_mode=ParseMode.HTML,
                        reply_markup=list_kb(tracks))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    if data == "close":
        try:
            await q.edit_message_text("✖️ List closed. Send a song name to search again!")
        except Exception:
            pass
        return

    if data == "new":
        try:
            await q.edit_message_text("🔍 Type a song name to search!")
        except Exception:
            pass
        return

    tracks: list[dict] = context.chat_data.get("tracks") or []

    if data == "back":
        if not tracks:
            await q.edit_message_text("⌛ Expired — search again please.")
            return
        await q.edit_message_text(list_text(context.chat_data.get("query", ""), tracks),
                                  parse_mode=ParseMode.HTML, reply_markup=list_kb(tracks))
        return

    parts = data.split("|")
    if len(parts) != 2 or parts[0] not in ("track", "full"):
        return
    idx = int(parts[1])

    if not tracks or idx >= len(tracks):
        await q.edit_message_text("⌛ Expired — search again please.")
        return
    t = tracks[idx]

    if parts[0] == "track":
        d = music_service.fmt_duration(t["duration"])
        caption = (f"🎵 <b>{esc(t['title'])}</b>\n"
                   f"👤 {esc(t['artist'])}\n"
                   f"💿 {esc(t['album'])}  [{d}]")
        await q.edit_message_text(
                caption, parse_mode=ParseMode.HTML, reply_markup=detail_kb(t, idx))
        return

    # parts[0] == "full" -> full song as MP3
    runner = _ChatRunner(context.bot, q.message.chat_id)
    await run_download_delivery(runner, t)


# ---------------- delivery engine (chat buttons + Mini App) ----------------

class _ChatRunner:
    """Uniform status/audio delivery for callbacks and Mini App requests."""

    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self._note: Message | None = None

    async def status(self, text: str) -> None:
        if self._note is None:
            self._note = await self.bot.send_message(
                self.chat_id, text, parse_mode=ParseMode.HTML)
        else:
            try:
                await self._note.edit_text(text)
            except Exception:
                pass

    async def status_done(self) -> None:
        if self._note is None:
            return
        try:
            await self._note.delete()
        except Exception:
            pass
        self._note = None

    async def audio(self, *, audio_path=None, audio_bytes=None, title,
                    performer=None, duration=None, caption=None, thumb=None):
        if audio_path:
            with open(audio_path, "rb") as fh:
                await self.bot.send_audio(
                    chat_id=self.chat_id, audio=fh,
                    title=title, performer=performer,
                    duration=duration or None,
                    caption=caption, parse_mode=ParseMode.HTML,
                    thumbnail=thumb,
                    read_timeout=120, write_timeout=600, connect_timeout=60,
                )
        elif audio_bytes:
            await self.bot.send_audio(
                chat_id=self.chat_id, audio=audio_bytes,
                title=title, performer=performer,
                duration=duration or None,
                caption=caption, parse_mode=ParseMode.HTML,
            )


async def run_download_delivery(runner: _ChatRunner, t: dict,
                                query_text: str | None = None) -> None:
    """Full song as MP3; on failure just report why (no 30s preview fallback)."""
    query = query_text or f"{t['artist']} {t['title']}".strip()
    await runner.status("⏳ Getting the full song…\n<i>(usually 10–30 seconds)</i>")
    try:
        path, info = await downloader.download_best(
            query, notify=runner.status,
            title=t["title"], artist=t["artist"], duration=t["duration"] or None)
    except Exception as e:
        log.warning("full download failed: %s", e)
        await runner.status(
            "⚠️ Couldn't fetch the full song right now.\n"
            f"<i>{esc(str(e)[-180:])}</i>\n"
            "Please try again in a bit, or use the 🔗 Full track link.")
        await runner.status_done()
        return

    try:
        title = clean_song_title(info.get("title") or t["title"])[:60]
        duration = int(info.get("duration") or t["duration"] or 0)
        caption = (f"🎵 <b>{esc(clean_song_title(title))}</b>\n"
                   f"👤 {esc(t['artist'])}\n"
                   f"⏱ {music_service.fmt_duration(duration)} · full song 🎵")
        thumb = await music_service.download_file(t.get("thumb") or "")
        await runner.audio(
            audio_path=path, title=title,
            performer=t["artist"][:60] or None,
            duration=duration or None,
            caption=caption, thumb=thumb,
        )
    finally:
        downloader.cleanup(path)
        await runner.status_done()


# ---------------- Mini App (Telegram WebApp) ----------------

MINI_SESSIONS: dict[int, dict] = {}

# In-container the page is served from static/index.html. This embedded copy
# is used when that file is missing (e.g. the deploy repo lacks the static
# folder), so the Mini App can never 404.
MINI_APP_FALLBACK = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Music Finder</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #0e1621);
    --sec: var(--tg-theme-secondary-bg-color, #1c2733);
    --text: var(--tg-theme-text-color, #e8eef5);
    --hint: var(--tg-theme-hint-color, #8d99a9);
    --accent: var(--tg-theme-button-color, #2ea6ff);
    --accentText: var(--tg-theme-button-text-color, #fff);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header {
    padding: 14px 16px 8px; display: flex; align-items: center; gap: 10px;
  }
  header h1 { font-size: 18px; margin: 0; font-weight: 700; }
  header .tag { font-size: 12px; color: var(--hint); }
  .searchbar {
    display: flex; gap: 8px; padding: 8px 12px 12px;
  }
  input[type="search"] {
    flex: 1; padding: 12px 14px; border-radius: 12px; border: none;
    background: var(--sec); color: var(--text); font-size: 15px; outline: none;
  }
  button {
    border: none; border-radius: 12px; padding: 12px 18px;
    background: var(--accent); color: var(--accentText); font-size: 15px; font-weight: 600;
  }
  button:disabled { opacity: .55; }
  #status { text-align: center; color: var(--hint); font-size: 13px; padding: 2px 0 10px; min-height: 18px; }
  .card {
    background: var(--sec); margin: 8px 10px; padding: 12px 14px;
    border-radius: 14px; display: flex; align-items: center; gap: 12px;
  }
  .card .info { flex: 1; min-width: 0; }
  .card .t { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card .a { color: var(--hint); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card .d { color: var(--hint); font-size: 12px; }
  .card button { padding: 8px 12px; font-size: 13px; border-radius: 10px; }
  footer { text-align: center; color: var(--hint); font-size: 12px; padding: 14px 0 24px; }
</style>
</head>
<body>
<header>
  <h1>🎵 Music Finder</h1>
  <span class="tag">full MP3 → your chat</span>
</header>

<div class="searchbar">
  <input id="q" type="search" placeholder="Song name or artist…"
         autocomplete="off" autocorrect="off" spellcheck="false">
  <button id="go">Search</button>
</div>
<div id="status"></div>
<div id="results"></div>

<footer>Results are sent directly into this chat 📎</footer>

<script>
  const tg = window.Telegram.WebApp;
  const $ = (id) => document.getElementById(id);
  const statusEl = $("status"), resultsEl = $("results");

  tg.ready();
  tg.expand();
  tg.setHeaderColor ? tg.setHeaderColor('bg_color') : null;

  const s = (text) => { statusEl.textContent = text; };

  async function api(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let j = {};
    try { j = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(
      j.error === "unauthorized"
        ? "sign in failed — close this app and reopen it via the Telegram button"
        : (j.error || ("HTTP " + res.status)));
    return j;
  }

  async function search() {
    const q = $("q").value.trim();
    if (!q) { s("Type a song name first 🙂"); return; }
    resultsEl.innerHTML = "";
    $("go").disabled = true;
    s("🔎 Searching… (first query after idle may take a bit longer)");
    try {
      const r = await api("/api/search", { initData: tg.initData || "", query: q });
      if (!r.tracks.length) { s("😕 Nothing found — try the exact title."); return; }
      s(r.tracks.length + " results — tap a card");
      resultsEl.innerHTML = "";
      r.tracks.forEach(t => {
        const card = document.createElement("div");
        card.className = "card";

        const info = document.createElement("div");
        info.className = "info";
        const t1 = document.createElement("div"); t1.className = "t";
        t1.textContent = (t.index + 1) + ". " + t.title;
        const t2 = document.createElement("div"); t2.className = "a";
        t2.textContent = t.artist + (t.album ? " · " + t.album : "");
        const t3 = document.createElement("div"); t3.className = "d";
        t3.textContent = Math.floor(t.duration / 60) + ":" +
          String(t.duration % 60).padStart(2, "0");
        info.append(t1, t2, t3);

        const btn = document.createElement("button");
        btn.textContent = "♪ Get";
        btn.onclick = () => send(t, card);

        card.append(info, btn);
        resultsEl.append(card);
      });
    } catch (e) {
      s("⚠️ " + e.message);
    } finally {
      $("go").disabled = false;
    }
  }

  async function send(t, card) {
    const btn = card.querySelector("button");
    btn.disabled = true;
    btn.textContent = "…";
    tg.HapticFeedback.impactOccurred("light");
    s("⬇️ Fetching full song — watch your chat");
    try {
      const r = await api("/api/send",
        { initData: tg.initData || "", index: t.index });
      if (r.ok) {
        btn.textContent = "✓ Sent";
        s("✅ On its way to your chat!");
        tg.HapticFeedback.notificationOccurred("success");
      } else {
        btn.disabled = false;
        btn.textContent = "♪ Get";
        s("⚠️ " + (r.error || "failed"));
      }
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "♪ Get";
      s("⚠️ " + e.message);
    }
  }

  $("go").addEventListener("click", search);
  $("q").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") search();
  });
  // pre-fill with pasted text or startParameter
  if (tg.initDataUnsafe && tg.initDataUnsafe.start_param) {
    $("q").value = tg.initDataUnsafe.start_param;
    search();
  }
</script>
</body>
</html>"""


def _remember_mini(uid: int, tracks: list[dict], query: str) -> None:
    MINI_SESSIONS[uid] = {"tracks": tracks, "query": query, "ts": time.time()}
    while len(MINI_SESSIONS) > 100:
        oldest = min(MINI_SESSIONS, key=lambda k: MINI_SESSIONS[k]["ts"])
        del MINI_SESSIONS[oldest]


def validate_initdata(raw: str) -> dict | None:
    """HMAC-validate Telegram WebApp initData; returns the user dict or None."""
    user, _ = validate_initdata_ex(raw)
    return user


def validate_initdata_ex(raw: str) -> tuple[dict | None, str]:
    """Same as validate_initdata but also returns a failure reason:
    'ok' | 'empty' | 'malformed' | 'no-hash' | 'hash-mismatch' | 'bad-user'."""
    if not raw or not raw.strip():
        return None, "empty"
    try:
        fields = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
    except ValueError:
        return None, "malformed"
    provided = fields.pop("hash", "")
    if not provided:
        return None, "no-hash"
    # Per https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app:
    # the data-check-string is built from the *URL-decoded* key=value pairs
    # (sorted alphabetically, `hash` excluded) joined with newlines. Hashing the
    # raw urlencoded string never matches when values contain %XX escapes
    # (the `user` field always does), so values must be decoded first. The
    # `signature` field, when present, IS part of the hash (verified against
    # real captured Telegram initData samples).
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, provided):
        return None, "hash-mismatch"
    try:
        user = json.loads(fields.get("user") or "")
        uid = int(user.get("id") or 0)
    except Exception:
        return None, "bad-user"
    return (user if uid else None), ("ok" if uid else "bad-user")


def _mini_handlers(app, aio_web):
    bot = app.bot

    def _server_bot() -> str:
        uname = getattr(app.bot_data, "get", lambda *_: None)("bot_username") \
            or "the bot"
        return f"@{uname}"

    async def serve_app(_request):
        try:
            page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except FileNotFoundError:
            page = MINI_APP_FALLBACK
        return aio_web.Response(text=page, content_type="text/html",
                                charset="utf-8")

    async def api_diag(request):
        """Public diagnostics so deploy state can be checked from a browser."""
        import yt_dlp
        import shutil
        info: dict = {
            "server_bot": _server_bot().strip("@"),
            "bot_token_id": config.BOT_TOKEN.split(":", 1)[0],
            "yt_dlp": yt_dlp.version.__version__,
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "cookies": downloader.COOKIES_FILE or None,
        }
        try:
            r = await bot.get_me()
            info["api_ok"] = True
        except Exception as e:
            info["api_ok"] = False
            info["api_error"] = str(e)[:120]
        try:
            import httpx
            rp = httpx.get("http://127.0.0.1:4416/ping", timeout=4)
            info["pot_provider"] = {"ok": True,
                                    "version": rp.json().get("version")}
        except Exception as e:
            info["pot_provider"] = {"ok": False, "error": str(e)[:120]}
        return aio_web.json_response(info)

    async def api_search(request):
        try:
            body = await request.json()
        except Exception:
            return aio_web.json_response({"ok": False, "error": "bad json"}, status=400)
        user, reason = validate_initdata_ex(body.get("initData") or "")
        if user is None:
            log.warning("Mini App auth failed (reason=%s)", reason)
            if reason in ("empty", "malformed", "no-hash"):
                return aio_web.json_response({
                    "ok": False, "error":
                    "no Telegram session — open the app via the 🎵 Music App "
                    f"button in the chat with {_server_bot()} "
                    "(not a browser tab or history link)"}, status=401)
            if reason == "hash-mismatch":
                return aio_web.json_response({
                    "ok": False, "error":
                    f"app opened by a different bot — send /start to "
                    f"{_server_bot()} and tap the Music App button there "
                    "(or check the server's BOT_TOKEN)"}, status=401)
            return aio_web.json_response(
                {"ok": False, "error": "sign in failed — reopen the app"}, status=401)
        query = (body.get("query") or "").strip()[:120]
        if not query:
            return aio_web.json_response({"ok": False, "error": "no query"}, status=400)
        tracks = await music_service.search_all(query, config.RESULTS_LIMIT)
        if not tracks:
            return aio_web.json_response({"ok": True, "tracks": []})
        _remember_mini(user["id"], tracks, query)
        return aio_web.json_response({
            "ok": True,
            "tracks": [{"index": i, "title": t["title"], "artist": t["artist"],
                        "album": t["album"], "duration": t["duration"]}
                       for i, t in enumerate(tracks)],
        })

    async def api_send(request):
        try:
            body = await request.json()
        except Exception:
            return aio_web.json_response({"ok": False, "error": "bad json"}, status=400)
        user = validate_initdata(body.get("initData") or "")
        if user is None:
            return aio_web.json_response(
                {"ok": False, "error":
                 "sign in failed — reopen the app via the Music App button"},
                status=401)
        session = MINI_SESSIONS.get(user["id"])
        if not session:
            return aio_web.json_response({"ok": False, "error": "search first"}, status=400)
        try:
            idx = int(body.get("index", 0))
        except (TypeError, ValueError):
            return aio_web.json_response({"ok": False, "error": "bad index"}, status=400)
        tracks = session["tracks"]
        if not 0 <= idx < len(tracks):
            return aio_web.json_response({"ok": False, "error": "bad index"}, status=400)
        t = tracks[idx]
        runner = _ChatRunner(bot, user["id"])
        task = asyncio.create_task(run_download_delivery(runner, t))

        def _log_task(fut):
            if fut.exception():
                log.error("mini send failed: %s", fut.exception())
        task.add_done_callback(_log_task)
        return aio_web.json_response({"ok": True})

    return serve_app, api_search, api_send, api_diag


# ---------------- entry points ----------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", context.error)


def build_app() -> Application:
    app = (Application.builder()
           .token(config.BOT_TOKEN)
           .read_timeout(120)
           .write_timeout(600)
           .connect_timeout(60)
           .pool_timeout(60)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    app.add_error_handler(on_error)
    return app


def main():
    base = os.getenv("RENDER_EXTERNAL_URL")
    port = os.getenv("PORT")

    if base and port and port.isdigit():
        check_pot_provider()
        asyncio.run(_run_webhook_mode(int(port), base.rstrip("/")))
        return

    health_port = os.getenv("HEALTH_PORT")
    if health_port and health_port.isdigit():
        _start_health_server(int(health_port))

    app = build_app()
    check_pot_provider()
    log.info("Bot starting (long polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


async def _run_webhook_mode(port: int, base: str) -> None:
    """Cloud mode: aiohttp webserver with webhook + Mini App routes."""
    from aiohttp import web as aio_web

    app = build_app()
    bot = app.bot
    app.bot_data["webapp_url"] = f"{base}/app/"
    path_seg = config.BOT_TOKEN.split(":", 1)[0]
    secret = hashlib.sha256(config.BOT_TOKEN.encode()).hexdigest()
    webhook_url = f"{base}/{path_seg}"

    await app.initialize()
    me = await bot.get_me()
    app.bot_data["bot_username"] = me.username
    log.info("Server bot identity: @%s (token id %s)",
             me.username, config.BOT_TOKEN.split(":", 1)[0])
    await bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )
    await app.start()

    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🎵 Music App",
                                         web_app=WebAppInfo(url=f"{base}/app/")))
    except Exception as e:
        log.warning("menu button not set: %s", e)

    server = aio_web.Application()
    server.router.add_post(f"/{path_seg}", (webhook_ingest := _make_ingest(app, bot, secret)))
    server.router.add_get("/healthz", lambda _r: aio_web.Response(text="ok"))
    serve_app, api_search, api_send, api_diag = _mini_handlers(app, aio_web)
    server.router.add_get("/", serve_app)
    server.router.add_get("/app", serve_app)
    server.router.add_get("/app/", serve_app)
    server.router.add_get("/diag", api_diag)
    server.router.add_post("/api/search", api_search)
    server.router.add_post("/api/send", api_send)

    runner = aio_web.AppRunner(server)
    await runner.setup()
    await aio_web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Webhook server live: %s (port %d)", webhook_url, port)
    log.info("Mini App served at %s/app/ and %s/", base, base)

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


def _make_ingest(app, bot, secret):
    async def ingest(request) -> "object":
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            from aiohttp import web as aio_web
            return aio_web.Response(status=403)
        try:
            update = Update.de_json(await request.json(), bot)
            if update is not None:
                asyncio.create_task(app.process_update(update))
        except Exception:
            log.exception("bad webhook update")
        from aiohttp import web as aio_web
        return aio_web.Response()
    return ingest


if __name__ == "__main__":
    main()
