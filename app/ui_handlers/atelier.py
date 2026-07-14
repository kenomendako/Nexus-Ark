"""ui_handlers のうち「アトリエ（作品・Webアプリ配信・権限・アイコン）」ドメイン。

ui_handlers パッケージから再エクスポートされ、呼び出し側は従来どおり
ui_handlers.<関数名> でアクセスできる。
"""

from PIL import Image, ImageOps
from pathlib import Path
import agent_delegation.manager as agent_delegation_manager
import atelier_app_grants
import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer
import gradio as gr
import os
import pandas as pd
from urllib.parse import quote
import shutil
import subprocess
import threading
import time
import traceback
import logging

logger = logging.getLogger(__name__)

from ._tailscale import (
    _get_tailscale_dns_name,
    _get_tailscale_ipv4,
    _get_tailscale_serve_status,
    _get_tailscale_serve_status_json,
    _tailscale_serve_points_to_port,
    _summarize_tailscale_serve_json,
)


# --- アトリエ関連の表示カラム/スコープ定義 ---
ATELIER_WORK_COLUMNS = ["work_id", "state", "kind", "created_at", "last_referenced_at", "title"]
ATELIER_APP_COLUMNS = ["app", "PC URL", "同一Wi-Fi URL", "index.html"]
ATELIER_APP_PENDING_COLUMNS = ["app_id", "scope", "reason"]
ATELIER_APP_ACTIVE_GRANT_COLUMNS = ["app_id", "scope", "granted_at", "expires_at"]
ATELIER_APP_WRITE_SCOPES = {
    "write_location", "send_chat", "write_event", "write_calendar", "write_items", "write_notes",
    "write_autonomy", "use_voice", "manage_push",
}
ATELIER_APP_OUTWARD_SCOPES = {"post_twitter"}


def _atelier_scope_label(scope: str) -> str:
    labels = {
        "read_chat": "会話履歴の読み取り",
        "read_memory": "記憶検索",
        "read_notes": "ノート読み取り",
        "read_calendar": "予定読み取り",
        "read_twitter": "Twitter下書き読み取り",
        "read_items": "アイテム読み取り",
        "read_letters": "手紙箱読み取り",
        "read_autonomy": "自律行動状態の読み取り",
        "write_location": "現在地変更",
        "send_chat": "発話・応答生成",
        "write_event": "外部イベント送信",
        "write_calendar": "予定追加",
        "write_items": "アイテム使用・移動",
        "write_notes": "ノート更新",
        "write_autonomy": "自律行動設定変更",
        "use_voice": "音声入力・音声合成",
        "manage_push": "Push通知端末の管理",
        "post_twitter": "Twitter投稿・下書き却下",
    }
    return labels.get(str(scope or "").strip(), str(scope or "").strip())


def _atelier_write_scope_warning(scope: str) -> str:
    scope = str(scope or "").strip()
    if scope == "send_chat":
        return "⚠️ `send_chat` はペルソナの応答生成や通知を誘発できます。信頼できるアプリだけに許可してください。"
    if scope == "write_event":
        return "⚠️ `write_event` は外部イベントをログへ注入し、重要度によって通知を誘発できます。"
    if scope == "write_location":
        return "⚠️ `write_location` はこのルームの現在地を変更できます。"
    if scope == "write_calendar":
        return "⚠️ `write_calendar` はルームに設定されたペルソナ専用カレンダーへ予定を追加できます。"
    if scope == "write_items":
        return "⚠️ `write_items` は所持品の消費・贈呈・配置・拾得を行えます。信頼できるアプリだけに許可してください。"
    if scope == "write_notes":
        return "⚠️ `write_notes` は研究ノート・創作ノートの内容を書き換えられます。"
    if scope == "write_autonomy":
        return "⚠️ `write_autonomy` はペルソナの自律行動設定を変更できます。"
    if scope == "use_voice":
        return "⚠️ `use_voice` は音声文字起こし・音声合成を実行し、設定によってAPI利用料が発生します。"
    if scope == "manage_push":
        return "⚠️ `manage_push` は通知端末の登録・解除とテスト通知を実行できます。通知先を確認してください。"
    return ""


def _atelier_outward_scope_warning(scope: str, room_name: str = "") -> str:
    scope = str(scope or "").strip()
    if scope != "post_twitter":
        return ""
    warning = (
        "⚠️⚠️ `post_twitter` は、このアプリがペルソナの Twitter 下書きを公開または却下できる最高リスク権限です。"
        "approve は本文差し替え付きで投稿できるため、下書きが1件でもあると任意本文の外部発信につながります。"
        "信頼できるアプリだけに許可してください。"
    )
    if room_name and not _room_atelier_https_only(room_name):
        warning += (
            "\n\n⚠️ このルームは「アプリ配信をHTTPS(Tailscale)経由のみに制限」がOFFです。"
            "外部発信権限はこのアプリだけに限定し、HTTPS経由で使う設定を推奨します。"
        )
    return warning


def _atelier_workspace_root(room_name: str) -> Path | None:
    room = str(room_name or "").strip()
    if not room:
        return None
    try:
        root, _exclude_dirs, _exclude_files = agent_delegation_manager._persona_workspace(room)
        return Path(root).resolve()
    except Exception:
        traceback.print_exc()
        return None


