"""委任エージェントの「役割（ロール）プロファイル」。

ロール = 委任の「装備一式」を名前付きで束ねた再利用プリセット。ペルソナが
「リサーチャーとして」「デザイナーとして」と一言ロールを指定すると、その役割に
ふさわしい既定値（タスク種別・権限ティア・Web可否・期待アウトプット・進め方）が
まとめて乗る。

プレイブック（skill_pack）と同じ 2 層構成：

- 運営層: `agent_roles/*.md`（リポジトリ同梱・コード資産。アプリ更新で最新化）。
- ユーザー層: `characters/_shared/memory/agent_roles/*.md`（各環境固有・全ペルソナ共通。
  更新で消えない保護領域。ユーザーが自分で追加・編集できる）。

両層をマージし、`id` 衝突時は**ユーザー層を優先**（運営ロールを上書き・無効化できる）。

- 純標準ライブラリのみ。フロントマターは依存追加を避ける極小の自前パーサで解釈する。
- 読み込み失敗・非存在は「ロール無し」として安全に握りつぶす（委任のクリティカルパスにしない）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import constants

logger = logging.getLogger(__name__)

_ROLES_DIRNAME = "agent_roles"
# ユーザー追加ロールの置き場（共通スキル/プレイブックと同じ保護領域）。
_USER_ROLES_SUBPATH = ("_shared", "memory", "agent_roles")

_VALID_TIERS = {"read", "write", "full"}
_VALID_WORKSPACE_KINDS = {"project", "persona", "persona_project_read"}
_VALID_MODEL_HINTS = {"fast", "balanced", "deep"}

_USER_ROLES_README = """\
このフォルダは「ユーザー追加ロール」の置き場です。
委任エージェント（依頼を実行する裏方）に渡す「装備一式」を、名前付きで束ねて自分で足せます。

- 1ロール = 1つの .md ファイル。フロントマター（--- で囲む）＋本文。
- 運営同梱の agent_roles/ とマージされ、同じ id ならこちら（ユーザー）が優先されます。
- アプリ更新で消えない保護領域です（共通スキルと同じ扱い）。

例（reviewer.md）:
---
id: reviewer
title: レビュー役
summary: 仕上がりを点検してほしいとき。指摘を箇条書きで返してほしい時に。
permission_tier: read
allow_web_tools: false
expected_output: |
  - 良い点 / 直すべき点を分けて箇条書き。
  - 重大度（高/中/低）を各項目に付ける。
