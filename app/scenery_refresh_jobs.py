"""非同期な情景更新要求のセッション境界。

Gradio の ``gr.State`` に置く ``SceneryRefreshState`` は、ひとつのブラウザ
セッションに属する情景更新の世代・保留状態・結果だけを保持する。実行中の
LLM 要求を強制停止することはできないため、世代が変わった要求の戻り値は
UI へ返さずに破棄する。

既定の投入先は共有リソースとして最大8件の待ち queue と2本の daemon worker
を遅延起動する。room や token を共有グローバルへ置かないことが重要であり、
セッション値は常に ``SceneryRefreshState`` 内だけで管理する。テストや別の
実行環境では ``schedule(..., submitter=...)`` で投入先を差し替えられる。
"""

from __future__ import annotations

import contextvars
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional


SCENERY_REFRESH_QUEUE_MAXSIZE = 8
SCENERY_REFRESH_WORKER_COUNT = 2


_RefreshJob = Callable[[], None]


@dataclass(frozen=True)
class _RefreshResult:
    """結果の世代を値と同一のレコードとして保持する。"""

    token: int
    room: str
    value: Any


class _SceneryRefreshResourceScheduler:
    """情景更新で共有するbounded queueとdaemon workerの小さな実装。"""

    def __init__(self) -> None:
        self.queue: queue.Queue[_RefreshJob] = queue.Queue(
            maxsize=SCENERY_REFRESH_QUEUE_MAXSIZE
        )
        self._workers: list[threading.Thread] = []
        self._start_lock = threading.Lock()

    def submit(self, job: _RefreshJob) -> bool:
        """jobを待ちqueueへ即時投入する。

        ``put_nowait`` だけを使い、queueが満杯なら呼び出し側を待たせずに
        ``False`` を返す。workerの起動は最初の投入時だけ行う。
        """

        self._ensure_workers()
        try:
            self.queue.put_nowait(job)
        except queue.Full:
            return False
        return True

    def _ensure_workers(self) -> None:
        # workerの数を固定し、同時にscheduleされた最初の2件でも3本以上に
        # ならないようにする。threadはdaemonなのでUI終了を待機させない。
        with self._start_lock:
            while len(self._workers) < SCENERY_REFRESH_WORKER_COUNT:
                worker = threading.Thread(
                    target=self._worker_loop,
                    name="nexus-scenery-refresh",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)

    def _worker_loop(self) -> None:
        while True:
            job = self.queue.get()
            try:
                # State側のjob wrapperが通常の例外を結果へ出さずに処理する。
                # ここでも最後の境界を置き、注入された異常なjobでworkerが
                # 死んで共有scheduler全体が詰まらないようにする。
                try:
                    job()
                except BaseException:
                    pass
            finally:
                self.queue.task_done()


# これはroom/sessionの状態ではなく、プロセス内で共有する実行資源だけを
# 保持する。sessionごとの値はSceneryRefreshStateに置く。
_RESOURCE_SCHEDULER = _SceneryRefreshResourceScheduler()


