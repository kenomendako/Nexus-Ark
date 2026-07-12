"""ui_handlers のうち Tailscale 連携ヘルパー群。

atelier（アトリエ配信）と api_gateway の両ドメインから共有されるため、
循環インポートを避ける独立サブモジュールに切り出している。
"""

import json
import shutil
import subprocess


def _run_tailscale_command(args: list[str], timeout: int = 3) -> str:
    """Tailscale CLIの短い結果を取得する。失敗時は空文字を返す。"""
    if not shutil.which("tailscale"):
        return ""
    try:
        result = subprocess.run(
            ["tailscale", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()
    except Exception:
        return ""


def _get_tailscale_dns_name() -> str:
    status_json = _run_tailscale_command(["status", "--json"], timeout=3)
    if not status_json:
        return ""
    try:
        status = json.loads(status_json)
        dns_name = ((status.get("Self") or {}).get("DNSName") or "").strip().rstrip(".")
        return dns_name
    except Exception:
        return ""


def _get_tailscale_ipv4() -> str:
    output = _run_tailscale_command(["ip", "-4"], timeout=2)
    return output.splitlines()[0].strip() if output else ""


def _get_tailscale_serve_status() -> str:
    return _run_tailscale_command(["serve", "status"], timeout=5)


def _get_tailscale_serve_status_json() -> dict:
    """Tailscale ServeのJSON状態を取得する。未対応/失敗時は空dictを返す。"""
    output = _run_tailscale_command(["serve", "status", "--json"], timeout=5)
    if not output:
        return {}
    try:
        parsed = json.loads(output)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _tailscale_serve_target_patterns(port: int) -> list[str]:
    return [
        f"http://127.0.0.1:{port}",
        f"https://127.0.0.1:{port}",
        f"127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"https://localhost:{port}",
        f"localhost:{port}",
    ]


def _tailscale_serve_points_to_port(serve_status: str, serve_status_json: dict, port: int) -> bool:
    """Serve状態が現在のAPI Gatewayポートを指しているか、出力形式差を吸収して判定する。"""
    patterns = _tailscale_serve_target_patterns(port)
    haystacks = []
    if serve_status:
        haystacks.append(serve_status)
    if serve_status_json:
        haystacks.append(json.dumps(serve_status_json, ensure_ascii=False))
    return any(pattern in haystack for haystack in haystacks for pattern in patterns)


def _summarize_tailscale_serve_json(serve_status_json: dict, port: int) -> str:
    """Serve JSONの要点を、人間が確認しやすい短い診断行へ圧縮する。"""
    if not serve_status_json:
        return ""

    target_patterns = _tailscale_serve_target_patterns(port)
    routes: list[str] = []

    def walk(value, path: str = ""):
        if len(routes) >= 6:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                walk(child, child_path)
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                child_path = f"{path}[{idx}]"
                walk(child, child_path)
        elif isinstance(value, str):
            if any(pattern in value for pattern in target_patterns):
                routes.append(f"`{path}` -> `{value}`")

    walk(serve_status_json)
    if not routes:
        return "- Serve JSON診断: 現在のAPI port向け転送はJSON上では見つかりませんでした。"
    return "- Serve JSON診断: " + " / ".join(routes)
