#!/bin/bash

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}---------------------------------------------------${NC}"
echo -e "${GREEN} Nexus Ark Launching (WSL/Linux)${NC}"
echo -e "${GREEN}---------------------------------------------------${NC}"

# Ensure we are in the script's directory
cd "$(dirname "$0")" || exit 1

# Check if uv is installed, if not, install it
if ! command -v uv &> /dev/null; then
    echo -e "${YELLOW}[INFO] 'uv' not found. Installing uv...${NC}"
    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        echo -e "${GREEN}[OK] uv installed successfully.${NC}"
        # Add to PATH for current session
        export PATH="$HOME/.local/bin:$PATH"
        # Source shell config if exists
        if [ -f "$HOME/.bashrc" ]; then
            source "$HOME/.bashrc" 2>/dev/null || true
        fi
    else
        echo -e "${RED}[ERROR] Failed to install uv. Please install manually:${NC}"
        echo -e "${YELLOW}   curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
        exit 1
    fi
fi

# Verify uv is now available
if ! command -v uv &> /dev/null; then
    # Try with explicit path
    if [ -f "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    else
        echo -e "${RED}[ERROR] uv still not found after installation.${NC}"
        echo -e "${YELLOW}   Please restart your terminal and try again.${NC}"
        exit 1
    fi
fi

# Sync dependencies (only installs if needed, very fast if up-to-date)
echo -e "${YELLOW}[INFO] Checking dependencies...${NC}"
if uv sync --quiet; then
    echo -e "${GREEN}[OK] Dependencies ready.${NC}"
else
    echo -e "${RED}[ERROR] Failed to sync dependencies.${NC}"
    exit 1
fi

# Launch Application
echo -e "${GREEN}[INFO] Starting Nexus Ark...${NC}"
echo -e "${YELLOW}Access URL: http://0.0.0.0:7860 (Local)${NC}"
echo -e "${YELLOW}Remote Access: http://<Tailscale-IP>:7860${NC}"
echo -e "${GREEN}---------------------------------------------------${NC}"

uv run nexus_ark.py

# Check exit code
if [ $? -ne 0 ]; then
    echo -e "${RED}[ERROR] Nexus Ark exited with error.${NC}"
fi

echo -e "${GREEN}---------------------------------------------------${NC}"
echo -e "Application Closed."
