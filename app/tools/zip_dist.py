import os
import json
import zipfile

DIST_DIR = "dist"
VERSION_FILE = os.path.join(DIST_DIR, "app", "version.json")

def main():
    if not os.path.exists(DIST_DIR):
        print(f"Error: {DIST_DIR} does not exist.")
        return

    version = "unknown"
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r") as f:
                data = json.load(f)
                version = data.get("version", "unknown")
        except Exception as e:
            print(f"Warning: Could not read version file: {e}")

    zip_filename = f"NexusArk_{version}.zip"
    print(f"Creating zip file: {zip_filename} from {DIST_DIR} (excluding 'updates' directory)...")
    
    try:
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(DIST_DIR):
                # 'updates' フォルダ（過去のtar.gz更新パッケージ群）をZIP内容から除外する
                # パブリックリポジトリには必要なため dist/updates/ 自体は残すが、
                # 初回インストール用のZIPには含めない
                if "updates" in dirs:
                    dirs.remove("updates")
                
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, DIST_DIR)
                    zipf.write(file_path, arcname)
                    
        print(f"Success! Created {zip_filename}")
    except Exception as e:
        print(f"Error creating zip: {e}")

if __name__ == "__main__":
    main()
