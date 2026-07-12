from __future__ import annotations

import html
import json
import logging
import mimetypes
import re
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import config_manager
import atelier_app_grants
import room_manager
from api import atelier_tokens
from agent_delegation.manager import _persona_workspace

logger = logging.getLogger(__name__)

app = FastAPI(title="Nexus Ark Atelier Static Apps", version="0.1.0", docs_url=None, redoc_url=None)
_server: Optional[uvicorn.Server] = None
_server_thread: Optional[threading.Thread] = None

_APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_ATELIER_PWA_VERSION = "v4"
_DENIED_DIR_NAMES = {"_nexus", "_source", "_preview", "__pycache__", ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache"}
_ATELIER_LIBS_ROOT = (Path(__file__).resolve().parent.parent / "assets" / "atelier_libs").resolve()
_ATELIER_LIB_ALLOWED_SUFFIXES = {".js", ".map", ".txt"}
_DENIED_FILE_NAMES = {
    ".env",
    ".env.local",
    "config.json",
    "credentials.json",
    "secrets.json",
    "token.json",
}
_DENIED_EXTENSIONS = {
    ".py",
    ".pyc",
    ".pyo",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".bat",
    ".cmd",
    ".ps1",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite",
    ".db",
}
_SENSITIVE_NAME_RE = re.compile(r"(secret|credential|private[_-]?key|api[_-]?key|auth[_-]?token)", re.IGNORECASE)
_NEXUS_MANIFEST_NAME = "nexus.json"
_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _csp_for_request(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


def _settings() -> dict:
    return config_manager.CONFIG_GLOBAL.get("atelier_serve_settings", {}) or {}


def _api_integration_enabled() -> bool:
    return bool(_settings().get("api_integration_enabled", False))


def _api_gateway_settings() -> dict:
    return config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {}


def _api_origin_for_request(request: Request) -> str:
    api_settings = _api_gateway_settings()
    configured = str(_settings().get("api_origin") or "").strip().rstrip("/")
    if configured:
        return configured
    api_port = int(api_settings.get("port", 8000) or 8000)
    if request.url.scheme == "https":
        return f"https://{request.url.hostname}"
    host = request.url.hostname or "127.0.0.1"
    return f"http://{host}:{api_port}"


def _csp_for_request(request: Request) -> str:
    if not _api_integration_enabled():
        return _CSP
    api_origin = _api_origin_for_request(request)
    return _CSP.replace("connect-src 'self';", f"connect-src 'self' {api_origin};")


def _workspace_root(room_name: str) -> Path:
    root, _exclude_dirs, _exclude_files = _persona_workspace(_validate_room_name(room_name))
    return Path(root).resolve()


def _workspace_policy(room_name: str) -> tuple[Path, set[str], set[str]]:
    root, exclude_dirs, exclude_files = _persona_workspace(_validate_room_name(room_name))
    return Path(root).resolve(), {str(item) for item in exclude_dirs or []}, {str(item) for item in exclude_files or []}


def _apps_root(room_name: str) -> Path:
    return (_workspace_root(room_name) / "apps").resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        return path.is_relative_to(parent)
    except AttributeError:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False


def _validate_app_name(app_name: str) -> str:
    name = str(app_name or "").strip()
    if not _APP_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="app not found")
    return name


def _validate_room_name(room_name: str) -> str:
    name = str(room_name or "").strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or any(part in {".", ".."} for part in Path(name).parts)
    ):
        raise HTTPException(status_code=404, detail="room not found")
    return name


def _deny_sensitive_path(path: Path) -> None:
    for part in path.parts:
        if part in _DENIED_DIR_NAMES:
            raise HTTPException(status_code=404, detail="path not found")
        if part.startswith("."):
            raise HTTPException(status_code=404, detail="path not found")
        if _SENSITIVE_NAME_RE.search(part):
            raise HTTPException(status_code=404, detail="path not found")
    name = path.name.lower()
    if name in _DENIED_FILE_NAMES or path.suffix.lower() in _DENIED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="path not found")


def _deny_excluded_path(path: Path, workspace: Path, exclude_dirs: set[str], exclude_files: set[str]) -> None:
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        raise HTTPException(status_code=404, detail="path not found")
    parts = relative.parts
    if any(part in exclude_dirs for part in parts):
        raise HTTPException(status_code=404, detail="path not found")
    if path.name in exclude_files or str(relative) in exclude_files:
        raise HTTPException(status_code=404, detail="path not found")