def _atelier_empty_placeholder_root() -> Path:
    root = Path(os.getenv("TMPDIR", "/tmp")) / "nexus_ark_empty_atelier_placeholder"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def build_atelier_file_intro(room_name: str) -> str:
    room = str(room_name or "").strip()
    if not room:
        return "📁 ルームを選択してください。"
    return (
        f"📁 これは **{room}専用のアトリエ** の中身です。"
        "NexusArk本体とは別の、隔離された作業フォルダです。"
        f"{room} がチャットで頼まれた時や自律行動の中で、ここに作品やアプリを作ります。"
    )


def atelier_file_room_change_hint(room_name: str):
    return gr.update(value=build_atelier_file_intro(room_name))


def _atelier_serve_settings() -> dict:
    defaults = {
        "enabled": False,
        "host": "0.0.0.0",
        "port": 8765,
        "tailscale_https_port": 8443,
        "auto_start_tailscale_serve": False,
        "api_integration_enabled": False,
        "api_origin": "",
    }
    settings = dict(defaults)
    settings.update(config_manager.CONFIG_GLOBAL.get("atelier_serve_settings", {}) or {})
    return settings


def _room_atelier_app_api_settings(room_name: str) -> dict:
    if not room_name:
        return {}
    try:
        room_config = room_manager.get_room_config(str(room_name).strip()) or {}
        overrides = room_config.get("override_settings", {}) if isinstance(room_config, dict) else {}
        settings = overrides.get("atelier_app_api", {})
        return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}


def _room_atelier_https_only(room_name: str) -> bool:
    return bool(_room_atelier_app_api_settings(room_name).get("https_only", False))


def atelier_app_api_room_updates(room_name: str):
    settings = _room_atelier_app_api_settings(str(room_name or "").strip())
    return gr.update(value=bool(settings.get("https_only", False)))


def _atelier_app_pending_dataframe(room_name: str) -> pd.DataFrame:
    try:
        return pd.DataFrame(atelier_app_grants.pending_requests(str(room_name or "").strip()), columns=ATELIER_APP_PENDING_COLUMNS)
    except Exception:
        traceback.print_exc()
        return pd.DataFrame(columns=ATELIER_APP_PENDING_COLUMNS)


def _atelier_app_active_grants_dataframe(room_name: str) -> pd.DataFrame:
    try:
        return pd.DataFrame(atelier_app_grants.active_grants(str(room_name or "").strip()), columns=ATELIER_APP_ACTIVE_GRANT_COLUMNS)
    except Exception:
        traceback.print_exc()
        return pd.DataFrame(columns=ATELIER_APP_ACTIVE_GRANT_COLUMNS)


def refresh_atelier_app_grants(room_name: str):
    return (
        _atelier_app_pending_dataframe(room_name),
        _atelier_app_active_grants_dataframe(room_name),
        {},
        {},
        "アトリエアプリ権限: 最新の台帳を読み込みました。",
        "",
    )


def handle_atelier_app_pending_select(room_name: str, df: pd.DataFrame, evt: gr.SelectData):
    try:
        if df is None or evt is None:
            return {}, "保留リクエストを選択してください。", ""
        row_index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        row = df.iloc[int(row_index)]
        selection = {"app_id": str(row.get("app_id") or ""), "scope": str(row.get("scope") or "")}
        scope = selection["scope"]
        warning_markdown = ""
        if scope in ATELIER_APP_OUTWARD_SCOPES:
            warning = _atelier_outward_scope_warning(scope, room_name)
            warning_markdown = (
                f"{warning}\n\n**outward 系を許可する場合は、専用の外部発信確認チェックを入れてから「選択リクエストを許可」を押してください。**"
            )
        else:
            warning = _atelier_write_scope_warning(scope)
            warning_markdown = (
                f"{warning}\n\n**write 系を許可する場合は、下の確認チェックを入れてから「選択リクエストを許可」を押してください。**"
                if warning
                else ""
            )
        return (
            selection,
            f"選択中: `{selection['app_id']}` / `{selection['scope']}`（{_atelier_scope_label(selection['scope'])}）",
            warning_markdown,
        )
    except Exception as e:
        traceback.print_exc()
        return {}, f"選択処理に失敗しました: {type(e).__name__}: {e}", ""


def handle_atelier_app_active_grant_select(df: pd.DataFrame, evt: gr.SelectData):
    try:
        if df is None or evt is None:
            return {}, "有効な許可を選択してください。", ""
        row_index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        row = df.iloc[int(row_index)]
        selection = {"app_id": str(row.get("app_id") or ""), "scope": str(row.get("scope") or "")}
        return selection, f"選択中: `{selection['app_id']}` / `{selection['scope']}`", ""
    except Exception as e:
        traceback.print_exc()
        return {}, f"選択処理に失敗しました: {type(e).__name__}: {e}", ""


