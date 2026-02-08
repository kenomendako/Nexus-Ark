Python製デスクトップアプリ（Windows）における llama-cpp-python の配布・依存管理・エンコーディング問題に関する包括的調査報告書

目次
エグゼクティブサマリー

序論：ローカルLLMアプリケーション配布の技術的課題 2.1 コンパイル済み拡張モジュールと「バイナリ配布問題」 2.2 llama-cpp-python の特異性とビルド要件 2.3 Windows環境における開発者体験とエンドユーザー体験の乖離

第1部：現代的な依存管理ツール uv と pyproject.toml による解決策の模索 3.1 Pythonパッケージング標準（PEP）におけるハードウェア検出の限界 3.2 uv の依存解決ロジックと「条件付きインストール」の技術的制約 3.3 tool.uv.sources とマルチインデックス構成の深層分析 3.4 結論：純粋な宣言的構成（Declarative Configuration）の不可能性

第2部：Windows特有のエンコーディング問題（CP932 vs UTF-8）の根本対策 4.1 UnicodeDecodeError の発生メカニズム：site.py とWindowsファイルシステムの衝突 4.2 Pythonの起動シーケンスとエンコーディング決定プロセス 4.3 既存の回避策（python -S）の問題点とリスク 4.4 推奨される根本対策：PYTHONUTF8 環境変数の活用と安全性評価

第3部：先行事例におけるアーキテクチャ分析 5.1 Text-Generation-WebUI (Oobabooga) の「ポータブル環境」戦略 5.2 InvokeAI のインストーラー設計とハードウェア検出ロジック 5.3 スタンドアロン化ツール（PyApp, Briefcase）の適用可能性

第4部：推奨アーキテクチャ「ハイブリッド・ランチャーパターン」の提案 6.1 アーキテクチャ概要：宣言的管理と命令的ブートストラップの分離 6.2 pyproject.toml の最適化構成 6.3 動的ハードウェア検出と uv コマンド構築（PowerShell実装詳細） 6.4 ユーザー体験（UX）と保守性の向上

将来展望：Pythonパッケージングエコシステムの進化と「Wheel Variants」

結論

1. エグゼクティブサマリー
本報告書は、Python製チャットボットアプリケーション「Nexus Ark」のWindows向け配布において直面している、llama-cpp-python のビルド依存問題および日本語環境特有のエンコーディング問題に対する包括的な技術調査結果である。

調査の結果、pyproject.toml の設定のみでGPU/CPUの条件分岐を自動化することは、現在のPythonパッケージング標準（PEP 508）の仕様上不可能であることが判明した。uv は非常に高速かつ強力なパッケージマネージャーであるが、依存関係の解決は静的なメタデータに基づいており、実行環境の「GPUの有無」や「CUDAバージョン」といった動的なハードウェア状態を検知してインストール対象を切り替える機能は有していない。

しかし、既存の「独自スクリプトによる後入れ（pip install の直接呼び出し）」という手法は、依存関係の整合性を損なうリスクが高く、保守性の観点から推奨されない。これに代わる現代的な解決策として、本報告書では**「ハイブリッド・ランチャーパターン」**を提案する。これは、uv の持つ「追加インデックスURL（--extra-index-url）」や「Extras（オプション依存関係）」の機能を活用しつつ、ハードウェア検出ロジックのみを軽量なブートストラップスクリプト（PowerShell等）に委譲するアーキテクチャである。これにより、パッケージ管理の責務を uv に一元化しつつ、柔軟なハードウェア対応を実現できる。

また、日本語パス環境で発生する UnicodeDecodeError については、Windowsのレガシーなコードページ（CP932）とPythonの起動処理（site.py）の不整合が原因である。これに対する最も堅牢かつ副作用の少ない解決策は、アプリケーション起動時に環境変数 PYTHONUTF8=1 を注入することである。これにより、システム設定を変更することなく、アプリケーションプロセス内でのみUTF-8モードを強制し、エンコーディング問題を根本から解決可能である。

本報告書では、これらの技術的根拠を詳述するとともに、先行事例（Text-Generation-WebUI, InvokeAI）の分析を通じて、Nexus Arkが採用すべき具体的な実装指針を提示する。

