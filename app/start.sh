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

start_tailscale_lite_https() {
    if [ "${NEXUS_ARK_START_TAILSCALE_SERVE:-0}" != "1" ]; then
        return
    fi

    if ! command -v tailscale &> /dev/null; then
        echo -e "${YELLOW}[WARN] tailscale command not found. Skipping Lite HTTPS serve.${NC}"
        return
    fi

    local api_port="${NEXUS_ARK_API_PORT:-8000}"
    export NEXUS_ARK_API_ENABLED="${NEXUS_ARK_API_ENABLED:-1}"
    local target_url="http://127.0.0.1:${api_port}"
    local status_log="/tmp/nexus_ark_tailscale_serve_status.log"
    local serve_log="/tmp/nexus_ark_tailscale_serve.log"
    local dns_name
    dns_name="$(timeout 5s tailscale status --json 2>/dev/null | .venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); print((data.get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"

    if timeout 8s tailscale serve status >"$status_log" 2>&1 && grep -q "$target_url" "$status_log"; then
        echo -e "${GREEN}[OK] Tailscale HTTPS serve is already configured.${NC}"
        if [ -n "$dns_name" ]; then
            echo -e "${YELLOW}Lite HTTPS: https://${dns_name}/lite${NC}"
        else
            echo -e "${YELLOW}Lite HTTPS: https://<your-device>.<tailnet>.ts.net/lite${NC}"
        fi
        return
    fi

    echo -e "${YELLOW}[INFO] Configuring Tailscale HTTPS for Nexus Ark Lite...${NC}"
    if timeout 20s tailscale serve --bg --https=443 "$target_url" >"$serve_log" 2>&1; then
        echo -e "${GREEN}[OK] Tailscale HTTPS serve configured.${NC}"
        if [ -n "$dns_name" ]; then
            echo -e "${YELLOW}Lite HTTPS: https://${dns_name}/lite${NC}"
        else
            echo -e "${YELLOW}Lite HTTPS: https://<your-device>.<tailnet>.ts.net/lite${NC}"
        fi
    else
        local serve_exit=$?
        if [ $serve_exit -eq 124 ]; then
            echo -e "${YELLOW}[WARN] Tailscale HTTPS serve setup timed out. Continuing Nexus Ark startup.${NC}"
        else
            echo -e "${YELLOW}[WARN] Tailscale HTTPS serve setup did not complete.${NC}"
        fi
        echo -e "${YELLOW}       Check: tailscale serve status${NC}"
        if [ -s "$serve_log" ]; then
            sed 's/^/       /' "$serve_log"
        elif [ -s "$status_log" ]; then
            sed 's/^/       /' "$status_log"
        fi
    fi
}

while true; do
    # Sync dependencies (runs every loop iteration to pick up updates)
    echo -e "${YELLOW}[INFO] Checking dependencies...${NC}"
    if uv sync --quiet; then
        echo -e "${GREEN}[OK] Dependencies ready.${NC}"
    else
        echo -e "${RED}[ERROR] Failed to sync dependencies.${NC}"
        exit 1
    fi

    echo -e "${GREEN}[INFO] Starting Nexus Ark...${NC}"
    echo -e "${YELLOW}Access URL: http://0.0.0.0:7860 (Local)${NC}"
    echo -e "${YELLOW}Remote Access: http://<Tailscale-IP>:7860${NC}"
    if [ "${NEXUS_ARK_API_ENABLED:-0}" = "1" ] || [ "${NEXUS_ARK_START_TAILSCALE_SERVE:-0}" = "1" ]; then
        echo -e "${YELLOW}Lite PWA: http://127.0.0.1:${NEXUS_ARK_API_PORT:-8000}/lite${NC}"
    fi
    echo -e "${GREEN}---------------------------------------------------${NC}"

    start_tailscale_lite_https

    uv run nexus_ark.py
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 123 ]; then
        echo -e "${YELLOW}[INFO] Update signal received.${NC}"
        # --- Apply staged update files ---
        STAGING_DIR="$(pwd)/update_staging"
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

            # ルートの pyproject.toml を app/ から同期
            if [ -f "app/pyproject.toml" ]; then
                cp -f "app/pyproject.toml" "./pyproject.toml"
                echo -e "${GREEN}[INFO] pyproject.toml synced from app/.${NC}"
            fi

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
