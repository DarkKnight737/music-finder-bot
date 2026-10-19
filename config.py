import os

# On your laptop: uses this default. In the cloud: set BOT_TOKEN env var.
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN", "8758768624:AAEb9RtgXfdTIP1swW7VUmjFeKM194dIarA")

# How many results keyboard to show per query
RESULTS_LIMIT = 8
