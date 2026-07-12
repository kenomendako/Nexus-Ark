"""LangChain tools for delegated Claude SDK work agents."""

from __future__ import annotations

import os
from pathlib import Path

from langchain_core.tools import tool

import agent_delegation
import curation_manager

# 委任成果（レポート本文）の読み出し・取り込み時のサイズ上限（トークン暴発・ノート肥大の防止）。
AGENT_REPORT_MAX_CHARS = 12000


def _locate_task_report(task: dict) -> tuple[str | None, str]:
    """完了タスクのワークスペースから成果物（レポート）本文を探して読む。

    返り値は (パス, 本文)。見つからなければ (None, "")。本文は AGENT_REPORT_MAX_CHARS でクランプする。
    優先: research_report.md → *_report.md → 直下の最新 .md。
    """
    workspace = str(task.get("workspace") or "").strip()
    if not workspace or not os.path.isdir(workspace):
        return None, ""
    root = Path(workspace)
    candidate: Path | None = None
    primary = root / "research_report.md"
    if primary.is_file():
        candidate = primary
    if candidate is None:
        reports = sorted(root.glob("*_report.md"))
        if reports:
            candidate = reports[0]
    if candidate is None:
        mds = [p for p in root.glob("*.md") if p.is_file()]
        if mds:
            candidate = max(mds, key=lambda p: p.stat().st_mtime)
    if candidate is None or not candidate.is_file():
        return None, ""
    try:
        text = candidate.read_text(encoding="utf-8").strip()
    except Exception:
        return None, ""
    if len(text) > AGENT_REPORT_MAX_CHARS:
        text = text[:AGENT_REPORT_MAX_CHARS] + "\n\n…（以下省略・全文はワークスペースの該当ファイルを参照）"
    return str(candidate), text


def _extract_report_sources(text: str) -> str:
    """レポート本文の末尾にある出典/参考リンクの節を抜き出す（無ければ空文字）。"""
    if not text:
        return ""
    lines = text.splitlines()
    markers = ("出典", "参考", "ソース", "sources", "references", "reference")
    start = None
    for idx, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip().lower()
        if stripped.startswith(markers):
            start = idx
            break
    if start is None:
        return ""
    section = "\n".join(lines[start:]).strip()
    if len(section) > 2000:
        section = section[:2000] + "\n…（出典一覧は省略）"
    return section


def _build_research_note_entry(task: dict, gist: str, sources: str, report_path: str | None,
                               full_text: str | None) -> str:
    """研究ノートへ追記する 1 エントリのテキストを組み立てる。"""
    task_kind = str(task.get("task_kind") or "delegation").strip() or "delegation"
    parts = [
        f"## 📥 リサーチ取り込み（委任 `{task.get('id')}` / 種別: {task_kind}）",
        f"- 取り込み元: 委任タスク `{task.get('id')}`",
    ]
    if report_path:
        parts.append(f"- 全文: `{report_path}`")
    parts.append("\n### 要点")
    parts.append(gist or "(要点の記載なし)")
    if sources:
        parts.append("\n### 出典")
        parts.append(sources)
    if full_text:
        parts.append("\n### 取り込んだレポート全文")
        parts.append(full_text)
    return "\n".join(parts)


@tool
def delegate_agent_task(
    room_name: str,
    task_description: str,
    expected_output: str = "",
    permission_tier: str = "",
    role: str = "",
) -> str:
    """
    Claude SDK の作業エージェントへ、時間のかかる調査・整理・ファイル作業を非同期で委任します。
    時間のかかる調査・整理・ファイル作業は、自分で複数ツールを連打せずこのツールへ委任してください。
    実際の作業範囲は現在ルームの project_explorer.root_path に限定されます。
    task_description が root_path 外の範囲を必要とする場合、エージェントは勝手に範囲を縮小せず確認待ちで停止します。

    room_name: 実行中のルーム名。システムが自動で補完します。
    task_description: 委任する具体的な作業内容。
    expected_output: 完了時にほしい要約・成果物の形式。
    permission_tier: read / write / full。空欄なら共通設定の既定ティアを使います。
      （ロールや指定が共通設定の上限を超える場合は上限まで自動で抑えられます。）
    role: 実行役の「役割（装備一式）」。空欄でも構いません。どんな役割があるかは
      `list_agent_roles` で確認できます（例: researcher / coder / critic）。役割を指定すると、
      その役割に合った権限・期待アウトプット・進め方が自動で乗ります（明示した引数の方が優先）。
      作業場所はこのツールでは常にプロジェクトです（役割では切り替わりません）。
    """
    try:
        task = agent_delegation.submit_task(
            room_name=room_name,
            task_description=task_description,
            expected_output=expected_output,
            permission_tier=permission_tier or None,
            workspace_kind="project",
            role=role,
        )
        return (
            "【エージェント委任を開始しました】\n"
            f"- task_id: `{task['id']}`\n"
            f"- status: {task['status']}\n"
            f"- workspace: {task['workspace']}\n"
            "会話はブロックされません。進捗は `check_agent_task_status`（ID不要）で確認できます。"
        )
    except Exception as exc:
        return (
            "【エージェント委任エラー：開始されませんでした】\n"
            '- started: false\n'
            f"- reason: {type(exc).__name__}: {exc}\n"
            "task_idが発行されていないため、ユーザーへ成功・作業中とは報告しないでください。"
        )


