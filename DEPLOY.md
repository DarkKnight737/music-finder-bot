# Deploy to Render (free) 🚂

The bot runs on Render's **free web service** — no card, no trial clock.
It sleeps after ~15 min of silence and wakes automatically on your next
Telegram message or Mini App tap (~1 min cold start, then instant).

## Files to upload to your GitHub repo (once, then only on changes)
- `bot.py` — chat bot + Mini App backend
- `downloader.py` — full-song engine (yt-dlp + PO-token support)
- `music_service.py` — Deezer + iTunes search
- `audius_source.py` — Audius fallback source
- `saavn_source.py` — JioSaavn fallback source
- `config.py` — token (env var overrides)
- `requirements.txt`
- `entrypoint.sh`
- `static/index.html` — Mini App UI (drag the whole `static` folder,
  or Create file → `static/index.html` and paste contents)
- `Dockerfile`

(`render.yaml` already adds prompt fields for `YTDLP_COOKIES` /
`YTDLP_PROXY`; fill them when the Blueprint asks. The `_make_cookie_env.py`
helper stays on your PC — nothing to upload.)

**Never upload to GitHub:** `token.txt` (your local bot-token file) or
`cookies.txt` — both are personal login credentials. The token lives on
Render as the `BOT_TOKEN` env var; on your laptop it's read from
`token.txt` next to `config.py`.

