"""委任エージェント向けの内製スキルパック（playbook）。

2層構成で、タスク種別に合うノウハウを委任プロンプトへ注入するための土台。

- 運営層: `agent_skills/*.md`（リポジトリ同梱・コード資産）。アプリ更新で常に最新化される、
  運営キュレーション済みのノウハウ（アプリ制作の作法など）。
- ユーザー層: `characters/_shared/memory/agent_playbooks/*.md`（各環境固有・全ペルソナ共通）。
  ユーザーが自分で追加・編集できる。共通スキルと同じ「更新で消えない保護領域」に置く。

両層を読み込んでマージし、`id` が衝突した場合は**ユーザー層を優先**する（運営プレイブックを
ユーザーが上書き・無効化できる）。

- 純標準ライブラリのみ（Claude Agent SDK 非依存）。
- フロントマターは依存追加を避けるため極小の自前パーサで解釈する。
- ディレクトリ非存在・パース失敗は「スキル無し」として安全に握りつぶす
  （委任のクリティカルパスにしない）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import constants

logger = logging.getLogger(__name__)

# 注入ブロック全体の文字数バジェット（プロンプト肥大・コスト悪化の防止）
DEFAULT_TOTAL_BUDGET = 6000
# 個別スキルが max_chars 未指定のときの上限
DEFAULT_SKILL_MAX_CHARS = 4000

_SKILLS_DIRNAME = "agent_skills"
# ユーザー追加プレイブックの置き場（共通スキルと同じ保護領域。全ペルソナ共通・環境固有）。
_USER_SKILLS_SUBPATH = ("_shared", "memory", "agent_playbooks")

_USER_SKILLS_README = """\
このフォルダは「ユーザー追加プレイブック」の置き場です。
委任エージェント（依頼を実行する裏方）に渡す、タスクの進め方ノウハウを自分で足せます。

- 1ノウハウ = 1つの .md ファイル。フロントマター（--- で囲む）＋本文。
- 運営同梱の agent_skills/ とマージされ、同じ id ならこちら（ユーザー）が優先されます。
- アプリ更新で消えない保護領域です（共通スキルと同じ扱い）。

例（research-note.md）:
---
id: research-note
title: 調べ物のまとめ方
summary: 調べ物を委任するとき。出典つきで箇条書きにまとめてほしい時に。
applies_to:
  task_kinds: [deep_research]
  keywords: [調査, リサーチ]
  require_keywords: true
