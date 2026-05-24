#!/bin/bash
set -e

echo "=== Mise à jour depuis GitHub ==="
rm -rf /tmp/bot
git clone --depth=1 https://github.com/riourik/trading-bot /tmp/bot
cp -r /tmp/bot/. /app/

echo "=== Démarrage du bot ==="
exec python -m bot.main
