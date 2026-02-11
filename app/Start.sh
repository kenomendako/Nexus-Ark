#!/bin/bash

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║       Nexus Ark を起動中...           ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# Move to app directory (relative to this script's location)
cd "$(dirname "$0")/app" || {
    echo "[ERROR] app フォルダが見つかりません。"
    exit 1
}

# Delegate to internal start script
if [ -f "start.sh" ]; then
    ./start.sh
else
    echo "[ERROR] app/start.sh が見つかりません。"
    exit 1
fi
