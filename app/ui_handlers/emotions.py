"""感情項目の設定と、本人の申告状態を表示するUIハンドラ。

このモジュールは、感情の定義をユーザーが管理するための画面と、
本人が既に申告した状態を読むための画面を分けて扱う。申告強度や
申告の終了状態をこの画面から変更しないことが重要である。
"""

from copy import deepcopy
from collections import Counter
from html import escape
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import gradio as gr

from emotion_decay_policy import EmotionDecayPolicy
from emotion_definition_manager import EmotionDefinitionManager
from emotion_ui import EMOTION_STATE_HEADERS
from motivation_manager import MotivationManager


NEW_EMOTION = "__new__"
NEW_EMOTION_LABEL = "＋ 新しい項目を追加"
EMOTION_OUTPUT_COUNT = 11

_DEFAULT_POLICY = EmotionDecayPolicy.default()
_MISSING = object()


def _ensure_output_count(values: Sequence[Any]) -> Tuple[Any, ...]:
    """Gradioの配線に渡す戻り値を常に11個へそろえる。"""
    values = tuple(values)
    if len(values) >= EMOTION_OUTPUT_COUNT:
        return values[:EMOTION_OUTPUT_COUNT]
    return values + (gr.update(),) * (EMOTION_OUTPUT_COUNT - len(values))