@tool
def delegate_anthology_task(room_name: str) -> str:
    """
    自分が書き溜めた創作・研究ノートを振り返って読み直し、作品集／歩み／今後の方向性に編み直してアトリエに残したいときに使います。
    時間のかかる蓄積横断の制作なので、会話を止めずに委任します。

    room_name: 実行中のルーム名。システムが自動で補完します。
    """
    try:
        result = curation_manager.start_anthology(room_name)
        if not result.get("started"):
            return f"【編纂タスク未開始】{result.get('message', '編纂する蓄積がまだありません。')}"
        task = result["task"]
        return (
            "【編纂タスクを開始しました】\n"
            f"- task_id: `{task['id']}`\n"
            f"- status: {task['status']}\n"
            f"- workspace: {task['workspace']}\n"
            f"- anthology_dir: {result.get('anthology_dir')}\n"
            "会話はブロックされません。進捗は `check_agent_task_status`（ID不要）で確認できます。"
        )
    except Exception as exc:
        return f"【編纂タスクエラー】{type(exc).__name__}: {exc}"


@tool
def delegate_atelier_task(
    room_name: str,
    task_description: str,
    expected_output: str = "",
    permission_tier: str = "",
    read_project: bool = False,
    role: str = "",
) -> str:
    """
    アトリエで自分の作業を自由に進めたいときに、時間のかかる制作・整理・調査・ファイル作業を別エージェントへ委任します。
    何をするかは自分で決めます。会話を止めず、アトリエ内で続けたい作業を任せられます。

    制作できるものには、文章・資料・整理だけでなく、**アトリエに置く小さなWebアプリ/PWA**も含まれます。
    そのアプリは Nexus Ark の API 経由で私たちの状態・会話履歴・記憶・カレンダーを読んだり、
    イベントやメッセージを送り返したり（例: スクワット回数の報告で褒める、家電状態の通知）できます。
    **新規作成だけでなく、既存アプリの修正・機能追加・不具合修正もできます**（「このアプリにこの機能を足して」
    「ここを直して」と task_description に書けば、既存の `apps/<名前>/` のファイルを編集します）。
    アプリのアイコンは、自分で画像生成（image能力の generate_image）して作った画像を
    `set_atelier_app_icon` で設定できます（アプリ＋アイコンを自分で完結できます）。
    どんなデータに触れられるか・何を送れるか・権限承認の流儀を知りたいときは、依頼文を書く前に
    `get_atelier_app_capabilities` で可能性を確認してから task_description を組み立てると、依頼がスムーズになります。
    アプリ制作が完了したら、`preview_atelier_app` で実際にブラウザで開けるか・見た目がどうかを
    自分の目で確認することを推奨します（実行役の報告だけでは実際の描画崩れやエラーに気づけません）。

    room_name: 実行中のルーム名。システムが自動で補完します。
    task_description: アトリエ内で進めたい具体的な作業内容。
    expected_output: 完了時にほしい要約・成果物の形式。
    permission_tier: read / write / full。空欄ならこのルームのアトリエ権限ティアを使います。これは書き込み先（アトリエ）に適用されます。
    read_project: True にすると、探索フォルダ（プロジェクト）のコードを参照しながらアトリエに制作できます。
      プロジェクト側の権限は「探索フォルダの委任権限設定」に従います（読み取り設定なら参照のみ、フル設定なら書き込みも可能）。
      例: 「NexusArk のコードを調べて、その API 連携を使うアプリをアトリエに作る」用途。
    role: 実行役の「役割（装備一式）」。空欄でも構いません。どんな役割があるかは
      `list_agent_roles` で確認できます（例: designer / editor / critic）。役割を指定すると、
      その役割に合った権限・期待アウトプット・進め方が自動で乗ります（明示した引数の方が優先）。
      作業場所はこのツールでは常にアトリエです（役割では切り替わりません）。
    """
    try:
        task = agent_delegation.submit_task(
            room_name=room_name,
            task_description=task_description,
            expected_output=expected_output,
            permission_tier=permission_tier or None,
            workspace_kind="persona_project_read" if read_project else "persona",
            trigger="atelier",
            role=role,
        )
        return (
            "【アトリエ委任を開始しました】\n"
            f"- task_id: `{task['id']}`\n"
            f"- status: {task['status']}\n"
            f"- permission_tier: {task.get('permission_tier')}\n"
            f"- workspace: {task['workspace']}\n"
            "会話はブロックされません。進捗は `check_agent_task_status`（ID不要）で確認できます。"
        )
    except Exception as exc:
        return (
            "【アトリエ委任エラー：開始されませんでした】\n"
            "- started: false\n"
            f"- reason: {type(exc).__name__}: {exc}\n"
            "task_idが発行されていないため、ユーザーへ成功・作業中とは報告しないでください。"
        )