2. 序論：ローカルLLMアプリケーション配布の技術的課題
2.1 コンパイル済み拡張モジュールと「バイナリ配布問題」
Pythonはインタープリタ型言語であり、純粋なPythonコードであれば「Write Once, Run Anywhere」に近い可搬性を持つ。しかし、数値計算やAI推論の領域では、パフォーマンスを追求するためにC/C++やCUDAで記述された拡張モジュールが不可欠である。これらのモジュールは、実行環境のOS、CPUアーキテクチャ（x64/ARM64）、そしてハードウェアアクセラレータ（GPUの種類とドライババージョン）に密結合したバイナリ形式（Wheel）で配布される必要がある。

Windows環境におけるこの問題は特に深刻である。Linux環境では開発ツールチェーン（GCC等）が標準的に利用可能であることが多いが、一般的なWindowsユーザーの環境には Visual Studio Build Tools や CMake といったコンパイル環境が存在しない。したがって、pip install package がソース配布（sdist）にフォールバックしてローカルビルドを試みた場合、ほぼ確実に失敗する。これが「Nexus Ark」が直面している「導入の壁」の正体である。

2.2 llama-cpp-python の特異性とビルド要件
llama-cpp-python は、Georgi Gerganov氏による llama.cpp のPythonバインディングである。このライブラリは、単なるラッパーではなく、インストール時に llama.cpp 本体をビルドし、Pythonから ctypes 経由で呼び出せる共有ライブラリ（.dll/.so）を生成する。

標準的なPyPIリポジトリで配布されている llama-cpp-python は、汎用性を重視しており、特定のGPUアクセラレーション（CUDA, ROCm）を有効にしたバイナリを含んでいない場合が多い。GPUアクセラレーションを有効にするには、インストール時に環境変数（例：CMAKE_ARGS="-DGGML_CUDA=on"）を設定してビルドを行う必要があるが、前述の通りWindowsユーザーには不可能である。

この問題を解決するために、メンテナである abetlen 氏は、GitHub Releases 上で「ビルド済みWheel」を提供している。これらは「CPU（AVX2）」「CUDA 11.8」「CUDA 12.1」など、特定の環境向けにコンパイルされたバイナリ群である。Nexus Arkの課題は、**「ユーザーの環境に合致した正しいWheelを、ユーザーに意識させることなく自動選択し、インストールさせること」**にある。

2.3 Windows環境における開発者体験とエンドユーザー体験の乖離
開発者は通常、適切なドライバとコンパイラが整備された環境で作業を行うため、uv pip install や pip install が成功しやすい。しかし、配布先のエンドユーザー環境は多岐にわたる。

CUDA非搭載PC: 純粋なCPU推論が必要（AVX2等の命令セット対応も考慮が必要）。

CUDA搭載PC（古いドライバ）: 最新のCUDA 12.x向けWheelが動作しない可能性がある。

日本語ユーザー名: C:\Users\田中太郎\... といったパスが含まれることで、ASCII前提のツールチェーンやスクリプトがクラッシュする。

これらの乖離を埋めるためには、単なる依存関係定義（requirements.txt や pyproject.toml）を超えた、**「環境適応型インストーラー」**の設計が不可欠となる。

3. 第1部：現代的な依存管理ツール uv と pyproject.toml による解決策の模索
「Nexus Ark」プロジェクトでは、高速なパッケージマネージャーである uv を採用している。uv は pyproject.toml を第一級市民として扱い、決定論的な依存解決とロックファイル（uv.lock）の生成を行う。ここでは、uv の機能だけでハードウェアに応じたWheelの自動選択が可能かを検証する。

3.1 Pythonパッケージング標準（PEP）におけるハードウェア検出の限界
Pythonの依存関係定義は、PEP 508 (Dependency specification for Python Software Packages) に準拠している。この規格では、「環境マーカー（Environment Markers）」を使用して、特定の条件下でのみ依存関係をインストールすることが可能である。

利用可能な標準マーカーの例：

sys_platform: OSの識別（例: win32, linux, darwin）

platform_machine: CPUアーキテクチャ（例: AMD64, aarch64）

python_version: Pythonのバージョン（例: 3.12）

implementation_name: 実装系（例: cpython）

存在しないマーカー：

gpu_available: GPUの有無

cuda_version: インストールされているCUDAのバージョン

vram_size: ビデオメモリ容量

PEP 508 は「静的な環境情報（OSやPythonインタプリタ自体の属性）」に基づいて依存関係を定義することを目的としており、「動的なハードウェア状態」や「外部ドライバの状態」を判定する機能は意図的に排除されている。これは、依存解決の再現性と決定論的性質を保証するためである。もし has_gpu のようなマーカーが存在すれば、同じ pyproject.toml から生成されるロックファイルが、実行するマシンによって全く異なる内容になり、ロックファイルの意義が失われるためである。

