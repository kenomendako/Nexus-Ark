"""Lite から本体ランタイムを安全に制御するための小さな契約。

このモジュールは API Gateway の配線や認証を持たず、呼び出し側が room の
存在確認・権限確認を済ませた後に使う。外部 LLM 要求を強制終了する機能では
なく、同じ room/request の処理が次の再試行へ進む前に停止を伝えるための
インメモリな協調停止境界を提供する。

すでに開始した外部要求はこの境界から取り消せない。停止を受け付けても、
実行済み要求の費用や副作用を取り消したり返金したりすることはできない。
呼び出し側は ``scope.checkpoint()`` を API 呼び出しの直前とリトライ待機の
後に置き、``mark_external_request_started/finished`` で進行中の要求を記録する。

一時的現在地は本体の ``TemporaryLocationManager`` を正本として扱う。Lite は
必要時の読取、手動テキストの適用、モード解除だけを行い、GPS 取得・画像からの
LLM 情景生成・保存済み地点一覧の自動読込を行わない。
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Dict, Literal, Mapping, Optional


MAX_REQUEST_ID_LENGTH = 200
MAX_SCENERY_TEXT_LENGTH = 4_000
MAX_SOURCE_DESCRIPTION_LENGTH = 500


_CURRENT_LITE_REQUEST: contextvars.ContextVar[Optional["LiteRequestScope"]] = contextvars.ContextVar(
    "nexus_ark_lite_request_scope",
    default=None,
)


class LiteRequestStopped(RuntimeError):
    """Lite の応答要請が協調停止されたことを示す。"""

    code = "lite_request_stopped"

    def __init__(
        self,
        room_id: str,
        request_id: str,
        *,
        external_request_in_flight: bool = False,
    ) -> None:
        self.room_id = room_id
        self.request_id = request_id
        self.external_request_in_flight = external_request_in_flight
        super().__init__(
            "Lite応答要請の停止を受け付けました。"
            "すでに開始した外部要求は取り消せず、以後の再試行だけを抑止します。"
        )


@dataclass(frozen=True)
class LiteStopResult:
    """停止要求の結果。秘密値や本文は含めない。"""

    room_id: str
    request_id: Optional[str]
    status: Literal["requested", "already_requested", "not_found"]
    matched_request_ids: tuple[str, ...] = ()
    external_request_in_flight_count: int = 0

    @property
    def accepted(self) -> bool:
        return self.status in {"requested", "already_requested"}

    def as_dict(self) -> Dict[str, Any]:
        """API response にそのまま変換できる公開用辞書。"""

        return {
            "room_id": self.room_id,
            "request_id": self.request_id,
            "status": self.status,
            "accepted": self.accepted,
            "matched_request_ids": list(self.matched_request_ids),
            "external_request_in_flight_count": self.external_request_in_flight_count,
            "external_request_cancellation": "unsupported",
            "fee_refund": "unsupported",
        }


@dataclass(frozen=True)
class LiteActiveRequest:
    """現在実行中のLite要請の公開可能な状態。"""

    room_id: str
    request_id: str
    stop_requested: bool = False
    external_request_in_flight: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "request_id": self.request_id,
            "stop_requested": self.stop_requested,
            "external_request_in_flight": self.external_request_in_flight,
        }


@dataclass(frozen=True)
class TemporaryLocationState:
    """一時的現在地の Lite 公開用スナップショット。

    ``image_path`` はローカルファイルパスなので既定の ``as_dict`` では公開
    しない。Lite の現在地共有はテキストを契約の中心にし、画像配信が必要な
    場合は別の認証済み asset 契約を利用する。
    """

    room_id: str
    active: bool
    scenery_text: str = ""
    source_description: str = ""
    created_at: str = ""
    image_path: str = ""
    location_name: str = ""

    def as_dict(
        self,
        *,
        include_image_path: bool = False,
        image_asset_url: Optional[str] = None,
        public_image_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "room_id": self.room_id,
            "active": self.active,
            "scenery_text": self.scenery_text,
            "location_name": self.location_name,
            "source_description": self.source_description,
            "created_at": self.created_at,
            "has_image": bool(self.image_path),
            "image_path": public_image_path,
            "image_asset_url": image_asset_url,
        }
        if include_image_path:
            result["image_path"] = self.image_path
        return result


@dataclass
class _RequestRecord:
    request_id: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    external_request_in_flight: int = 0
    # サービス側scopeが閉じても、graph workerの外部要求が残っている間は
    # 停止イベントをregistryに保持する。
    scope_closed: bool = False


class LiteRequestScope:
    """ひとつの Lite 応答要請に紐づく停止ハンドル。

    ``LiteRuntimeControls.begin`` が返す。サービス層は処理の最初に
    ``checkpoint`` を呼び、LLM stream や provider のリトライ境界で同じ
    checkpoint を呼ぶ。``close`` は必ず finally で実行する。外部要求が
    残っている場合、close後もgraph workerが停止イベントを観測できるよう
    registry上の記録を保持する。
    """

    def __init__(self, controller: "LiteRuntimeControls", room_id: str, request_id: str) -> None:
        self._controller = controller
        self.room_id = room_id
        self.request_id = request_id
        self._closed = False

    @property
    def stop_requested(self) -> bool:
        return self._controller.is_stop_requested(self.room_id, self.request_id)

    @property
    def external_request_in_flight(self) -> bool:
        return self._controller.external_request_in_flight(self.room_id, self.request_id)

    def checkpoint(self) -> None:
        """停止済みなら例外を送出し、以後の retry/continuation を止める。"""

        if self.stop_requested:
            raise LiteRequestStopped(
                self.room_id,
                self.request_id,
                external_request_in_flight=self.external_request_in_flight,
            )

    # 親側の retry 実装から意味を読み取りやすくする別名。
    before_retry = checkpoint

    def mark_external_request_started(self) -> None:
        """外部要求の開始を記録する。要求そのものはここでは発行しない。"""

        self.checkpoint()
        self._controller.mark_external_request_started(self.room_id, self.request_id)

    def mark_external_request_finished(self) -> None:
        """外部要求の完了を記録する。二重完了は無視する。"""

        self._controller.mark_external_request_finished(self.room_id, self.request_id)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._controller.end(self.room_id, self.request_id, self)

    def wait(self, timeout_seconds: float) -> bool:
        """停止通知を待ちながらretry待機する。停止時は例外を送出する。"""

        timeout = max(0.0, float(timeout_seconds))
        if timeout <= 0:
            self.checkpoint()
            return False
        stopped = self._controller.wait_for_stop(self.room_id, self.request_id, timeout)
        if stopped:
            self.checkpoint()
        return stopped

    def __enter__(self) -> "LiteRequestScope":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


class LiteRuntimeControls:
    """room/request 単位のインメモリな協調停止レジストリ。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, _RequestRecord]] = {}

    @staticmethod
    def _room_id(room_id: str) -> str:
        value = str(room_id or "").strip()
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("valid room_id is required")
        return value

    @staticmethod
    def _request_id(request_id: Optional[str]) -> str:
        value = str(request_id or "").strip()
        if len(value) > MAX_REQUEST_ID_LENGTH:
            raise ValueError("request_id is too long")
        return value or f"lite-{uuid.uuid4().hex}"

    def begin(self, room_id: str, request_id: Optional[str] = None) -> LiteRequestScope:
        room = self._room_id(room_id)
        request = self._request_id(request_id)
        with self._lock:
            room_records = self._active.setdefault(room, {})
            if request in room_records:
                raise ValueError("request_id is already active for this room")
            room_records[request] = _RequestRecord(request)
        return LiteRequestScope(self, room, request)

    def stop(self, room_id: str, request_id: Optional[str] = None) -> LiteStopResult:
        """現在アクティブな処理を止め、将来の別要請は止めない。

        ``request_id`` を省略すると room 内の全アクティブ要請を対象にする。
        対象がない場合は ``not_found`` を返す。これは「次に開始される要請を
        永続停止する」操作ではないため、誤って後続の会話を巻き込まない。
        """

        room = self._room_id(room_id)
        request = str(request_id or "").strip() or None
        if request and len(request) > MAX_REQUEST_ID_LENGTH:
            raise ValueError("request_id is too long")
        with self._lock:
            records = self._active.get(room, {})
            if request is None:
                selected = list(records.values())
            else:
                target = records.get(request)
                selected = [target] if target is not None else []
            if not selected:
                return LiteStopResult(room, request, "not_found")

            was_already_requested = all(record.stop_event.is_set() for record in selected)
            for record in selected:
                record.stop_event.set()
            return LiteStopResult(
                room,
                request,
                "already_requested" if was_already_requested else "requested",
                tuple(record.request_id for record in selected),
                sum(record.external_request_in_flight for record in selected),
            )

    def is_stop_requested(self, room_id: str, request_id: str) -> bool:
        room = self._room_id(room_id)
        request = self._request_id(request_id)
        with self._lock:
            record = self._active.get(room, {}).get(request)
            return bool(record and record.stop_event.is_set())

    def external_request_in_flight(self, room_id: str, request_id: str) -> bool:
        room = self._room_id(room_id)
        request = self._request_id(request_id)
        with self._lock:
            record = self._active.get(room, {}).get(request)
            return bool(record and record.external_request_in_flight > 0)

    def mark_external_request_started(self, room_id: str, request_id: str) -> None:
        room = self._room_id(room_id)
        request = self._request_id(request_id)
        with self._lock:
            record = self._active.get(room, {}).get(request)
            if record is None:
                raise RuntimeError("Lite request scope is no longer active")
            if record.stop_event.is_set():
                raise LiteRequestStopped(
                    room,
                    request,
                    external_request_in_flight=record.external_request_in_flight > 0,
                )
            record.external_request_in_flight += 1

    def mark_external_request_finished(self, room_id: str, request_id: str) -> None:
        room = self._room_id(room_id)
        request = self._request_id(request_id)
        with self._lock:
            record = self._active.get(room, {}).get(request)
            if record is None:
                return
            record.external_request_in_flight = max(0, record.external_request_in_flight - 1)
            if record.scope_closed and record.external_request_in_flight == 0:
                self._remove_record_locked(room, request)

    def wait_for_stop(self, room_id: str, request_id: str, timeout_seconds: float) -> bool:
        room = self._room_id(room_id)
        request = self._request_id(request_id)
        with self._lock:
            record = self._active.get(room, {}).get(request)
            if record is None:
                return False
            stop_event = record.stop_event
        return stop_event.wait(max(0.0, float(timeout_seconds)))

    def end(self, room_id: str, request_id: str, scope: LiteRequestScope) -> None:
        room = self._room_id(room_id)
        request = self._request_id(request_id)
        with self._lock:
            record = self._active.get(room, {}).get(request)
            if record is None:
                return
            # 同じ request_id を別 scope が誤って消さないよう identity を確認する。
            if getattr(scope, "_controller", None) is not self:
                return
            record.scope_closed = True
            if record.external_request_in_flight == 0:
                self._remove_record_locked(room, request)

    def _remove_record_locked(self, room: str, request: str) -> None:
        """呼び出し側が ``self._lock`` を保持している前提でregistryから除去する。"""
        records = self._active.get(room)
        if records is None:
            return
        records.pop(request, None)
        if not records:
            self._active.pop(room, None)

    def active_request_ids(self, room_id: str) -> tuple[str, ...]:
        room = self._room_id(room_id)
        with self._lock:
            return tuple(self._active.get(room, {}).keys())

    def active_requests(self, room_id: str) -> tuple[LiteActiveRequest, ...]:
        room = self._room_id(room_id)
        with self._lock:
            records = tuple(self._active.get(room, {}).values())
            return tuple(
                LiteActiveRequest(
                    room_id=room,
                    request_id=record.request_id,
                    stop_requested=record.stop_event.is_set(),
                    external_request_in_flight=record.external_request_in_flight > 0,
                )
                for record in records
            )


