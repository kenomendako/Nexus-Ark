# google_auth_helper.py
"""
Googleカレンダー連携のためのOAuth 2.0認証ヘルパー。
（計画書フェーズ1：設定管理とOAuthフローのインフラ構築 に対応）

提供する機能:
- ローカルリダイレクトサーバーによる自動コード受け取りフロー（推奨方式）
- 手動コード入力フロー（認証URL生成 → コード引き換え）（フォールバック方式）
- リフレッシュトークンからの認証情報（Credentials）復元と自動更新
- 認証済みCalendar APIサービスの構築
- トークンの失効（revoke）

依存ライブラリ（google-auth / google-auth-oauthlib / google-api-python-client）は
未インストール環境でもモジュール自体はインポートできるよう、すべて関数内で遅延インポートする。
これにより、ライブラリ未導入でもアプリ全体の起動には影響しない（グレースフルデグラデーション）。
"""

import os
from typing import Optional, List, Dict, Any, Tuple

# Googleはcalendarスコープに加えて openid 等を返すことがあり、oauthlib がデフォルトで
# 「Scope has changed」例外を投げる。手動コードフローでは無害なため緩和する。
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# --- スコープ定義（読み取りと書き込みを分離可能にする） ---
SCOPE_READONLY = "https://www.googleapis.com/auth/calendar.readonly"
SCOPE_EVENTS = "https://www.googleapis.com/auth/calendar.events"
DEFAULT_SCOPES: List[str] = [SCOPE_READONLY, SCOPE_EVENTS]

# OAuthエンドポイント（ユーザー提供のClient ID/Secretと組み合わせて使用）
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_REVOKE_URI = "https://oauth2.googleapis.com/revoke"


class GoogleAuthError(Exception):
    """Google認証に関わるエラー。"""
    pass


def _require_google_libs() -> None:
    """Google連携ライブラリの存在を確認する。無ければ分かりやすいエラーを投げる。"""
    try:
        import google_auth_oauthlib.flow  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
        import google.auth.transport.requests  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
    except ImportError as e:
        raise GoogleAuthError(
            "Google連携ライブラリが見つかりません。"
            "`google-auth` `google-auth-oauthlib` `google-api-python-client` を導入してください。"
            f"（詳細: {e}）"
        )


