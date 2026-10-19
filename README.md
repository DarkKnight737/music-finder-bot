# 🎵 Music Finder Telegram Bot

Find any song by name — get the full song as an MP3, artwork info,
and a link to the full track.

## Files
- `bot.py` — bot logic (polling, buttons, sending audio)
- `downloader.py` — full-song search/download via yt-dlp (YouTube → MP3)
- `music_service.py` — Deezer + iTunes Search API (free, no API key)
- `config.py` — bot token & settings

## Run
```
pip install -r requirements.txt
pip install yt-dlp
python bot.py
```

## Usage
1. Open your bot in Telegram → press **Start**.
2. Type a song name, e.g. `blinding lights`.
3. Pick a result from the list:
   - **⬇️ Full song (MP3)** — searches YouTube, downloads the full track,
     converts to 192 kbps MP3 (ffmpeg) and sends it to you.
   - **🔗 Full track** — opens the song on Deezer/iTunes.

## Notes
- Full songs come from YouTube audio via `yt-dlp` → MP3 (needs `ffmpeg`; found on this machine ✔).
- If a full download fails, the bot tells you why in chat and you can retry.
- Telegram bots can upload files up to ~50 MB — normal songs are fine.
- Bot uses long polling — no server needed. Stop with `Ctrl+C`.
