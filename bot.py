"""Telegram music-finder bot: search songs by name, send full MP3s."""
import html
import logging
import os
import re
import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import config
import downloader
import music_service


def _start_health_server(port: int) -> None:
    """Hugging Face Spaces Docker: containers must listen on their app port."""
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

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("musicbot")

WELCOME = (
    "👋 <b>Hi! I'm your Music Finder.</b>\n\n"
    "🔍 Just type any <b>song name</b> (artist + song works best) and I'll find it.\n\n"
    "What I can do:\n"
    "• 🎧 Find tracks on Deezer + iTunes\n"
    "• ⬇️ Send the <b>full song</b> as MP3\n"
    "• 🔗 Link to the full track\n\n"
    "Try it: type <code>blinding lights</code>"
)


def esc(s: str) -> str:
    return html.escape(s or "")


def clean_song_title(s: str) -> str:
    """Strip YouTube junk like '(Official Video)', '(Lyrics)' etc."""
    return re.sub(
        r"\s*\((?:official\s*)?(?:video|audio|lyrics?|music\s*video|mv|hd|hq|visualizer)[^)]*\)\s*",
        "", s or "", flags=re.IGNORECASE).strip()


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
    lines += ["", "<i>Tap a track → get the full song as MP3 🎵</i>"]
    return "\n".join(lines)


def detail_kb(t: dict, idx: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⬇️ Full song (MP3)", callback_data=f"full|{idx}")],
        [InlineKeyboardButton("🎧 30s preview", callback_data=f"dl|{idx}")],
    ]
    if t["link"]:
        rows.append([InlineKeyboardButton("🔗 Full track", url=t["link"])])
    rows.append([InlineKeyboardButton("◀️ Back to results", callback_data="back")])
    return InlineKeyboardMarkup(rows)


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        WELCOME, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


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
    if len(parts) != 2 or parts[0] not in ("track", "dl", "full"):
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

    if parts[0] == "dl":
        await send_preview(q, t)
        return

    # parts[0] == "full" -> full song as MP3
    await send_full(q, t)


async def send_preview(q, t: dict) -> None:
    """Send the official 30-second preview."""
    await q.answer("⏳ Downloading…", show_alert=False)
    note = await context_msg(q).reply_text("⬇️ Loading preview…")
    blob = await music_service.download_file(t["preview"])
    try:
        await note.delete()
    except Exception:
        pass
    if not blob:
        await note.edit_text("⚠️ Preview not available — open the full track with the 🔗 button.")
        return
    await context_msg(q).reply_audio(
        audio=blob,
        title=t["title"][:60],
        performer=t["artist"][:60],
        caption=f"🎧 {esc(t['title'])} — {esc(t['artist'])}",
        parse_mode=ParseMode.HTML,
    )


async def send_full(q, t: dict) -> None:
    """Search YouTube for the full track, convert to MP3 and send."""
    chat = context_msg(q)
    note = await chat.reply_text(
        "⏳ Getting the full song…\n<i>(usually takes 10–30 seconds)</i>",
        parse_mode=ParseMode.HTML)

    async def upd(text: str) -> None:
        try:
            await note.edit_text(text)
        except Exception:
            pass

    query = f"{t['artist']} {t['title']}".strip()
    try:
        path, info = await downloader.download_best(query, notify=upd)
    except Exception as e:
        log.warning("full download failed: %s", e)
        await upd("⚠️ Couldn't fetch the full version — sending the 30s preview instead…")
        blob = await music_service.download_file(t["preview"])
        if blob:
            await chat.reply_audio(
                audio=blob,
                title=t["title"][:60],
                performer=t["artist"][:60],
                caption=f"🎧 {esc(t['title'])} — {esc(t['artist'])}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await upd("⚠️ Download failed. Try the 🔗 Full track button instead.")
        return

    try:
        title = clean_song_title(info.get("title") or t["title"])[:60]
        duration = int(info.get("duration") or t["duration"] or 0)
        caption = (
            f"🎵 <b>{esc(clean_song_title(title))}</b>\n"
            f"👤 {esc(t['artist'])}\n"
            f"⏱ {music_service.fmt_duration(duration)} · full song 🎵"
        )
        thumb = await music_service.download_file(t.get("thumb") or "")
        with open(path, "rb") as fh:
            await chat.reply_audio(
                audio=fh,
                title=title,
                performer=t["artist"][:60],
                duration=duration or None,
                caption=caption,
                parse_mode=ParseMode.HTML,
                thumbnail=thumb,
            )
    finally:
        downloader.cleanup(path)
        try:
            await note.delete()
        except Exception:
            pass


def context_msg(q) -> object:
    """Chat the button came from (for sending follow-up messages)."""
    return q.message


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", context.error)


def build_app() -> "Application":
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
        # ---- Cloud (Render): webhook mode, required on free plan --------
        import hashlib
        from aiohttp import web as aio_web

        app = build_app()
        port_i = int(port)

        extra = aio_web.Application()

        async def _healthz(_) -> "aio_web.Response":
            return aio_web.Response(text="ok")

        extra.router.add_get("/healthz", _healthz)

        app.run_webhook(
            listen="0.0.0.0",
            port=port_i,
            url_path=config.BOT_TOKEN.split(":", 1)[0],  # opaque path segment
            url=base,
            secret_token=hashlib.sha256(config.BOT_TOKEN.encode()).hexdigest(),
            webhook_app=extra,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        # Render's health check hits /healthz; Telegram posts updates to
        # https://<Render-URL>/<path>. Both keep the instance awake.
        log.info("Webhook server live on port %s (%s)", port_i, base)
        return

    health_port = os.getenv("HEALTH_PORT")
    if health_port and health_port.isdigit():
        _start_health_server(int(health_port))

    # ---- Local: long polling ------------------------------------------
    app = build_app()
    log.info("Bot starting (long polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
