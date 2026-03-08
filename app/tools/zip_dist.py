
import shutil
import os
import json

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

    zip_filename = f"NexusArk_{version}"
    print(f"Creating zip file: {zip_filename}.zip from {DIST_DIR}...")
    
    try:
        shutil.make_archive(zip_filename, 'zip', root_dir=DIST_DIR)
        print(f"Success! Created {zip_filename}.zip")
    except Exception as e:
        print(f"Error creating zip: {e}")

if __name__ == "__main__":
    main()