_DEFAULT_RUNTIME_CONTROLS = LiteRuntimeControls()


def begin_lite_request(room_id: str, request_id: Optional[str] = None) -> LiteRequestScope:
    """プロセス内共有controllerで要請を開始する。"""

    return _DEFAULT_RUNTIME_CONTROLS.begin(room_id, request_id)


@contextmanager
def lite_request_scope(room_id: str, request_id: Optional[str] = None):
    """サービス層から使う共有scope。

    contextvarへ登録するため、同じスレッド内のgemini/LLMFactory呼び出しは
    request_idを引数で受け渡さなくても同じ停止境界を参照できる。別スレッドへ
    処理を移す場合は ``contextvars.copy_context().run(...)`` を使う。
    """

    scope = begin_lite_request(room_id, request_id)
    try:
        with bind_lite_request_scope(scope):
            scope.checkpoint()
            yield scope
    finally:
        scope.close()


@contextmanager
def bind_lite_request_scope(scope: Optional[LiteRequestScope]):
    """既存scopeを現在のcontextへ束縛する（スレッド引き継ぎ用）。"""

    if scope is None:
        yield None
        return
    token = _CURRENT_LITE_REQUEST.set(scope)
    try:
        yield scope
    finally:
        _CURRENT_LITE_REQUEST.reset(token)