def _number_text(value: Any, missing: str = "—") -> str:
    """数値を表示用の文字列にする。UIへ数値以外を漏らさない。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return missing
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return missing
    if not math.isfinite(value):
        return missing
    if value == 0:
        return "0"
    if value.is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _as_display_text(value: Any, missing: str = "—") -> str:
    if value is None:
        return missing
    text = str(value)
    return text if text else missing


def _valid_policy(raw: Any) -> Optional[Dict[str, float | int]]:
    try:
        return EmotionDecayPolicy.validate(raw)
    except (TypeError, ValueError):
        return None


def _policy_text(
    raw: Any,
    valid: Any,
    *,
    source: Optional[str] = None,
    invalid_text: str = "不正（保存値を保持）",
) -> str:
    """時間条件を、人間が設定と申告の違いを確認できる文字列にする。"""
    policy = _valid_policy(raw) if valid is True else None
    if policy is None:
        return invalid_text
    body = (
        f"半減期 {_number_text(policy['half_life_hours'])}時間 / "
        f"最長提示期間 {_number_text(policy['max_age_hours'])}時間"
    )
    if source == "legacy_default":
        return f"旧記録の標準: {body}"
    return body


def _policy_values(raw: Any, valid: Any) -> Tuple[Optional[float], Optional[float]]:
    policy = _valid_policy(raw) if valid is True else None
    if policy is None:
        return None, None
    return float(policy["half_life_hours"]), float(policy["max_age_hours"])


def _policy_equal(first: Any, second: Any) -> bool:
    first_policy = _valid_policy(first)
    second_policy = _valid_policy(second)
    return first_policy == second_policy


def _new_baseline() -> Dict[str, Any]:
    return {
        "name": "",
        "description": "",
        "enabled": True,
        "half_life_hours": float(_DEFAULT_POLICY["half_life_hours"]),
        "max_age_hours": float(_DEFAULT_POLICY["max_age_hours"]),
    }


def _new_session(room_name: str, category: str, baseline: Dict[str, Any], *, is_new: bool,
                 configured_policy_valid: bool = True,
                 definition_valid: bool = True) -> Dict[str, Any]:
    """保存時のルーム、対象、CAS版、表示時点のフォーム値を保持する。"""
    baseline_copy = deepcopy(baseline)
    return {
        "room": room_name,
        "category": category,
        "definition_revision": None if is_new else baseline_copy.pop("definition_revision", None),
        "baseline": baseline_copy,
        "is_new": bool(is_new),
        "configured_policy_valid": bool(configured_policy_valid),
        "definition_valid": bool(definition_valid),
    }


def _new_choice() -> Tuple[str, str]:
    return NEW_EMOTION_LABEL, NEW_EMOTION


def _choices(catalog: Iterable[Dict[str, Any]]) -> List[Tuple[str, Any]]:
    raw_choices: List[Tuple[str, Any]] = []
    for item in catalog:
        # get_emotion_catalog() には、定義のない保存状態も unsupported として
        # 含まれる。定義欄を持つ行だけを選択肢へ載せ、未知IDを編集対象にしない。
        if "definition_valid" not in item:
            continue
        category = item.get("category")
        if category is None:
            continue
        name = item.get("name")
        label = _as_display_text(name, _as_display_text(category))
        if item.get("definition_valid") is not True:
            label += "（定義不正）"
        elif item.get("enabled") is False:
            label += "（使用停止）"
        if label == NEW_EMOTION_LABEL:
            label += "（既存の項目）"
        raw_choices.append((label, category))

    counts = Counter(label for label, _category in raw_choices)
    reserved = {label for label, _category in raw_choices} | {NEW_EMOTION_LABEL}
    used = set()
    seen: Counter[str] = Counter()
    choices: List[Tuple[str, Any]] = []
    for label, category in raw_choices:
        seen[label] += 1
        display = label
        if counts[label] > 1:
            number = seen[label]
            display = f"{label}（{number}）"
            # ユーザーが連番つきの名前を付けても、その項目の表示と衝突させない。
            while display in reserved or display in used:
                number += 1
                display = f"{label}（{number}）"
        used.add(display)
        choices.append((display, category))
    # sentinelは既存ユーザー項目の同名表示に suffix を付けた後で追加する。
    choices.append(_new_choice())
    return choices


def _find_item(catalog: Iterable[Dict[str, Any]], category: Any) -> Optional[Dict[str, Any]]:
    for item in catalog:
        if item.get("category") == category:
            return item
    return None


def _row_state(item: Dict[str, Any]) -> str:
    """catalogの状態を一覧の利用者向けラベルへ変換する。"""
    if item.get("status") == "unsupported":
        return "未対応"
    if item.get("definition_valid") is not True:
        return "不正"

    status = item.get("status")
    if status == "unreported":
        return "未申告"
    if status == "settled":
        return "本人が終了"
    if status == "invalid":
        reported_status = item.get("reported_status")
        if reported_status == "settled":
            return "本人が終了（時間条件不正）"
        if reported_status == "active":
            return "時間条件不正"
        return "不正"
    if status != "active":
        return "不正"

    if item.get("decay_policy_valid") is not True:
        if "decay_policy_valid" in item:
            return "時間条件不正"
        return "不正"
    current = item.get("current_intensity")
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        return "不正"
    try:
        current = float(current)
        elapsed = float(item.get("elapsed_hours"))
    except (TypeError, ValueError, OverflowError):
        return "不正"
    if not math.isfinite(current) or not math.isfinite(elapsed):
        return "不正"
    policy = _valid_policy(item.get("decay_policy"))
    if policy is None:
        return "不正"
    if elapsed >= float(policy["max_age_hours"]):
        return "提示期間終了"
    if current < MotivationManager.EMOTION_MIN_CONTEXT_INTENSITY:
        return "参考値が提示基準未満"
    return "申告中"


def _usage_state(item: Dict[str, Any]) -> str:
    if item.get("status") == "unsupported":
        return "未対応"
    if item.get("definition_valid") is not True:
        return "不正"
    return "使用中" if item.get("enabled") is True else "停止中"


def _latest_intensity(item: Dict[str, Any]) -> str:
    if item.get("status") == "unreported" or "intensity" not in item:
        return "—"
    return _number_text(item.get("intensity"))


def _current_intensity(item: Dict[str, Any]) -> str:
    status = item.get("status")
    if status == "unreported":
        return "—"
    if status == "settled":
        return "0"
    return _number_text(item.get("current_intensity"))


def _reported_at(item: Dict[str, Any]) -> str:
    if item.get("status") == "unreported":
        return "—"
    # MotivationManagerがstarted_atを最新の本人見直し時刻として返す。
    return _as_display_text(item.get("started_at"))


def _state_row(item: Dict[str, Any]) -> List[str]:
    category = item.get("category")
    label = item.get("name")
    if item.get("status") == "unsupported":
        label = _as_display_text(category)
    else:
        label = _as_display_text(label, _as_display_text(category))

    applied = _policy_text(
        item.get("decay_policy"),
        item.get("decay_policy_valid"),
        source=item.get("decay_policy_source"),
    )
    if item.get("status") == "unreported" or "decay_policy_valid" not in item:
        applied = "—"
    configured = _policy_text(
        item.get("configured_decay_policy"),
        item.get("configured_decay_policy_valid"),
    )
    if item.get("status") == "unsupported":
        configured = "—"

    return [
        label,
        _usage_state(item),
        _row_state(item),
        _latest_intensity(item),
        _reported_at(item),
        _current_intensity(item),
        "提示条件内" if item.get("presented") is True else "対象外",
        applied,
        configured,
    ]


def _state_rows(catalog: Iterable[Dict[str, Any]]) -> List[List[str]]:
    rows = [_state_row(item) for item in catalog]
    return [[_as_display_text(value) for value in row] for row in rows]


def _state_table_html(rows: List[List[str]]) -> str:
    """閲覧専用の全行を描画し、項目名などのユーザー入力を文字として扱う。"""
    cell_style = (
        "padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere;"
        "border:1px solid var(--border-color-primary,#ddd);"
    )
    headers = "".join(
        f'<th scope="col" style="{cell_style}">{escape(label)}</th>'
        for label in EMOTION_STATE_HEADERS
    )
    body = "".join(
        "<tr>" + "".join(
            f'<td style="{cell_style}">{escape(value)}</td>' for value in row
        ) + "</tr>"
        for row in rows
    )
    return (
        '<div role="region" aria-label="本人の感情状態（閲覧専用）" tabindex="0" '
        'style="overflow-x:auto;">'
        '<table style="width:100%;min-width:1100px;table-layout:fixed;border-collapse:collapse;">'
        '<caption style="text-align:left;padding-bottom:8px;">本人の感情状態（閲覧専用）</caption>'
        f"<thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _explanation(item: Optional[Dict[str, Any]], *, is_new: bool = False) -> str:
    if is_new:
        return (
            "新しい感情項目を追加します。名前は必須、説明は任意です。"
            "使用する状態で追加します。意味が異なる場合は既存項目の改名ではなく、"
            "新しい項目として追加してください。"
        )
    if item is None:
        return "選択した感情項目を読み取れません。一覧を更新してください。"
    if item.get("status") == "unsupported":
        return "この保存状態に対応する項目定義がありません。保存による自動修復は行いません。"
    if item.get("definition_valid") is not True:
        return "項目定義が不正です。保存を無効にし、保存値を自動修復しません。"

    lines: List[str] = []
    configured_valid = item.get("configured_decay_policy_valid") is True
    configured_text = _policy_text(
        item.get("configured_decay_policy"), configured_valid
    )
    status = item.get("status")
    if status == "unreported":
        lines.append("本人の申告はまだありません。申告に適用中の時間条件はありません。")
    else:
        has_applied_policy_flag = "decay_policy_valid" in item
        applied_valid = item.get("decay_policy_valid") is True
        applied_text = _policy_text(
            item.get("decay_policy"), applied_valid,
            source=item.get("decay_policy_source"),
        )
        if not has_applied_policy_flag:
            lines.append(
                "本人の申告記録または状態形式が不正です。時間条件の不正とは断定せず、"
                "保存内容を自動修復しません。"
            )
        elif applied_valid:
            lines.append(f"この申告に適用中の時間条件: {applied_text}。")
            if item.get("decay_policy_source") == "legacy_default":
                lines.append("旧形式の申告のため、固定された標準条件を使っています。")
        else:
            lines.append(
                "申告に保存された時間条件が不正です。参考値は作らず、保存値を保持します。"
            )
            if item.get("reported_status") == "settled":
                lines.append("本人はこの気持ちを終了したと判断しています。")

    if configured_valid:
        if status != "unreported" and item.get("decay_policy_valid") is True:
            if _policy_equal(item.get("decay_policy"), item.get("configured_decay_policy")):
                lines.append("設定中の時間条件と同じです。")
            else:
                lines.append(
                    f"現在の設定中の時間条件: {configured_text}。"
                    "設定変更は既存の申告へ遡及せず、次の申告から適用します。"
                )
        else:
            lines.append(f"次の申告に使う設定中の時間条件: {configured_text}。")
    else:
        lines.append(
            "設定中の時間条件が不正です。未来の版や形式不正を含め、保存値を自動修復しません。"
            "既存申告に有効な適用条件があれば、その条件で状態の表示を継続します。"
            "次の気持ちの見直しには時間設定の修正が必要です。"
        )

    if item.get("presented") is True:
        lines.append(
            "提示対象は条件内にある候補を示します。毎ターンの最大2件選抜結果を示すものではありません。"
        )
    elif status == "active":
        state = _row_state(item)
        if state == "提示期間終了":
            lines.append("申告は提示期間を過ぎています。本人が終了したこととは区別されます。")
        elif state == "参考値が提示基準未満":
            lines.append("参考値が提示の閾値未満です。本人が終了したこととは区別されます。")
    if item.get("enabled") is False:
        lines.append("この項目は使用停止中です。保存済みの本人の申告は削除・終了しません。")
    return "\n".join(lines)


def _form_session(room_name: str, item: Dict[str, Any]) -> Dict[str, Any]:
    configured_valid = item.get("configured_decay_policy_valid") is True
    half_life, max_age = _policy_values(
        item.get("configured_decay_policy"), configured_valid
    )
    baseline = {
        "name": _as_display_text(item.get("name"), ""),
        "description": _as_display_text(item.get("description"), ""),
        "enabled": item.get("enabled") is True,
        "half_life_hours": half_life,
        "max_age_hours": max_age,
        "definition_revision": item.get("definition_revision"),
    }
    return _new_session(
        room_name,
        item.get("category"),
        baseline,
        is_new=False,
        configured_policy_valid=configured_valid,
        definition_valid=item.get("definition_valid") is True,
    )


def _form_outputs(
    *,
    item: Optional[Dict[str, Any]],
    session: Dict[str, Any],
    status: str,
    selected_category: Any,
    choices: List[Tuple[str, Any]],
    editable: bool,
    rows: List[List[str]],
    is_new: bool = False,
) -> Tuple[Any, ...]:
    if is_new:
        baseline = _new_baseline()
        name = baseline["name"]
        description = baseline["description"]
        enabled = baseline["enabled"]
        half_life = baseline["half_life_hours"]
        max_age = baseline["max_age_hours"]
    else:
        baseline = session.get("baseline", {}) if isinstance(session, dict) else {}
        name = _as_display_text(baseline.get("name"), "")
        description = _as_display_text(baseline.get("description"), "")
        enabled = baseline.get("enabled") is True
        half_life = baseline.get("half_life_hours")
        max_age = baseline.get("max_age_hours")

    # Definition validity is checked before this helper. A broken policy keeps the
    # metadata editable while its two numeric fields stay empty.
    name_editable = editable
    enabled_editable = editable and not is_new
    save_editable = editable
    detail = _explanation(item, is_new=is_new)
    save_label = "感情項目を追加" if is_new else "感情項目の設定を保存"
    return _ensure_output_count(
        (
            _state_table_html(rows),
            gr.update(choices=choices, value=selected_category, interactive=bool(choices)),
            gr.update(value=name, interactive=name_editable),
            gr.update(value=description, interactive=name_editable),
            gr.update(value=enabled, interactive=enabled_editable),
            gr.update(value=half_life, interactive=editable),
            gr.update(value=max_age, interactive=editable),
            gr.update(value=detail, interactive=False),
            gr.update(value=save_label, interactive=save_editable),
            status,
            session,
        )
    )


def _valid_room_reader(room_name: Any) -> Tuple[EmotionDefinitionManager, MotivationManager]:
    if not isinstance(room_name, str) or not room_name.strip():
        raise ValueError("ルームが選択されていません。")
    definitions = EmotionDefinitionManager(room_name)
    if not definitions.config_file.parent.is_dir() or not definitions.config_file.is_file():
        raise ValueError("既存ルームの設定ファイルを読み込めません。ルームを確認してください。")
    # initialize=False はmemoryディレクトリ、感情ログ、内部状態を新規作成しない。
    manager = MotivationManager(room_name, initialize=False)
    return definitions, manager


def _room_status(room_name: Any, message: str) -> str:
    room = _as_display_text(room_name, "未選択")
    return f"対象ルーム: {room}。{message}"


def _friendly_error(exc: Exception) -> str:
    """内部のCAS/API用語を、画面上の自然な案内へ置き換える。"""
    message = str(exc)
    lowered = message.lower()
    if "revision" in lowered or "更新されています" in message or "対象が変わっています" in message:
        return "最新の設定が変わっています。最新設定を再読してから保存してください。"
    if "項目id" in lowered or "感情項目id" in lowered:
        return "同じ項目を確認できません。最新設定を再読してください。"
    if "使用状態" in message:
        return message.replace("使用状態", "使用する状態")
    return message


def _read_catalog(room_name: str) -> Tuple[List[Dict[str, Any]], List[Tuple[str, Any]]]:
    _definitions, manager = _valid_room_reader(room_name)
    catalog = manager.get_emotion_catalog()
    if not isinstance(catalog, list):
        raise ValueError("感情項目一覧の形式が不正です。")
    return catalog, _choices(catalog)


def _invalid_panel_outputs(status: str) -> Tuple[Any, ...]:
    # NEWは選択肢として常に残すが、ルームが無効な間は操作を受け付けない。
    return _ensure_output_count(
        (
            _state_table_html([]),
            gr.update(choices=[_new_choice()], value=None, interactive=False),
            gr.update(value="", interactive=False),
            gr.update(value="", interactive=False),
            gr.update(value=True, interactive=False),
            gr.update(value=None, interactive=False),
            gr.update(value=None, interactive=False),
            gr.update(value="", interactive=False),
            gr.update(value="感情項目の設定を保存", interactive=False),
            status,
            {},
        )
    )


def _preserve_outputs(status: str) -> Tuple[Any, ...]:
    """保存失敗時にフォーム・選択・CAS Stateを一切再読せず保持する。"""
    return _ensure_output_count((*(gr.update() for _ in range(9)), status, gr.update()))


def _saved_but_reload_failed(room_name: Any, exc: Exception) -> Tuple[Any, ...]:
    """書込み後の再読失敗を保存失敗と混同せず、安全側へ閉じる。"""
    return _invalid_panel_outputs(
        _room_status(
            room_name,
            "保存済みですが再読に失敗しました。重複保存を避けるため、"
            f"再読が完了するまで操作を無効にしています: {_friendly_error(exc)}",
        )
    )


def _panel_from_catalog(
    room_name: str,
    catalog: List[Dict[str, Any]],
    choices: List[Tuple[str, Any]],
    category: Any = _MISSING,
    status: str = "感情項目を読み込みました。",
) -> Tuple[Any, ...]:
    definition_items = [item for item in catalog if "definition_valid" in item]
    if category is _MISSING:
        category = definition_items[0].get("category") if definition_items else NEW_EMOTION

    if category == NEW_EMOTION:
        baseline = _new_baseline()
        session = _new_session(room_name, NEW_EMOTION, baseline, is_new=True)
        output = _form_outputs(
            item=None,
            session=session,
            status=status,
            selected_category=NEW_EMOTION,
            choices=choices,
            editable=True,
            rows=_state_rows(catalog),
            is_new=True,
        )
        return output

    item = _find_item(catalog, category)
    if item is None or "definition_valid" not in item:
        # stale/未対応IDは選択状態を不用意に書き換えず、編集欄をreadonlyにする。
        session: Dict[str, Any] = {}
        output = _form_outputs(
            item=item,
            session=session,
            status=_room_status(room_name, "選択した感情項目は一覧にありません。保存を無効にしました。"),
            selected_category=category,
            choices=choices,
            editable=False,
            rows=_state_rows(catalog),
        )
        return output

    session = _form_session(room_name, item)
    editable = item.get("definition_valid") is True
    output = _form_outputs(
        item=item,
        session=session,
        status=status,
        selected_category=category,
        choices=choices,
        editable=editable,
        rows=_state_rows(catalog),
    )
    return output


def handle_load_emotion_panel(room_name):
    """ルーム選択または画面ロード時に、一覧と設定フォームを読み込む。"""
    try:
        if not isinstance(room_name, str) or not room_name.strip():
            return _invalid_panel_outputs(_room_status(room_name, "ルームが選択されていません。"))
        catalog, choices = _read_catalog(room_name)
        return _panel_from_catalog(
            room_name,
            catalog,
            choices,
            status=(
                _room_status(
                    room_name,
                    "感情項目を読み込みました。提示対象は条件内の候補であり、"
                    "毎ターンの最大2件選抜結果ではありません。",
                )
            ),
        )
    except Exception as exc:
        return _invalid_panel_outputs(
            _room_status(room_name, f"感情項目の読み込みに失敗しました: {_friendly_error(exc)}")
        )


def handle_select_emotion_definition(room_name, category, session):
    """定義を選択してフォームを更新する。選択自体は永続化しない。"""
    try:
        if not isinstance(room_name, str) or not room_name.strip():
            return _invalid_panel_outputs(_room_status(room_name, "ルームが選択されていません。"))
        catalog, choices = _read_catalog(room_name)
        if category == NEW_EMOTION:
            return _panel_from_catalog(
                room_name, catalog, choices, NEW_EMOTION,
                _room_status(
                    room_name,
                    "新しい感情項目の追加フォームです。保存時に新しい項目を追加します。",
                ),
            )
        return _panel_from_catalog(
            room_name, catalog, choices, category,
            _room_status(
                room_name,
                "感情項目を読み込みました。名前変更は同じ項目として扱い、"
                "意味が異なる項目は新規追加します。",
            ),
        )
    except Exception as exc:
        # 選択時の読取失敗も、古いフォームを消さずstatusだけ伝える。
        return _preserve_outputs(
            _room_status(room_name, f"感情項目の読み込みに失敗しました: {_friendly_error(exc)}")
        )


def _session_matches(session: Any, room_name: str, category: Any) -> bool:
    if not isinstance(session, dict) or not session:
        return False
    return session.get("room") == room_name and session.get("category") == category


def _float_input(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _build_policy_from_inputs(half_life_hours: Any, max_age_hours: Any) -> Dict[str, Any]:
    half_life = _float_input(half_life_hours)
    max_age = _float_input(max_age_hours)
    if half_life is None or max_age is None:
        raise ValueError("半減期と最長提示期間は両方入力してください。")
    return EmotionDecayPolicy.validate(
        {
            "version": 1,
            "half_life_hours": half_life,
            "max_age_hours": max_age,
        }
    )


def _existing_update_kwargs(
    session: Dict[str, Any],
    name: Any,
    description: Any,
    enabled: Any,
    half_life_hours: Any,
    max_age_hours: Any,
) -> Dict[str, Any]:
    baseline = session.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("保存時のフォーム基準がありません。一覧を再読してください。")
    if not isinstance(name, str) or not isinstance(description, str) or not isinstance(enabled, bool):
        raise ValueError("名前・説明・使用状態の入力形式を確認してください。")

    kwargs: Dict[str, Any] = {}
    if name != baseline.get("name"):
        kwargs["name"] = name
    if description != baseline.get("description"):
        kwargs["description"] = description
    if enabled != baseline.get("enabled"):
        kwargs["enabled"] = enabled

    baseline_half = baseline.get("half_life_hours")
    baseline_max = baseline.get("max_age_hours")
    configured_valid = session.get("configured_policy_valid") is True
    half_is_none = half_life_hours is None
    max_is_none = max_age_hours is None
    if half_is_none != max_is_none:
        raise ValueError("半減期と最長提示期間は両方入力してください。")
    if half_is_none and max_is_none:
        if configured_valid:
            raise ValueError("半減期と最長提示期間を入力してください。")
        # 破損した現在設定は空欄のまま保持し、metadataだけ更新できる。
    else:
        policy = _build_policy_from_inputs(half_life_hours, max_age_hours)
        current_policy = {
            "version": 1,
            "half_life_hours": baseline_half,
            "max_age_hours": baseline_max,
        }
        if not configured_valid or not _policy_equal(policy, current_policy):
            kwargs["decay_policy"] = policy
    return kwargs


def handle_save_emotion_definition(
    room_name,
    category,
    name,
    description,
    enabled,
    half_life_hours,
    max_age_hours,
    session,
):
    """定義をCAS付きで差分保存し、成功時だけ一覧とフォームを再読する。"""
    if not _session_matches(session, room_name, category):
        return _preserve_outputs(
            _room_status(
                room_name,
                "画面のルームまたは項目が現在の選択と異なります。"
                "保存せず、最新設定を再読してください。",
            )
        )

    is_new = category == NEW_EMOTION
    if not is_new and session.get("definition_valid") is not True:
        return _preserve_outputs(
            _room_status(
                room_name,
                "選択した項目の定義が不正なため保存できません。"
                "保存値を自動修復せず、最新設定を再読してください。",
            )
        )
    try:
        definitions, _manager = _valid_room_reader(room_name)
        if is_new:
            if enabled is not True:
                return _preserve_outputs(
                    _room_status(
                        room_name,
                        "新しい項目は使用する状態で追加してください。"
                        "使用しない状態のまま追加する操作は行いません。",
                    )
                )
            policy = _build_policy_from_inputs(half_life_hours, max_age_hours)
            created = definitions.add(name, description, decay_policy=policy)
            created_category = created.get("id") if isinstance(created, dict) else None
            if not EmotionDefinitionManager.is_supported_id(created_category):
                return _saved_but_reload_failed(
                    room_name,
                    ValueError("追加した項目を確認できません。"),
                )
            try:
                catalog, choices = _read_catalog(room_name)
                return _panel_from_catalog(
                    room_name,
                    catalog,
                    choices,
                    created_category,
                    _room_status(
                        room_name,
                        f"感情項目「{name}」を追加しました。新しい項目を選択しています。",
                    ),
                )
            except Exception as exc:
                return _saved_but_reload_failed(room_name, exc)

        if not isinstance(category, str) or not EmotionDefinitionManager.is_supported_id(category):
            return _preserve_outputs(
                _room_status(room_name, "保存対象の項目を確認できません。保存を無効にしました。")
            )

        # セッションに保存した最新設定の照合値をそのままbackendへ渡す。
        expected_revision = session.get("definition_revision")
        if not isinstance(expected_revision, str) or not expected_revision:
            return _preserve_outputs(
                _room_status(
                    room_name,
                    "保存時の最新設定を確認できません。最新設定を再読してください。",
                )
            )
        kwargs = _existing_update_kwargs(
            session,
            name,
            description,
            enabled,
            half_life_hours,
            max_age_hours,
        )
        try:
            # 差分が空でもupdateを通す。backend側のnoopは書き込まず、
            # その時点の定義版だけをCASで確認する。
            definitions.update(category, expected_revision=expected_revision, **kwargs)
        except Exception as exc:
            return _preserve_outputs(
                _room_status(room_name, f"感情項目の保存に失敗しました: {_friendly_error(exc)}")
            )

        save_status = (
            f"感情項目「{name}」を保存しました。"
            if kwargs
            else "変更はありません。最新の設定を確認しました。"
        )

        # 更新成功後のみ再読する。再読に失敗した場合は、保存済みのまま
        # save/sessionを無効にして、同じ値を重複保存できないようにする。
        try:
            catalog, choices = _read_catalog(room_name)
            return _panel_from_catalog(
                room_name,
                catalog,
                choices,
                category,
                _room_status(room_name, save_status),
            )
        except Exception as exc:
            return _saved_but_reload_failed(room_name, exc)
    except Exception as exc:
        return _preserve_outputs(
            _room_status(room_name, f"感情項目の保存に失敗しました: {_friendly_error(exc)}")
        )


__all__ = [
    "NEW_EMOTION",
    "handle_load_emotion_panel",
    "handle_select_emotion_definition",
    "handle_save_emotion_definition",
]