def handle_grant_atelier_app_scope(
    room_name: str,
    selection: dict,
    write_confirmed: bool = False,
    outward_confirmed: bool = False,
):
    app_id = str((selection or {}).get("app_id") or "").strip()
    scope = str((selection or {}).get("scope") or "").strip()
    if not app_id or not scope:
        pending, active, _pending_state, _active_state, _status, _warning = refresh_atelier_app_grants(room_name)
        return pending, active, {}, "アトリエアプリ権限: 許可する保留リクエストを選択してください。", ""
    if scope in ATELIER_APP_OUTWARD_SCOPES and not bool(outward_confirmed):
        warning = _atelier_outward_scope_warning(scope, room_name)
        return (
            gr.update(),
            gr.update(),
            gr.update(),
            "アトリエアプリ権限: 外部発信権限の確認が必要です。",
            f"{warning}\n\n**外部発信確認チェックを入れてから許可してください。**",
        )
    if scope in ATELIER_APP_WRITE_SCOPES and not bool(write_confirmed):
        warning = _atelier_write_scope_warning(scope)
        warning_markdown = (
            f"{warning}\n\n**確認チェックを入れてから許可してください。**"
            if warning
            else "**確認チェックを入れてから許可してください。**"
        )
        return (
            gr.update(),
            gr.update(),
            gr.update(),
            "アトリエアプリ権限: write権限の確認が必要です。",
            warning_markdown,
        )
    try:
        atelier_app_grants.grant_scope(str(room_name or "").strip(), app_id, scope)
        warning_markdown = _atelier_outward_scope_warning(scope, room_name) if scope in ATELIER_APP_OUTWARD_SCOPES else ""
        return (
            _atelier_app_pending_dataframe(room_name),
            _atelier_app_active_grants_dataframe(room_name),
            {},
            f"アトリエアプリ権限: `{app_id}` に `{scope}` を許可しました。",
            warning_markdown if warning_markdown and not _room_atelier_https_only(room_name) else "",
        )
    except Exception as e:
        traceback.print_exc()
        return gr.update(), gr.update(), gr.update(), f"アトリエアプリ権限: 許可に失敗しました ({e})", gr.update()


def handle_deny_atelier_app_scope(room_name: str, selection: dict):
    app_id = str((selection or {}).get("app_id") or "").strip()
    scope = str((selection or {}).get("scope") or "").strip()
    if not app_id or not scope:
        return gr.update(), gr.update(), {}, "アトリエアプリ権限: 拒否する保留リクエストを選択してください。", ""
    try:
        atelier_app_grants.deny_scope(str(room_name or "").strip(), app_id, scope)
        return (
            _atelier_app_pending_dataframe(room_name),
            _atelier_app_active_grants_dataframe(room_name),
            {},
            f"アトリエアプリ権限: `{app_id}` の `{scope}` を拒否しました。",
            "",
        )
    except Exception as e:
        traceback.print_exc()
        return gr.update(), gr.update(), gr.update(), f"アトリエアプリ権限: 拒否に失敗しました ({e})", gr.update()


def handle_revoke_atelier_app_scope(room_name: str, selection: dict):
    app_id = str((selection or {}).get("app_id") or "").strip()
    scope = str((selection or {}).get("scope") or "").strip()
    if not app_id or not scope:
        return gr.update(), gr.update(), {}, "アトリエアプリ権限: 失効する許可を選択してください。", ""
    try:
        atelier_app_grants.revoke_scope(str(room_name or "").strip(), app_id, scope)
        return (
            _atelier_app_pending_dataframe(room_name),
            _atelier_app_active_grants_dataframe(room_name),
            {},
            f"アトリエアプリ権限: `{app_id}` の `{scope}` を失効しました。",
            "",
        )
    except Exception as e:
        traceback.print_exc()
        return gr.update(), gr.update(), gr.update(), f"アトリエアプリ権限: 失効に失敗しました ({e})", gr.update()


def _atelier_app_url(room_name: str, app_name: str, host: str = "127.0.0.1") -> str:
    settings = _atelier_serve_settings()
    port = int(settings.get("port", 8765) or 8765)
    return f"http://{host}:{port}/atelier/{quote(str(room_name or ''), safe='')}/{quote(str(app_name or ''), safe='')}/"


def _atelier_https_app_url(room_name: str, app_name: str) -> str:
    settings = _atelier_serve_settings()
    https_port = int(settings.get("tailscale_https_port", 8443) or 8443)
    dns_name = _get_tailscale_dns_name()
    host = dns_name if dns_name else "<PCのTailscale DNS名>.ts.net"
    return f"https://{host}:{https_port}/atelier/{quote(str(room_name or ''), safe='')}/{quote(str(app_name or ''), safe='')}/"


def _atelier_apps_dataframe(room_name: str) -> pd.DataFrame:
    try:
        from atelier_serve.server import list_atelier_apps

        rows = []
        for item in list_atelier_apps(str(room_name or "").strip()):
            name = item.get("name", "")
            rows.append(
                {
                    "app": name,
                    "PC URL": _atelier_app_url(room_name, name, "127.0.0.1"),
                    "同一Wi-Fi URL": _atelier_app_url(room_name, name, "<PCのIPアドレス>"),
                    "index.html": item.get("path", ""),
                }
            )
        return pd.DataFrame(rows, columns=ATELIER_APP_COLUMNS)
    except Exception:
        traceback.print_exc()
        return pd.DataFrame(columns=ATELIER_APP_COLUMNS)


def _atelier_app_choices(df: pd.DataFrame) -> list[tuple[str, str]]:
    if df is None or df.empty:
        return []
    return [(str(row["app"]), str(row["app"])) for _, row in df.iterrows() if str(row.get("app") or "").strip()]


