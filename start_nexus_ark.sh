#!/bin/bash
set -e

echo "🚀 Starting Nexus Ark..."

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "❌ 'uv' command not found. Please install uv first."
    echo "   curl -LsSF https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "📦 Syncing dependencies..."
uv sync

echo "✨ Launching Application..."
uv run nexus_ark.py
