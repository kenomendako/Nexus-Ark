"""停止中の新版に、旧版からデータだけをコピーする公開入口。"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

# 新規展開先のmanifest検証前にbytecodeを生成しない。
sys.dont_write_bytecode = True

# -Iで起動し、選択された旧版からPython moduleを読み込まない。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from legacy_bootstrap.data_import import DataImportError, import_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    phases = {
        'validate': '引き継ぎ元と新版を確認しました。',
        'stage': '会話・記憶・設定をコピーします。データ量により数分かかります。',
        'staged': 'コピーしたデータを照合しました。新版へ反映します。',
        'verify': '元のデータと新版を最終確認しています。',
    }
    def progress(phase):
        if phase in phases:
            print('[Nexus Ark] ' + phases[phase], flush=True)
    try:
        result = import_data(Path(args.source), Path(args.destination),
                             progress=progress)
    except Exception as exc:
        # 個人path、設定、会話、キーをconsoleや診断へ出さない。
        code = exc.code if isinstance(exc, DataImportError) else type(exc).__name__
        descriptions = {
            'source-missing-data': '使用中の旧版または完全バックアップのNexus Arkフォルダーを選んでください。',
            'source-incomplete': '移行途中の新版は引き継ぎ元にできません。直前まで正常に使っていたフォルダーを選んでください。',
            'source-unused-package': '未使用の配布パッケージが選ばれています。直前まで使っていたフォルダーを選んでください。',
            'source-target-overlap': '引き継ぎ元と新版は、互いに含まれない別のフォルダーにしてください。',
            'destination-used': 'この新版は使用済み、または作業途中です。ZIPを別の新しいフォルダーへ展開してください。',
            'destination-not-fresh': '未起動の、新しく展開した新版が必要です。ZIPを別の新しいフォルダーへ展開してください。',
            'insufficient-space': '新版の保存先の空き容量が不足しています。データとPython環境のための空き容量を確保してください。',
            'source-changed': 'コピー中に元のデータが変わりました。旧版を通常終了してから、新しい展開先でやり直してください。',
            'import-locked': '新版の起動または他の引き継ぎ処理と重なりました。終了後、新しい展開先でやり直してください。',
            'unsafe-path': 'リンクや特殊なファイルを検出したため停止しました。元のフォルダーを残してください。',
        }
        print(f'[Nexus Ark] データ移行を完了できませんでした（確認コード: {code}）。', flush=True)
        if code in descriptions:
            print(descriptions[code], flush=True)
        print('元のフォルダーは引き続き正本として使用してください。新版は起動せず、ZIPを別の新しいフォルダーへ展開し直してください。', flush=True)
        return 1
    print('[Nexus Ark] コピーと照合が完了しました。新版フォルダーの Start.bat から起動してください。', flush=True)
    print('会話・記憶・設定を確認するまで、元のフォルダーとバックアップを残してください。', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