def _build_atelier_app_detail(room_name: str, app_name: str | None) -> str:
    room = str(room_name or "").strip()
    name = str(app_name or "").strip()
    settings = _atelier_serve_settings()
    port = int(settings.get("port", 8765) or 8765)
    https_port = int(settings.get("tailscale_https_port", 8443) or 8443)
    if not room:
        return "ルームを選択してください。"
    if not name:
        return "アプリはまだ見つかりません。`workspace/apps/<name>/index.html` を作るとここに表示されます。"

    local_url = _atelier_app_url(room, name, "127.0.0.1")
    lan_url = _atelier_app_url(room, name, "<PCのIPアドレス>")
    https_url = _atelier_https_app_url(room, name)
    serve_command = f"tailscale serve --bg --https={https_port} http://127.0.0.1:{port}"
    enabled = bool(settings.get("enabled"))
    enabled_text = "有効" if enabled else "無効"
    api_enabled_text = "有効" if settings.get("api_integration_enabled", False) else "無効"
    return (
        f"#### {name}\n"
        f"- アトリエ配信: **{enabled_text}** / Port `{port}`\n"
        f"- アトリエAPI連携: **{api_enabled_text}**（有効時は `_nexus/config` から自ルーム用の限定トークンを取得できます）\n"
        f"- PCで開く: `{local_url}`\n"
        f"- 同一Wi-Fiスマホ: `{lan_url}`\n"
        f"- PWA用HTTPS候補: `{https_url}`\n\n"
        "manifest / Service Worker はアトリエ配信サーバが自動合成します。"
        "`localhost`（PC）または Tailscale HTTPS で開くと、対応ブラウザでホーム画面に追加できます。"
        "同一Wi-Fiの plain HTTP ではブラウザ制約によりPWA登録は行わず、通常のWebアプリとして表示します。\n\n"
        "Tailscale HTTPSを手動設定する場合:\n\n"
        f"```bash\n{serve_command}\n```\n"
    )


def refresh_atelier_file_and_app_view(room_name: str):
    """アトリエworkspace取り出し導線とWebアプリ一覧を更新する。"""
    room = str(room_name or "").strip()
    root = _atelier_workspace_root(room)
    empty_df = pd.DataFrame(columns=ATELIER_APP_COLUMNS)
    if not root:
        return (
            gr.update(root_dir=str(_atelier_empty_placeholder_root()), value=[]),
            gr.update(value=None, interactive=False),
            empty_df,
            gr.update(choices=[], value=None),
            _build_atelier_app_detail(room, None),
            "",
            build_atelier_app_open_guide("", ""),
            "ルームを選択してください。",
        )
    df = _atelier_apps_dataframe(room)
    choices = _atelier_app_choices(df)
    selected = choices[0][1] if choices else None
    detail = _build_atelier_app_detail(room, selected)
    qr = _atelier_app_qr_html(room, selected)
    guide = build_atelier_app_open_guide(room, selected)
    status = f"成果物: workspace `{root}` / アプリ {len(df)}件"
    return (
        gr.update(root_dir=str(root), value=[]),
        gr.update(value=None, interactive=False),
        df,
        gr.update(choices=choices, value=selected),
        detail,
        qr,
        guide,
        status,
    )


def handle_atelier_file_select(room_name: str, selected):
    """FileExplorerで選択したworkspace内ファイルをDownloadButtonへ渡す。"""
    root = _atelier_workspace_root(room_name)
    if not root:
        return gr.update(value=None, interactive=False), "成果物: ルーム未選択"
    values = selected if isinstance(selected, list) else [selected]
    for value in values:
        if not value:
            continue
        try:
            candidate = Path(str(value)).expanduser().resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                return gr.update(value=str(candidate), interactive=True), f"成果物: `{candidate.name}` を取り出せます。"
        except Exception:
            continue
    return gr.update(value=None, interactive=False), "成果物: ファイルを選択してください。"


def handle_atelier_app_row_select(room_name: str, df: pd.DataFrame, evt: gr.SelectData):
    try:
        if df is None or evt is None:
            return gr.update(), gr.update(), gr.update(), gr.update()
        row_index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        if row_index is None:
            return gr.update(), gr.update(), gr.update(), gr.update()
        app_name = str(df.iloc[int(row_index)]["app"] or "").strip()
        if not app_name:
            return gr.update(), "アプリ名を取得できませんでした。", gr.update(), gr.update()
        return (
            gr.update(value=app_name),
            _build_atelier_app_detail(room_name, app_name),
            _atelier_app_qr_html(room_name, app_name),
            build_atelier_app_open_guide(room_name, app_name),
        )
    except Exception as e:
        traceback.print_exc()
        return gr.update(), f"アプリ選択の処理に失敗しました: {type(e).__name__}: {e}", gr.update(), gr.update()


