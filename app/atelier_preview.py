from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import config_manager
import playwright_utils
from atelier_serve import server as atelier_server

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def _preview_base_url() -> str:
    settings = config_manager.CONFIG_GLOBAL.get("atelier_serve_settings", {}) or {}
    port = int(settings.get("port", 8765) or 8765)
    host = str(settings.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _ensure_server_running() -> None:
    thread = getattr(atelier_server, "_server_thread", None)
    if thread and thread.is_alive():
        return
    settings = config_manager.CONFIG_GLOBAL.get("atelier_serve_settings", {}) or {}
    port = int(settings.get("port", 8765) or 8765)
    host = str(settings.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    atelier_server.start_server(port=port, host=host, daemon=True)
    time.sleep(0.4)


def _app_url(room_name: str, app_name: str) -> str:
    room = atelier_server.quote(atelier_server._validate_room_name(room_name), safe="")
    app = atelier_server.quote(atelier_server._validate_app_name(app_name), safe="")
    return f"{_preview_base_url()}/atelier/{room}/{app}/"


def capture(room_name: str, app_name: str, *, wait_ms: int = 2500, viewport: dict[str, int] | None = None) -> dict[str, Any]:
    """Open an atelier app in headless Chromium and save preview artifacts under app/_preview/."""
    if not PLAYWRIGHT_AVAILABLE:
        return {
            "ok": False,
            "error": "Playwright がインストールされていません。`.venv/bin/python -m pip install playwright` を実行してください。",
        }

    room, app, _workspace, app_root, _exclude_dirs, _exclude_files = atelier_server._app_root_for_existing_app(room_name, app_name)
    preview_dir = app_root / "_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    screenshot_path = preview_dir / f"{stamp}.png"
    report_path = preview_dir / f"{stamp}.json"

    console_errors: list[str] = []
    console_warnings: list[str] = []
    page_errors: list[str] = []
    url = _app_url(room, app)
    viewport = viewport or {"width": 390, "height": 844}

    try:
        _ensure_server_running()
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as launch_exc:
                if playwright_utils.is_executable_missing_error(launch_exc):
                    return {
                        "ok": False,
                        "error": (
                            "Playwright の Chromium ブラウザが見つかりません。"
                            "`.venv/bin/python -m playwright install chromium` を実行してください。"
                        ),
                    }
                raise
            try:
                context = browser.new_context(viewport=viewport, device_scale_factor=1)
                page = context.new_page()

                def _record_console_message(msg) -> None:
                    line = f"{msg.type}: {msg.text}"
                    if msg.type == "error":
                        console_errors.append(line)
                    elif msg.type == "warning":
                        console_warnings.append(line)

                page.on("console", _record_console_message)
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                response = page.goto(url, wait_until="networkidle", timeout=15000)
                status = response.status if response else None
                page.wait_for_timeout(max(0, int(wait_ms)))
                page.screenshot(path=str(screenshot_path), full_page=True)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — ツール返却用に型名つきで保持する。
        report = {
            "ok": False,
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
            "console_errors": console_errors,
            "console_warnings": console_warnings,
            "page_errors": page_errors,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report | {"report_path": str(report_path)}

    report = {
        "ok": not console_errors and not page_errors and (status is None or status < 400),
        "url": url,
        "status": status,
        "screenshot_path": str(screenshot_path),
        "report_path": str(report_path),
        "console_errors": console_errors,
        "console_warnings": console_warnings,
        "page_errors": page_errors,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