def _resolve_app_file(room_name: str, app_name: str, requested_path: str) -> Path:
    app_name = _validate_app_name(app_name)
    workspace, exclude_dirs, exclude_files = _workspace_policy(room_name)
    app_root = (_apps_root(room_name) / app_name).resolve()
    if not _is_relative_to(app_root, workspace):
        raise HTTPException(status_code=404, detail="app not found")
    _deny_excluded_path(app_root, workspace, exclude_dirs, exclude_files)
    index_path = (app_root / "index.html").resolve()
    if not index_path.exists() or not index_path.is_file():
        raise HTTPException(status_code=404, detail="app not found")

    rel = (requested_path or "index.html").strip("/")
    rel = rel or "index.html"
    candidate = (app_root / rel).resolve()
    if not (_is_relative_to(candidate, app_root) and _is_relative_to(candidate, workspace)):
        raise HTTPException(status_code=404, detail="path not found")
    _deny_excluded_path(candidate, workspace, exclude_dirs, exclude_files)
    _deny_sensitive_path(candidate)
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="path not found")
    return candidate


def _resolve_atelier_lib_file(requested_path: str) -> Path:
    rel = (requested_path or "").strip().lstrip("/")
    if not rel or "\x00" in rel:
        raise HTTPException(status_code=404, detail="library not found")
    candidate = (_ATELIER_LIBS_ROOT / rel).resolve()
    if not _is_relative_to(candidate, _ATELIER_LIBS_ROOT):
        raise HTTPException(status_code=404, detail="library not found")
    name = candidate.name.lower()
    suffix_allowed = candidate.suffix.lower() in _ATELIER_LIB_ALLOWED_SUFFIXES
    license_allowed = name == "license" or name.endswith("license") or name.endswith("license.txt")
    if not suffix_allowed and not license_allowed:
        raise HTTPException(status_code=404, detail="library not found")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="library not found")
    return candidate


def _app_root_for_existing_app(room_name: str, app_name: str) -> tuple[str, str, Path, Path, set[str], set[str]]:
    room = _validate_room_name(room_name)
    app_name = _validate_app_name(app_name)
    workspace, exclude_dirs, exclude_files = _workspace_policy(room)
    app_root = (_apps_root(room) / app_name).resolve()
    if not (_is_relative_to(app_root, workspace) and app_root.exists() and app_root.is_dir()):
        raise HTTPException(status_code=404, detail="app not found")
    _deny_excluded_path(app_root, workspace, exclude_dirs, exclude_files)
    _deny_sensitive_path(app_root)
    index_path = (app_root / "index.html").resolve()
    if not (
        index_path.exists()
        and index_path.is_file()
        and _is_relative_to(index_path, app_root)
        and _is_relative_to(index_path, workspace)
    ):
        raise HTTPException(status_code=404, detail="app not found")
    _deny_excluded_path(index_path, workspace, exclude_dirs, exclude_files)
    return room, app_name, workspace, app_root, exclude_dirs, exclude_files


def _app_scope_path(room_name: str, app_name: str) -> str:
    return f"/atelier/{quote(room_name, safe='')}/{quote(app_name, safe='')}/"


def _nexus_asset_path(room_name: str, app_name: str, filename: str) -> str:
    return f"{_app_scope_path(room_name, app_name)}_nexus/{filename}"


def _resolve_app_icon(room_name: str, app_name: str) -> Path:
    _room, _app, workspace, app_root, exclude_dirs, exclude_files = _app_root_for_existing_app(room_name, app_name)
    for name in ("icon.png", "icon-512.png", "icon-512x512.png", "icon-192.png", "icon-192x192.png"):
        candidate = (app_root / name).resolve()
        if not (_is_relative_to(candidate, app_root) and _is_relative_to(candidate, workspace)):
            continue
        try:
            _deny_excluded_path(candidate, workspace, exclude_dirs, exclude_files)
            _deny_sensitive_path(candidate)
        except HTTPException:
            continue
        if candidate.exists() and candidate.is_file():
            return candidate
    fallback = Path("icon.png").resolve()
    if fallback.exists() and fallback.is_file():
        return fallback
    raise HTTPException(status_code=404, detail="icon not found")


