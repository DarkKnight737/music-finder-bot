import os
import re


def _local_token() -> str:
    """Local-run convenience: first non-comment line of token.txt."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "token.txt"),
                  encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except FileNotFoundError:
        pass
    return ""


# Precedence: BOT_TOKEN env var (cloud/Render) → token.txt (laptop only —
# never upload that file to GitHub; it is not in DEPLOY.md's upload list).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip() or _local_token()

if not BOT_TOKEN:
    raise SystemExit(
        "No bot token found.\n"
        "  • Cloud (Render): set the BOT_TOKEN environment variable\n"
        "  • Laptop: put the token on a single line in token.txt\n"
        "    next to config.py, then run bot.py again")

_token_re = re.compile(r"^\d+:[\w-]{30,}$")
if not _token_re.match(BOT_TOKEN):
    raise SystemExit(
        "BOT_TOKEN doesn't look like a Telegram token (expected "
        "'<digits>:<secret>'). Check for a truncated or partially "
        "pasted value.")


def token_id() -> str:
    return BOT_TOKEN.split(":", 1)[0] if BOT_TOKEN else ""


# How many results keyboard to show per query
RESULTS_LIMIT = 8
