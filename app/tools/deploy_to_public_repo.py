import os
import shutil
import subprocess
import sys

# 設定
DIST_DIR = "dist"
PUBLIC_REPO_URL = "https://github.com/kenomendako/Nexus-Ark.git"
BRANCH_NAME = "main"

def run_command(command, cwd=None):
    """コマンドを実行し、エラーがあれば停止する"""
    print(f"Running: {command}")
    try:
        subprocess.check_call(command, shell=True, cwd=cwd)
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {command}")
        sys.exit(1)

def main():
    # 1. dist ディレクトリの確認
    if not os.path.exists(DIST_DIR):
        print(f"Error: {DIST_DIR} directory not found. Please run build_release.py first.")
        sys.exit(1)

    print("🚀 Starting deployment to public repository...")
    
    # 2. dist 内の .git を削除（クリーンな状態にする）
    git_dir = os.path.join(DIST_DIR, ".git")
    if os.path.exists(git_dir):
        print(f"Cleaning existing .git directory in {DIST_DIR}...")
        shutil.rmtree(git_dir)

    # 3. Git 初期化とコミット
    print(f"Initializing Git in {DIST_DIR}...")
    run_command("git init", cwd=DIST_DIR)
    run_command(f"git checkout -b {BRANCH_NAME}", cwd=DIST_DIR)
    run_command("git add .", cwd=DIST_DIR)
    
    # Git Identity configuration
    run_command("git config user.email 'nexus-ark-bot@example.com'", cwd=DIST_DIR)
    run_command("git config user.name 'Nexus Ark Bot'", cwd=DIST_DIR)

    commit_message = f"Release build deployed at {os.popen('date').read().strip()}"
    run_command(f'git commit -m "{commit_message}"', cwd=DIST_DIR)

    # 4. リモート設定とプッシュ
    print(f"Setting remote to {PUBLIC_REPO_URL}...")
    run_command(f"git remote add origin {PUBLIC_REPO_URL}", cwd=DIST_DIR)
    
    print("Pushing to public repository (force push)...")
    run_command(f"git push -f origin {BRANCH_NAME}", cwd=DIST_DIR)

    print("✅ Deployment complete! The 'dist' folder content has been pushed to the public repository.")

if __name__ == "__main__":
    main()