class SceneryRefreshState:
    """Gradio sessionに属する情景更新の世代と結果。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._token = 0
        self._room: Optional[str] = None
        self._registered_token: Optional[int] = None
        self._job_pending_token: Optional[int] = None
        self._result: Optional[_RefreshResult] = None
        self._closed = False

    def __deepcopy__(self, memo: dict[int, Any]) -> "SceneryRefreshState":
        """GradioがStateを複製しても、別セッションの状態を引き継がない。"""

        copied = type(self)()
        memo[id(self)] = copied
        return copied

    def begin(self, room: str) -> int:
        """roomの新しい更新世代を開始し、そのtokenを返す。"""

        with self._lock:
            self._token += 1
            self._room = room
            # 新世代開始時に旧job/resultを論理的に無効化する。実行中の旧
            # workは止められないが、完了時の世代確認で戻り値を捨てる。
            self._registered_token = None
            self._job_pending_token = None
            self._result = None
            return self._token

    def invalidate(self) -> None:
        """現在のjob/resultを無効にする。"""

        with self._lock:
            self._token += 1
            self._room = None
            self._registered_token = None
            self._job_pending_token = None
            self._result = None

    def close(self) -> None:
        """状態を閉じ、以後のjob投入を拒否する。"""

        with self._lock:
            if not self._closed:
                self._token += 1
            self._closed = True
            self._room = None
            self._registered_token = None
            self._job_pending_token = None
            self._result = None

    def is_current(self, token: int, room: str) -> bool:
        """tokenとroomが現在の未終了世代に一致するか確認する。"""

        with self._lock:
            return self._is_current_locked(token, room)

    def matches_room(self, room: str) -> bool:
        """stateが未終了で、現在roomと一致するか確認する。

        poll側がUIのroom選択とsession stateのroomを照合するための境界で
        あり、tokenや結果の有無は判定しない。room切替の途中やclose後に
        古い結果を誤って扱わないよう、内部roomの読取は必ずLock下で行う。
        """

        with self._lock:
            return not self._closed and self._room == room

    def schedule(
        self,
        token: int,
        room: str,
        work: Callable[[], Any],
        submitter: Optional[Callable[[_RefreshJob], bool]] = None,
    ) -> bool:
        """workを非blockingに投入し、投入を受け付けたか返す。

        同じtokenについては、実行中・完了済み・結果取得済みを問わず一度
        だけ受け付ける。submitterが ``False`` を返すか例外を送出した場合は
        その投入だけを取り消し、ここから同期実行や内部再試行は行わない。
        """

        with self._lock:
            if not self._is_current_locked(token, room):
                return False
            if self._registered_token == token:
                return False

            # scheduleを呼んだUI側のcontextvarsをworkerへ持ち込む。
            work_context = contextvars.copy_context()
            self._registered_token = token
            self._job_pending_token = token

        def run_job() -> None:
            self._run_job(token, room, work, work_context)

        submit = submitter if submitter is not None else _RESOURCE_SCHEDULER.submit
        try:
            accepted = bool(submit(run_job))
        except Exception:
            accepted = False

        if accepted:
            return True

        # submitterが拒否した場合のみ登録を戻す。begin()や次世代のschedule()
        # が先に進んでいたら、その新しい登録を壊さない。
        with self._lock:
            if (
                self._registered_token == token
                and self._is_current_locked(token, room)
            ):
                self._registered_token = None
                self._job_pending_token = None
        return False

    def needs_polling(self) -> bool:
        """job待機中・実行中、または未消費の結果があるか返す。

        Gradio側はこの値をTimerの有効化判定に使う。結果を消費した後も
        ``_registered_token`` は同一tokenの二重投入防止のため残るので、
        busy判定には使わず、job専用のin-flight印と結果だけを確認する。
        """

        with self._lock:
            if self._closed:
                return False
            if self._job_pending_token is not None:
                if self._is_current_locked(self._job_pending_token, self._room):
                    return True
            result = self._result
            return bool(
                result is not None
                and self._is_current_locked(result.token, result.room)
            )

    def has_result(self, room: str) -> bool:
        """指定roomの現在世代に利用可能な結果があるか返す。"""

        with self._lock:
            result = self._result
            return bool(
                result is not None
                and result.room == room
                and self._is_current_locked(result.token, result.room)
            )

    def take(
        self,
        room: str,
        accept: Callable[[Any], bool],
    ) -> Any | None:
        """現在の結果をacceptで確認して一度だけ取り出す。

        acceptはlock外で実行する。評価中のroom切替や新世代の結果生成を
        妨げず、再取得したlockで候補レコードと世代が同一の時だけ返す。
        """

        with self._lock:
            candidate = self._result
            if candidate is None:
                return None
            if candidate.room != room:
                # 別roomの呼出しで、現在roomの有効な結果まで消費しない。
                return None
            if not self._is_current_locked(candidate.token, candidate.room):
                if self._result is candidate:
                    self._result = None
                return None
            token = candidate.token
            value = candidate.value

        try:
            accepted = bool(accept(value))
        except BaseException:
            accepted = False

        with self._lock:
            # acceptの途中にbegin()/invalidate()/close()や別resultの生成が
            # あった場合、候補以外のresultには触れず、古い値だけを返さない。
            if self._result is not candidate:
                return None
            if not self._is_current_locked(token, room):
                self._result = None
                return None
            self._result = None
            if not accepted:
                return None
            return value

    def _run_job(
        self,
        token: int,
        room: str,
        work: Callable[[], Any],
        work_context: contextvars.Context,
    ) -> None:
        with self._lock:
            if (
                self._registered_token != token
                or not self._is_current_locked(token, room)
            ):
                if self._job_pending_token == token:
                    self._job_pending_token = None
                return

        try:
            # 実行前の確認後に世代が切り替わる競合は避けられないが、実行
            # 中のworkを強制中断せず、完了時の確認で結果を無効化する。
            value = work_context.run(work)
        except BaseException:
            with self._lock:
                if self._job_pending_token == token:
                    self._job_pending_token = None
            return

        if value is None:
            with self._lock:
                if self._job_pending_token == token:
                    self._job_pending_token = None
            return

        with self._lock:
            if (
                self._registered_token != token
                or not self._is_current_locked(token, room)
            ):
                if self._job_pending_token == token:
                    self._job_pending_token = None
                return
            self._result = _RefreshResult(token=token, room=room, value=value)
            self._job_pending_token = None

    def _is_current_locked(self, token: int, room: str) -> bool:
        return (
            not self._closed
            and self._token == token
            and self._room == room
        )


__all__ = ["SceneryRefreshState"]