def get_current_lite_request_scope() -> Optional[LiteRequestScope]:
    """現在のcontextに紐づくscopeを返す。"""

    scope = _CURRENT_LITE_REQUEST.get()
    if scope is None:
        return None
    # サービス側が先にcloseしても、graph workerが外部要求中ならscopeを
    # 維持し、LLMFactory側のretry/checkpointへ停止を伝える。
    if scope._closed and not scope.external_request_in_flight:
        return None
    return scope


def checkpoint_lite_request(scope: Optional[LiteRequestScope] = None) -> None:
    """現在のLite処理が停止済みなら例外を送出する。"""

    target = scope or get_current_lite_request_scope()
    if target is not None:
        target.checkpoint()


def wait_lite_retry(
    timeout_seconds: float,
    scope: Optional[LiteRequestScope] = None,
) -> None:
    """Lite停止を監視しながらretry待機する。scope外では通常sleepする。"""

    target = scope or get_current_lite_request_scope()
    if target is None:
        time.sleep(max(0.0, float(timeout_seconds)))
        return
    target.wait(timeout_seconds)


interruptible_lite_sleep = wait_lite_retry


def mark_lite_external_request_started(scope: Optional[LiteRequestScope] = None) -> None:
    target = scope or get_current_lite_request_scope()
    if target is not None:
        target.mark_external_request_started()