def _atelier_app_qr_html(room_name: str, app_name: str) -> str:
    """スマホ用HTTPS(Tailscale) URLのQRを <img> data URI のHTMLで返す。

    gr.Image のラベル札/全画面ツールバー（左上にラベルが被って読めない・全画面の閉じる不具合）を
    避けるため、ツールバーの無い素の <img> をHTMLで表示する。
    Tailscale DNS名が取れない／segno未導入／対象アプリ無しの場合は空文字（QRなしで継続）。
    """
    room = str(room_name or "").strip()
    name = str(app_name or "").strip()
    if not room or not name:
        return ""
    # 実DNS名が無いとスマホから到達できるHTTPS URLにならないためQRは出さない
    if not _get_tailscale_dns_name():
        return ""
    url = _atelier_https_app_url(room, name)
    try:
        import base64
        from io import BytesIO

        import segno

        buf = BytesIO()
        # border=4 でクワイエットゾーン（余白）を確保し読み取りやすくする
        segno.make(url, error="m").save(buf, kind="png", scale=6, border=4)
        data = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        # segno未導入や生成失敗時はQRなしで継続（URLは detail テキストに表示される）
        return ""
    return (
        "<div style=\"text-align:center;margin:4px 0\">"
        "<div style=\"font-size:0.85em;color:#9aa3ab;margin-bottom:6px\">スマホ用QR（Tailscale HTTPS）</div>"
        f"<img alt=\"スマホ用QRコード\" src=\"data:image/png;base64,{data}\" "
        "style=\"width:240px;height:240px;max-width:100%;background:#fff;padding:8px;"
        "border-radius:8px;display:inline-block\" />"
        "</div>"
    )