結論1: pyproject.toml 内に dependencies = ["llama-cpp-python-cuda; gpu_exists"] のような記述を行う標準的な方法は存在しない。

3.2 uv の依存解決ロジックと「条件付きインストール」の技術的制約
uv は非常に高度なリゾルバ（PubGrubアルゴリズムを採用）を持つが、あくまで PEP 準拠の範囲内で動作する。しかし、uv 独自の拡張機能として tool.uv.sources テーブルが存在する。これを使えば、パッケージ名とインデックスURLのマッピングが可能である。

仮説：環境マーカーを使ったソースの切り替えは可能か？
uv のドキュメントによれば、tool.uv.sources 内でも環境マーカーを使用することができる。

Ini, TOML
[tool.uv.sources]
torch = [
    { index = "pytorch-cu121", marker = "sys_platform == 'linux'" },
    { index = "pytorch-cpu", marker = "sys_platform == 'darwin'" }
]
このように、OSレベルでの切り替えは可能である。しかし、前述の通り「GPUの有無」を表すマーカーがないため、同一のOS（Windows）内で、ハードウェア構成に応じて異なるインデックス（CPU版 vs CUDA版）を宣言的に切り替えることはできない。

3.3 tool.uv.sources とマルチインデックス構成の深層分析
Nexus Arkが直面している状況は、以下のような構成が必要となる。

デフォルト: CPU版の llama-cpp-python（abetlen/whl/cpu）

CUDA環境: GPU版の llama-cpp-python（abetlen/whl/cu124等）

uv では、extra-index-url を指定することで複数のリポジトリからパッケージを探索させることができる。しかし、llama-cpp-python の場合、CPU版とCUDA版でパッケージ名（llama_cpp_python）とバージョン番号（例: 0.3.0）が同一であるケースが多い。

Abetlenのリポジトリでは、以下のようにディレクトリが分かれている：

https://.../whl/cpu/llama_cpp_python-0.3.0-cp312-win_amd64.whl

https://.../whl/cu124/llama_cpp_python-0.3.0-cp312-win_amd64.whl

uv（および pip）が複数のインデックスを与えられた場合、バージョンが同じであれば、どちらが優先されるかはリゾルバの実装依存、あるいは不定となる可能性がある。通常、より具体的なバージョン指定（ローカルバージョン識別子 +cu124 など）があれば区別可能だが、llama-cpp-python の配布物が厳密に PEP 440 に準拠したローカルバージョン識別子を付与していない場合（または同一バージョンのバイナリ置換を行っている場合）、競合が発生する。

さらに重要な点として、pyproject.toml に記述された [tool.uv.sources] はプロジェクト全体で静的に適用される。ユーザーAにはソースX、ユーザーBにはソースYを使わせる、という動的な切り替えは、設定ファイルの書き換えなしには実現できない。

3.4 結論：純粋な宣言的構成（Declarative Configuration）の不可能性
以上の調査から、「uv や pip の設定（pyproject.toml）だけで、Windows環境向けにハードウェアを自動検知してインストールさせる」ことは不可能であると断定できる。

これは uv の欠陥ではなく、Pythonパッケージングエコシステム全体の設計思想（再現性と静的な依存定義の重視）によるものである。したがって、解決策は**「静的な定義（依存関係のリスト）」と「動的な選択（どのインデックスから取得するか）」の分離**にある。

すなわち、pyproject.toml には「llama-cpp-python が必要である」という事実のみを記述し、「どのリポジトリからダウンロードするか」という情報は、インストーラー（ランチャー）が実行時に uv コマンドの引数として注入する必要がある。

4. 第2部：Windows特有のエンコーディング問題（CP932 vs UTF-8）の根本対策
Nexus Arkの開発チームが遭遇した UnicodeDecodeError は、Windows版Pythonにおける長年の課題である。暫定対応としての python -S（siteパッケージの読み込みスキップ）は、virtualenv 環境下でのパス設定や .pth ファイルの処理を無効化してしまうため、依存ライブラリが正しくロードされない等の重大な副作用を引き起こすリスクが高く、恒久的な対策としては不適切である。

4.1 UnicodeDecodeError の発生メカニズム：site.py とWindowsファイルシステムの衝突
エラーが発生する箇所は、Pythonの起動処理を担う標準ライブラリ site.py 内の addpackage 関数である。

起動時: Pythonインタプリタが起動すると、site.py が自動的にインポートされる。

