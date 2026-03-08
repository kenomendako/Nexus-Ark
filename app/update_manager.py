import os
import logging
import json
from pathlib import Path
from tufup.client import Client
from version_manager import VersionManager

logger = logging.getLogger(__name__)

class UpdateManager:
    """
    Nexus Ark の自動更新を管理するクラス。
    tufup (The Update Framework) を使用して、安全な差分更新を実現します。
    """
    APP_NAME = "Nexus-Ark"
    # デフォルトの更新サーバーURL (GitHub Pages等を想定)
    DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/kenomendako/Nexus-Ark/main/updates/"

    def __init__(self, update_url=None):
        self.current_version = VersionManager.get_current_version()
        self.install_dir = Path(__file__).parent.resolve()
        
        # プロジェクトルートの特定 (2段階構造 dist/app を考慮)
        self.project_root = self.install_dir
        if self.install_dir.name == "app":
            self.project_root = self.install_dir.parent
            
        # メタデータディレクトリはプロジェクトルート直下
        self.metadata_dir = self.project_root / "metadata"
        self.metadata_dir.mkdir(exist_ok=True)
        
        # config.json からの上書きをチェック (プロジェクトルートまたはインストールディレクトリ)
        source_info = "default"
        if not update_url:
            # プロジェクトルート -> インストールディレクトリ の順で探す
            config_paths = [self.project_root / "config.json", self.install_dir / "config.json"]
            for config_path in config_paths:
                if config_path.exists():
                    try:
                        with open(config_path, "r", encoding="utf-8") as f:
                            config_data = json.load(f)
                            update_url = config_data.get("update_url")
                            if update_url:
                                source_info = f"config.json ({config_path.parent.name})"
                                break
                    except Exception as e:
                        logger.error(f"Error loading config.json for update_url: {e}")
        else:
            source_info = "provided argument"

        self.update_url = update_url or self.DEFAULT_UPDATE_URL
        if not self.update_url.endswith("/"):
            self.update_url += "/"
            
        logger.info(f"UpdateManager initialized with URL: {self.update_url} (source: {source_info})")
        logger.info(f"Metadata dir: {self.metadata_dir}")
        logger.info(f"Current version: {self.current_version}")

        # ステージングディレクトリ（展開先）: プロジェクトルート直下に配置
        self.staging_dir = self.project_root / "update_staging"

        # tufup クライアントの初期化
        try:
            self.client = Client(
                app_name=self.APP_NAME,
                app_install_dir=str(self.install_dir),
                current_version=self.current_version,
                metadata_dir=str(self.metadata_dir),
                metadata_base_url=self.update_url + "metadata/",
                target_dir=str(self.install_dir),
                target_base_url=self.update_url + "targets/",
                extract_dir=self.staging_dir, # 更新ファイルをステージング領域に展開
            )
            logger.info(f"tufup client initialized. Metadata base URL: {self.update_url}metadata/")
        except Exception as e:
            logger.error(f"Failed to initialize tufup client: {e}")
            raise

    def is_configured(self):
        """
        更新システムが正しく構成されているか確認します。
        """
        return (self.metadata_dir / "root.json").exists()

    def check_for_updates(self):
        """
        更新を確認し、新しいバージョンがあればその情報を返します。
        
        Returns:
            tuple: (new_version_string, message) または (None, message)
        """
        if not self.is_configured():
            logger.warning("Update system not configured: metadata/root.json is missing.")
            return None, "更新サーバーが設定されていません。"

        try:
            logger.info(f"Checking for updates from {self.update_url}...")
            # リモートのメタデータを取得して比較
            # check_for_updates は新しいアーカイブの TargetMeta を返す (なければ None)
            new_archive_meta = self.client.check_for_updates()
            if new_archive_meta:
                new_version = str(new_archive_meta.version)
                logger.info(f"New version found: {new_version}")
                return new_version, f"新しいバージョン {new_version} が利用可能です。"
            else:
                logger.info("No updates found.")
                return None, "最新バージョンを使用中です。"
        except Exception as e:
            logger.error(f"Failed to check for updates: {e}")
            return None, f"更新確認エラー: {e}"

    def download_and_apply(self, progress_hook=None):
        """
        更新パッケージをダウンロードし、現在のインストール環境に適用します。
        適用後はアプリケーションの再起動が必要です。

        Args:
            progress_hook (callable): 進捗通知用コールバック関数

        Returns:
            tuple: (success_bool, message)
        """
        if not self.is_configured():
            return False, "更新システムが構成されていません。"

        try:
            # 適用前に必ず最新のメタデータを再チェックして状態を更新する
            # (UIハンドラが毎回インスタンスを作り直すため、tufup側の状態を復元する必要がある)
            logger.info("Refreshing update state before application...")
            new_archive_meta = self.client.check_for_updates()
            if not new_archive_meta:
                 # すでに最新か、確認エラー
                 return False, "適用可能な更新が見つかりませんでした。"

            logger.info(f"Downloading update (Version: {new_archive_meta.version})...")
            
            import platform
            if platform.system() == "Windows":
                # Windowsでは確実にファイルを置き換えるため、別スレッドでダウンロードと展開を行う
                # ステージング領域への展開まで行い、実際のファイル移動はランチャー(Start.bat)に任せる
                import threading
                def _do_update_windows():
                    try:
                        self.client.download_and_apply_update(
                            progress_hook=progress_hook,
                            skip_confirmation=True,
                            install=lambda **kwargs: logger.info(f"Update extracted to: {self.staging_dir}")
                        )
                        logger.info("Update prepared in staging. Signaling restart...")
                        self.trigger_restart()
                    except Exception as e:
                        logger.error(f"Windows update staging failure in thread: {e}")

                threading.Thread(target=_do_update_windows, daemon=True).start()
                return True, "更新データの準備を開始しました。完了すると自動的に再起動しますので、そのまましばらくお待ちください。"
            else:
                # Linux/macOS では通常通り同期実行を試みる
                if self.client.download_and_apply_update(progress_hook=progress_hook, skip_confirmation=True):
                    logger.info("Update applied successfully.")
                    return True, "更新の適用に成功しました。まもなく自動的に再起動します..."
                else:
                    logger.warning("Failed to apply update.")
                    return False, "更新の適用に失敗しました。"
        except Exception as e:
            logger.error(f"Update application error: {e}")
            return False, f"予期せぬエラーが発生しました: {e}"

    def trigger_restart(self):
        """
        アプリケーションを明示的に再起動します。
        exit code 123 を返して終了し、ランチャーに後継処理を任せます。
        """
        logger.info("Restarting application (exit code 123)...")
        import threading
        import time
        import os
        
        def _delayed_exit():
            time.sleep(5)
            os._exit(123)
            
        threading.Thread(target=_delayed_exit, daemon=True).start()

    @classmethod
    def quick_check(cls):
        """
        簡便に更新チェックを行うためのクラスメソッド。
        """
        instance = cls()
        return instance.check_for_updates()
