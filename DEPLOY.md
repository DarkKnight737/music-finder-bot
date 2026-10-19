# Deploy to Render (free) 🚂

The bot runs on Render's **free web service** — no card, no trial clock.
It sleeps after ~15 min of silence and wakes automatically on your next
Telegram message or Mini App tap (~1 min cold start, then instant).

## Files to upload to your GitHub repo (once, then only on changes)
- `bot.py` — chat bot + Mini App backend
- `downloader.py` — full-song engine (yt-dlp + PO-token support)
- `music_service.py` — Deezer + iTunes search
- `config.py` — token (env var overrides)
- `requirements.txt`
- `entrypoint.sh`
- `static/index.html` — Mini App UI (drag the whole `static` folder,
  or Create file → `static/index.html` and paste contents)
- `Dockerfile`

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

## Why full downloads may still occasionally fail
YouTube objecting to datacenter IPs is an arms race. The container now:
- runs the *bgutil PO-token provider* (Node server on `127.0.0.1:4416`;
  the logs print `POT provider OK (version …)` when it started correctly),
- retries with alternate player clients (`web_safari` → `tv,tv_simply` → `mweb`,
  and falls back to SoundCloud if YouTube's search fails).

**The most reliable fix is a YouTube cookies file** (YouTube trusts signed-in
browsers more than server IPs):
1. Install a browser extension like *Get cookies.txt LOCALLY* (Chrome/Firefox).
2. On youtube.com, while logged in, export cookies in **Netscape format**.
3. Render dashboard → your service → **Environment → Secret Files** → add a
   file named `cookies.txt` with that content (it appears at
   `/etc/secrets/cookies.txt` inside the container and is picked up
   automatically — the logs print `Using YouTube cookies: …`).
4. Redeploy.

If a failure still happens, the bot tells you exactly why in chat
(`⚠️ Couldn't fetch the full song right now … <reason>`). Paste that reason
line if you need help.

## Troubleshooting
| Symptom | Meaning / fix |
|---|---|
| 401 / unauthorized in Mini App | Open the app via the Telegram button, not the raw URL. |
| 409 Conflict in bot logs | Another instance polls the same token. Stop it. |
| "Invalid Token" build logs | Token env var typo / missing. |
| Webhook registered but bot silent | Delete + re-create webhook: send a `/start` after deploy. |