def build_atelier_app_open_guide(room_name: str, app_name: str = "") -> str:
    """アプリをスマホ・PCで開くための3ステップ手順＋現在状態のMarkdownをHTMLで返す。"""
    room = str(room_name or "").strip()
    name = str(app_name or "").strip()
    settings = _atelier_serve_settings()
    enabled = bool(settings.get("enabled"))
    api_enabled = bool(settings.get("api_integration_enabled", False))
    https_only = _room_atelier_https_only(room) if room else False
    dns_name = _get_tailscale_dns_name()

    if not name:
        state = "🟢 アプリ配信は有効です。" if enabled else "🔴 アプリ配信は無効です。下の「アプリ配信を有効にする」を押してください。"
        return (
            f"**{state}**\n\n"
            "上の「このアトリエの中身を読み込む」を押すと、アプリ一覧とURL・QRコードが表示されます。\n"
            "（アプリは `apps/<名前>/index.html` を作ると現れます。「ファイルを取り出す」とは別物で、"
            "アプリは“取り出す”のではなく URL や QR で開いて使います。）"
        )

    state_line = (
        f"> 配信: **{'ON' if enabled else 'OFF'}** ・ API連携: **{'ON' if api_enabled else 'OFF'}** ・ "
        f"HTTPS(Tailscale)限定: **{'ON' if https_only else 'OFF'}**"
    )
    notes = []
    if not enabled:
        notes.append("⚠️ 配信がOFFです。下の「⚙️ 配信と接続情報」で「アトリエ配信を有効化」してください。")
    if not api_enabled:
        notes.append("⚠️ API連携がOFFです。アプリがデータを読み書きするには「アトリエアプリにAPIを渡す」をONに。")
    if not dns_name:
        notes.append(
            "ℹ️ Tailscale HTTPS が未設定のため、スマホ用QR/URLが出ません。"
            "下の「⚙️ 配信と接続情報」→「Tailscale HTTPS設定を実行」を押してください。"
        )
    notes_block = ("\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""

    return (
        f"##### 「{name}」をスマホで使う（3ステップ）\n"
        "1. スマホに **Tailscale** アプリを入れ、PCと同じアカウントでログイン（PCと同じネットワークに入る）。\n"
        "2. このすぐ下の「選択アプリ」に出る **QRコード** を読み取る（または「⚙️ 配信と接続情報」の「PWA用HTTPS」URLを開く）。"
        "開いたらブラウザのメニューから **「ホーム画面に追加」** でアプリ化。\n"
        "3. アプリを一度開くと、下の「🔑 アプリの権限」に**許可待ち**が出ます。"
        "選んで **「選択リクエストを許可」** すると、アプリがこのルームのデータを使えます。\n\n"
        f"{state_line}{notes_block}"
    )


def handle_enable_atelier_serve_for_apps(room_name: str, app_name: str = ""):
    """Webアプリ利用導線から、既存の接続設定を保ったまま配信だけ有効化する。"""
    settings = _atelier_serve_settings()
    status, connection = handle_save_atelier_serve_settings(
        True,
        settings.get("host", "0.0.0.0"),
        settings.get("port", 8765),
        settings.get("tailscale_https_port", 8443),
        settings.get("auto_start_tailscale_serve", False),
        settings.get("api_integration_enabled", False),
        room_name,
        app_name,
    )
    from .delegation import build_atelier_delegation_readiness, _atelier_delegation_readiness_state
    readiness = _atelier_delegation_readiness_state(room_name)
    return (
        gr.update(value=True),
        status,
        connection,
        gr.update(value=build_atelier_app_open_guide(room_name, app_name)),
        gr.update(value=build_atelier_delegation_readiness(room_name)),
        gr.update(visible=not bool(readiness.get("ready"))),
    )


def handle_atelier_app_dropdown_change(room_name: str, app_name: str):
    return (
        _build_atelier_app_detail(room_name, app_name),
        _atelier_app_qr_html(room_name, app_name),
        build_atelier_app_open_guide(room_name, app_name),
    )


def apply_atelier_app_icon(room_name, app_name, normal_path, maskable_path="") -> str:
    """生成/アップロード画像をアトリエアプリ（apps/<name>）のアイコンに設定する共通処理。

    512x512の正方形に整え、枠いっぱい用(maskable)は未指定なら通常画像から自動生成する。
    UIハンドラ・ペルソナ用ツール（set_atelier_app_icon）の双方から再利用する。成功/失敗のメッセージ文字列を返す。
    """
    if not normal_path and not maskable_path:
        return "画像を指定してください。"
    try:
        from atelier_serve.server import _app_root_for_existing_app

        _room, _app, _workspace, app_root, _exclude_dirs, _exclude_files = _app_root_for_existing_app(room_name, app_name)
    except Exception:
        traceback.print_exc()
        return "対象アプリが見つかりません（apps/<名前>/index.html を先に作成してください）。"

    app_root = Path(app_root)
    if normal_path:
        with Image.open(normal_path) as raw_img:
            _square_512(raw_img).save(app_root / "icon.png", format="PNG")
        if not maskable_path:
            with Image.open(normal_path) as raw_img:
                _auto_maskable_512(raw_img).save(app_root / "icon-maskable.png", format="PNG")

    if maskable_path:
        with Image.open(maskable_path) as raw_img:
            _square_512(raw_img).save(app_root / "icon-maskable.png", format="PNG")

    return "✅ アイコンを設定しました（保存先: 通常=icon.png / 枠いっぱい=icon-maskable.png）。"


def handle_set_atelier_app_icon(room_name, app_name, normal_path, maskable_path):
    try:
        msg = apply_atelier_app_icon(room_name, app_name, normal_path, maskable_path)
        if msg.startswith("✅"):
            return (
                msg + "リロードで反映されます（キャッシュは自動更新。スマホは念のためホームから削除→再追加で確実）。",
                gr.update(value=None),
                gr.update(value=None),
            )
        return msg, gr.update(), gr.update()
    except Exception as e:
        traceback.print_exc()
        return f"アイコン設定に失敗しました: {type(e).__name__}: {e}", gr.update(), gr.update()


def _atelier_work_title(work: dict) -> str:
    timestamp = str(work.get("anthology_timestamp") or work.get("created_at") or "")
    state = str(work.get("state") or "locked")
    if state == "locked":
        return f"🔒 鍵付きの作品 ███ {timestamp[:19]}"
    if state == "archived":
        return f"屋根裏の作品 {timestamp[:19]}"
    return f"開示済みの作品 {timestamp[:19]}"


def _atelier_works_dataframe(works: list[dict], view_state: str = "active") -> pd.DataFrame:
    rows = []
    for work in works:
        state = str(work.get("state") or "locked")
        if view_state == "active" and state == "archived":
            continue
        if view_state == "archived" and state != "archived":
            continue
        rows.append(
            {
                "work_id": str(work.get("id") or ""),
                "state": state,
                "kind": str(work.get("kind") or ""),
                "created_at": str(work.get("created_at") or ""),
                "last_referenced_at": str(work.get("last_referenced_at") or ""),
                "title": _atelier_work_title(work),
            }
        )
    return pd.DataFrame(rows, columns=ATELIER_WORK_COLUMNS)


def _atelier_work_choices(works: list[dict], view_state: str = "active") -> list[tuple[str, str]]:
    choices = []
    df = _atelier_works_dataframe(works, view_state=view_state)
    for row in df.to_dict(orient="records"):
        work_id = str(row.get("work_id") or "")
        if not work_id:
            continue
        label = f"{row.get('state')} | {row.get('title')} | {row.get('created_at')}"
        choices.append((label, work_id))
    return choices


def refresh_atelier_view(room_name: str, view_state: str = "active"):
    """アトリエ作品一覧と選択作品表示を再読み込みする。"""
    try:
        import curation_manager

        room = str(room_name or "").strip()
        if not room:
            empty = pd.DataFrame(columns=ATELIER_WORK_COLUMNS)
            return empty, gr.update(choices=[], value=None), "ルームを選択してください。", "アトリエ: ルーム未選択"
        state = "archived" if str(view_state or "active") == "archived" else "active"
        works = curation_manager.list_atelier_works(room)
        df = _atelier_works_dataframe(works, view_state=state)
        choices = _atelier_work_choices(works, view_state=state)
        selected = choices[0][1] if choices else None
        detail = curation_manager.format_atelier_work_for_display(room, selected) if selected else "アトリエ作品はまだありません。"
        status = f"アトリエ: {len(df)}件（表示: {'屋根裏部屋' if state == 'archived' else '現行'}）"
        return df, gr.update(choices=choices, value=selected), detail, status
    except Exception as e:
        traceback.print_exc()
        empty = pd.DataFrame(columns=ATELIER_WORK_COLUMNS)
        return empty, gr.update(choices=[], value=None), f"アトリエ一覧の読み込みに失敗しました: {type(e).__name__}: {e}", "アトリエ: 読み込み失敗"


def load_atelier_work_detail(room_name: str, work_id: str) -> str:
    try:
        import curation_manager

        return curation_manager.format_atelier_work_for_display(str(room_name or "").strip(), str(work_id or "").strip())
    except Exception as e:
        traceback.print_exc()
        return f"アトリエ作品の読み込みに失敗しました: {type(e).__name__}: {e}"


def handle_atelier_work_row_select(room_name: str, df: pd.DataFrame, evt: gr.SelectData):
    try:
        if df is None or evt is None:
            return gr.update(), gr.update()
        row_index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        if row_index is None:
            return gr.update(), gr.update()
        work_id = str(df.iloc[int(row_index)]["work_id"] or "").strip()
        if not work_id:
            return gr.update(), "作品IDを取得できませんでした。"
        return gr.update(value=work_id), load_atelier_work_detail(room_name, work_id)
    except Exception as e:
        traceback.print_exc()
        return gr.update(), f"アトリエ作品選択の処理に失敗しました: {type(e).__name__}: {e}"


def handle_delete_archived_atelier_work(room_name: str, work_id: str, view_state: str = "archived"):
    try:
        import curation_manager

        curation_manager.delete_archived_atelier_work(str(room_name or "").strip(), str(work_id or "").strip())
        return refresh_atelier_view(room_name, view_state or "archived")
    except Exception as e:
        traceback.print_exc()
        works_df, dropdown, detail, status = refresh_atelier_view(room_name, view_state or "archived")
        return works_df, dropdown, f"屋根裏部屋の削除に失敗しました: {type(e).__name__}: {e}\n\n{detail}", status


def build_atelier_serve_connection_help(room_name: str = "", app_name: str = "") -> str:
    """アトリエ静的配信の接続先を表示するMarkdownを生成する。"""
    settings = _atelier_serve_settings()
    port = int(settings.get("port", 8765) or 8765)
    https_port = int(settings.get("tailscale_https_port", 8443) or 8443)
    enabled = bool(settings.get("enabled"))
    api_enabled = bool(settings.get("api_integration_enabled", False))
    dns_name = _get_tailscale_dns_name()
    tailscale_ip = _get_tailscale_ipv4()
    serve_status = _get_tailscale_serve_status()
    serve_status_json = _get_tailscale_serve_status_json()
    serve_command = f"tailscale serve --bg --https={https_port} http://127.0.0.1:{port}"
    serve_configured = _tailscale_serve_points_to_port(serve_status, serve_status_json, port)
    serve_diagnostic = _summarize_tailscale_serve_json(serve_status_json, port)

    room = str(room_name or "").strip() or "<room>"
    app = str(app_name or "").strip() or "<app>"
    local_url = _atelier_app_url(room, app, "127.0.0.1")
    lan_url = _atelier_app_url(room, app, "<PCのIPアドレス>")
    tailnet_http_url = f"http://{tailscale_ip}:{port}/atelier/{quote(room, safe='')}/{quote(app, safe='')}/" if tailscale_ip else f"http://<Tailscale IP>:{port}/atelier/{quote(room, safe='')}/{quote(app, safe='')}/"
    https_host = dns_name if dns_name else "<PCのTailscale DNS名>.ts.net"
    https_url = f"https://{https_host}:{https_port}/atelier/{quote(room, safe='')}/{quote(app, safe='')}/"

    if not shutil.which("tailscale"):
        serve_line = "- Tailscale Serve: 未確認（Tailscale CLIが見つかりません）"
    elif serve_configured:
        serve_line = f"- Tailscale Serve: **設定済み**（アトリエ port `{port}` へ転送）"
    else:
        serve_line = "- Tailscale Serve: 未設定または別ポート向けに設定済み"
    serve_diagnostic_block = f"{serve_diagnostic}\n" if serve_diagnostic else ""
    return (
        "##### 接続情報（今の設定での開き方）\n"
        f"- アトリエ配信: **{'有効' if enabled else '無効'}** / Port `{port}` / HTTPS Port `{https_port}`\n"
        f"- アトリエAPI連携: **{'有効' if api_enabled else '無効'}**（有効時のみ `_nexus/config` と API向け `connect-src` を有効化）\n"
        f"- PC内確認: `{local_url}`\n"
        f"- 同一Wi-Fi: `{lan_url}`\n"
        f"- Tailscale HTTP: `{tailnet_http_url}`\n"
        f"- PWA用HTTPS候補: `{https_url}`\n"
        f"{serve_line}\n"
        f"{serve_diagnostic_block}\n"
        "Lite PWA/API Gatewayとは別ポートで配信します。Tailscaleでも `:8443` などの別ポートを使い、LiteのToken保存オリジンと分離してください。\n\n"
        "アトリエ配信は静的・無認証です。Hostが `0.0.0.0` の場合、同一LAN/Tailnet内でURLを知る相手は閲覧できます。信頼できる私的ネットワーク内で使ってください。\n\n"
        "手動設定コマンド:\n\n"
        f"```bash\n{serve_command}\n```\n"
    )


def handle_configure_tailscale_atelier_https(room_name: str = "", app_name: str = ""):
    """アトリエ静的配信用のTailscale Serve設定を実行する。"""
    settings = _atelier_serve_settings()
    port = int(settings.get("port", 8765) or 8765)
    https_port = int(settings.get("tailscale_https_port", 8443) or 8443)
    if not shutil.which("tailscale"):
        return (
            gr.update(value=build_atelier_serve_connection_help(room_name, app_name)),
            gr.update(value=f"Tailscale CLIが見つかりません。PC側で `tailscale serve --https={https_port} http://127.0.0.1:{port}` を実行してください。"),
        )

    serve_status = _get_tailscale_serve_status()
    serve_status_json = _get_tailscale_serve_status_json()
    if _tailscale_serve_points_to_port(serve_status, serve_status_json, port):
        return (
            gr.update(value=build_atelier_serve_connection_help(room_name, app_name)),
            gr.update(value="アトリエ用Tailscale HTTPSは既に設定済みです。"),
        )

    command = ["tailscale", "serve", "--bg", f"--https={https_port}", f"http://127.0.0.1:{port}"]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
        output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part and part.strip())
        if result.returncode == 0:
            return (
                gr.update(value=build_atelier_serve_connection_help(room_name, app_name)),
                gr.update(value="アトリエ用Tailscale HTTPS設定を実行しました。"),
            )
        message = output or "Tailscale Serve設定に失敗しました。"
        return (
            gr.update(value=build_atelier_serve_connection_help(room_name, app_name)),
            gr.update(value=f"Tailscale Serve設定に失敗しました。\n\n```text\n{message}\n```"),
        )
    except subprocess.TimeoutExpired:
        return (
            gr.update(value=build_atelier_serve_connection_help(room_name, app_name)),
            gr.update(value="Tailscale Serve設定がタイムアウトしました。Tailscaleの認証/HTTPS有効化画面が待機している可能性があります。"),
        )
    except Exception as e:
        return (
            gr.update(value=build_atelier_serve_connection_help(room_name, app_name)),
            gr.update(value=f"Tailscale Serve設定でエラーが発生しました: {e}"),
        )