priority: 50
---
（本文：この役割の実行役へ渡したい進め方を書く）
"""


# ---------------------------------------------------------------------------
# データ
# ---------------------------------------------------------------------------


@dataclass
class Role:
    id: str
    title: str
    summary: str = ""
    guidance: str = ""  # 本文＝委任プロンプトに足す追加インストラクション
    task_kind: str = ""
    workspace_kind: str = ""
    permission_tier: str = ""
    allow_web_tools: bool | None = None  # None = ロール未指定（触らない）
    expected_output: str = ""
    model_hint: str = ""  # 将来のモデルルーティング用（Phase 2）。現状は未使用。
    playbooks: list[str] = field(default_factory=list)  # 任意。Phase 1 では記録のみ。
    priority: int = 0
    max_turns: int = 0
    timeout_seconds: int = 0
    source: str = ""


# ---------------------------------------------------------------------------
# ディレクトリ
# ---------------------------------------------------------------------------


def _default_roles_dir() -> Path:
    # agent_delegation/roles.py -> リポジトリ直下の agent_roles/（運営層）
    return Path(__file__).resolve().parent.parent / _ROLES_DIRNAME


def _user_roles_dir() -> Path:
    return Path(constants.ROOMS_DIR).joinpath(*_USER_ROLES_SUBPATH)


def _ensure_user_roles_dir() -> Path:
    """ユーザー層ディレクトリを用意し、空なら使い方READMEを置く。失敗しても致命的にしない。"""
    directory = _user_roles_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        readme = directory / "README.txt"  # .md ではないのでロールとして読まれない
        if not readme.exists() and not any(directory.glob("*.md")):
            readme.write_text(_USER_ROLES_README, encoding="utf-8")
    except OSError:
        logger.debug("user roles dir prepare failed: %s", directory, exc_info=True)
    return directory


# ---------------------------------------------------------------------------
# パース
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), m.group(2)


def _parse_inline_list(value: str) -> list[str]:
    value = (value or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    items = [v.strip().strip("'\"") for v in value.split(",")]
    return [v for v in items if v]


def _to_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _to_bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    return None


def _norm_tier_or_blank(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_TIERS else ""


def _norm_workspace_or_blank(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_WORKSPACE_KINDS else ""


def _norm_model_hint_or_blank(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_MODEL_HINTS else ""


def _parse_role_frontmatter(fm: str) -> dict[str, Any]:
    """ロール用の極小フロントマターパーサ（PyYAML 非依存）。

    対応：`key: value` スカラ / `key: [a, b]` インラインリスト /
    `key: |`（または `>`）のブロックスカラ（後続のインデント行を本文として集める）。
    """
    data: dict[str, Any] = {}
    lines = fm.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip()
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value in ("|", ">"):
            # ブロックスカラ：以降のインデント行（indent>0）を本文として集める。
            block: list[str] = []
            base_indent: int | None = None
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "":
                    block.append("")
                    i += 1
                    continue
                indent = len(nxt) - len(nxt.lstrip())
                if indent == 0:
                    break
                if base_indent is None:
                    base_indent = indent
                block.append(nxt[base_indent:] if len(nxt) >= base_indent else nxt.lstrip())
                i += 1
            data[key] = "\n".join(block).strip("\n")
        else:
            data[key] = value.strip().strip("'\"")
    return data


def _role_from_file(path: Path) -> Role | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm_text, body = _split_frontmatter(text)
    meta = _parse_role_frontmatter(fm_text) if fm_text else {}
    role_id = str(meta.get("id") or path.stem).strip()
    if not role_id:
        return None
    return Role(
        id=role_id,
        title=str(meta.get("title") or role_id),
        summary=str(meta.get("summary") or ""),
        guidance=body.strip(),
        task_kind=str(meta.get("task_kind") or "").strip().lower(),
        workspace_kind=_norm_workspace_or_blank(meta.get("workspace_kind")),
        permission_tier=_norm_tier_or_blank(meta.get("permission_tier")),
        allow_web_tools=_to_bool_or_none(meta.get("allow_web_tools")),
        expected_output=str(meta.get("expected_output") or "").strip(),
        model_hint=str(meta.get("model_hint") or "").strip().lower(),
        playbooks=_parse_inline_list(str(meta.get("playbooks") or "")),
        priority=_to_int(meta.get("priority")),
        max_turns=_to_int(meta.get("max_turns")),
        timeout_seconds=_to_int(meta.get("timeout_seconds")),
        source=str(path),
    )


# ---------------------------------------------------------------------------
# ロード（mtime キャッシュ・2層マージ）
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {"signature": None, "roles": {}}


def _dir_signature(directory: Path) -> tuple:
    try:
        entries = sorted(directory.glob("*.md"))
    except OSError:
        return ()
    return tuple((str(p), p.stat().st_mtime) for p in entries)


def _load_dir(directory: Path) -> list[Role]:
    if not directory.is_dir():
        return []
    out: list[Role] = []
    for path in sorted(directory.glob("*.md")):
        try:
            role = _role_from_file(path)
        except Exception:  # noqa: BLE001 — ロール読込は委任を止めない
            logger.debug("role parse failed: %s", path, exc_info=True)
            role = None
        if role is not None:
            out.append(role)
    return out


def _resolve_dirs(roles_dir: Path | None) -> list[Path]:
    if roles_dir is not None:
        return [roles_dir]
    return [_default_roles_dir(), _ensure_user_roles_dir()]


def load_roles(roles_dir: Path | None = None) -> dict[str, Role]:
    """運営層＋ユーザー層のロールを読み込みマージする（id 衝突はユーザー層優先）。"""
    dirs = _resolve_dirs(roles_dir)
    signature = tuple((str(d), _dir_signature(d)) for d in dirs)
    if signature == _cache.get("signature"):
        return dict(_cache.get("roles") or {})

    merged: dict[str, Role] = {}
    for directory in dirs:
        for role in _load_dir(directory):
            merged[role.id] = role  # 後勝ち：ユーザー層が運営層を上書き
    _cache["signature"] = signature
    _cache["roles"] = merged
    return dict(merged)


def _invalidate_cache() -> None:
    _cache["signature"] = None


def get_role(role_id: str, *, roles_dir: Path | None = None) -> Role | None:
    rid = str(role_id or "").strip().lower()
    if not rid:
        return None
    return load_roles(roles_dir).get(rid)


# ---------------------------------------------------------------------------
# 適用（明示引数優先で未指定スロットを埋める）
# ---------------------------------------------------------------------------


def apply_role(role: Role | None, params: dict[str, Any]) -> dict[str, Any]:
    """ロール既定値で `params` の未指定スロットを埋めて返す（明示値は常に優先）。

    `params` のキー：task_kind / workspace_kind / permission_tier / expected_output /
    allow_web_tools / max_turns / timeout_seconds。
    付随情報として role_id / role_guidance / role_playbooks を追加する。
    """
    out = dict(params)
    out.setdefault("role_id", "")
    out.setdefault("role_guidance", "")
    out.setdefault("role_playbooks", [])
    if role is None:
        return out

    def fill_str(key: str, role_val: str) -> None:
        if not str(out.get(key) or "").strip() and str(role_val or "").strip():
            out[key] = role_val

    fill_str("task_kind", role.task_kind)
    fill_str("workspace_kind", role.workspace_kind)
    fill_str("permission_tier", role.permission_tier)
    fill_str("expected_output", role.expected_output)

    # allow_web_tools はロールでは「有効化のみ」を許す（無効化で安全要件を崩さない）。
    if out.get("allow_web_tools") is None and role.allow_web_tools is True:
        out["allow_web_tools"] = True

    if not _to_int(out.get("max_turns")) and role.max_turns:
        out["max_turns"] = role.max_turns
    if not _to_int(out.get("timeout_seconds")) and role.timeout_seconds:
        out["timeout_seconds"] = role.timeout_seconds

    out["role_id"] = role.id
    out["role_guidance"] = role.guidance
    out["role_playbooks"] = list(role.playbooks)
    return out


# ---------------------------------------------------------------------------
# 目次（Phase 3 のペルソナ向けツール用。Phase 1 から提供しておく）
# ---------------------------------------------------------------------------


def _equipment_label(role: Role) -> str:
    parts: list[str] = []
    if role.workspace_kind:
        ws = {
            "project": "プロジェクト",
            "persona": "アトリエ",
            "persona_project_read": "アトリエ＋プロジェクト読取",
        }.get(role.workspace_kind, role.workspace_kind)
        parts.append(f"作業場所={ws}")
    if role.permission_tier:
        parts.append(f"権限={role.permission_tier}")
    if role.allow_web_tools:
        parts.append("Web=可")
    if role.task_kind:
        parts.append(f"種別={role.task_kind}")
    return " / ".join(parts) if parts else "（既定の装備）"


def list_roles(*, roles_dir: Path | None = None) -> list[dict[str, Any]]:
    """利用可能なロールの目次（id / title / summary / 装備）を優先度順で返す。"""
    roles = sorted(
        load_roles(roles_dir).values(), key=lambda r: (-r.priority, r.id)
    )
    return [
        {
            "id": r.id,
            "title": r.title,
            "summary": r.summary,
            "equipment": _equipment_label(r),
        }
        for r in roles
    ]


# ---------------------------------------------------------------------------
# ユーザー層ロールの編集（UI 管理用。skill_pack の編集APIと対）
# ---------------------------------------------------------------------------

OPERATOR_LAYER = "operator"
USER_LAYER = "user"

_ID_ALLOWED = re.compile(r"[^a-z0-9_-]+")


def _safe_role_id(raw: str) -> str:
    """ファイル名に使える安全な id へ正規化（英小文字・数字・ハイフン・アンダースコア）。"""
    value = str(raw or "").strip().lower().replace(" ", "-")
    value = _ID_ALLOWED.sub("", value)
    return value.strip("-_")


def _user_role_path(role_id: str) -> Path:
    """ユーザー層内の .md パスを安全に解決（パストラバーサル防止）。"""
    safe_id = _safe_role_id(role_id)
    if not safe_id:
        raise ValueError("ロールのID（半角英数・ハイフン）を入力してください。")
    base = _ensure_user_roles_dir().resolve()
    path = (base / f"{safe_id}.md").resolve()
    if base != path.parent:
        raise ValueError("不正なロールIDです。")
    return path


def list_roles_detailed() -> list[dict[str, Any]]:
    """運営層・ユーザー層を別々に読み、層と上書き状態つきの一覧を返す（UI用）。"""
    op = {r.id: r for r in _load_dir(_default_roles_dir())}
    user = {r.id: r for r in _load_dir(_ensure_user_roles_dir())}
    rows: list[dict[str, Any]] = []
    for role_id in set(op) | set(user):
        in_user = role_id in user
        effective = user.get(role_id) or op.get(role_id)
        rows.append(
            {
                "id": role_id,
                "title": effective.title,
                "summary": effective.summary,
                "equipment": _equipment_label(effective),
                "layer": USER_LAYER if in_user else OPERATOR_LAYER,
                "overridden": in_user and role_id in op,
                "editable": in_user,
                "priority": effective.priority,
            }
        )
    rows.sort(key=lambda r: (-int(r["priority"]), str(r["id"])))
    return rows


def read_role_source(role_id: str, layer: str) -> str:
    """指定層の実ファイル本文（フロントマター込み）を返す。"""
    safe_id = _safe_role_id(role_id)
    if not safe_id:
        return ""
    base = _default_roles_dir() if layer == OPERATOR_LAYER else _ensure_user_roles_dir()
    path = base / f"{safe_id}.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def parse_role(content: str) -> dict[str, Any]:
    """frontmatter 込みの本文をフォーム用フィールドへ分解する。"""
    fm_text, body = _split_frontmatter(content or "")
    meta = _parse_role_frontmatter(fm_text) if fm_text else {}
    return {
        "id": str(meta.get("id") or ""),
        "title": str(meta.get("title") or ""),
        "summary": str(meta.get("summary") or ""),
        "workspace_kind": _norm_workspace_or_blank(meta.get("workspace_kind")),
        "permission_tier": _norm_tier_or_blank(meta.get("permission_tier")),
        "allow_web_tools": bool(_to_bool_or_none(meta.get("allow_web_tools"))),
        "task_kind": str(meta.get("task_kind") or "").strip(),
        "model_hint": _norm_model_hint_or_blank(meta.get("model_hint")),
        "expected_output": str(meta.get("expected_output") or "").strip(),
        "priority": _to_int(meta.get("priority")),
        "body": body.strip(),
    }


def compose_role(
    *,
    role_id: str,
    title: str,
    summary: str,
    workspace_kind: str,
    permission_tier: str,
    allow_web_tools: bool,
    task_kind: str,
    expected_output: str,
    priority: int,
    body: str,
    model_hint: str = "",
) -> str:
    """フォーム値から frontmatter 込みの .md 本文を合成する。"""
    safe_id = _safe_role_id(role_id)
    lines = ["---", f"id: {safe_id}"]
    if str(title or "").strip():
        lines.append(f"title: {title.strip()}")
    if str(summary or "").strip():
        lines.append(f"summary: {summary.strip()}")
    ws = _norm_workspace_or_blank(workspace_kind)
    if ws:
        lines.append(f"workspace_kind: {ws}")
    tier = _norm_tier_or_blank(permission_tier)
    if tier:
        lines.append(f"permission_tier: {tier}")
    if bool(allow_web_tools):
        lines.append("allow_web_tools: true")
    if str(task_kind or "").strip():
        lines.append(f"task_kind: {task_kind.strip()}")
    hint = _norm_model_hint_or_blank(model_hint)
    if hint:
        lines.append(f"model_hint: {hint}")
    expected = str(expected_output or "").strip()
    if expected:
        lines.append("expected_output: |")
        for ln in expected.splitlines():
            lines.append(f"  {ln}")
    try:
        prio = int(priority)
    except (TypeError, ValueError):
        prio = 0
    lines.append(f"priority: {prio}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"


def validate_role_content(content: str) -> tuple[bool, str]:
    """保存前の検証。読めれば (True, 警告) / 駄目なら (False, エラー)。"""
    fm_text, body = _split_frontmatter(content or "")
    if not fm_text:
        return False, "フロントマター（--- で囲む見出し）がありません。"
    meta = _parse_role_frontmatter(fm_text)
    if not str(meta.get("id") or "").strip():
        return False, "id が指定されていません。"
    if not body.strip():
        return False, "本文（進め方）が空です。1行でも書いてください。"
    return True, ""


def save_user_role(role_id: str, content: str) -> dict[str, Any]:
    """ユーザー層へロールを書き込む（新規/更新兼用）。"""
    ok, message = validate_role_content(content)
    if not ok:
        raise ValueError(message)
    path = _user_role_path(role_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    _invalidate_cache()
    return {"id": path.stem, "path": str(path), "warning": message}


def delete_user_role(role_id: str) -> dict[str, Any]:
    """ユーザー層のロールを削除する（運営層は対象外）。"""
    path = _user_role_path(role_id)
    if not path.is_file():
        raise FileNotFoundError("削除対象のユーザーロールが見つかりません。")
    path.unlink()
    _invalidate_cache()
    return {"id": path.stem, "path": str(path)}
