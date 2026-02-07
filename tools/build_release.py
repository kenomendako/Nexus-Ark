import os
import shutil
import json
import datetime
import glob

# Configuration
DIST_DIR = "dist"
APP_DIR = os.path.join(DIST_DIR, "app")  # Two-tier structure: dist/app/
SOURCE_DIR = "."

# Launcher files to copy to dist root
LAUNCHER_DIR = "assets/launchers"

# Files/Directories to EXCLUDE globally
GLOBAL_IGNORE_PATTERNS = [
    ".git", ".gitignore", ".github", ".agent", ".gemini", ".vscode", ".idea",
    "__pycache__", "*.pyc", "*.pyo", "*.log",
    "venv", "env", ".venv", "dist", "build", "tmp", "temp",
    "tests", "outing", "tools", "scripts",
    "bra_scraper.py", "test_*.py",
    "README.md", "README_DIST.md",  # Will use launcher README
    "INBOX.md",
    "LICENSE", # Copy manually if needed
    # --- User-specific data (NEVER include in distribution) ---
    "config.json",
    "alarms.json",
    "redaction_rules.json",
    ".gemini_key_states.json",
    ".memos",
    "backups",
    # --- Development artifacts ---
    "MagicMock",
    "rooms",  # Empty legacy folder
    # --- Legacy launcher files (replaced by two-tier structure) ---
    "ネクサスアーク.bat",
    "start_nexus_ark.bat",
    "start_nexus_ark.sh",
]

# Specific handling for directories
# key: directory path, value: list of ignore patterns for that directory
DIR_SPECIFIC_IGNORE = {
    "characters": [
        "log.txt",
        "log.txt.migrated",
        "memory.json",
        "memory",  # Exclude memory directory (Vector DB)
        "chroma_db",
        "generated_images",
        "audio_cache",
        "backups",
        "log_archives",
        "*.bak",
        "core_memory.txt",
        "current_location.txt",
        "notepad.md",
        "rag_data",
        "logs",
        "cache",
        "watchlist_cache",
        "spaces",
        "log_import_source",
        "private",
        "notes",
        "attachments",
        "Mondayさん", # Specific User Characters (Example)
        "SystemPromptSample.txt",
        "backup.log",
        "test_room",
        "ImportTest",
        "LegacyPropTest",
        "MemoryTest",
        "Alice",
        "Default",
        "G3テスト",
        "アルト記憶パック法.txt",
        "エテルノ",
        "テスト",
        "ノワール",
        "マルチエ",
        "リュカ",
        "ルシアン",
        "ログ整理法.txt",
        "思考ログテスト",
        "新規ルームテスト１",
        "美帆",
        "脱衣執事",
        "設定テスト",
        "音声機能テスト",
    ],
    "docs": ["*"], # Exclude everything in docs by default, we will cherry-pick
    "assets": ["launchers"],  # Launcher files are copied to root, not app/assets/
}

# Files to explicitly INCLUDE in app/ (Cherry-pick)
INCLUDE_FILES = [
    ("docs/NEXUS_ARK_SPECIFICATION.md", "docs/NEXUS_ARK_SPECIFICATION.md"),
]