def handle_save_atelier_serve_settings(enabled: bool, host: str, port: int, tailscale_https_port: int, auto_start_tailscale_serve: bool, api_integration_enabled: bool, room_name: str, app_name: str = ""):
    """アトリエ静的配信サーバ設定を保存し、必要に応じて再起動する。"""
    try:
        host = (host or "").strip() or "0.0.0.0"
        if _room_atelier_https_only(room_name):
            host = "127.0.0.1"
        port = int(port or 8765)
        https_port = int(tailscale_https_port or 8443)
        if not (1 <= port <= 65535):
            return gr.update(value="成果物: ❌ 配信ポート番号は1〜65535で指定してください。"), gr.update()
        if not (1 <= https_port <= 65535):
            return gr.update(value="成果物: ❌ HTTPSポート番号は1〜65535で指定してください。"), gr.update()

        config_manager.save_atelier_serve_settings(
            enabled=enabled,
            host=host,
            port=port,
            tailscale_https_port=https_port,
            auto_start_tailscale_serve=auto_start_tailscale_serve,
            api_integration_enabled=api_integration_enabled,
        )

        from atelier_serve.server import start_server as start_atelier_server
        from atelier_serve.server import stop_server as stop_atelier_server

        try:
            stop_atelier_server()
        except Exception as stop_err:
            logger.warning(f"Failed to stop Atelier Serve server: {stop_err}")

        time.sleep(0.5)

        if enabled:
            try:
                start_atelier_server(port=port, host=host, daemon=True)
                if auto_start_tailscale_serve and shutil.which("tailscale"):
                    threading.Thread(
                        target=handle_configure_tailscale_atelier_https,
                        args=(room_name, app_name),
                        daemon=True,
                    ).start()
                return (
                    gr.update(value="成果物: 🟢 設定を保存し、アトリエ配信サーバを起動/再起動しました。"),
                    gr.update(value=build_atelier_serve_connection_help(room_name, app_name)),
                )
            except Exception as start_err:
                logger.error(f"Failed to start Atelier Serve server: {start_err}")
                return (
                    gr.update(value=f"成果物: ⚠️ 設定を保存しましたが、配信サーバ起動に失敗しました ({start_err})。"),
                    gr.update(value=build_atelier_serve_connection_help(room_name, app_name)),
                )
        return (
            gr.update(value="成果物: ⚪ 無効として保存し、アトリエ配信サーバを停止しました。"),
            gr.update(value=build_atelier_serve_connection_help(room_name, app_name)),
        )
    except Exception as e:
        logger.error(f"Failed to save Atelier Serve settings: {e}")
        return gr.update(value=f"成果物: ❌ エラーが発生しました ({e})"), gr.update()