priority: 50
---
（ここに、実行役エージェントへ渡したい進め方を書く）
"""


def _default_skills_dir() -> Path:
    # agent_delegation/skill_pack.py -> リポジトリ直下の agent_skills/（運営層）
    return Path(__file__).resolve().parent.parent / _SKILLS_DIRNAME


def _user_skills_dir() -> Path:
    # characters/_shared/memory/agent_playbooks/（ユーザー層・全ペルソナ共通）
    return Path(constants.ROOMS_DIR).joinpath(*_USER_SKILLS_SUBPATH)


def _ensure_user_skills_dir() -> Path:
    """ユーザー層ディレクトリを用意し、空なら使い方READMEを置く。失敗しても致命的にしない。"""
    directory = _user_skills_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        readme = directory / "README.txt"  # .md ではないのでスキルとして読まれない
        if not readme.exists() and not any(directory.glob("*.md")):
            readme.write_text(_USER_SKILLS_README, encoding="utf-8")
    except OSError:
        logger.debug("user skills dir prepare failed: %s", directory, exc_info=True)
    return directory


@dataclass
class Skill:
    id: str
    title: str
    body: str
    summary: str = ""  # ペルソナ向けの一言（どんな依頼で役立つか）。Phase 3 の目次表示用。
    workspace_kinds: list[str] = field(default_factory=list)
    task_kinds: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    require_keywords: bool = False
    priority: int = 0
    max_chars: int = DEFAULT_SKILL_MAX_CHARS
    source: str = ""

    def trimmed_body(self) -> str:
        body = self.body.strip()
        cap = self.max_chars if self.max_chars > 0 else DEFAULT_SKILL_MAX_CHARS
        if len(body) <= cap:
            return body
        return body[:cap].rstrip() + "\n…(truncated)"


def _parse_inline_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    items = [v.strip().strip("'\"") for v in value.split(",")]
    return [v for v in items if v]


def _split_frontmatter(text: str) -> tuple[str, str]:
    """`--- ... ---` ブロックを (frontmatter, body) に分割。無ければ ("", text)。"""
    if not text.startswith("---"):
        return "", text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), m.group(2)


def _parse_frontmatter(fm: str) -> dict[str, Any]:
    """この計画で使う形だけを解釈する極小パーサ（PyYAML 不採用）。

    - トップレベル: id / title / priority / max_chars （スカラ）
    - applies_to: 配下に workspace_kinds / task_kinds / keywords （インライン or ブロックの list）
      と require_keywords: true/false
    """
    data: dict[str, Any] = {"applies_to": {}}
    in_applies = False
    pending_list_key: str | None = None  # applies_to 配下のブロック list を組み立て中のキー

    for raw in fm.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        # ブロック list の項目（"- value"）
        if stripped.startswith("- ") and in_applies and pending_list_key:
            data["applies_to"].setdefault(pending_list_key, []).append(
                stripped[2:].strip().strip("'\"")
            )
            continue

        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()

        if indent == 0:
            pending_list_key = None
            if key == "applies_to":
                in_applies = True
                continue
            in_applies = False
            if key in ("id", "title", "summary"):
                data[key] = value.strip().strip("'\"")
            elif key in ("priority", "max_chars"):
                try:
                    data[key] = int(value)
                except ValueError:
                    pass
        else:
            # applies_to 配下
            if not in_applies:
                continue
            if key == "require_keywords":
                data["applies_to"][key] = value.lower() in ("1", "true", "yes", "on")
                pending_list_key = None
                continue
            if value:
                data["applies_to"][key] = _parse_inline_list(value)
                pending_list_key = None
            else:
                # ブロック list の開始（次行以降の "- item" を集める）
                data["applies_to"].setdefault(key, [])
                pending_list_key = key
    return data


def _norm_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v).strip().lower() for v in values if str(v).strip()]


def _skill_from_file(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm_text, body = _split_frontmatter(text)
    meta = _parse_frontmatter(fm_text) if fm_text else {"applies_to": {}}
    applies = meta.get("applies_to") or {}
    skill_id = str(meta.get("id") or path.stem).strip()
    if not skill_id:
        return None
    return Skill(
        id=skill_id,
        title=str(meta.get("title") or skill_id),
        body=body,
        summary=str(meta.get("summary") or ""),
        workspace_kinds=_norm_list(applies.get("workspace_kinds")),
        task_kinds=_norm_list(applies.get("task_kinds")),
        keywords=_norm_list(applies.get("keywords")),
        require_keywords=bool(applies.get("require_keywords")),
        priority=int(meta.get("priority") or 0),
        max_chars=int(meta.get("max_chars") or DEFAULT_SKILL_MAX_CHARS),
        source=str(path),
    )


# mtime ベースの簡易キャッシュ: (signature) -> list[Skill]
_cache: dict[str, Any] = {"signature": None, "skills": []}


def _dir_signature(skills_dir: Path) -> tuple:
    try:
        entries = sorted(skills_dir.glob("*.md"))
    except OSError:
        return ()
    return tuple((str(p), p.stat().st_mtime) for p in entries)


def _load_dir(directory: Path) -> list[Skill]:
    if not directory.is_dir():
        return []
    skills: list[Skill] = []
    for path in sorted(directory.glob("*.md")):
        try:
            skill = _skill_from_file(path)
        except Exception:  # noqa: BLE001 — スキル読込は委任を止めない
            logger.debug("skill parse failed: %s", path, exc_info=True)
            skill = None
        if skill is not None:
            skills.append(skill)
    return skills


def _resolve_dirs(skills_dir: Path | None) -> list[Path]:
    """読み込むディレクトリ列を返す（後勝ち＝後ろのディレクトリが id 衝突で優先）。

    skills_dir 明示時はそのディレクトリ単体（テスト・特殊用途）。
    既定時は [運営層, ユーザー層] の順（ユーザー層が優先）。
    """
    if skills_dir is not None:
        return [skills_dir]
    return [_default_skills_dir(), _ensure_user_skills_dir()]


def load_skill_pack(skills_dir: Path | None = None) -> list[Skill]:
    """運営層＋ユーザー層のスキルを読み込みマージする。非存在・失敗時は空。

    同一 id は後勝ち（ユーザー層が運営層を上書き）。
    """
    dirs = _resolve_dirs(skills_dir)
    signature = tuple((str(d), _dir_signature(d)) for d in dirs)
    if signature == _cache.get("signature"):
        return list(_cache.get("skills") or [])

    merged: dict[str, Skill] = {}
    for directory in dirs:
        for skill in _load_dir(directory):
            merged[skill.id] = skill  # 後勝ち：ユーザー層が運営層を上書き
    skills = list(merged.values())

    _cache["signature"] = signature
    _cache["skills"] = skills
    return list(skills)


def _matches(skill: Skill, workspace_kind: str, task_kind: str, description: str) -> tuple[bool, int]:
    """(該当するか, 加点) を返す。"""
    bonus = 0
    matched = False
    has_scope = bool(skill.workspace_kinds or skill.task_kinds)
    keyword_hit = bool(skill.keywords) and bool(description) and any(
        kw in description for kw in skill.keywords
    )

    if has_scope:
        # スコープ宣言ありは確定マッチが主軸。keyword は順位の底上げのみ。
        if skill.require_keywords and not keyword_hit:
            return False, 0
        if skill.workspace_kinds and workspace_kind in skill.workspace_kinds:
            matched = True
        if skill.task_kinds and task_kind in skill.task_kinds:
            matched = True
        if matched and keyword_hit:
            bonus += 10
    elif skill.keywords:
        # スコープ無し＋keyword あり = キーワード限定スキル。説明にヒットしたときだけ候補。
        # （アトリエ等のスコープ宣言ありスキルへは波及しないので誤注入にならない。）
        if keyword_hit:
            matched = True
            bonus += 10
    else:
        # スコープも keyword も無い = 汎用スキル（常に候補）。
        matched = True
    return matched, bonus


def select_skills(
    task: dict[str, Any],
    *,
    skills_dir: Path | None = None,
    total_budget: int = DEFAULT_TOTAL_BUDGET,
) -> list[Skill]:
    """タスクに合うスキルを優先度順・バジェット内で選ぶ。"""
    workspace_kind = str(task.get("workspace_kind") or "").strip().lower()
    task_kind = str(task.get("task_kind") or "").strip().lower()
    description = str(task.get("task_description") or "").lower()

    scored: list[tuple[int, int, Skill]] = []
    seen: set[str] = set()
    for skill in load_skill_pack(skills_dir):
        if skill.id in seen:
            continue
        matched, bonus = _matches(skill, workspace_kind, task_kind, description)
        if not matched:
            continue
        seen.add(skill.id)
        scored.append((skill.priority + bonus, skill.priority, skill))

    # 優先度（加点込み）降順、同点は元 priority 降順、最後に id で安定化
    scored.sort(key=lambda t: (-t[0], -t[1], t[2].id))

    selected: list[Skill] = []
    used = 0
    for _, _, skill in scored:
        body = skill.trimmed_body()
        if used + len(body) > total_budget and selected:
            break
        selected.append(skill)
        used += len(body)
    return selected


def render_skill_block(skills: list[Skill]) -> str:
    """選択済みスキルを委任プロンプトに差し込む文字列へ整形。空なら空文字。"""
    if not skills:
        return ""
    parts = ["## Reusable playbooks (curated by Nexus Ark)"]
    for skill in skills:
        parts.append(f"\n### {skill.title}\n{skill.trimmed_body()}")
    return "\n".join(parts).strip()


def skill_block_for_task(task: dict[str, Any], *, skills_dir: Path | None = None) -> str:
    """タスク向けの注入ブロックを返す（選択＋整形のワンショット）。"""
    return render_skill_block(select_skills(task, skills_dir=skills_dir))


def _applies_label(skill: Skill) -> str:
    """ペルソナ向けに「いつ役立つか」を短く言語化する。"""
    if skill.workspace_kinds and ("persona" in skill.workspace_kinds or "persona_project_read" in skill.workspace_kinds):
        return "アトリエのアプリ制作を委任するとき"
    if skill.task_kinds:
        return "／".join(skill.task_kinds) + " の委任のとき"
    if skill.keywords:
        return "次の話題を含む委任のとき: " + "、".join(skill.keywords)
    return "あらゆる委任で参照される基本ノウハウ"


def list_playbooks(*, skills_dir: Path | None = None) -> list[dict[str, Any]]:
    """利用可能な playbook の目次（id / title / summary / 適用条件）を優先度順で返す。

    Phase 3: ペルソナが「どんな種類の委任に確立したノウハウがあるか」を把握し、
    オーケストレーション（依頼の組み立て）に活かすための一覧。本文全文は含めない。
    """
    skills = sorted(load_skill_pack(skills_dir), key=lambda s: (-s.priority, s.id))
    catalog: list[dict[str, Any]] = []
    for skill in skills:
        catalog.append(
            {
                "id": skill.id,
                "title": skill.title,
                "summary": skill.summary,
                "applies_when": _applies_label(skill),
                "workspace_kinds": list(skill.workspace_kinds),
                "task_kinds": list(skill.task_kinds),
            }
        )
    return catalog


# ---------------------------------------------------------------------------
# ユーザー層プレイブックの編集（UI 管理用）
# ---------------------------------------------------------------------------

OPERATOR_LAYER = "operator"
USER_LAYER = "user"

# フォーム編集の「適用条件」種別（UIのラジオに対応）
APPLY_ATELIER = "atelier"       # アトリエのアプリ制作（workspace_kinds=persona系）
APPLY_RESEARCH = "research"     # ディープリサーチ（task_kinds=deep_research）
APPLY_KEYWORD = "keyword"       # キーワードに一致した委任のとき
APPLY_GENERAL = "general"       # あらゆる委任で参照される基本ノウハウ

_PERSONA_WORKSPACE_KINDS = ["persona", "persona_project_read"]
_RESEARCH_TASK_KINDS = ["deep_research"]
_ID_ALLOWED = re.compile(r"[^a-z0-9_-]+")


def _safe_playbook_id(raw: str) -> str:
    """ファイル名に使える安全な id へ正規化（英小文字・数字・ハイフン・アンダースコア）。"""
    value = str(raw or "").strip().lower().replace(" ", "-")
    value = _ID_ALLOWED.sub("", value)
    return value.strip("-_")


def _invalidate_cache() -> None:
    _cache["signature"] = None


def _user_playbook_path(skill_id: str) -> Path:
    """ユーザー層内の .md パスを安全に解決（パストラバーサル防止）。"""
    safe_id = _safe_playbook_id(skill_id)
    if not safe_id:
        raise ValueError("プレイブックのID（半角英数・ハイフン）を入力してください。")
    base = _ensure_user_skills_dir().resolve()
    path = (base / f"{safe_id}.md").resolve()
    if base != path.parent:
        raise ValueError("不正なプレイブックIDです。")
    return path


def apply_kind_of(skill: Skill) -> str:
    """Skill から UI フォームの「適用条件」種別を判定する。"""
    if skill.workspace_kinds and any(k in _PERSONA_WORKSPACE_KINDS for k in skill.workspace_kinds):
        return APPLY_ATELIER
    if skill.task_kinds and any(k in _RESEARCH_TASK_KINDS for k in skill.task_kinds):
        return APPLY_RESEARCH
    if skill.keywords:
        return APPLY_KEYWORD
    return APPLY_GENERAL


def list_playbooks_detailed() -> list[dict[str, Any]]:
    """運営層・ユーザー層を別々に読み、層と上書き状態つきの一覧を返す（UI用）。

    1 id につき 1 行。layer = 実効版がどちらの層にあるか（user が運営を上書きしていれば user）。
    overridden = 同 id が両層にある（ユーザーが運営版を隠している）。
    editable = ユーザー層に実体がある（編集・削除できる）。
    """
    op = {s.id: s for s in _load_dir(_default_skills_dir())}
    user = {s.id: s for s in _load_dir(_ensure_user_skills_dir())}
    rows: list[dict[str, Any]] = []
    for skill_id in set(op) | set(user):
        in_user = skill_id in user
        effective = user.get(skill_id) or op.get(skill_id)
        rows.append(
            {
                "id": skill_id,
                "title": effective.title,
                "summary": effective.summary,
                "applies_when": _applies_label(effective),
                "layer": USER_LAYER if in_user else OPERATOR_LAYER,
                "overridden": in_user and skill_id in op,
                "editable": in_user,
                "priority": effective.priority,
            }
        )
    rows.sort(key=lambda r: (-int(r["priority"]), str(r["id"])))
    return rows


def read_playbook_source(skill_id: str, layer: str) -> str:
    """指定層の実ファイル本文（フロントマター込み）を返す。"""
    safe_id = _safe_playbook_id(skill_id)
    if not safe_id:
        return ""
    base = _default_skills_dir() if layer == OPERATOR_LAYER else _ensure_user_skills_dir()
    path = base / f"{safe_id}.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def parse_playbook(content: str) -> dict[str, Any]:
    """frontmatter 込みの本文をフォーム用フィールドへ分解する。"""
    fm_text, body = _split_frontmatter(content or "")
    meta = _parse_frontmatter(fm_text) if fm_text else {"applies_to": {}}
    applies = meta.get("applies_to") or {}
    skill = Skill(
        id=str(meta.get("id") or ""),
        title=str(meta.get("title") or ""),
        body=body,
        summary=str(meta.get("summary") or ""),
        workspace_kinds=_norm_list(applies.get("workspace_kinds")),
        task_kinds=_norm_list(applies.get("task_kinds")),
        keywords=_norm_list(applies.get("keywords")),
        priority=int(meta.get("priority") or 0),
    )
    return {
        "id": skill.id,
        "title": skill.title,
        "summary": skill.summary,
        "apply_kind": apply_kind_of(skill),
        "keywords": list(skill.keywords),
        "priority": skill.priority,
        "body": body.strip(),
    }


def compose_playbook(
    *,
    skill_id: str,
    title: str,
    summary: str,
    apply_kind: str,
    keywords: list[str] | str,
    priority: int,
    body: str,
) -> str:
    """フォーム値から frontmatter 込みの .md 本文を合成する。"""
    safe_id = _safe_playbook_id(skill_id)
    if isinstance(keywords, str):
        kw_list = [k.strip() for k in re.split(r"[,、\s]+", keywords) if k.strip()]
    else:
        kw_list = [str(k).strip() for k in (keywords or []) if str(k).strip()]

    lines = ["---", f"id: {safe_id}"]
    if title.strip():
        lines.append(f"title: {title.strip()}")
    if summary.strip():
        lines.append(f"summary: {summary.strip()}")

    applies: list[str] = []
    if apply_kind == APPLY_ATELIER:
        applies.append(f"  workspace_kinds: [{', '.join(_PERSONA_WORKSPACE_KINDS)}]")
    elif apply_kind == APPLY_RESEARCH:
        applies.append(f"  task_kinds: [{', '.join(_RESEARCH_TASK_KINDS)}]")
    if kw_list:
        applies.append(f"  keywords: [{', '.join(kw_list)}]")
    if applies:
        lines.append("applies_to:")
        lines.extend(applies)
    else:
        lines.append("applies_to: {}")

    try:
        prio = int(priority)
    except (TypeError, ValueError):
        prio = 0
    lines.append(f"priority: {prio}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"


def validate_playbook_content(content: str) -> tuple[bool, str]:
    """保存前の検証。読めれば (True, 警告文字列) / 駄目なら (False, エラー文字列)。"""
    fm_text, body = _split_frontmatter(content or "")
    if not fm_text:
        return False, "フロントマター（--- で囲む見出し）がありません。"
    meta = _parse_frontmatter(fm_text)
    if not str(meta.get("id") or "").strip():
        return False, "id が指定されていません。"
    if not body.strip():
        return False, "本文が空です。進め方を1行でも書いてください。"
    warn = ""
    if len(body.strip()) > DEFAULT_SKILL_MAX_CHARS:
        warn = f"本文が長い（{len(body.strip())}字）ため、委任時は約{DEFAULT_SKILL_MAX_CHARS}字で切り詰められます。"
    return True, warn


def save_user_playbook(skill_id: str, content: str) -> dict[str, Any]:
    """ユーザー層へプレイブックを書き込む（新規/更新兼用）。"""
    ok, message = validate_playbook_content(content)
    if not ok:
        raise ValueError(message)
    path = _user_playbook_path(skill_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    _invalidate_cache()
    return {"id": path.stem, "path": str(path), "warning": message}


def delete_user_playbook(skill_id: str) -> dict[str, Any]:
    """ユーザー層のプレイブックを削除する（運営層は対象外）。"""
    path = _user_playbook_path(skill_id)
    if not path.is_file():
        raise FileNotFoundError("削除対象のユーザープレイブックが見つかりません。")
    path.unlink()
    _invalidate_cache()
    return {"id": path.stem, "path": str(path)}


# --- プレイブックを育てる（E：提案→人のレビュー→採用） ----------------------------------
# 提案は「採用候補のプレイブック .md」そのもの＋提案メタ（frontmatter 追加キー）で持つ。
# ユーザー層とは別の保護領域 agent_playbook_proposals/ に置き、採用時のみ save_user_playbook で本採用する。
# AI は提案するだけ。採用は人（UI）のみ。

_PROPOSALS_SUBPATH = ("_shared", "memory", "agent_playbook_proposals")
_PROPOSAL_META_KEYS = ("proposal_id", "target_id", "created_at", "source_task", "reason")


def _proposals_dir() -> Path:
    return Path(constants.ROOMS_DIR).joinpath(*_PROPOSALS_SUBPATH)


def _ensure_proposals_dir() -> Path:
    directory = _proposals_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return directory


def _proposal_path(proposal_id: str) -> Path:
    """提案 .md パスを安全に解決（パストラバーサル防止）。"""
    safe = _safe_playbook_id(proposal_id)
    if not safe:
        raise ValueError("提案IDが不正です。")
    base = _ensure_proposals_dir().resolve()
    path = (base / f"{safe}.md").resolve()
    if base != path.parent:
        raise ValueError("不正な提案IDです。")
    return path


def _read_proposal_meta(fm_text: str) -> dict[str, str]:
    """提案メタ（reason / source_task / created_at 等）を frontmatter から拾う。

    `_parse_frontmatter` はこれらのキーを無視するため（playbook 本体を汚さない）、UI 表示用に別途読む。
    """
    meta: dict[str, str] = {}
    for raw in fm_text.splitlines():
        if raw[:1].isspace():  # トップレベルのみ（applies_to 配下は無視）
            continue
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in _PROPOSAL_META_KEYS:
            meta[key] = value.strip().strip("'\"")
    return meta


def _clean_playbook_from_proposal(content: str) -> str:
    """提案 .md から提案メタを除いたクリーンなプレイブック本文を再合成する（採用時に使う）。"""
    fields = parse_playbook(content)
    return compose_playbook(
        skill_id=fields["id"],
        title=fields["title"],
        summary=fields["summary"],
        apply_kind=fields["apply_kind"],
        keywords=fields["keywords"],
        priority=fields["priority"],
        body=fields["body"],
    )


def save_proposal(
    *,
    target_id: str,
    title: str,
    body: str,
    summary: str = "",
    apply_kind: str = APPLY_GENERAL,
    keywords: list[str] | str = "",
    priority: int = 50,
    source_task: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """プレイブック改善案を pending 領域へ保存する（ユーザー層には反映しない）。"""
    safe_target = _safe_playbook_id(target_id)
    if not safe_target:
        raise ValueError("対象プレイブックID（半角英数・ハイフン）を入力してください。")
    candidate = compose_playbook(
        skill_id=safe_target,
        title=title,
        summary=summary,
        apply_kind=apply_kind,
        keywords=keywords,
        priority=priority,
        body=body,
    )
    ok, message = validate_playbook_content(candidate)
    if not ok:
        raise ValueError(message)

    created_at = datetime.now().isoformat()
    proposal_id = f"{safe_target}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    fm_text, body_text = _split_frontmatter(candidate)
    meta_lines = [
        f"proposal_id: {proposal_id}",
        f"target_id: {safe_target}",
        f"created_at: {created_at}",
    ]
    if str(source_task or "").strip():
        meta_lines.append(f"source_task: {str(source_task).strip()}")
    if str(reason or "").strip():
        meta_lines.append(f"reason: {str(reason).replace(chr(10), ' ').strip()}")
    proposal_content = (
        "---\n" + fm_text.rstrip() + "\n" + "\n".join(meta_lines) + "\n---\n\n" + body_text.strip() + "\n"
    )
    path = _proposal_path(proposal_id)
    path.write_text(proposal_content, encoding="utf-8")
    return {"proposal_id": proposal_id, "target_id": safe_target, "path": str(path), "warning": message}


def list_proposals() -> list[dict[str, Any]]:
    """pending 提案の一覧（UI用）。新しい順。"""
    rows: list[dict[str, Any]] = []
    for path in sorted(_ensure_proposals_dir().glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm_text, _ = _split_frontmatter(content)
        meta = _read_proposal_meta(fm_text)
        fields = parse_playbook(content)
        rows.append(
            {
                "proposal_id": meta.get("proposal_id") or path.stem,
                "target_id": meta.get("target_id") or fields["id"],
                "title": fields["title"],
                "apply_kind": fields["apply_kind"],
                "reason": meta.get("reason", ""),
                "source_task": meta.get("source_task", ""),
                "created_at": meta.get("created_at", ""),
            }
        )
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows


def read_proposal(proposal_id: str) -> str:
    """提案 .md の本文（frontmatter 込み）を返す。"""
    try:
        path = _proposal_path(proposal_id)
    except ValueError:
        return ""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def adopt_proposal(proposal_id: str) -> dict[str, Any]:
    """提案を採用し、ユーザー層プレイブックへ本反映する（同 id は上書き）。採用後、提案は削除。"""
    path = _proposal_path(proposal_id)
    if not path.is_file():
        raise FileNotFoundError("採用対象の提案が見つかりません。")
    content = path.read_text(encoding="utf-8")
    fields = parse_playbook(content)
    target_id = _safe_playbook_id(fields["id"])
    if not target_id:
        raise ValueError("提案の対象プレイブックIDが不正です。")
    clean = _clean_playbook_from_proposal(content)
    saved = save_user_playbook(target_id, clean)
    path.unlink()
    return {
        "proposal_id": proposal_id,
        "target_id": target_id,
        "path": saved.get("path"),
        "warning": saved.get("warning", ""),
    }


def discard_proposal(proposal_id: str) -> dict[str, Any]:
    """提案を却下（削除）する。ユーザー層には何も反映しない。"""
    path = _proposal_path(proposal_id)
    if not path.is_file():
        raise FileNotFoundError("却下対象の提案が見つかりません。")
    path.unlink()
    return {"proposal_id": proposal_id}