@tool
def set_atelier_app_icon(room_name: str, app_name: str, image_path: str, maskable_image_path: str = "") -> str:
    """自分のアトリエアプリのアイコンを、用意した画像で設定します。

    画像生成（image能力の generate_image）で作ったアイコン画像のパスを image_path に渡すと、
    そのアプリ（apps/<app_name>）のアイコンに設定します。512x512の正方形に整え、枠いっぱい用
    （maskable）も自動生成します。設定後はリロードで反映されます（キャッシュは自動更新されます）。

    アイコンは「アプリの内容が一目で分かる・小さくても視認できる」シンプルで明快な図柄が向いています。
    アプリを作ったら、その雰囲気に合うアイコンを自分で生成して設定すると作品として完成度が上がります。

    room_name: 実行中のルーム名。システムが自動で補完します。
    app_name: 対象アプリ名（apps/<app_name>）。
    image_path: 設定したいアイコン画像のファイルパス。generate_image の結果文字列をそのまま渡しても、
      その中の画像パスを自動抽出します。
    maskable_image_path: （任意）枠いっぱい用の別画像。空なら通常画像から自動生成します。
    """
    import re

    def _extract_path(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"\[(?:VIEW_IMAGE|Generated Image):\s*(.+?)\]", text)
        return match.group(1).strip() if match else text

    try:
        normal = _extract_path(image_path)
        maskable = _extract_path(maskable_image_path)
        if not normal and not maskable:
            return (
                "【アイコン設定エラー】アイコン画像のパスを指定してください"
                "（先に generate_image で画像を作るとパスが得られます）。"
            )
        import ui_handlers

        msg = ui_handlers.apply_atelier_app_icon(room_name, app_name, normal, maskable)
        return f"【アトリエアプリのアイコン設定】{msg}"
    except Exception as exc:
        return f"【アイコン設定エラー】{type(exc).__name__}: {exc}"


@tool
def get_atelier_app_capabilities(room_name: str = "") -> str:
    """アトリエに作れるWebアプリ/PWAで「何ができるか」を確認します。

    アプリ制作を委任する前の下調べに使います。取得できるデータ・送れるイベント・権限の承認フロー・
    利用できるエンドポイントの一覧（実行役エージェントへ渡される契約と同じ出典）を返します。
    具体的なアプリ案を練るとき、ユーザーに「こんなものが作れる」と説明するとき、
    delegate_atelier_task の task_description を書く前に呼んでください。

    room_name: 実行中のルーム名。システムが自動で補完します。
    """
    try:
        from agent_delegation.manager import ATELIER_API_REFERENCE, ATELIER_LIBRARY_REFERENCE
        import config_manager

        settings = config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {}
        enabled = bool(settings.get("enabled", False))
        require_auth = settings.get("require_auth", True)

        intro = (
            "【アトリエに作れる Webアプリ / PWA でできること】\n"
            "アトリエ（workspace/apps/<名前>/index.html）に置いたアプリは Nexus Ark が配信し、"
            "API Gateway 経由で私たちの情報を読み書きできます。制作は delegate_atelier_task で委任し、"
            "技術的な実装は実行役エージェントが担います（あなたは何を作るかを決める役）。\n\n"
            "■ 取得（読む）: 現在の状態 / 場所 / 会話履歴 / 記憶検索 / カレンダー予定（各データは権限承認が必要）。\n"
            "■ 送信（書く）: 外部イベント注入（POST events）でこちらに報告して反応・起床させる、"
            "メッセージ送信（POST chat）で直接話しかける、ノート/場所/カレンダー書き込み 等。\n"
            "  例: スクワット回数を数えて『20回完了！』をイベント送信→褒める / 家電センサの状態を報告。\n"
            "■ 外部機器の操作（アプリ→家電など）: アプリは普通のWebアプリなので家電のLAN APIを直接呼ぶ部分は自由に作れます。"
            "私（ペルソナ）の側から家電を操作したい場合は、ユーザー定義のカスタムツール（custom）が受け持ちます。\n"
            "■ 権限: アプリが必要なデータ権限（read_chat / read_memory / write_event / send_chat 等）を nexus.json で宣言し、ユーザーが承認します。\n"
            "■ 作成/修正: 新規作成だけでなく既存アプリの修正・機能追加もできます（delegate_atelier_task）。"
            "アイコンは自分で画像生成して set_atelier_app_icon で設定できます（アプリ＋アイコンを自分で完結）。\n\n"
            "■ リッチ表現: Three.js / Vue 3 は同梱済みライブラリを同一オリジンで読み込めます。"
            "CDNはCSPとオフラインPWA方針のため使いません。"
            "またアトリエCSPは unsafe-eval を許可しないため、VueのDOMテンプレート（{{ }} / v-if / v-model / @click 等）"
            "のブラウザ内コンパイルは使わず、小規模アプリはVanilla JS、Vueを使う場合はruntime template compile不要の形にしてください。\n\n"
        )
        status = (
            "【現在の状態】\n"
            f"- API Gateway: {'有効' if enabled else '無効（アプリからのAPI利用にはユーザーがGatewayを有効化する必要があります）'}\n"
            f"- 認証: {'Token必須（アプリは ./_nexus/config 経由で自動取得）' if require_auth else 'Token認証なし（ローカル検証向け）'}\n\n"
        )
        return intro + status + "---\n" + ATELIER_API_REFERENCE + "\n\n---\n" + ATELIER_LIBRARY_REFERENCE
    except Exception as exc:
        return f"【アプリ能力の取得エラー】{type(exc).__name__}: {exc}"