def load_version():
    """Load or create version.json"""
    version_file = "version.json"
    default_version = {
        "version": "1.0.0",
        "release_date": datetime.date.today().isoformat(),
        "min_python_version": "3.10",
        "github_repo": "kenomendako/Nexus-Ark",
        "knowledge_version": "1.0.0"
    }
    
    if os.path.exists(version_file):
        with open(version_file, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        return default_version

def save_version(data, dist_path=None):
    """Save version.json to source and/or dist"""
    # Always update source
    with open("version.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    
    # Copy to dist if specified
    if dist_path:
        with open(os.path.join(dist_path, "version.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

def is_ignored(path, names):
    """Callback for shutil.copytree to ignore files"""
    ignore_set = set()
    rel_path = os.path.relpath(path, SOURCE_DIR)
    
    # 1. Check Global Ignores
    for pattern in GLOBAL_IGNORE_PATTERNS:
        ignore_set.update(glob.fnmatch.filter(names, pattern))
    
    # 2. Check Directory Specific Ignores
    # Standardize path separators
    normalized_rel_path = rel_path.replace("\\", "/")
    
    for dir_key, patterns in DIR_SPECIFIC_IGNORE.items():
        # Check if we are inside a specific directory (e.g., characters/Alice)
        # Check if current path (rel_path) matches the target dir or is a subdir of it
        if normalized_rel_path == dir_key or normalized_rel_path.startswith(dir_key + "/"):
            # Apply patterns to the files/dirs within this path
            for pattern in patterns:
                ignore_set.update(glob.fnmatch.filter(names, pattern))
    
    # --- Special Logic for Characters Whitelist ---
    # We want to EXCLUDE ALL characters in the "characters" folder for distribution.
    # The default character data should be provided via "assets/sample_persona".
    if normalized_rel_path == "characters":
        # Filter out EVERYTHING in characters folder
        # We only keep the folder itself empty (or with .gitkeep if we copy it)
        # Since ignore returns a set of names to ignore from the current list 'names'
        ignore_set.update(names)
    # ---------------------------------------------


    return ignore_set


    return ignore_set

def main():
    print(f"🚀 Starting Release Build (Two-Tier Structure)...")
    
    # 1. Prepare Dist Directory
    if os.path.exists(DIST_DIR):
        print(f"🗑️  Cleaning existing dist directory: {DIST_DIR}")
        shutil.rmtree(DIST_DIR)
    
    # 2. Copy Source Tree to app/ subdirectory
    print(f"📂 Copying project files to app/...")
    shutil.copytree(SOURCE_DIR, APP_DIR, ignore=is_ignored, dirs_exist_ok=True)
    
    # 3. Copy launcher files to dist root
    print(f"🚀 Copying launcher files to root...")
    if os.path.exists(LAUNCHER_DIR):
        for filename in os.listdir(LAUNCHER_DIR):
            src = os.path.join(LAUNCHER_DIR, filename)
            dest = os.path.join(DIST_DIR, filename)
            if os.path.isfile(src):
                shutil.copy2(src, dest)
                print(f"   - {filename}")
    else:
        print(f"⚠️  Warning: Launcher directory not found: {LAUNCHER_DIR}")
    
    # 4. Cherry-pick files to app/
    print(f"🍒 Cherry-picking specific files...")
    for src, dest in INCLUDE_FILES:
        if os.path.exists(src):
            dest_full = os.path.join(APP_DIR, dest)
            os.makedirs(os.path.dirname(dest_full), exist_ok=True)
            shutil.copy2(src, dest_full)
            print(f"   - Copied {src} -> {dest}")
        else:
            print(f"⚠️  Warning: Source file not found: {src}")

    # 5. Inject Knowledge to Sample Persona
    spec_src = "docs/NEXUS_ARK_SPECIFICATION.md"
    sample_knowledge_dest = os.path.join(APP_DIR, "assets/sample_persona/Olivie/knowledge/NEXUS_ARK_SPECIFICATION.md")
    
    if os.path.exists(spec_src):
        os.makedirs(os.path.dirname(sample_knowledge_dest), exist_ok=True)
        shutil.copy2(spec_src, sample_knowledge_dest)
        print(f"🧠 Updated Sample Persona Knowledge")
    else:
        print(f"⚠️  Warning: Specification file not found: {spec_src}")

    # 6. Version Management
    ver_data = load_version()
    ver_data["release_date"] = datetime.date.today().isoformat()
    # Save to app/ directory
    with open(os.path.join(APP_DIR, "version.json"), "w", encoding="utf-8") as f:
        json.dump(ver_data, f, indent=4, ensure_ascii=False)
    print(f"🏷️  Version info updated: {ver_data['version']}")

    # 7. Print final structure
    print(f"\n✨ Build Complete! Output: {os.path.abspath(DIST_DIR)}")
    print(f"\n📁 Distribution Structure:")
    print(f"   {DIST_DIR}/")
    print(f"   ├── Start.bat       (Windows起動)")
    print(f"   ├── Start.sh        (Mac/Linux起動)")
    print(f"   ├── README.md       (使い方)")
    print(f"   └── app/            (アプリ本体)")
    print(f"       └── nexus_ark.py, etc...")
    print(f"\n   Review the 'dist' folder before pushing to the release repository.")

if __name__ == "__main__":
    main()

