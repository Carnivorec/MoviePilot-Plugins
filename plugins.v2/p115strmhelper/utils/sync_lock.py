from threading import Lock
from typing import Optional

STRM_SYNC_TASK_FULL = "full"
STRM_SYNC_TASK_INCREMENT = "increment"


class StrmSyncRunGuard:
    """
    STRM 同步任务互斥控制器
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._state_lock = Lock()
        self._current_task: Optional[str] = None
        self._current_task_kind: Optional[str] = None
        self._pending_full_sync = False
        self._pending_full_sync_runner = False

    @property
    def current_task(self) -> Optional[str]:
        """
        当前持有同步执行权的任务名称
        """
        with self._state_lock:
            return self._current_task

    @property
    def current_task_kind(self) -> Optional[str]:
        """
        当前持有同步执行权的任务类型
        """
        with self._state_lock:
            return self._current_task_kind

    @property
    def pending_full_sync(self) -> bool:
        """
        是否存在待执行全量同步请求
        """
        with self._state_lock:
            return self._pending_full_sync

    def acquire(self, task_name: str, task_kind: Optional[str] = None) -> bool:
        """
        尝试获取同步执行权

        :param task_name: 当前任务名称
        :param task_kind: 当前任务类型
        :return: 获取成功返回 True，否则返回 False
        """
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            with self._state_lock:
                self._current_task = task_name
                self._current_task_kind = task_kind
        return acquired

    def release(self) -> Optional[str]:
        """
        释放同步执行权

        :return: 被释放任务的类型
        """
        if not self._lock.locked():
            return None
        with self._state_lock:
            task_kind = self._current_task_kind
            self._current_task = None
            self._current_task_kind = None
        self._lock.release()
        return task_kind

    def mark_full_sync_pending(self) -> bool:
        """
        登记一个待执行全量同步请求

        :return: 首次登记返回 True，已存在待执行请求返回 False
        """
        with self._state_lock:
            was_pending = self._pending_full_sync
            self._pending_full_sync = True
            return not was_pending

    def clear_full_sync_pending(self) -> bool:
        """
        清除待执行全量同步请求

        :return: 清除前存在待执行请求返回 True，否则返回 False
        """
        with self._state_lock:
            was_pending = self._pending_full_sync
            self._pending_full_sync = False
            return was_pending

    def reserve_full_sync_runner(self) -> bool:
        """
        预留一个待执行全量同步补跑线程

        :return: 成功预留返回 True
        """
        with self._state_lock:
            if not self._pending_full_sync or self._pending_full_sync_runner:
                return False
            self._pending_full_sync_runner = True
            return True

    def finish_full_sync_runner(self) -> None:
        """
        标记待执行全量同步补跑线程已结束
        """
        with self._state_lock:
            self._pending_full_sync_runner = False