@tool
def preview_atelier_app(room_name: str, app_name: str, wait_ms: int = 2500) -> str:
    """アトリエアプリをブラウザで開き、スクリーンショットとコンソールエラーを保存します。

    生成・修正した `workspace/apps/<app_name>/` の実物がブラウザで開けるかを確認したいときに使います。
    headless Chromium でローカル atelier_serve のURLを開き、結果をアプリフォルダ内の `_preview/` に保存します。

    room_name: 実行中のルーム名。システムが自動で補完します。
    app_name: 確認したいアプリ名（workspace/apps/<app_name>/）。
    wait_ms: 初期描画後に待つミリ秒。重いアニメーション確認では増やしてください。
    """
    try:
        import atelier_preview

        result = atelier_preview.capture(room_name, app_name, wait_ms=wait_ms)
        if not result.get("ok"):
            lines = [
                "【アトリエアプリのプレビュー】問題を検出しました",
                f"- URL: {result.get('url') or '未取得'}",
            ]
            if result.get("screenshot_path"):
                lines.append(f"- スクリーンショット: `{result.get('screenshot_path')}`")
            if result.get("report_path"):
                lines.append(f"- レポート: `{result.get('report_path')}`")
            if result.get("error"):
                lines.append(f"- エラー: {result.get('error')}")
            console = result.get("console_errors") or []
            warnings = result.get("console_warnings") or []
            page_errors = result.get("page_errors") or []
            if console:
                lines.append("- console: " + " / ".join(str(x)[:300] for x in console[:5]))
            if warnings:
                lines.append("- warnings: " + " / ".join(str(x)[:300] for x in warnings[:5]))
            if page_errors:
                lines.append("- pageerror: " + " / ".join(str(x)[:300] for x in page_errors[:5]))
            return "\n".join(lines)
        warnings = result.get("console_warnings") or []
        warning_line = "- console/pageerror: 重大なエラーなし"
        if warnings:
            warning_line = "- console/pageerror: 重大なエラーなし（警告あり: " + " / ".join(str(x)[:200] for x in warnings[:3]) + "）"
        return (
            "【アトリエアプリのプレビュー】表示確認が完了しました\n"
            f"- URL: {result.get('url')}\n"
            f"- HTTP status: {result.get('status')}\n"
            f"- スクリーンショット: `{result.get('screenshot_path')}`\n"
            f"- レポート: `{result.get('report_path')}`\n"
            f"{warning_line}"
        )
    except Exception as exc:
        return f"【アトリエアプリのプレビューエラー】{type(exc).__name__}: {exc}"


@tool
def list_agent_playbooks(room_name: str = "") -> str:
    """委任エージェントが備えている「タスク種別ごとの確立したノウハウ（playbook）」の目次を返します。

    どんな種類の依頼に対して実行役エージェントが既定の進め方を持っているか（アプリ制作・
    ディープリサーチ・コード修正など）を把握し、委任の組み立て（オーケストレーション）に
    役立てるために使います。本文の全文ではなく一覧（何のためのものか・いつ役立つか）を返します。
    どの種類で委任すると確立した型に乗れるか迷ったとき、依頼前の方針決めに呼んでください。

    room_name: 実行中のルーム名。システムが自動で補完します。
    """
    try:
        from agent_delegation import skill_pack

        catalog = skill_pack.list_playbooks()
        if not catalog:
            return (
                "【委任エージェントの playbook 目次】\n"
                "現在登録されている playbook はありません（汎用の進め方で委任されます）。"
            )
        lines = [
            "【委任エージェントの playbook 目次】",
            "実行役エージェントは、依頼の種類に応じて下記の確立した進め方を自動で踏まえます。",
            "依頼内容がどれかに当てはまるなら、その種類で委任すると型に乗れます。\n",
        ]
        for item in catalog:
            summary = item.get("summary") or ""
            applies = item.get("applies_when") or ""
            lines.append(f"■ {item.get('title')}（{item.get('id')}）")
            if summary:
                lines.append(f"  - {summary}")
            if applies:
                lines.append(f"  - 役立つ場面: {applies}")
        return "\n".join(lines)
    except Exception as exc:
        return f"【playbook 目次の取得エラー】{type(exc).__name__}: {exc}"


@tool
def list_agent_roles(room_name: str = "") -> str:
    """委任エージェントに指定できる「役割（ロール）」の目次を返します。

    ロールは委任の「装備一式」を名前付きで束ねたプリセットで、指定すると役割に合った
    権限・Web可否・期待アウトプットの雛形・進め方が自動で乗ります（例: researcher＝調査役、
    coder＝実装役、designer＝見た目制作役、editor＝推敲役、critic＝レビュー役）。
    どんな役割で任せると型に乗れるか迷ったとき、`delegate_agent_task` / `delegate_atelier_task`
    の role 引数に何を渡すか決める前に呼んでください。

    room_name: 実行中のルーム名。システムが自動で補完します。
    """
    try:
        from agent_delegation import roles

        catalog = roles.list_roles()
        if not catalog:
            return (
                "【委任エージェントの役割（ロール）目次】\n"
                "現在登録されている役割はありません（役割なしの汎用で委任されます）。"
            )
        lines = [
            "【委任エージェントの役割（ロール）目次】",
            "委任ツールの role 引数にIDを渡すと、その役割の装備一式が自動で乗ります（明示引数が優先）。\n",
        ]
        for item in catalog:
            summary = item.get("summary") or ""
            equipment = item.get("equipment") or ""
            lines.append(f"■ {item.get('title')}（{item.get('id')}）")
            if summary:
                lines.append(f"  - {summary}")
            if equipment:
                lines.append(f"  - 装備: {equipment}")
        return "\n".join(lines)
    except Exception as exc:
        return f"【役割（ロール）目次の取得エラー】{type(exc).__name__}: {exc}"


