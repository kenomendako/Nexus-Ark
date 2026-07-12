"""Tools for reading and changing persona closet appearance profiles."""

from typing import List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

import closet_manager
from src.features.item_manager import ItemManager


class ChangeOutfitArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    note: str = Field("", description="現在の装いの補足メモ")
    worn_ids: Optional[List[str]] = Field(None, description="着用中にするクローゼット項目IDのリスト")


class RegisterItemToClosetArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    item_id: str = Field(..., description="インベントリ上のアイテムID")
    part: str = Field("その他", description=f"部位。{'/'.join(closet_manager.CLOSET_PARTS)}")
    owner: str = Field("ペルソナ", description="アイテム所有者。ユーザー または ペルソナ")


def _closet_disabled(room_name: str) -> bool:
    return not closet_manager.load_persona_profile(room_name).get("enabled")


def _user_closet_disabled(room_name: str) -> bool:
    return not closet_manager.load_user_profile(room_name).get("enabled")


def _format_item_line(item: dict, worn_ids=None) -> str:
    marker = "（着用中）" if item.get("id") in set(worn_ids or []) else ""
    desc = f" - {item.get('description')}" if item.get("description") else ""
    ref = f" / 参照画像: {item.get('reference_image')}" if item.get("reference_image") else ""
    return f"- {item.get('id')}: {item.get('name')} [{item.get('part')}] {marker}{desc}{ref}".rstrip()


def _item_description(item: dict) -> str:
    parts = []
    for key in ("description", "base_info", "flavor_text"):
        value = str(item.get(key) or "").strip()
        if value:
            parts.append(value)
    appearance = item.get("appearance") if isinstance(item.get("appearance"), dict) else {}
    for key in ("description", "design", "texture"):
        value = str(appearance.get(key) or "").strip()
        if value:
            parts.append(value)
    return "\n".join(dict.fromkeys(parts))


@tool
def read_closet(room_name: str) -> str:
    """
    ペルソナ自身のクローゼット外見プロファイルを読む。
    自分自身の姿を含む画像生成や、外見の一貫性が必要な場面で使う。
    enabled=false の場合は、クローゼット未設定として扱う。
    参照画像パスがある場合は、必要に応じて view_past_image で視覚に読み込める。
    """
    try:
        profile = closet_manager.load_persona_profile(room_name)
        base = profile.get("base", {}) or {}
        description = str(base.get("description") or "").strip()
        reference_images = base.get("reference_images") or []

        if not profile.get("enabled"):
            return "【クローゼット未設定】このルームではクローゼット外見プロファイルが無効です。"

        lines = ["【クローゼット外見プロファイル】", "", "## ベース外見"]
        lines.append(description if description else "（ベース外見の説明は未入力です）")
        lines.extend(["", "## 参照画像"])
        if reference_images:
            lines.extend(f"- {path}" for path in reference_images)
            lines.append("")
            lines.append("参照画像は必要に応じて `view_past_image` で確認できます。")
        else:
            lines.append("（参照画像は登録されていません）")

        current = profile.get("current", {}) or {}
        worn_ids = current.get("worn") or []
        worn_items = [closet_manager.get_closet_item(room_name, closet_id) for closet_id in worn_ids]
        worn_items = [item for item in worn_items if item]
        lines.extend(["", "## 現在の装い"])
        note = str(current.get("note") or "").strip()
        lines.append(f"メモ: {note}" if note else "メモ: （未入力）")
        if worn_items:
            for item in worn_items:
                lines.append(_format_item_line(item, worn_ids))
            if any(item.get("reference_image") for item in worn_items):
                lines.append("着用項目の参照画像は必要に応じて `view_past_image` で確認できます。")
        else:
            lines.append("（着用中のクローゼット項目はありません）")
        return "\n".join(lines)
    except Exception as e:
        return f"【エラー】クローゼット外見プロファイルの読み込みに失敗しました: {e}"


@tool
def read_user_closet(room_name: str) -> str:
    """
    ユーザー（相手）のクローゼット外見プロファイルを読む。
    ユーザーの姿を含む画像生成や、相手の外見の一貫性が必要な場面で使う。
    enabled=false の場合は、ユーザー外見未設定として扱う。
    参照画像パスがある場合は、必要に応じて view_past_image で視覚に読み込める。
    """
    try:
        profile = closet_manager.load_user_profile(room_name)
        base = profile.get("base", {}) or {}
        description = str(base.get("description") or "").strip()
        reference_images = base.get("reference_images") or []

        if not profile.get("enabled"):
            return "【ユーザー外見未設定】このルームではユーザー外見プロファイルが無効、または未設定です。"

        scope_label = "共通設定" if closet_manager.is_user_closet_common(room_name) else "このルーム専用"
        lines = ["【ユーザー外見プロファイル】", f"適用元: {scope_label}", "", "## ベース外見"]
        lines.append(description if description else "（ユーザー外見の説明は未入力です）")
        lines.extend(["", "## 参照画像"])
        if reference_images:
            lines.extend(f"- {path}" for path in reference_images)
            lines.append("")
            lines.append("参照画像は必要に応じて `view_past_image` で確認できます。")
        else:
            lines.append("（参照画像は登録されていません）")

        current = profile.get("current", {}) or {}
        worn_ids = current.get("worn") or []
        worn_items = [closet_manager.get_effective_user_closet_item(room_name, closet_id) for closet_id in worn_ids]
        worn_items = [item for item in worn_items if item]
        lines.extend(["", "## 現在の装い"])
        note = str(current.get("note") or "").strip()
        lines.append(f"メモ: {note}" if note else "メモ: （未入力）")
        if worn_items:
            for item in worn_items:
                lines.append(_format_item_line(item, worn_ids))
            if any(item.get("reference_image") for item in worn_items):
                lines.append("着用項目の参照画像は必要に応じて `view_past_image` で確認できます。")
        else:
            lines.append("（着用中のユーザークローゼット項目はありません）")
        return "\n".join(lines)
    except Exception as e:
        return f"【エラー】ユーザー外見プロファイルの読み込みに失敗しました: {e}"


