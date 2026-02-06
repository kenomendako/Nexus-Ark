import os
import shutil
import json
import datetime
import glob

# Configuration
DIST_DIR = "dist"
SOURCE_DIR = "."

# Files/Directories to EXCLUDE globally
GLOBAL_IGNORE_PATTERNS = [
    ".git", ".gitignore", ".github", ".agent", ".vscode", ".idea",
    "__pycache__", "*.pyc", "*.pyo", "*.log",
    "venv", "env", "dist", "build", "tmp", "temp",
    "tests", "outing", "tools",
    "bra_scraper.py", "test_*.py",
    "README.md", # We will copy README_DIST.md as README.md if it exists
    "INBOX.md",
    "LICENSE", # Copy manually if needed
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
}

# Files to explicitly INCLUDE (Cherry-pick)
INCLUDE_FILES = [
    ("docs/NEXUS_ARK_SPECIFICATION.md", "docs/NEXUS_ARK_SPECIFICATION.md"),
    ("README_DIST.md", "README.md"), # Rename on copy
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
    print(f"🚀 Starting Release Build...")
    
    # 1. Prepare Dist Directory
    if os.path.exists(DIST_DIR):
        print(f"🗑️  Cleaning existing dist drectory: {DIST_DIR}")
        shutil.rmtree(DIST_DIR)
    
    # 2. Copy Source Tree with Filters
    print(f"📂 Copying project files...")
    shutil.copytree(SOURCE_DIR, DIST_DIR, ignore=is_ignored, dirs_exist_ok=True)
    
    # 3. Cherry-pick files
    print(f"🍒 Cherry-picking specific files...")
    for src, dest in INCLUDE_FILES:
        if os.path.exists(src):
            dest_full = os.path.join(DIST_DIR, dest)
            os.makedirs(os.path.dirname(dest_full), exist_ok=True)
            shutil.copy2(src, dest_full)
            print(f"   - Copied {src} -> {dest}")
        else:
            print(f"⚠️  Warning: Source file not found: {src}")

    # 5. Inject Knowledge to Sample Persona
    # Ensure Olive in assets/sample_persona gets the latest specification
    spec_src = "docs/NEXUS_ARK_SPECIFICATION.md"
    sample_knowledge_dest = os.path.join(DIST_DIR, "assets/sample_persona/Olivie/knowledge/NEXUS_ARK_SPECIFICATION.md")
    
    if os.path.exists(spec_src):
        # Verify destination directory exists (it should if assets were copied)
        os.makedirs(os.path.dirname(sample_knowledge_dest), exist_ok=True)
        shutil.copy2(spec_src, sample_knowledge_dest)
        print(f"🧠 Updated Sample Persona Knowledge: {sample_knowledge_dest}")
    else:
        print(f"⚠️  Warning: Specification file not found: {spec_src}")


    # 4. Version Management
    ver_data = load_version()
    ver_data["release_date"] = datetime.date.today().isoformat()
    # Update version info in dist
    save_version(ver_data, DIST_DIR)
    print(f"🏷️  Version info updated: {ver_data['version']}")

    print(f"✨ Build Complete! Output: {os.path.abspath(DIST_DIR)}")
    print(f"   Review the 'dist' folder before pushing to the release repository.")

if __name__ == "__main__":
    main()