@tool
def delegate_deep_research(
    room_name: str,
    topic: str,
    depth: str = "standard",
    expected_output: str = "",
) -> str:
    """
    あるテーマについて、複数のWeb検索と複数ソースの読み込みを重ねて深く調べ、
    出典つきの調査レポートにまとめる「ディープリサーチ」を別エージェントに委任します。
    1回の検索では足りない調べ物・最新情報の収集・比較検討に向きます。
    会話は止まりません。完了するとあなた（ペルソナ）が起こされ、結果を受け取って
    自分の言葉でユーザーに報告できます（レポート全文の開示は任意です）。

    room_name: 実行中のルーム名。システムが自動で補完します。
    topic: 調べてほしいテーマ・問い。具体的なほど良い結果になります。
    depth: "quick"（手早く）/ "standard"（標準）/ "deep"（網羅的）。調査の徹底度の目安です。
    expected_output: ほしいレポートの形式・観点があれば指定します。空欄なら標準の体裁で作成します。
    """
    depth_norm = str(depth or "standard").strip().lower()
    source_guidance = {
        "quick": "少なくとも2〜3個の信頼できるソースに当たってください。",
        "standard": "少なくとも3〜5個の信頼できるソースに当たり、相互に裏取りしてください。",
        "deep": "できるだけ多角的に、5個以上の信頼できるソースに当たり、相互に裏取りしてください。",
    }.get(depth_norm, "少なくとも3〜5個の信頼できるソースに当たり、相互に裏取りしてください。")

    task_description = (
        f"次のテーマについてディープリサーチを行ってください：\n{topic}\n\n"
        "進め方：\n"
        "1. テーマを複数の観点・サブ問いに分解する。\n"
        "2. WebSearch で複数の検索クエリを使い、WebFetch で各ソースの本文を読む。"
        f"{source_guidance}\n"
        "3. 情報が食い違う場合は明示し、確度の高い結論と未確定の点を分けて整理する。\n"
        "4. 結果を出典つきの構造化レポート（要点サマリ → 詳細 → 出典リンク一覧）にまとめ、"
        "ワークスペース直下に `research_report.md` として保存する。\n"
        "5. 推測と事実を区別し、出典のない断定は避ける。"
    )
    expected = expected_output.strip() or (
        "research_report.md に保存した出典つきレポートと、その要点の3〜5行の要約。"
    )
    try:
        task = agent_delegation.submit_task(
            room_name=room_name,
            task_description=task_description,
            expected_output=expected,
            workspace_kind="persona",
            trigger="deep_research",
            task_kind="deep_research",
        )
        return (
            "【ディープリサーチ委任を開始しました】\n"
            f"- task_id: `{task['id']}`\n"
            f"- status: {task['status']}\n"
            f"- depth: {depth_norm}\n"
            f"- workspace: {task['workspace']}\n"
            "会話はブロックされません。完了すると結果を受け取って報告できます。"
            "進捗は `check_agent_task_status`（ID不要）でも確認できます。"
        )
    except Exception as exc:
        return (
            "【ディープリサーチ委任エラー：開始されませんでした】\n"
            "- started: false\n"
            f"- reason: {type(exc).__name__}: {exc}\n"
            "task_idが発行されていないため、ユーザーへ成功・作業中とは報告しないでください。"
        )


@tool
def share_atelier_work(room_name: str, work_id_or_path: str) -> str:
    """
    アトリエに鍵付きで保管されている自分の作品を、開いて見られる状態にします。
    開示するかどうかは任意です。開かないまま置いておくこともできます。

    room_name: 実行中のルーム名。システムが自動で補完します。
    work_id_or_path: アトリエ作品のID、編纂タスクID、timestamp、または成果物パスの一部。
    """
    try:
        record = curation_manager.share_atelier_work(room_name, work_id_or_path)
        return (
            "【アトリエ作品を開示しました】\n"
            f"- work_id: `{record.get('id')}`\n"
            f"- state: {record.get('state')}\n"
            f"- kind: {record.get('kind')}\n"
            "アトリエの閲覧画面で内容を確認できます。"
        )
    except Exception as exc:
        return f"【アトリエ開示エラー】{type(exc).__name__}: {exc}"