def _square_512(img: Image.Image) -> Image.Image:
    rgba = img.convert("RGBA")
    width, height = rgba.size
    side = max(width, height, 1)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.alpha_composite(rgba, ((side - width) // 2, (side - height) // 2))
    return canvas.resize((512, 512), Image.Resampling.LANCZOS)


def _corner_median_rgb(img: Image.Image) -> tuple[int, int, int]:
    rgb = img.convert("RGB")
    width, height = rgb.size
    corners = [
        rgb.getpixel((0, 0)),
        rgb.getpixel((max(width - 1, 0), 0)),
        rgb.getpixel((0, max(height - 1, 0))),
        rgb.getpixel((max(width - 1, 0), max(height - 1, 0))),
    ]
    return tuple(sorted(pixel[index] for pixel in corners)[len(corners) // 2] for index in range(3))


def _auto_maskable_512(img: Image.Image) -> Image.Image:
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    background = (244, 247, 245) if alpha.getextrema()[0] < 255 else _corner_median_rgb(rgba)
    canvas = Image.new("RGB", (512, 512), background)
    foreground = _square_512(rgba).resize((410, 410), Image.Resampling.LANCZOS)
    canvas.paste(foreground, ((512 - 410) // 2, (512 - 410) // 2), foreground)
    return canvas