パス探索: site.py は site-packages ディレクトリ内の .pth ファイル（パス設定ファイル）を探索し、その内容を読み込んで sys.path に追加しようとする。

エンコーディングの決定: Windows版Python（特にバージョン3.12以前やレガシーモード）では、ファイルシステムのパスやファイル内容を読み込む際、デフォルトでシステムロケール（日本語環境では CP932 / Shift-JIS）を使用する場合がある。

衝突: uv などのモダンなツールは、内部的にUTF-8を一貫して使用する。uv が作成した仮想環境（.venv）内の設定ファイルやパスに、CP932で表現できない文字が含まれている場合、あるいはUTF-8でエンコードされたバイト列をCP932として無理やりデコードしようとした場合に、UnicodeDecodeError が発生する。

特に、ユーザー名に日本語が含まれる場合（例: C:\Users\開発者\...）、このパス自体はCP932で表現可能であっても、uv が生成する設定ファイルや内部処理との間でエンコーディングの不一致が生じやすい。

4.2 Pythonの起動シーケンスとエンコーディング決定プロセス
Python 3.7以降、PEP 540 (Add a new UTF-8 Mode) が導入された。これは、OSのロケール設定に関わらず、Pythonインタプリタの入出力およびファイルシステムエンコーディングを強制的に UTF-8 として扱うモードである。

Windows 10/11には「ベータ: ワールドワイド言語サポートでUnicode UTF-8を使用」というシステム設定が存在するが、これを有効にするとレガシーなWindowsアプリケーション（古い業務アプリやゲーム等）が文字化けを起こす副作用があり、エンドユーザーに設定変更を強いることは推奨されない。

4.3 既存の回避策（python -S）の問題点とリスク
現在採用されている python -S は、「site モジュールの初期化を行わない」というオプションである。これにより site.py が実行されないためクラッシュは回避できるが、以下の副作用がある。

site-packages ディレクトリが sys.path に自動追加されない。

.pth ファイルによるパス操作が無効になる。

ユーザーごとの設定（user_site）が読み込まれない。

これは、標準ライブラリ以外のパッケージ（FastAPIやllama-cpp-python等）をインポートできなくなる可能性が高く、アプリケーションの正常動作を保証できない。

4.4 推奨される根本対策：PYTHONUTF8 環境変数の活用と安全性評価
最も効果的かつ副作用の少ない対策は、アプリケーションのプロセススコープ内でのみ「UTF-8モード」を有効化することである。

具体的には、環境変数 PYTHONUTF8=1 を設定する。

効果: この環境変数が設定された状態でPythonインタプリタが起動すると、Pythonは強制的に「UTF-8モード」で動作する。

影響範囲: site.py を含む全ての標準ライブラリが、ファイルパスやI/OをUTF-8として扱うようになる。これにより、CP932でのデコード試行によるクラッシュが回避される。

安全性: この設定は環境変数を設定したプロセスとその子プロセスにのみ影響するため、ユーザーのシステム全体や他のアプリケーションには一切影響を与えない。

検証結果: 多くの日本語Windows環境におけるPython関連のトラブル（特に pipenv, poetry, uv 等のツール利用時）において、この PYTHONUTF8=1 は「特効薬」として機能することが確認されている。

5. 第3部：先行事例におけるアーキテクチャ分析
Nexus Arkと同様の課題（Python製、LLM利用、Windows配布）を持つ先行プロジェクトが、どのようにこの問題を解決しているか分析する。

5.1 Text-Generation-WebUI (Oobabooga) の「ポータブル環境」戦略
この分野で最も有名なプロジェクトの一つである Text-Generation-WebUI は、非常に泥臭いが確実なアプローチを採用している。

依存管理: requirements.txt を使用しているが、llama-cpp-python などのハードウェア依存パッケージは意図的に除外されている場合がある、あるいはコメントアウトされている。

インストーラー: start_windows.bat というバッチファイルがエントリーポイントとなる。このバッチファイルは、内部で one_click.py というPythonスクリプト（またはシェルスクリプト）を呼び出す。

ハードウェア検出: インストール時に、ユーザーに対して対話形式で「GPUのメーカー（NVIDIA / AMD / Apple / None）」を尋ねる。

インストール実行: ユーザーの回答に基づき、スクリプト内で pip install torch --index-url... や pip install llama-cpp-python... というコマンド文字列を動的に生成し、実行する。