def _resolve_app_maskable_icon(room_name: str, app_name: str) -> Path:
    _room, _app, workspace, app_root, exclude_dirs, exclude_files = _app_root_for_existing_app(room_name, app_name)
    for name in ("icon-maskable.png", "icon-maskable-512.png", "icon-maskable-512x512.png"):
        candidate = (app_root / name).resolve()
        if not (_is_relative_to(candidate, app_root) and _is_relative_to(candidate, workspace)):
            continue
        try:
            _deny_excluded_path(candidate, workspace, exclude_dirs, exclude_files)
            _deny_sensitive_path(candidate)
        except HTTPException:
            continue
        if candidate.exists() and candidate.is_file():
            return candidate
    fallback = Path("icon-maskable.png").resolve()
    if fallback.exists() and fallback.is_file():
        return fallback
    raise HTTPException(status_code=404, detail="maskable icon not found")


def _icon_asset_version(room_name: str, app_name: str, *, maskable: bool = False) -> str:
    """アイコン画像の更新時刻(mtime)を版数文字列で返す（URLのキャッシュバスター用）。

    アイコンを差し替えるとmtimeが変わり、manifest/apple-touch-iconのURL(`?v=...`)も変わるため、
    ブラウザ・Service Worker双方のキャッシュ（URL単位）を確実にすり抜けて新しいアイコンが反映される。
    """
    try:
        path = (
            _resolve_app_maskable_icon(room_name, app_name)
            if maskable
            else _resolve_app_icon(room_name, app_name)
        )
        return str(int(path.stat().st_mtime))
    except Exception:
        return "0"


def _app_content_version(app_root: Path) -> str:
    """アプリフォルダ内ファイルの最終更新時刻の最大値を版数文字列で返す。

    Service Workerのキャッシュ名に含めることで、アプリのどれか1ファイルでも編集すると
    キャッシュ名が変わり、SWが入れ替わって古いキャッシュを破棄→最新の本体が確実に配信される。
    """
    try:
        latest = 0.0
        for p in app_root.rglob("*"):
            if p.is_file():
                if "_preview" in p.relative_to(app_root).parts:
                    continue
                try:
                    mtime = p.stat().st_mtime
                    if mtime > latest:
                        latest = mtime
                except OSError:
                    continue
        return str(int(latest))
    except Exception:
        return "0"