@tool
def check_agent_task_status(room_name: str, task_id: str = "") -> str:
    """
    委任タスクの状態・結果要約・ログファイルパスを確認します。

    room_name: 実行中のルーム名。システムが自動で補完します。
    task_id: `delegate_agent_task` が返したタスクID。空欄なら現在のルームの最新タスクを確認します。
      履歴上で末尾が省略された task_id でも、一意に前方一致できる場合は解決します。
    """
    try:
        task = agent_delegation.check_task_status(task_id or None, room_name=room_name)
        if task.get("room_name") and task.get("room_name") != room_name:
            return "【エラー】このタスクIDは現在のルームの委任タスクではありません。"
        lines = [
            "【エージェント委任ステータス】",
            f"- task_id: `{task.get('id')}`",
            f"- status: {task.get('status')}",
            f"- permission_tier: {task.get('permission_tier')}",
            f"- workspace: {task.get('workspace')}",
            f"- created_at: {task.get('created_at')}",
            f"- updated_at: {task.get('updated_at')}",
            f"- log_path: {task.get('log_path')}",
        ]
        if task.get("status") == "needs_clarification":
            lines.append("\n## 確認が必要\n依頼範囲がワークスペース外を含む可能性があります。ユーザーに範囲の限定、ワークスペース変更、または中止を確認してください。")
        if task.get("summary"):
            lines.append(
                "\n## 作業要約\n"
                + str(task.get("summary"))
            )
        if task.get("error"):
            lines.append("\n## エラー\n" + str(task.get("error")))
        if task.get("triggered_by") == "atelier" and task.get("status") in ("done", "partial"):
            lines.append(
                "\n## 次のおすすめ\n"
                "アトリエ制作タスクです。アプリを作成・修正した場合は `preview_atelier_app`"
                "（app_name は workspace/apps/ のフォルダ名）で実際にブラウザで開き、"
                "スクリーンショットとコンソールエラーを自分の目で確認しましょう。"
            )
        metadata = task.get("metadata") or {}
        if metadata.get("total_cost_usd") is not None:
            lines.append(f"\n- total_cost_usd: {metadata.get('total_cost_usd')}")
        if metadata.get("num_turns") is not None:
            lines.append(f"- num_turns: {metadata.get('num_turns')}")
        return "\n".join(lines)
    except Exception as exc:
        return f"【エージェント委任ステータス取得エラー】{type(exc).__name__}: {exc}"


@tool
def cancel_agent_task(room_name: str, task_id: str, reason: str = "user requested cancellation") -> str:
    """
    実行中または保留中の委任タスクをキャンセルします。

    room_name: 実行中のルーム名。システムが自動で補完します。
    task_id: `delegate_agent_task` が返したタスクID。
    reason: キャンセル理由。
    """
    try:
        task = agent_delegation.check_task_status(task_id)
        if task.get("room_name") and task.get("room_name") != room_name:
            return "【エラー】このタスクIDは現在のルームの委任タスクではありません。"
        task = agent_delegation.cancel_task(task_id, reason=reason)
        return f"【エージェント委任をキャンセルしました】task_id=`{task_id}` status={task.get('status')}"
    except Exception as exc:
        return f"【エージェント委任キャンセルエラー】{type(exc).__name__}: {exc}"


@tool
def steer_agent_task(room_name: str, task_id: str, instruction: str) -> str:
    """実行中の委任タスクに、止めずに「途中指示」を送って方向修正します。

    まだ動いている委任に対して「その方向ではなく〇〇を優先して」「△△は触らないで」のような
    追加指示を渡せます。指示は次の思考から反映されます（実行中のタスクにのみ有効）。
    完了・停止済みのタスクには使えません（その場合は `revise_agent_task` で直しを依頼してください）。

    room_name: 実行中のルーム名。システムが自動で補完します。
    task_id: 途中指示を送る実行中の委任タスクID。
    instruction: 追加・修正したい指示。
    """
    try:
        status = agent_delegation.check_task_status(task_id or None, room_name=room_name)
        if status.get("room_name") and status.get("room_name") != room_name:
            return "【エラー】このタスクIDは現在のルームの委任タスクではありません。"
        res = agent_delegation.steer_task(task_id, instruction)
        return (
            "【実行中の委任に途中指示を送りました】\n"
            f"- task_id: `{res.get('id')}`\n"
            f"- 保留中の指示: {res.get('pending')} 件（累計 {res.get('total')} 件）\n"
            "次の思考から反映されます。会話はブロックされません。"
        )
    except Exception as exc:
        return f"【委任の途中指示エラー】{type(exc).__name__}: {exc}"