分析: 自動化というよりは「対話型インストール」であり、ユーザーに技術的な判断（GPUの有無）を委ねている。依存解決は pip に丸投げしており、ロックファイルによる厳密なバージョン管理は行われていないことが多い（Conda環境を利用することで緩和している）。

5.2 InvokeAI のインストーラー設計とハードウェア検出ロジック
InvokeAI は、より洗練されたインストーラーを持っている。

ツールチェーン: 最近のバージョンでは uv の採用が進んでいる。

ハードウェア検出: Pythonスクリプトまたはシェルスクリプト内で、単純なコマンド実行（nvidia-smi の有無チェックなど）や、環境変数チェックを行う。

構成管理: pyproject.toml を使用しつつ、GPUサポートのための追加パッケージは、やはりインストールスクリプト側で制御している。

彼らのアプローチから学べるのは、**「インストーラー（ランチャー）こそがハードウェア検出の責務を持つべき」**という設計思想である。アプリケーション本体や pyproject.toml は汎用的な状態を保ち、起動前のレイヤーで環境差異を吸収している。

5.3 スタンドアロン化ツール（PyApp, Briefcase）の適用可能性
Briefcase (BeeWare): Pythonアプリをネイティブインストーラー（MSI等）にパッケージングするツール。しかし、バイナリ依存の激しい llama-cpp-python のようなパッケージを含める場合、クロスコンパイルや環境ごとのWheel選択が非常に困難になる。

PyApp (Astral): uv の開発元が提供する、Pythonアプリを単一バイナリにラップするツール。PyAppは実行時にPythonをダウンロードし、依存関係を解決する機能を持つ。非常に有望だが、現時点では「ハードウェア検出に基づいて依存関係を動的に切り替える」高度なロジックを埋め込む機能は限定的である。

結論: 既存のパッケージングツール単体ではNexus Arkの要件（特定の非公式Wheelリポジトリの動的選択）を完全には満たせないため、薄いラッパー（スクリプト）が必要となる。

6. 第4部：推奨アーキテクチャ「ハイブリッド・ランチャーパターン」の提案
以上の調査に基づき、Nexus Arkに最適な「現代的でスマートな解決策」を提案する。これは、暫定対応の「独自スクリプトによる後入れ」を進化させ、uv の機能を最大限に活用しつつ、ハードウェア検出とエンコーディング対策を統合するアーキテクチャである。

6.1 アーキテクチャ概要：宣言的管理と命令的ブートストラップの分離
システムの構成要素を以下の3つに分離する。

静的定義 (pyproject.toml): アプリケーションの基本依存関係（FastAPI等）と、LLMバックエンドを「オプション（Extras）」として定義。

パッケージマネージャー (uv): 依存関係の解決、インストール、仮想環境管理、ロックファイルの維持を担当。

スマートランチャー (PowerShell): ハードウェア検出、エンコーディング設定、そして uv への「適切なインデックスURL」の引き渡しを担当。

6.2 pyproject.toml の最適化構成
llama-cpp-python をプロジェクトから除外するのではなく、optional-dependencies に含めることで、プロジェクトの一部として管理する。

Ini, TOML
[project]
name = "nexus_ark"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi",
    "uvicorn",
    "python-multipart",
    # その他の共通ライブラリ
]

[project.optional-dependencies]
# ここで定義することで、uv lock の管理対象となる
local-llm = [
    "llama-cpp-python>=0.3.0",
]

[tool.uv]
# PyPIをデフォルトインデックスとして明示
index-url = "https://pypi.org/simple"
6.3 動的ハードウェア検出と uv コマンド構築（PowerShell実装詳細）
従来のバッチファイル（.bat）は機能が貧弱でエラーハンドリングが困難であるため、PowerShellスクリプト（.ps1）の採用を強く推奨する。PowerShellはWMI/CIMへのアクセスが容易で、GPU検出ロジックを堅牢に実装できる。

以下に、提案する Launch.ps1 の実装例を示す。

PowerShell
<#
.SYNOPSIS
    Nexus Ark スマートランチャー
    ハードウェア検出、エンコーディング修正、依存同期を一括管理する
#>

# エラー発生時に停止
$ErrorActionPreference = "Stop"

# --- 1. エンコーディング問題の根本対策 ---
# site.py の UnicodeDecodeError を防ぐため、プロセス全体をUTF-8モード化
$env:PYTHONUTF8 = "1"

# --- 2. ハードウェア検出 (GPU Check) ---
Write-Host "Checking hardware configuration..." -ForegroundColor Cyan
$has_cuda = $false