## Steps
1. GitHub → repo → **Add file → Upload files** (or "Add file → Create new
   file" for `static/index.html`) → **Commit changes**.
2. render.com → **New → Blueprint** (it reads `render.yaml`) → pick the repo.
3. Enter the `BOT_TOKEN` secret value when prompted → **Apply**.
4. First build takes ~4–6 min now (node + npm for the PO-token provider).
   Success line: `INFO musicbot: Webhook server live: https://<app>.onrender.com/8758768624`

## The Mini App
- Menu button **🎵 Music App** is registered automatically; also pinned via
  the reply keyboard after `/start`.
- URL: `https://<app>.onrender.com/app/`
- Search → tap **♪ Get** → the MP3 lands in your chat (same engine as chat).

## The quick fix for "confirm you're not a bot"

That error means YouTube refuses downloads from hosted server IPs. Two
settings fix it — set either one, then redeploy:

**Option A — YouTube cookies (most reliable):**
1. Install the browser extension *Get cookies.txt LOCALLY* (Chrome/Firefox).
2. On youtube.com, while signed in, export cookies in **Netscape format**.
3. On your PC, run the helper in this folder (it finds the file in
   Downloads/Desktop automatically, validates it, and writes the exact
   value to `_YTDLP_COOKIES_VALUE.txt`):
   ```
   python _make_cookie_env.py
   ```
4. Open `_YTDLP_COOKIES_VALUE.txt`, Ctrl+A → copy.
5. Give the value to the bot one of three ways:
   - **Render Environment tab:** key `YTDLP_COOKIES`, paste the value.
   - **Blueprint:** it now prompts for `YTDLP_COOKIES` when deploying.
   - **Render Secret File:** Secrets → add `cookies.txt` with the original
     (un-encoded) file content instead (appears at
     `/etc/secrets/cookies.txt`, picked up automatically).
6. Redeploy; the logs print `Using YouTube cookies: …` and `/diag` shows
   `cookies: "…"` when it's working. The chat will show
   `🍪 Using YouTube cookies for the best quality…` on the next request.
   If YouTube *still* says "not a bot" with cookies set, the cookie file
   has expired — re-export it (the bot then says so in chat instead of
   failing cryptically).

**Option B — proxy:** add an Environment variable `YTDLP_PROXY` with any
HTTP/SOCKS proxy URL (e.g. a residential-proxy endpoint). All yt-dlp
traffic (YouTube + SoundCloud) then goes through it, so the server's own
IP is never the one YouTube sees.

## Why full downloads may still occasionally fail

**Fallback sources (new):** without cookies the bot works through sources
that don't need YouTube at all — first **JioSaavn** (official catalog,
then **SoundCloud**, then **Audius** (both datacenter-IP friendly). A
re-upload from SoundCloud/Audius is only ever used when its title,
artist/duration metadata matches the song you asked for, so you never get
the wrong track.

YouTube objecting to datacenter IPs is an arms race. The container now:
- runs the *bgutil PO-token provider* (Node server on `127.0.0.1:4416`;
  the logs print `POT provider OK (version …)` when it started correctly),
- retries with alternate player clients (`web_safari` → `tv,tv_simply` → `mweb`,
  and falls back to SoundCloud if YouTube's search fails).

If a failure still happens, the bot tells you exactly why in chat
(`⚠️ Couldn't fetch the full song right now … <reason>`). Paste that reason
line if you need help.

## Troubleshooting
| Symptom | Meaning / fix |
|---|---|
| "no Telegram session" in Mini App | The app was opened outside Telegram (browser tab/history). Open it via the 🎵 Music App button inside the bot chat. |
| "app opened by a different bot" | The Mini App button belongs to another bot than the deployed one. Send /start to the bot that's actually messaging you and use its Music App button; if correct, the server's `BOT_TOKEN` env var doesn't match — open `https://<app>.onrender.com/diag` and compare `server_bot` / `bot_token_id`. |
| Downloads always fail; check `/diag` | `pot_provider.ok = false` → POT provider didn't start (see deploy logs). `cookies = null` → add a YouTube `cookies.txt` or set `YTDLP_PROXY` (see "The quick fix for 'confirm you're not a bot'"). `proxy = null` → no proxy configured. `ffmpeg = false` → build broken. |
| 401 / unauthorized in Mini App | Open the app via the Telegram button, not the raw URL. |
| 409 Conflict in bot logs | Another instance polls the same token. Stop it. |
| "Invalid Token" build logs | Token env var typo / missing. |
| Webhook registered but bot silent | Delete + re-create webhook: send a `/start` after deploy.

## Diagnostics
Open `https://<app>.onrender.com/diag` in a browser. It reports:
- `server_bot` / `bot_token_id` — the bot identity the server actually uses
  (must match the bot you open the Mini App from),
- `api_ok` — can the server authenticate to Telegram,
- `pot_provider` — whether the yt-dlp PO-token helper is running,
- `cookies` — whether a YouTube cookies.txt was found (the most reliable
  fix for datacenter-IP download blocks),
- `proxy` — whether yt-dlp is routed via `YTDLP_PROXY`,
- `yt_dlp`, `ffmpeg` — versions / presence.

## Deep probe: /diag/sources

Try the exact download pipeline the bot uses, on the server itself:

```
curl "https://<app>.onrender.com/diag/sources" -H "X-Render-Secret: <BOT_TOKEN>"
```

- No query  → runs the full fallback chain (default: "shape of you ed sheeran")
  and answers `{"ok": true, "source": "<which one delivered>", "seconds": ...}`.
- `?q=<anything>` — probe a custom query.
- `?src=youtube|saavn|soundcloud|audius&q=...` — run ONLY that source and get
  its raw error list (rate-limited to one probe per 20 s; header value is the
  plain `BOT_TOKEN` or its sha256).

Handy facts learned on the live deployment:
- YouTube extraction now needs a **JavaScript runtime** (deno/node). The Docker
  image ships one (node, for the PO-token provider) and `downloader.py` wires
  it automatically — without it you get
  "The page needs to be reloaded / Requested format is not available" on every
  client, even with valid cookies.
- From a Render datacenter IP, YouTube still requires **valid** cookies
  (`Sign in to confirm you're not a bot` otherwise). Cookies exported from a
  browser session can be silently rotated/invalidated by Google within
  minutes — if `/diag/sources?src=youtube` shows that error *with* cookies
  set, re-export a fresh `cookies.txt` right after logging in.
