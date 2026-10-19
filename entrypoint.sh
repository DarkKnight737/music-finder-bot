#!/bin/sh
# Start the bgutil PO-token provider (helps yt-dlp on datacenter IPs), then the bot.
if [ -f /opt/bgutil/server/build/main.js ]; then
    node /opt/bgutil/server/build/main.js --port 4416 > /var/log/pot.log 2>&1 &
    echo "bgutil POT provider started"
else
    echo "WARNING: POT provider build missing — downloads may be IP-blocked"
fi
exec python bot.py
