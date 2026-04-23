#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Nexus Ark Log Cleanup Script
---------------------------
肥大化したログファイル（YYYY-MM.txtなど）から重複したログエントリを除去します。
エントリのヘッダー（## USERなど）と本文の内容が一致するものを「重複」とみなします。
"""

import os
import sys
import re
import hashlib
import argparse
from datetime import datetime

def cleanup_log_duplicates(file_path: str, dry_run: bool = False):
    if not os.path.exists(file_path):
        print(f"エラー: ファイルが見つかりません: {file_path}")
        return

    print(f"--- [Log Cleanup] 処理開始: {file_path} ---")
    
    # 統計用
    total_entries = 0
    unique_entries = 0
    
    # ヘッダーパターン (## USER:xx, ## AGENT:xx, ## SYSTEM:xx)
    header_pattern = re.compile(r'^(## (?:USER|AGENT|SYSTEM|NOTEPAD):.+?)$', re.MULTILINE)
    
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # ヘッダーで分割
        # split は [空, header1, body1, header2, body2, ...] を返す
        parts = header_pattern.split(content)
        
        seen_hashes = set()
        output_parts = []
        
        total_entries = (len(parts) - 1) // 2
        print(f"  - 検出されたエントリ数: {total_entries}")

        for i in range(1, len(parts), 2):
            header = parts[i]
            body = parts[i+1] if i+1 < len(parts) else ""
            
            # コンテンツのハッシュ値を計算して重複判定
            # 改行や空白の差異を無視するため、stripした内容でハッシュを取る
            entry_content = (header.strip() + body.strip())
            entry_hash = hashlib.md5(entry_content.encode('utf-8')).hexdigest()
            
            if entry_hash not in seen_hashes:
                seen_hashes.add(entry_hash)
                output_parts.append(f"{header}{body}")
                unique_entries += 1
        
        print(f"  - 一意なエントリ数: {unique_entries}")
        print(f"  - 削除される重複エントリ数: {total_entries - unique_entries}")

        if dry_run:
            print("  - [Dry Run] ファイルへの書き込みは行いません。")
            return

        if unique_entries == total_entries:
            print("  - 重複は見つかりませんでした。修正は不要です。")
            return

        # バックアップ作成
        backup_path = file_path + ".original"
        if not os.path.exists(backup_path):
            import shutil
            shutil.copy2(file_path, backup_path)
            print(f"  - 元ファイルをバックアップしました: {os.path.basename(backup_path)}")

        # 書き出し
        fixed_content = "".join(output_parts)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(fixed_content)
        
        print(f"--- [Log Cleanup] 完了: 正常に書き出されました ---")

    except Exception as e:
        print(f"エラー: 処理中にエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nexus Arkのログから重複エントリを削除します。")
    parser.add_argument("file_path", help="処理対象のログファイルパス (例: characters/MyRoom/logs/2026-03.txt)")
    parser.add_argument("--dry-run", action="store_true", help="実際に書き込まずに統計のみ表示します。")
    
    args = parser.parse_args()
    cleanup_log_duplicates(args.file_path, args.dry_run)