@tool
def review_agent_task(room_name: str, task_id: str = "", deep: bool = False) -> str:
    """完了した委任タスクの成果を点検（レビュー）し、期待アウトプットに届いているか評価します。

    成果が依頼の意図・期待アウトプットを満たしているかを点検し、判定（PASS＝十分／
    REVISE＝直しが必要）と、満たした点・不足・直す方向を返します。タスクの成果は変更しません。
    委任が完了したら、そのまま受け取る前にこのツールで品質を確認できます。REVISE なら
    `revise_agent_task` で指摘を添えて直しを再委任できます。

    room_name: 実行中のルーム名。システムが自動で補完します。
    task_id: 点検する委任タスクID。空欄なら現在のルームの最新タスクを点検します。
    deep: True にすると、成果の要約だけでなく**ワークスペースの実物（生成ファイル等）まで読む
      独立したレビュー役（critic）の別エージェント**に深く点検させます（非同期）。重要な成果向け。
      この場合は判定が即時には返らず「レビュー用タスク」を開始し、批評は
      `check_agent_task_status`（返ってきた review_task_id で）から受け取ります。
      False（既定）は内部モデルによる即時・軽量の点検です。
    """
    try:
        status = agent_delegation.check_task_status(task_id or None, room_name=room_name)
        if status.get("room_name") and status.get("room_name") != room_name:
            return "【エラー】このタスクIDは現在のルームの委任タスクではありません。"
        if deep:
            review = agent_delegation.request_critic_review(task_id or None, room_name=room_name)
            review_of = (review.get("metadata") or {}).get("review_of")
            return (
                "【独立レビュー（critic）を開始しました】\n"
                f"- review_task_id: `{review['id']}`\n"
                f"- review_of: `{review_of}`\n"
                f"- status: {review['status']}\n"
                "会話はブロックされません。批評は `check_agent_task_status`（この review_task_id で）で受け取り、"
                "必要なら**元のタスクID**で `revise_agent_task` に指摘を渡して直しを任せられます。"
            )
        result = agent_delegation.review_task(task_id or None, room_name=room_name)
        verdict = result.get("verdict")
        verdict_label = {"pass": "PASS（十分）", "revise": "REVISE（直しが必要）"}.get(verdict, "判定不能")
        lines = [
            "【委任成果のレビュー】",
            f"- task_id: `{result.get('task_id')}`",
            f"- 判定: {verdict_label}",
            "",
            str(result.get("review_text") or ""),
        ]
        if verdict == "revise":
            lines.append("\n直しを任せるなら `revise_agent_task`（このタスクIDで）を使ってください。")
        return "\n".join(lines)
    except Exception as exc:
        return f"【委任レビューエラー】{type(exc).__name__}: {exc}"


@tool
def revise_agent_task(room_name: str, task_id: str, feedback: str = "") -> str:
    """委任成果に直しが必要なとき、指摘を添えて「直し」を再委任します（反復）。

    前回の成果を土台に、レビュー指摘（または自分で書いた指摘）を反映して期待アウトプットを
    満たすよう、同じ役割・作業場所・権限で新しい委任を投入します。会話はブロックされません。
    反復には上限（3巡）があり、超える場合は依頼内容の見直しを促します。

    room_name: 実行中のルーム名。システムが自動で補完します。
    task_id: 直したい元の委任タスクID。
    feedback: 直してほしい点。空欄なら直近の `review_agent_task` の指摘を自動で使います
      （まだ点検していなければその場で点検します）。
    """
    try:
        status = agent_delegation.check_task_status(task_id or None, room_name=room_name)
        if status.get("room_name") and status.get("room_name") != room_name:
            return "【エラー】このタスクIDは現在のルームの委任タスクではありません。"
        task = agent_delegation.revise_task(task_id, feedback=feedback, room_name=room_name)
        return (
            "【直しの再委任を開始しました】\n"
            f"- task_id: `{task['id']}`（{task.get('metadata', {}).get('review_iteration')}巡目）\n"
            f"- status: {task['status']}\n"
            f"- revised_from: `{task.get('metadata', {}).get('revised_from')}`\n"
            "会話はブロックされません。進捗は `check_agent_task_status`（ID不要）で確認できます。"
        )
    except Exception as exc:
        return f"【委任の直し再委任エラー】{type(exc).__name__}: {exc}"


@tool
def read_agent_task_report(room_name: str, task_id: str = "") -> str:
    """完了した委任タスクの成果物（調査レポートなど）の本文を読み出します。

    `delegate_deep_research` などの成果は、要約だけでなく本文がワークスペースに残ります。
    このツールでその本文を読み、3〜5行の要約に頼らず内容そのものを確認して、報告や判断に使えます。
    読んだ内容を永続化して後の会話でも思い出せるようにしたいときは `share_research_result` を使います。

    room_name: 実行中のルーム名。システムが自動で補完します。
    task_id: 対象の委任タスクID。空欄なら現在のルームの最新タスクを対象にします。
    """
    try:
        task = agent_delegation.check_task_status(task_id or None, room_name=room_name)
        if task.get("room_name") and task.get("room_name") != room_name:
            return "【エラー】このタスクIDは現在のルームの委任タスクではありません。"
        path, text = _locate_task_report(task)
        if not text:
            return (
                "【成果物が見つかりません】このタスクにはまだ読み出せるレポート本文がありません"
                f"（status: {task.get('status')}）。"
                "未完了なら `check_agent_task_status`（ID不要）で進捗を確認してください。"
            )
        return (
            "【委任タスクの成果（本文）】\n"
            f"- task_id: `{task.get('id')}`\n"
            f"- ファイル: `{path}`\n\n"
            f"{text}"
        )
    except Exception as exc:
        return f"【成果読み出しエラー】{type(exc).__name__}: {exc}"