def _build_manifest(room_name: str, app_name: str) -> dict:
    room, app_name, _workspace, _app_root, _exclude_dirs, _exclude_files = _app_root_for_existing_app(room_name, app_name)
    scope = _app_scope_path(room, app_name)
    icon_v = _icon_asset_version(room, app_name)
    mask_v = _icon_asset_version(room, app_name, maskable=True)
    icon_src = f"{_nexus_asset_path(room, app_name, 'icon.png')}?v={icon_v}"
    maskable_src = f"{_nexus_asset_path(room, app_name, 'icon-maskable.png')}?v={mask_v}"
    return {
        "name": app_name,
        "short_name": app_name[:24] or "Atelier App",
        "start_url": scope,
        "scope": scope,
        "display": "standalone",
        "background_color": "#f4f7f5",
        "theme_color": "#16413f",
        "icons": [
            {"src": icon_src, "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": icon_src, "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": maskable_src, "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": maskable_src, "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


def _build_register_js(room_name: str, app_name: str) -> str:
    room, app_name, _workspace, _app_root, _exclude_dirs, _exclude_files = _app_root_for_existing_app(room_name, app_name)
    scope = _app_scope_path(room, app_name)
    sw_url = "_nexus/sw.js"
    return (
        "(() => {\n"
        "  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;\n"
        "  window.addEventListener('load', () => {\n"
        f"    navigator.serviceWorker.register({json.dumps(sw_url)}, {{ scope: './' }}).catch(() => {{}});\n"
        "  });\n"
        f"  window.__NEXUS_ATELIER_PWA_SCOPE__ = {json.dumps(scope)};\n"
        "})();\n"
    )


def _room_atelier_api_settings(room_name: str) -> dict:
    config = room_manager.get_room_config(_validate_room_name(room_name)) or {}
    overrides = config.get("override_settings", {}) if isinstance(config, dict) else {}
    settings = overrides.get("atelier_app_api", {})
    return settings if isinstance(settings, dict) else {}


def _read_nexus_manifest(app_root: Path, workspace: Path, exclude_dirs: set[str], exclude_files: set[str]) -> tuple[str, list[dict[str, str]]]:
    manifest_path = (app_root / _NEXUS_MANIFEST_NAME).resolve()
    if not (_is_relative_to(manifest_path, app_root) and _is_relative_to(manifest_path, workspace)):
        return "", []
    try:
        _deny_excluded_path(manifest_path, workspace, exclude_dirs, exclude_files)
    except HTTPException:
        return "", []
    if not manifest_path.exists() or not manifest_path.is_file():
        return "", []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return "", []
    if not isinstance(data, dict):
        return "", []
    name = str(data.get("name") or "").strip()[:80]
    requested: list[dict[str, str]] = []
    for item in data.get("requested_scopes", []) or []:
        # 仕様書（ATELIER_API_REFERENCE）が示す文字列配列形式 ["send_chat"] と、
        # 理由つきのオブジェクト形式 [{"scope": "send_chat", "reason": "..."}] の両方を受け付ける。
        if isinstance(item, str):
            scope = item.strip()
            reason = ""
        elif isinstance(item, dict):
            scope = str(item.get("scope") or "").strip()
            reason = str(item.get("reason") or "").strip()[:500]
        else:
            continue
        if scope not in atelier_app_grants.GRANTABLE_SCOPES:
            continue
        requested.append({"scope": scope, "reason": reason})
    return name, requested


def _build_sw_js(room_name: str, app_name: str) -> str:
    room, app_name, _workspace, app_root, _exclude_dirs, _exclude_files = _app_root_for_existing_app(room_name, app_name)
    scope = _app_scope_path(room, app_name)
    cache_prefix = f"nexus-atelier-{room}-{app_name}-"
    # アプリ内容の版数をキャッシュ名に含め、本体ファイル編集時に自動でキャッシュを破棄する
    content_version = _app_content_version(app_root)
    cache_name = f"{cache_prefix}{_ATELIER_PWA_VERSION}-{content_version}"
    assets = [
        scope,
        f"{scope}index.html",
        _nexus_asset_path(room, app_name, "manifest.webmanifest"),
        _nexus_asset_path(room, app_name, "icon.png"),
        _nexus_asset_path(room, app_name, "icon-maskable.png"),
    ]
    return f"""
const CACHE_NAME = {json.dumps(cache_name)};
const CACHE_PREFIX = {json.dumps(cache_prefix)};
const SCOPE_PATH = {json.dumps(scope)};
const CORE_ASSETS = {json.dumps(assets, ensure_ascii=False)};

self.addEventListener("install", (event) => {{
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      Promise.all(CORE_ASSETS.map((url) => cache.add(url).catch(() => null)))
    )
  );
  self.skipWaiting();
}});

self.addEventListener("activate", (event) => {{
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
}});

async function networkFirst(request) {{
  const cache = await caches.open(CACHE_NAME);
  try {{
    const response = await fetch(request);
    if (response && response.ok) {{
      cache.put(request, response.clone());
    }}
    return response;
  }} catch (error) {{
    const cached = await cache.match(request);
    if (cached) return cached;
    throw error;
  }}
}}

async function staleWhileRevalidate(request) {{
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  const update = fetch(request).then((response) => {{
    if (response && response.ok) {{
      cache.put(request, response.clone());
    }}
    return response;
  }}).catch(() => null);
  if (cached) return cached;
  const response = await update;
  if (response) return response;
  return fetch(request);
}}

self.addEventListener("fetch", (event) => {{
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  const isSharedLibrary = url.pathname.startsWith("/atelier/_lib/");
  if (!isSharedLibrary && !url.pathname.startsWith(SCOPE_PATH)) return;
  if (event.request.mode === "navigate" || url.pathname === SCOPE_PATH || url.pathname === `${{SCOPE_PATH}}index.html`) {{
    event.respondWith(networkFirst(event.request));
    return;
  }}
  event.respondWith(staleWhileRevalidate(event.request));
}});
""".lstrip()


def _inject_pwa_tags(index_path: Path, room_name: str, app_name: str) -> HTMLResponse:
    try:
        content = index_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = index_path.read_text(encoding="utf-8", errors="replace")
    # アプリ同梱の <link rel="manifest"> を除去する。
    # ブラウザは最初の manifest を採用するため、アプリ側が独自 manifest.json
    # （外部アイコンURL・不正な指定を含むことがある）を持つと、サーバが合成する
    # インストール可能な manifest より優先され、「インストール」ではなく
    # 「ショートカット作成」しか出ない原因になる。除去してサーバ合成版を唯一の有効 manifest にする。
    content = re.sub(
        r'<link\b[^>]*\brel\s*=\s*["\']manifest["\'][^>]*>',
        "",
        content,
        flags=re.IGNORECASE,
    )
    icon_v = _icon_asset_version(room_name, app_name)
    snippet = (
        '<link rel="manifest" href="_nexus/manifest.webmanifest">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-title" content="{title}">\n'
        '<link rel="apple-touch-icon" href="_nexus/icon.png?v={icon_v}">\n'
        '<script src="_nexus/register.js"></script>\n'
    ).format(title=html.escape(app_name, quote=True), icon_v=icon_v)
    lower = content.lower()
    head_index = lower.rfind("</head>")
    if head_index >= 0:
        content = content[:head_index] + snippet + content[head_index:]
    else:
        doctype_end = content.find(">")
        if content[:100].lower().startswith("<!doctype") and doctype_end >= 0:
            content = content[: doctype_end + 1] + "\n" + snippet + content[doctype_end + 1 :]
        else:
            content = snippet + content
    return HTMLResponse(content)


def list_atelier_apps(room_name: str) -> list[dict[str, str]]:
    room = str(room_name or "").strip()
    if not room:
        return []
    try:
        apps_root = _apps_root(room)
        workspace, exclude_dirs, exclude_files = _workspace_policy(room)
    except Exception:
        logger.debug("failed to resolve atelier workspace for %s", room, exc_info=True)
        return []
    if not apps_root.exists() or not apps_root.is_dir():
        return []

    apps: list[dict[str, str]] = []
    for child in sorted(apps_root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or not _APP_NAME_RE.fullmatch(child.name):
            continue
        try:
            resolved = child.resolve()
            if not (_is_relative_to(resolved, apps_root) and _is_relative_to(resolved, workspace)):
                continue
            _deny_excluded_path(resolved, workspace, exclude_dirs, exclude_files)
            _deny_sensitive_path(resolved)
            index_path = (resolved / "index.html").resolve()
            _deny_excluded_path(index_path, workspace, exclude_dirs, exclude_files)
            if index_path.exists() and index_path.is_file() and _is_relative_to(index_path, resolved):
                apps.append({"name": child.name, "path": str(index_path)})
        except Exception:
            logger.debug("skipping atelier app candidate: %s", child, exc_info=True)
    return apps


def _render_room_index(room_name: str) -> str:
    apps = list_atelier_apps(room_name)
    escaped_room = html.escape(room_name)
    items = []
    for item in apps:
        name = item["name"]
        url = f"/atelier/{quote(room_name, safe='')}/{quote(name, safe='')}/"
        items.append(f"<li><a href=\"{url}\">{html.escape(name)}</a></li>")
    body = "\n".join(items) if items else "<li>apps/&lt;name&gt;/index.html はまだ見つかりません。</li>"
    return (
        "<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>Nexus Ark Atelier - {escaped_room}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;line-height:1.6}"
        "a{color:#1f6feb}</style></head><body>"
        f"<h1>Atelier Apps: {escaped_room}</h1><ul>{body}</ul></body></html>"
    )


@app.get("/health", include_in_schema=False)
def health_check() -> dict:
    return {"status": "ok"}


@app.get("/atelier/_lib/{requested_path:path}", include_in_schema=False)
def atelier_lib_file(requested_path: str) -> FileResponse:
    return _file_response(_resolve_atelier_lib_file(requested_path))


@app.get("/atelier/{room_name}", include_in_schema=False)
def room_redirect(room_name: str) -> RedirectResponse:
    return RedirectResponse(url=f"/atelier/{quote(room_name, safe='')}/", status_code=307)


@app.get("/atelier/{room_name}/", include_in_schema=False)
def room_index(room_name: str) -> HTMLResponse:
    return HTMLResponse(_render_room_index(room_name))


@app.get("/atelier/{room_name}/{app_name}", include_in_schema=False)
def app_redirect(room_name: str, app_name: str) -> RedirectResponse:
    return RedirectResponse(url=f"/atelier/{quote(room_name, safe='')}/{quote(app_name, safe='')}/", status_code=307)


@app.get("/atelier/{room_name}/{app_name}/", include_in_schema=False)
def app_index(room_name: str, app_name: str) -> HTMLResponse:
    room, app_name, _workspace, _app_root, _exclude_dirs, _exclude_files = _app_root_for_existing_app(room_name, app_name)
    return _inject_pwa_tags(_resolve_app_file(room, app_name, "index.html"), room, app_name)


@app.get("/atelier/{room_name}/{app_name}/_nexus/manifest.webmanifest", include_in_schema=False)
def app_manifest(room_name: str, app_name: str) -> Response:
    manifest = _build_manifest(room_name, app_name)
    return Response(
        json.dumps(manifest, ensure_ascii=False),
        media_type="application/manifest+json",
    )


@app.get("/atelier/{room_name}/{app_name}/_nexus/sw.js", include_in_schema=False)
def app_service_worker(room_name: str, app_name: str) -> Response:
    room, app_name, _workspace, _app_root, _exclude_dirs, _exclude_files = _app_root_for_existing_app(room_name, app_name)
    response = Response(_build_sw_js(room, app_name), media_type="application/javascript")
    response.headers["Service-Worker-Allowed"] = _app_scope_path(room, app_name)
    # SW本体は常に再検証させ、アプリ更新でキャッシュ名が変わったことをブラウザが検知できるようにする
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/atelier/{room_name}/{app_name}/_nexus/register.js", include_in_schema=False)
def app_register_js(room_name: str, app_name: str) -> Response:
    return Response(_build_register_js(room_name, app_name), media_type="application/javascript")


@app.get("/atelier/{room_name}/{app_name}/_nexus/icon.png", include_in_schema=False)
def app_icon(room_name: str, app_name: str) -> FileResponse:
    # アイコン差し替えが確実に反映されるよう再検証を必須にする（URLの ?v= と併用）
    return FileResponse(
        _resolve_app_icon(room_name, app_name),
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/atelier/{room_name}/{app_name}/_nexus/icon-maskable.png", include_in_schema=False)
def app_icon_maskable(room_name: str, app_name: str) -> FileResponse:
    return FileResponse(
        _resolve_app_maskable_icon(room_name, app_name),
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/atelier/{room_name}/{app_name}/_nexus/config", include_in_schema=False)
def app_config(request: Request, room_name: str, app_name: str) -> JSONResponse:
    if not _api_integration_enabled():
        raise HTTPException(status_code=404, detail="api integration disabled")
    room, app_name, workspace, app_root, exclude_dirs, exclude_files = _app_root_for_existing_app(room_name, app_name)
    manifest_name, requested_scopes = _read_nexus_manifest(app_root, workspace, exclude_dirs, exclude_files)
    granted_scopes, pending_scopes = atelier_app_grants.update_manifest_requests(room, app_name, requested_scopes)
    embedded_scopes = [atelier_tokens.ATELIER_SCOPE_SAFE] + granted_scopes
    token = atelier_tokens.create_atelier_token(
        room,
        atelier_tokens.ATELIER_SCOPE_SAFE,
        app_id=app_name,
        scopes=embedded_scopes,
    )
    atelier_app_grants.record_audit(room, "token_issue", app_name, status="issued", details=",".join(granted_scopes))
    return JSONResponse(
        {
            "apiBase": _api_origin_for_request(request),
            "roomId": room,
            "appId": app_name,
            "appName": manifest_name or app_name,
            "scope": atelier_tokens.ATELIER_SCOPE_SAFE,
            "grantedScopes": embedded_scopes,
            "pendingScopes": pending_scopes,
            "token": token,
            "expiresIn": atelier_tokens.ATELIER_TOKEN_TTL_SECONDS,
        }
    )


@app.get("/atelier/{room_name}/{app_name}/{requested_path:path}", include_in_schema=False)
def app_file(room_name: str, app_name: str, requested_path: str):
    path = _resolve_app_file(room_name, app_name, requested_path)
    if requested_path.strip("/") == "index.html":
        room = _validate_room_name(room_name)
        name = _validate_app_name(app_name)
        return _inject_pwa_tags(path, room, name)
    return _file_response(path)


def _file_response(path: Path) -> FileResponse:
    media_type, _encoding = mimetypes.guess_type(str(path))
    return FileResponse(path, media_type=media_type or "application/octet-stream")


def start_server(port: Optional[int] = None, host: Optional[str] = None, daemon: bool = True) -> None:
    global _server, _server_thread
    if _server_thread and _server_thread.is_alive():
        print("--- [Atelier Serve] 既に実行中です ---")
        return

    settings = _settings()
    host = host or settings.get("host") or "0.0.0.0"
    port = int(port or settings.get("port") or 8765)
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s - uvicorn.access - %(levelname)s - %(message)s"
    config = uvicorn.Config(app, host=host, port=port, log_level="info", log_config=log_config)
    _server = uvicorn.Server(config)

    def run_server() -> None:
        print(f"--- [Atelier Serve] http://{host}:{port} で開始しました ---")
        _server.run()

    _server_thread = threading.Thread(target=run_server, daemon=daemon)
    _server_thread.start()


def stop_server() -> None:
    global _server
    if _server:
        _server.should_exit = True
        print("--- [Atelier Serve] 停止しました ---")