def mark_lite_external_request_finished(scope: Optional[LiteRequestScope] = None) -> None:
    target = scope or get_current_lite_request_scope()
    if target is not None:
        target.mark_external_request_finished()


def stop_lite_request(room_id: str, request_id: Optional[str] = None) -> LiteStopResult:
    """プロセス内共有controllerで現在の要請を停止する。"""

    return _DEFAULT_RUNTIME_CONTROLS.stop(room_id, request_id)


def get_active_lite_requests(room_id: str) -> tuple[LiteActiveRequest, ...]:
    """同じroomで現在処理中のLite要請を取得する。"""

    return _DEFAULT_RUNTIME_CONTROLS.active_requests(room_id)


def _location_room_name(room_name: str) -> str:
    value = str(room_name or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("valid room_name is required")
    return value


def _temporary_location_manager(manager: Any = None) -> Any:
    if manager is not None:
        return manager
    # Import lazily so GET/stop contract tests do not initialize the full agent stack.
    from agent.temporary_location_manager import TemporaryLocationManager

    return TemporaryLocationManager()


def _current_location_data(manager: Any, room_name: str) -> Mapping[str, Any]:
    current = manager.get_current_data(room_name)
    return current if isinstance(current, Mapping) else {}


def _text_value(value: Any, *, max_length: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > max_length:
        raise ValueError(f"text exceeds {max_length} characters")
    return text


def _validated_room_image_path(room_name: str, image_path: Any) -> str:
    """Liteから受け取った画像参照を同一roomの画像へ限定する。

    ``TemporaryLocationManager`` は本体UIが生成した一時ファイルも保持するため、
    既存値の読取・保持はそのまま許可する。一方、Liteから新しく指定する値は
    API Gatewayのroom asset検証を必ず通し、他room・非画像・存在しないパスを
    currentへ保存しない。
    """

    value = str(image_path or "").strip()
    if not value:
        raise ValueError("image_path is required")

    # 循環importを避けるため、Lite状態の適用時だけ遅延importする。
    from api import service

    try:
        return str(service.resolve_room_asset_path(room_name, value))
    except ValueError as first_error:
        # レスポンスで返すroom相対参照（例: attachments/api_uploads/photo.png）も
        # 受け付ける。絶対pathや既存のプロジェクト相対pathは上の検証を優先する。
        if Path(value).is_absolute():
            raise
        if str(first_error) != "asset not found":
            raise
        import constants

        room_relative_path = Path(constants.ROOMS_DIR) / room_name / value
        try:
            return str(service.resolve_room_asset_path(room_name, str(room_relative_path)))
        except ValueError as room_error:
            # room相対としても不正な場合は、room境界を検証した結果を返す。
            raise room_error from first_error


def get_temporary_location(room_name: str, *, manager: Any = None) -> TemporaryLocationState:
    """必要時に本体managerから現在地を一度だけ読み取る。"""

    room = _location_room_name(room_name)
    source = _temporary_location_manager(manager)
    current = _current_location_data(source, room)
    return TemporaryLocationState(
        room_id=room,
        active=bool(source.is_active(room)),
        scenery_text=_text_value(current.get("scenery_text"), max_length=MAX_SCENERY_TEXT_LENGTH),
        source_description=_text_value(
            current.get("source_description"), max_length=MAX_SOURCE_DESCRIPTION_LENGTH
        ),
        created_at=str(current.get("created_at") or "").strip(),
        image_path=str(current.get("image_path") or "").strip(),
        location_name=str(current.get("location_name") or "").strip(),
    )


def apply_temporary_location_text(
    room_name: str,
    scenery_text: str,
    *,
    source_description: Optional[str] = None,
    activate: bool = True,
    image_path: Optional[str] = None,
    clear_image: bool = False,
    location_name: Optional[str] = None,
    manager: Any = None,
) -> TemporaryLocationState:
    """手動テキストを現在地へ適用する。

    画像指定を省略した場合は既存の画像パスを保持する。Liteから新しい画像を
    指定する場合は同一roomの既存assetとして検証する。画像ファイルを読み込んだり
    LLMへ送ったりはしない。既存のsourceも、明示指定がない限り保持する。
    """

    room = _location_room_name(room_name)
    text = _text_value(scenery_text, max_length=MAX_SCENERY_TEXT_LENGTH)
    if not text:
        raise ValueError("scenery_text is required")
    if clear_image and image_path is not None:
        raise ValueError("image_path and clear_image cannot be used together")
    source = _temporary_location_manager(manager)
    current = _current_location_data(source, room)
    preserved_source = _text_value(
        current.get("source_description"), max_length=MAX_SOURCE_DESCRIPTION_LENGTH
    )
    next_source = (
        preserved_source
        if source_description is None
        else _text_value(source_description, max_length=MAX_SOURCE_DESCRIPTION_LENGTH)
    )
    if clear_image:
        next_image_path = ""
    elif image_path is None:
        next_image_path = str(current.get("image_path") or "").strip()
    else:
        next_image_path = _validated_room_image_path(room, image_path)
    update = {"source_description": next_source, "image_path": next_image_path}
    if location_name is not None:
        update["location_name"] = _text_value(location_name, max_length=120)
    source.update_current(room, text, **update)
    if activate:
        source.set_active(room, True)
    return get_temporary_location(room, manager=source)


def deactivate_temporary_location(room_name: str, *, manager: Any = None) -> TemporaryLocationState:
    """一時的現在地モードだけを解除し、保存済みの情景テキストは残す。"""

    room = _location_room_name(room_name)
    source = _temporary_location_manager(manager)
    source.set_active(room, False)
    return get_temporary_location(room, manager=source)