def build_client_config(client_id: str, client_secret: str,
                        redirect_uris: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    ユーザーが自身のGCPプロジェクトで発行したClient ID/Secretから、
    google-auth-oauthlib が要求する client_config 辞書を組み立てる。

    Nexus Ark側で共通のClient IDを埋め込まない方針のため、毎回この関数で構成する。
    """
    if not client_id or not client_secret:
        raise GoogleAuthError("Client ID / Client Secret が未設定です。")
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
            "redirect_uris": redirect_uris or ["http://localhost"],
        }
    }


def _credentials_to_dict(creds: Any) -> Dict[str, Any]:
    """Credentials オブジェクトから、保存・再構築に必要な情報のみを辞書化する。"""
    return {
        "refresh_token": getattr(creds, "refresh_token", None),
        "token": getattr(creds, "token", None),
        "scopes": list(getattr(creds, "scopes", None) or []),
        "client_id": getattr(creds, "client_id", None),
    }


def run_local_server_flow(client_id: str, client_secret: str,
                          scopes: Optional[List[str]] = None,
                          port: int = 0) -> Dict[str, Any]:
    """
    方式1（推奨）: ローカルの一時HTTPサーバーを起動し、ブラウザでの承認後に
    自動でコールバックを受け取ってトークンを取得する。

    Returns:
        {"refresh_token": str, "token": str, "scopes": [...], "client_id": str}
    Raises:
        GoogleAuthError: ライブラリ未導入・認証失敗時。
    """
    _require_google_libs()
    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = scopes or DEFAULT_SCOPES
    client_config = build_client_config(client_id, client_secret)
    try:
        flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
        # access_type=offline + prompt=consent でリフレッシュトークンを確実に得る
        creds = flow.run_local_server(
            port=port,
            access_type="offline",
            prompt="consent",
            authorization_prompt_message="",
        )
    except Exception as e:  # noqa: BLE001 - 認証フローの失敗は種類が多いため一括ラップ
        raise GoogleAuthError(f"ローカルリダイレクト認証に失敗しました: {e}")

    result = _credentials_to_dict(creds)
    if not result.get("refresh_token"):
        raise GoogleAuthError(
            "リフレッシュトークンが取得できませんでした。"
            "既に同意済みの場合は、Googleアカウントのアクセス権限を一度解除してから再試行してください。"
        )
    return result


def generate_auth_url(client_id: str, client_secret: str,
                      redirect_uri: str,
                      scopes: Optional[List[str]] = None) -> str:
    """
    方式2（フォールバック）: 手動コード入力用の認証URLを生成する。
    ローカルリダイレクトが使えない環境（リモートサーバー等）向け。

    ユーザーはこのURLをブラウザで開いて承認し、リダイレクト先URLに付与される
    認証コードを exchange_code() に渡す。
    """
    _require_google_libs()
    from google_auth_oauthlib.flow import Flow

    scopes = scopes or DEFAULT_SCOPES
    client_config = build_client_config(client_id, client_secret, redirect_uris=[redirect_uri])
    # PKCEを無効化する。手動コード方式ではURL生成とコード交換で別Flowを作るため、
    # PKCEを有効にすると code_verifier がすれ違い invalid_grant になる。
    # デスクトップ（confidential）クライアントは client_secret で認証されるためPKCE不要。
    flow = Flow.from_client_config(client_config, scopes=scopes, redirect_uri=redirect_uri,
                                   autogenerate_code_verifier=False)
    auth_url, _state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return auth_url


def exchange_code(client_id: str, client_secret: str,
                  redirect_uri: str, code: str,
                  scopes: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    方式2（フォールバック）: 認証コードをトークンに引き換える。

    Returns:
        {"refresh_token": str, "token": str, "scopes": [...], "client_id": str}
    """
    _require_google_libs()
    from google_auth_oauthlib.flow import Flow

    if not code:
        raise GoogleAuthError("認証コードが空です。")
    scopes = scopes or DEFAULT_SCOPES
    client_config = build_client_config(client_id, client_secret, redirect_uris=[redirect_uri])
    # 認証URL生成時とPKCE設定を揃える（無効化）。揃っていないと invalid_grant になる。
    flow = Flow.from_client_config(client_config, scopes=scopes, redirect_uri=redirect_uri,
                                   autogenerate_code_verifier=False)
    try:
        flow.fetch_token(code=code.strip())
    except Exception as e:  # noqa: BLE001
        raise GoogleAuthError(f"認証コードの引き換えに失敗しました: {e}")

    result = _credentials_to_dict(flow.credentials)
    if not result.get("refresh_token"):
        raise GoogleAuthError(
            "リフレッシュトークンが取得できませんでした。"
            "Googleアカウントのアクセス権限を一度解除してから再試行してください。"
        )
    return result


def credentials_from_refresh_token(client_id: str, client_secret: str,
                                   refresh_token: str,
                                   scopes: Optional[List[str]] = None) -> Any:
    """
    保存済みのリフレッシュトークンから Credentials を復元し、アクセストークンを更新する。

    Returns:
        google.oauth2.credentials.Credentials（更新済み）
    Raises:
        GoogleAuthError: 失効・無効化されている場合等。
    """
    _require_google_libs()
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not refresh_token:
        raise GoogleAuthError("リフレッシュトークンが未設定です。")
    scopes = scopes or DEFAULT_SCOPES
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=_TOKEN_URI,
        scopes=scopes,
    )
    try:
        creds.refresh(Request())
    except Exception as e:  # noqa: BLE001
        # リフレッシュトークン失効（テスト公開ステータスの7日失効を含む）等
        raise GoogleAuthError(f"アクセストークンの更新に失敗しました（再認証が必要な可能性）: {e}")
    return creds


def build_calendar_service(client_id: str, client_secret: str,
                           refresh_token: str,
                           scopes: Optional[List[str]] = None) -> Any:
    """
    リフレッシュトークンから認証済みの Calendar API サービスクライアントを構築する。

    Returns:
        googleapiclient.discovery.Resource（calendar v3）
    """
    _require_google_libs()
    from googleapiclient.discovery import build

    creds = credentials_from_refresh_token(client_id, client_secret, refresh_token, scopes)
    # cache_discovery=False: ファイルキャッシュ警告を避け、配布環境での書き込み副作用をなくす
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def revoke_token(token: str) -> bool:
    """
    トークン（アクセストークンまたはリフレッシュトークン）をGoogle側で失効させる。
    「認証を解除」操作で使用。ネットワーク失敗時もアプリ側の保存削除は別途行う想定。

    Returns:
        失効リクエストが成功（2xx）したら True。
    """
    if not token:
        return False
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({"token": token}).encode("utf-8")
        req = urllib.request.Request(
            _REVOKE_URI, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001
        print(f"--- [GoogleAuth] トークン失効リクエストに失敗（保存削除は継続）: {e} ---")
        return False