@tool
def list_closet(room_name: str) -> str:
    """クローゼットに登録済みの着用可能アイテム一覧を読む。"""
    try:
        if _closet_disabled(room_name):
            return "【クローゼット未設定】このルームではクローゼット外見プロファイルが無効です。"
        profile = closet_manager.load_persona_profile(room_name)
        worn_ids = profile.get("current", {}).get("worn") or []
        items = closet_manager.list_closet_items(room_name)
        if not items:
            return "【クローゼット】着用可能アイテムはまだ登録されていません。"
        lines = ["【クローゼット項目一覧】"]
        lines.extend(_format_item_line(item, worn_ids) for item in items)
        return "\n".join(lines)
    except Exception as e:
        return f"【エラー】クローゼット一覧の読み込みに失敗しました: {e}"


@tool
def wear_closet_item(room_name: str, closet_id: str) -> str:
    """指定したクローゼット項目を現在の装いに追加する。"""
    try:
        if _closet_disabled(room_name):
            return "【クローゼット未設定】このルームではクローゼット外見プロファイルが無効です。"
        profile = closet_manager.wear_item(room_name, closet_id)
        item = closet_manager.get_closet_item(room_name, closet_id)
        return f"【着用しました】{item.get('name') if item else closet_id}\n現在の着用ID: {', '.join(profile.get('current', {}).get('worn') or []) or 'なし'}"
    except Exception as e:
        return f"【エラー】クローゼット項目の着用に失敗しました: {e}"


@tool
def take_off_closet_item(room_name: str, closet_id: str) -> str:
    """指定したクローゼット項目を現在の装いから外す。"""
    try:
        if _closet_disabled(room_name):
            return "【クローゼット未設定】このルームではクローゼット外見プロファイルが無効です。"
        item = closet_manager.get_closet_item(room_name, closet_id)
        profile = closet_manager.take_off_item(room_name, closet_id)
        return f"【脱ぎました】{item.get('name') if item else closet_id}\n現在の着用ID: {', '.join(profile.get('current', {}).get('worn') or []) or 'なし'}"
    except Exception as e:
        return f"【エラー】クローゼット項目の解除に失敗しました: {e}"


@tool(args_schema=ChangeOutfitArgs)
def change_outfit(room_name: str, note: str = "", worn_ids: Optional[List[str]] = None) -> str:
    """現在の装いメモと着用中クローゼット項目IDをまとめて更新する。"""
    try:
        if _closet_disabled(room_name):
            return "【クローゼット未設定】このルームではクローゼット外見プロファイルが無効です。"
        profile = closet_manager.set_current_outfit(room_name, note, worn_ids or [])
        return f"【現在の装いを更新しました】\nメモ: {profile.get('current', {}).get('note') or '（未入力）'}\n着用ID: {', '.join(profile.get('current', {}).get('worn') or []) or 'なし'}"
    except Exception as e:
        return f"【エラー】現在の装いの更新に失敗しました: {e}"


@tool(args_schema=RegisterItemToClosetArgs)
def register_item_to_closet(room_name: str, item_id: str, part: str = "その他", owner: str = "ペルソナ") -> str:
    """インベントリの既存アイテムを、着用可能なクローゼット項目として登録する。"""
    try:
        is_user = str(owner or "").strip() == "ユーザー"
        if is_user:
            if _user_closet_disabled(room_name):
                return "【ユーザー外見未設定】このルームではユーザー外見プロファイルが無効、または未設定です。"
        elif _closet_disabled(room_name):
            return "【クローゼット未設定】このルームではクローゼット外見プロファイルが無効です。"

        manager = ItemManager(room_name)
        item = manager.get_item(item_id, is_user=is_user)
        if not item:
            other = manager.get_item(item_id, is_user=not is_user)
            if other:
                item = other
            else:
                return f"【登録できません】アイテムが見つかりません: {item_id}"
        image_path = item.get("image_path") or ""
        if not image_path:
            return "【登録できません】このアイテムには参照画像がありません。"
        if is_user:
            scope = "common" if closet_manager.is_user_closet_common(room_name) else "room"
            closet_item = closet_manager.add_user_closet_item(
                scope=scope,
                room_name=room_name,
                name=item.get("name") or "名称未設定",
                part=part,
                description=_item_description(item),
                reference_image=image_path,
                source="generated",
                linked_item_id=item.get("id") or item_id,
                tags=[item.get("category")] if item.get("category") else [],
            )
            return f"【ユーザークローゼット登録済み】{closet_item.get('name')} [{closet_item.get('part')}] / ID: {closet_item.get('id')}"
        else:
            closet_item = closet_manager.add_closet_item(
                room_name=room_name,
                name=item.get("name") or "名称未設定",
                part=part,
                description=_item_description(item),
                reference_image=image_path,
                source="generated",
                linked_item_id=item.get("id") or item_id,
                tags=[item.get("category")] if item.get("category") else [],
            )
            return f"【クローゼット登録済み】{closet_item.get('name')} [{closet_item.get('part')}] / ID: {closet_item.get('id')}"
    except Exception as e:
        return f"【エラー】クローゼット登録に失敗しました: {e}"
