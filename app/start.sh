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

while true; do
    echo -e "${GREEN}[INFO] Starting Nexus Ark...${NC}"
    echo -e "${YELLOW}Access URL: http://0.0.0.0:7860 (Local)${NC}"
    echo -e "${YELLOW}Remote Access: http://<Tailscale-IP>:7860${NC}"
    echo -e "${GREEN}---------------------------------------------------${NC}"

    uv run nexus_ark.py
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 123 ]; then
        echo -e "${YELLOW}[INFO] Update signal received.${NC}"
        # --- Apply staged update files ---
        STAGING_DIR="$(dirname "$0")/../update_staging"
        if [ -d "$STAGING_DIR" ]; then
            echo -e "${YELLOW}[INFO] Applying update from staging area...${NC}"
            rsync -a \
                --exclude='characters' \
                --exclude='memories' \
                --exclude='logs' \
                --exclude='metadata' \
                --exclude='backups' \
                --exclude='.venv' \
                --exclude='__pycache__' \
                --exclude='config.json' \
                --exclude='alarms.json' \
                --exclude='redaction_rules.json' \
                --exclude='.gemini_key_states.json' \
                --exclude='*.log' \
                "$STAGING_DIR/" "./"
            echo -e "${GREEN}[INFO] Update files applied successfully.${NC}"
            rm -rf "$STAGING_DIR"
        fi
        echo -e "${YELLOW}[INFO] Restarting application...${NC}"
        continue
    fi

    # Check exit code for other errors
    if [ $EXIT_CODE -ne 0 ]; then
        echo -e "${RED}[ERROR] Nexus Ark exited with error code $EXIT_CODE.${NC}"
    fi
    break
done

echo -e "${GREEN}---------------------------------------------------${NC}"
echo -e "Application Closed."