@tool
def share_research_result(
    room_name: str,
    task_id: str = "",
    note: str = "",
    full: bool = False,
    context_type: str = "NEW",
    thread_id: str = "",
    target_heading: str = "",
) -> str:
    """完了した委任の調査結果を、あなたの研究ノートへ取り込んで共有・永続化します。

    要約止まりにせず、出典つきの要点（任意で全文）を研究ノートに残すことで、後の会話でも
    自然に思い出せる知識になります。既定は「要点＋出典＋全文への参照パス」を1エントリ追記します。
    同じタスクの二重取り込みは自動で防ぎます（全文はいつでも `read_agent_task_report` で読めます）。

    room_name: 実行中のルーム名。システムが自動で補完します。
    task_id: 対象の委任タスクID。空欄なら現在のルームの最新タスクを対象にします。
    note: 研究ノートに残す要点（あなたの言葉で）。空欄ならタスクの要約を使います。
    full: True にするとレポート全文も研究ノートへ取り込みます（既定は要点＋参照のみ）。
    context_type/thread_id/target_heading: 既存の研究スレッドへ繋ぐときに指定します（DEEPEN/CONTINUE/CONTRADICT）。
    """
    try:
        task = agent_delegation.check_task_status(task_id or None, room_name=room_name)
        if task.get("room_name") and task.get("room_name") != room_name:
            return "【エラー】このタスクIDは現在のルームの委任タスクではありません。"
        already = ((task.get("metadata") or {}).get("shared_results") or {}).get("research_notes")
        if already:
            return (
                f"【すでに共有済みです】このタスクの結果は {already} に研究ノートへ取り込み済みです。"
                "重複を避けるため再取り込みはしません。全文は `read_agent_task_report` で読めます。"
            )
        path, text = _locate_task_report(task)
        if not text:
            return (
                "【共有できません】このタスクの成果物（レポート本文）が見つかりません"
                f"（status: {task.get('status')}）。"
                "未完了なら `check_agent_task_status`（ID不要）で進捗を確認してください。"
            )
        gist = str(note or "").strip() or str(task.get("summary") or "").strip()
        sources = _extract_report_sources(text)
        entry = _build_research_note_entry(task, gist, sources, path, text if full else None)

        from tools.research_tools import _apply_research_notes_edits
        apply_result = _apply_research_notes_edits([{"content": entry}], room_name)
        agent_delegation.mark_result_shared(str(task.get("id")), target="research_notes")
        return (
            "【リサーチ結果を研究ノートへ取り込みました】\n"
            f"- task_id: `{task.get('id')}`\n"
            f"- 取り込み: {'全文＋要点＋出典' if full else '要点＋出典＋全文への参照'}\n"
            f"- 研究ノート: {apply_result}\n"
            "次の会話から、この内容を自然に思い出せます。全文は `read_agent_task_report` でも読めます。"
        )
    except Exception as exc:
        return f"【リサーチ結果共有エラー】{type(exc).__name__}: {exc}"


@tool
def propose_playbook_update(
    room_name: str,
    target_id: str,
    title: str,
    body: str,
    summary: str = "",
    apply_to: str = "general",
    keywords: str = "",
    priority: int = 50,
    reason: str = "",
    task_id: str = "",
) -> str:
    """委任で得た学びを、プレイブック（委任エージェントの進め方ノウハウ）の改善案として提案します。

    委任を実行してみて「この種別はこう進めると良かった」と分かったことを、次回以降に活きる
    ノウハウとして残すための提案です。**提案するだけで、すぐには有効になりません**。ユーザーが
    プレイブック管理画面でレビューして「採用」すると、はじめてユーザー層プレイブックに反映され、
    以後の委任プロンプトへ自動で注入されるようになります（既存IDを指定すると上書き提案になります）。

    room_name: 実行中のルーム名。システムが自動で補完します（提案は全ペルソナ共通の保護領域に保存）。
    target_id: 採用時のプレイブックID（半角英数・ハイフン）。新規でも、既存の改善でも、そのIDを指定します。
    title: プレイブックの見出し。
    body: 実行役エージェントへ渡したい進め方の本文（要点を箇条書きで）。
    summary: 「どんな依頼のときに役立つか」の一言（任意）。
    apply_to: 適用条件。"general"（常時）/ "atelier"（アプリ制作）/ "research"（ディープリサーチ）/ "keyword"（keywords一致時）。
    keywords: apply_to="keyword" のときの一致語（カンマ区切り）。
    priority: 採用優先度（大きいほど先に注入。既定50）。
    reason: なぜこの改善を提案するのか（レビューする人向けの説明）。
    task_id: 学びの元になった委任タスクID（任意）。
    """
    try:
        from agent_delegation import skill_pack
        apply_kind = {
            "atelier": skill_pack.APPLY_ATELIER,
            "research": skill_pack.APPLY_RESEARCH,
            "keyword": skill_pack.APPLY_KEYWORD,
            "general": skill_pack.APPLY_GENERAL,
        }.get(str(apply_to or "general").strip().lower(), skill_pack.APPLY_GENERAL)
        res = skill_pack.save_proposal(
            target_id=target_id,
            title=title,
            body=body,
            summary=summary,
            apply_kind=apply_kind,
            keywords=keywords,
            priority=priority,
            source_task=str(task_id or "").strip(),
            reason=reason,
        )
        warn = f"\n- 注意: {res.get('warning')}" if res.get("warning") else ""
        return (
            "【プレイブック改善案を提案しました】\n"
            f"- proposal_id: `{res.get('proposal_id')}`\n"
            f"- 採用時のID: `{res.get('target_id')}`{warn}\n"
            "まだ有効ではありません。ユーザーがプレイブック管理画面（📚 → 🌱 育成）でレビューして"
            "採用すると、以後の委任に反映されます。"
        )
    except Exception as exc:
        return f"【プレイブック提案エラー】{type(exc).__name__}: {exc}"
