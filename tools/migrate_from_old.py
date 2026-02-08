import os
import sys
import shutil
import json
import logging
from pathlib import Path

# 親ディレクトリをパスに追加して version_manager をインポート可能にする
sys.path.append(str(Path(__file__).parent.parent))
from version_manager import VersionManager

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

class MigrationTool:
    def __init__(self, src_path, dest_path=None):
        self.src_path = Path(src_path)
        self.dest_path = Path(dest_path) if dest_path else Path(__file__).parent.parent
        self.assets_olivie_path = self.dest_path / "assets" / "sample_persona" / "Olivie"
        
        if not self.src_path.exists():
            raise ValueError(f"Source path does not exist: {self.src_path}")

    def migrate_all(self):
        """全てのデータを移行・マージする"""
        logger.info(f"Starting Two-stage migration from {self.src_path} to {self.dest_path}")
        
        # 1. ルート設定ファイルの同期（config.json 等）
        self.migrate_file("config.json")
        self.migrate_file("alarms.json")
        self.migrate_file("redaction_rules.json")
        self.migrate_file(".gemini_key_states.json")
        
        # 2. 全キャラクター・全ルームの丸ごと移行（旧環境優先）
        self.migrate_all_characters_wholesale()
        
        # 3. オリヴェの特例アップグレード（後処理）
        self.upgrade_olivie_special()
        
        logger.info("Two-stage migration completed successfully!")

    def migrate_file(self, filename):
        src_file = self.src_path / filename
        dest_file = self.dest_path / filename
        
        if src_file.exists():
            if dest_file.exists():
                backup_file = dest_file.with_suffix(dest_file.suffix + ".bak")
                shutil.copy2(dest_file, backup_file)
                logger.info(f"Created backup of existing root {filename}")
            
            shutil.copy2(src_file, dest_file)
            logger.info(f"Migrated root file: {filename}")

    def migrate_all_characters_wholesale(self):
        """characters フォルダ内の全ルームを丸ごとコピーする"""
        src_chars = self.src_path / "characters"
        dest_chars = self.dest_path / "characters"
        
        if not src_chars.exists():
            logger.warning("Source 'characters' directory not found.")
            return

        for char_dir in src_chars.iterdir():
            if not char_dir.is_dir() or char_dir.name.startswith("."):
                continue
                
            logger.info(f"Wholesale migrating room: {char_dir.name}")
            target_dir = dest_chars / char_dir.name
            
            if target_dir.exists():
                # 既存の状態をバックアップ
                backup_dir = target_dir.parent / f"{target_dir.name}_default_bak"
                if not backup_dir.exists():
                    shutil.move(target_dir, backup_dir)
                    logger.info(f"Backed up new-env default room: {char_dir.name}")
                else:
                    shutil.rmtree(target_dir) # 既にバックアップがあれば削除して置換

            # 旧環境から丸ごとコピー
            shutil.copytree(char_dir, target_dir)
            logger.info(f"Copied legacy room wholesale: {char_dir.name}")

    def upgrade_olivie_special(self):
        """オリヴェのみ、移行後に最新アセットで部分的アップグレードを行う"""
        olivie_dir = self.dest_path / "characters" / "Olivie"
        if not olivie_dir.exists():
            logger.warning("Olivie directory not found in destination after migration. Skipping upgrade.")
            return

        if not self.assets_olivie_path.exists():
            logger.warning("Olivie primary assets not found. Skipping upgrade.")
            return

        logger.info("Performing special upgrade for Olivie...")

        # 1. 知識 (Specifications) の置換
        # NEXUS_ARK_SPECIFICATION.md 等を最新化
        src_knowledge = self.assets_olivie_path / "knowledge"
        dest_knowledge = olivie_dir / "knowledge"
        if src_knowledge.exists():
            for spec_file in src_knowledge.glob("*.md"):
                shutil.copy2(spec_file, dest_knowledge / spec_file.name)
                logger.debug(f"Replaced specification: {spec_file.name}")

        # 2. 情景描写テキスト (world_settings.txt) の置換
        src_ws = self.assets_olivie_path / "spaces" / "world_settings.txt"
        dest_ws = olivie_dir / "spaces" / "world_settings.txt"
        if src_ws.exists():
            if not dest_ws.parent.exists(): dest_ws.parent.mkdir(parents=True)
            shutil.copy2(src_ws, dest_ws)
            logger.info("Updated world_settings.txt for Olivie")

        # 3. RAGインデックスの置換
        src_rag = self.assets_olivie_path / "rag_data" / "faiss_index"
        dest_rag = olivie_dir / "rag_data" / "faiss_index"
        if src_rag.exists():
            if dest_rag.exists(): shutil.rmtree(dest_rag)
            shutil.copytree(src_rag, dest_rag)
            logger.debug("Updated RAG index for Olivie")

        # 3. 情景画像の追加（既存は削除せず、新しいものを配置）
        src_images = self.assets_olivie_path / "spaces" / "images"
        dest_images = olivie_dir / "spaces" / "images"
        if src_images.exists():
            if not dest_images.exists(): dest_images.mkdir(parents=True)
            for img in src_images.iterdir():
                if img.is_file() and not (dest_images / img.name).exists():
                    shutil.copy2(img, dest_images / img.name)
            logger.debug("Added new scenery images for Olivie")

        # 4. ルーム設定 (room_config.json) のマージ
        # CSS/テーマ 設定のみ最新版からマ移植する
        self.merge_olivie_room_config(olivie_dir)

    def merge_olivie_room_config(self, olivie_dir):
        """オリヴェの room_config.json を読み込み、CSS/テーマ設定のみ最新化する"""
        config_path = olivie_dir / "room_config.json"
        assets_config_path = self.assets_olivie_path / "room_config.json"
        
        if not config_path.exists() or not assets_config_path.exists():
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            with open(assets_config_path, "r", encoding="utf-8") as f:
                new_config = json.load(f)

            # 更新対象のキー（テーマ関連）
            theme_keys = [k for k in new_config.get("override_settings", {}).keys() if k.startswith("theme_")]
            # その他、ボイスや描写系もユーザーが明示的に変えていなければ更新したいが、
            # 今回は安全第一でテーマ周りと特定項目に限定
            theme_keys.extend(["voice_id", "voice_style_prompt", "room_theme_enabled", "theme_ui_opacity"])

            if "override_settings" not in user_config:
                user_config["override_settings"] = {}

            for key in theme_keys:
                if key in new_config["override_settings"]:
                    user_config["override_settings"][key] = new_config["override_settings"][key]

            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(user_config, f, indent=2, ensure_ascii=False)
            
            logger.info("Successfully merged new CSS/Theme settings into Olivie's room_config.json")
        except Exception as e:
            logger.error(f"Error merging Olivie room_config: {e}")

def main():
    print("=== Nexus Ark Two-stage Migration Tool ===")
    candidates = VersionManager.find_legacy_candidates()
    
    selected_path = None
    if candidates:
        print("\nFound potential legacy installations:")
        for i, c in enumerate(candidates):
            print(f"[{i+1}] {c['version']} at {c['path']}")
        choice = input(f"\nSelect a candidate [1-{len(candidates)}] or Enter manual path: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            selected_path = candidates[int(choice)-1]['path']
        elif choice: selected_path = choice
    else:
        selected_path = input("\nEnter manual path to old Nexus Ark: ").strip()

    if selected_path:
        try:
            MigrationTool(selected_path).migrate_all()
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()