try {
    # WMIクエリでNVIDIA GPUを検索
    $gpu_info = Get-CimInstance Win32_VideoController
    if ($gpu_info.Name -match "NVIDIA") {
        $has_cuda = $true
        Write-Host "  > NVIDIA GPU Detected: $($gpu_info.Name)" -ForegroundColor Green
    } else {
        Write-Host "  > No NVIDIA GPU detected. Using CPU mode." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  > Hardware detection failed. Fallback to CPU mode." -ForegroundColor Red
}

# --- 3. 適切なインデックスURLの決定 ---
# CPU用（AVX2対応）とCUDA用のインデックスを切り替える
$base_wheel_url = "https://abetlen.github.io/llama-cpp-python/whl"

if ($has_cuda) {
    # CUDA 12.x 用 (ユーザーのドライバに合わせてより詳細な分岐も可能)
    $target_index = "$base_wheel_url/cu124"
    Write-Host "  > Mode: CUDA Accelerated" -ForegroundColor Green
} else {
    # CPU 用
    $target_index = "$base_wheel_url/cpu"
    Write-Host "  > Mode: CPU Only" -ForegroundColor Yellow
}

# --- 4. uv sync による依存同期 ---
# --extra local-llm : pyproject.toml で定義したオプション依存を有効化
# --extra-index-url : 動的に決定したインデックスを追加
Write-Host "Syncing dependencies via uv..." -ForegroundColor Cyan

# uv sync コマンドの構築と実行
# 注意: extra-index-url を渡すことで、uv はロックファイルの制約を守りつつ
# 指定されたリポジトリからバイナリを探す
uv sync --extra local-llm --extra-index-url $target_index

if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Dependency sync failed." -ForegroundColor Red
    Pause
    Exit 1
}

# --- 5. アプリケーション起動 ---
Write-Host "Launching Nexus Ark..." -ForegroundColor Cyan
uv run --module nexus_ark.main
6.4 ユーザー体験（UX）と保守性の向上
このアプローチの利点は以下の通りである。

ワンクリック実行: ユーザーは Launch.ps1（またはそれをラップしたショートカット/EXE）を実行するだけでよい。

冪等性（Idempotency）: uv sync は非常に高速であるため、毎回実行してもコストが低い。もしユーザーがPCを買い替えてGPUが変わった場合でも、次回の起動時に自動的に適切なWheelに置き換わる。

クリーンな構成: pyproject.toml は標準的な記述に留まり、ハードウェア固有の「汚れ」はランチャースクリプト内に閉じ込められる。

エンコーディングの安全性: $env:PYTHONUTF8 = "1" により、ユーザー名にどんな文字が含まれていてもPythonは正常に起動する。

7. 将来展望：Pythonパッケージングエコシステムの進化と「Wheel Variants」
現在、PyTorchチームやNVIDIA、そしてAstral（uv の開発元）などが協力し、"Wheel Variants" という新しい仕様策定に向けた議論が進んでいる。

これが実現すれば、pyproject.toml に以下のような記述が可能になる未来が来るかもしれない。

Ini, TOML
# 将来的な構文のイメージ（現在は未実装）
[dependencies]
torch = { version = "2.0", variant = "cuda", auto-detect = true }
この仕様は、パッケージマネージャーがインストール時にホストマシンのアクセラレータ（CUDAバージョン等）を検出し、適切な「バリアント」を自動選択する仕組みを目指している。しかし、現時点（2025-2026年）ではまだ提案段階あるいは実験的実装に留まっており、プロダクション環境で利用可能な標準機能とはなっていない。

したがって、現時点では本報告書で提案した「ランチャーによる動的注入」が、最も現実的かつモダンなベストプラクティスである。

8. 結論
Nexus Arkの課題に対する調査結果を総括する。

pyproject.toml の限界: ハードウェア条件分岐は記述できない。動的な仕組み（ランチャー）との併用が必須である。

スマートな解決策: uv sync に --extra-index-url を動的に渡すPowerShellランチャーを採用することで、独自スクリプトによる依存管理の断絶を解消し、uv のエコシステムに統合できる。

エンコーディング対策: python -S ではなく、環境変数 PYTHONUTF8=1 をランチャー内で設定することで、日本語パス問題を副作用なく根本解決できる。

推奨されるアクションアイテムは、現在の SetupLocalLLM.bat と setup_local_llm.py を廃止し、第6章で示した Launch.ps1 アーキテクチャへ移行することである。これにより、Windowsユーザーに対する配布の障壁は大幅に低減され、保守性も向上する。


