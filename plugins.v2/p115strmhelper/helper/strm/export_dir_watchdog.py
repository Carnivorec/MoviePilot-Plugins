"""
115 目录树导出 watchdog helper
"""

from __future__ import annotations

import codecs
import json
import os
import platform
import queue
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from multiprocessing import Process, Queue
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional
from uuid import uuid4

import httpx
from p115client import check_response
try:
    from p115client.tool.export_dir import export_dir_start
except ImportError:  # pragma: no cover - 兼容旧版 p115client
    from p115client.tool.export_dir import export_dir as export_dir_start
try:
    from p115client.tool.export_dir import export_dir_parse_iter_path as parse_export_dir_as_path_iter
except ImportError:  # pragma: no cover - 兼容旧版 p115client
    from p115client.tool.export_dir import parse_export_dir_as_path_iter

from app.log import logger


DEFAULT_EXPORT_DIR_STATUS_TIMEOUT_SECONDS = 900.0
DEFAULT_EXPORT_DIR_LOCK_WAIT_TIMEOUT_SECONDS = 900.0
DEFAULT_EXPORT_DIR_WATCHDOG_TIMEOUT_SECONDS = 1800.0
DEFAULT_EXPORT_DIR_WAIT_LOG_INTERVAL_SECONDS = 60.0
DEFAULT_EXPORT_DIR_LOCK_POLL_SECONDS = 1.0
DEFAULT_EXPORT_DIR_TERMINATE_GRACE_SECONDS = 10.0
DEFAULT_DOWNLOAD_TIMEOUT = {
    "connect": 30.0,
    "pool": 15.0,
    "read": 300.0,
    "write": 300.0,
}


if platform.system().lower().startswith("win"):
    from msvcrt import LK_LOCK, LK_NBLCK, LK_UNLCK, locking as msvcrt_locking

    def _lock_nonblocking(fd: int) -> None:
        msvcrt_locking(fd, LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        msvcrt_locking(fd, LK_UNLCK, 1)

else:
    from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock

    def _lock_nonblocking(fd: int) -> None:
        flock(fd, LOCK_EX | LOCK_NB)

    def _unlock(fd: int) -> None:
        flock(fd, LOCK_UN)


@dataclass
class ExportDirLogContext:
    """
    目录树导出阶段日志上下文

    :param request_id: 单次目录树导出的请求 ID
    :param pan_path: 115 网盘路径
    :param local_path: 本地媒体库路径
    :param status_timeout: 115 云端导出状态轮询超时
    :param lock_wait_timeout: export_dir.lock 等待超时
    :param watchdog_timeout: 单路径目录树导出总 watchdog 超时
    """

    request_id: str
    pan_path: str
    local_path: str
    status_timeout: float
    lock_wait_timeout: float
    watchdog_timeout: float
    sync_type: str = "增量STRM生成"
    started_at: float = field(default_factory=time.monotonic)

    def elapsed(self) -> float:
        """
        返回当前请求已耗时秒数

        :return: 已耗时秒数
        """
        return time.monotonic() - self.started_at

    def log(self, phase: str, message: str = "", level: str = "info", **extra: Any) -> None:
        """
        写入固定字段阶段日志

        :param phase: 阶段名
        :param message: 补充说明
        :param level: 日志级别
        :param extra: 附加字段
        """
        fields: Dict[str, Any] = {
            "request_id": self.request_id,
            "pan_path": self.pan_path,
            "local_path": self.local_path,
            "phase": phase,
            "elapsed": f"{self.elapsed():.3f}s",
            "status_timeout": f"{self.status_timeout:.0f}s",
            "lock_wait_timeout": f"{self.lock_wait_timeout:.0f}s",
            "watchdog_timeout": f"{self.watchdog_timeout:.0f}s",
        }
        fields.update({key: value for key, value in extra.items() if value is not None})
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        if message:
            detail = f"{detail} message={message}"
        log_func = getattr(logger, level, logger.info)
        log_func(f"【{self.sync_type}】【目录树导出】{detail}")



def export_dir_custom_escape(name: str) -> str:
    """
    处理 115 目录树中单引号转义

    :param name: 原始文件名
    :return: POSIX 转义后的文件名
    """
    from posixpatht import escape as posix_escape

    return posix_escape(name.replace("\\'", "'"))


def make_export_dir_request_id() -> str:
    """
    生成单次目录树导出 request_id

    :return: request_id 字符串
    """
    return uuid4().hex[:12]


def _to_positive_float(value: Any) -> Optional[float]:
    """
    转换正数配置

    :param value: 输入配置值
    :return: 正数浮点数或 None
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 0:
        return number
    return None


def resolve_export_dir_status_timeout(value: Any) -> float:
    """
    解析 115 云端导出状态轮询超时

    :param value: 用户配置值
    :return: 正数超时秒数
    """
    return _to_positive_float(value) or DEFAULT_EXPORT_DIR_STATUS_TIMEOUT_SECONDS


def resolve_export_dir_lock_wait_timeout(status_timeout: float) -> float:
    """
    解析 export_dir.lock 等待超时

    :param status_timeout: 115 云端导出状态轮询超时
    :return: 文件锁等待超时秒数
    """
    return max(DEFAULT_EXPORT_DIR_LOCK_WAIT_TIMEOUT_SECONDS, status_timeout * 2)


def resolve_export_dir_watchdog_timeout(status_timeout: float, lock_wait_timeout: float) -> float:
    """
    解析单路径目录树导出 watchdog 超时

    :param status_timeout: 115 云端导出状态轮询超时
    :param lock_wait_timeout: export_dir.lock 等待超时
    :return: watchdog 超时秒数
    """
    return max(
        DEFAULT_EXPORT_DIR_WATCHDOG_TIMEOUT_SECONDS,
        status_timeout * 4,
        lock_wait_timeout + status_timeout + 300,
    )


def build_download_timeout_config(slow_timeout: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """
    构建导出 txt HTTP 下载超时配置

    :param slow_timeout: 插件慢操作 timeout 配置
    :return: httpx timeout 字典
    """
    timeout = dict(DEFAULT_DOWNLOAD_TIMEOUT)
    if slow_timeout:
        for key in ("connect", "pool", "read", "write"):
            if value := _to_positive_float(slow_timeout.get(key)):
                timeout[key] = value
    return timeout


@contextmanager
def acquire_export_dir_lock(
    lock_path: Path,
    timeout_seconds: float,
    context: ExportDirLogContext,
    poll_seconds: float = DEFAULT_EXPORT_DIR_LOCK_POLL_SECONDS,
    tick_seconds: float = DEFAULT_EXPORT_DIR_WAIT_LOG_INTERVAL_SECONDS,
) -> Iterator[None]:
    """
    获取带等待超时的 export_dir.lock 独占锁

    :param lock_path: 锁文件路径
    :param timeout_seconds: 等待超时秒数
    :param context: 日志上下文
    :param poll_seconds: 重试间隔秒数
    :param tick_seconds: 等待日志节流秒数
    :raises TimeoutError: 文件锁等待超时
    """
    fd: Optional[int] = None
    acquired = False
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    last_tick = started_at
    context.log("lock_wait_start", timeout=f"{timeout_seconds:.0f}s")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        while True:
            try:
                _lock_nonblocking(fd)
                acquired = True
                context.log("lock_acquired")
                yield
                return
            except (BlockingIOError, OSError):
                elapsed = time.monotonic() - started_at
                if elapsed >= timeout_seconds:
                    context.log(
                        "lock_timeout",
                        "等待 export_dir.lock 超时",
                        level="error",
                        timeout=f"{timeout_seconds:.0f}s",
                    )
                    raise TimeoutError(
                        f"等待 export_dir.lock 超时: {timeout_seconds:.0f}s"
                    )
                now = time.monotonic()
                if now - last_tick >= tick_seconds:
                    last_tick = now
                    context.log(
                        "lock_wait_tick",
                        timeout=f"{timeout_seconds:.0f}s",
                    )
                time.sleep(min(poll_seconds, max(timeout_seconds - elapsed, 0.1)))
    finally:
        if fd is not None:
            if acquired:
                try:
                    _unlock(fd)
                    context.log("lock_released")
                except Exception as exc:
                    context.log(
                        "failed",
                        "释放 export_dir.lock 失败",
                        level="warning",
                        exception_type=type(exc).__name__,
                        exception=str(exc),
                    )
            os.close(fd)


def _iter_response_text_lines(response: httpx.Response) -> Iterator[str]:
    """
    将 HTTP 字节流按 utf-16 增量解码为文本行

    :param response: httpx 响应对象
    :return: 文本行迭代器
    """
    decoder = codecs.getincrementaldecoder("utf-16")()
    buffer = ""
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line + "\n"
    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer


def _write_export_dir_items(
    items: Iterable[str],
    output_path: Path,
    context: ExportDirLogContext,
) -> int:
    """
    写入目录树解析结果文件

    :param items: export_dir_parse_iter_path / parse_export_dir_as_path_iter 输出路径
    :param output_path: JSONL 结果路径
    :param context: 日志上下文
    :return: 写入数量
    """
    count = 0
    with output_path.open("w", encoding="utf-8") as file:
        for current_item in items:
            file.write(json.dumps(current_item, ensure_ascii=False) + "\n")
            count += 1
    context.log("parse_done", item_count=count)
    return count


def iter_export_dir_items(output_path: Path) -> Iterator[str]:
    """
    读取子进程写出的目录树 JSONL 文件

    :param output_path: JSONL 结果路径
    :return: 目录树路径迭代器
    """
    with output_path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                yield json.loads(line)



def wait_export_dir_result_with_log(
    *,
    client: Any,
    export_id: Any,
    timeout_seconds: float,
    context: ExportDirLogContext,
    request_kwargs: Dict[str, Any],
    check_interval: float = 1.0,
    tick_seconds: float = DEFAULT_EXPORT_DIR_WAIT_LOG_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    """
    等待 115 云端目录树导出结果并输出节流日志

    :param client: P115Client 实例
    :param export_id: 115 导出任务 ID
    :param timeout_seconds: 等待超时秒数
    :param context: 日志上下文
    :param request_kwargs: 115 请求参数
    :param check_interval: 状态轮询间隔秒数
    :param tick_seconds: 等待日志节流秒数
    :return: 导出结果数据
    :raises TimeoutError: 云端导出等待超时
    """
    started_at = time.monotonic()
    last_tick_at = started_at
    deadline = started_at + timeout_seconds
    while True:
        response = client.fs_export_dir_status(export_id, **request_kwargs)
        if data := check_response(response).get("data"):
            return data
        now = time.monotonic()
        remaining_seconds = deadline - now
        if remaining_seconds <= 0:
            context.log(
                "failed",
                "等待 115 云端目录树导出结果超时",
                level="error",
                export_id=export_id,
                timeout=f"{timeout_seconds:.0f}s",
            )
            raise TimeoutError(f"等待 115 云端目录树导出结果超时: {export_id}")
        if now - last_tick_at >= tick_seconds:
            last_tick_at = now
            context.log(
                "export_wait_tick",
                export_id=export_id,
                waited=f"{now - started_at:.3f}s",
            )
        time.sleep(min(check_interval, remaining_seconds))


def _merge_download_headers(client: Any, url: Any, request_kwargs: Dict[str, Any]) -> Dict[str, str]:
    """
    合并导出 txt 下载请求头

    :param client: P115Client 实例
    :param url: P115URL 或字符串
    :param request_kwargs: 请求参数
    :return: 请求头字典
    """
    headers: Dict[str, str] = {}
    if client_headers := getattr(client, "headers", None):
        headers.update(dict(client_headers))
    if request_headers := request_kwargs.get("headers"):
        headers.update(dict(request_headers))
    if url_headers := getattr(url, "headers", None):
        headers.update(dict(url_headers))
    return headers


def export_dir_worker_main(
    params: Dict[str, Any],
    result_queue: Queue,
    *,
    client_factory: Optional[Callable[..., Any]] = None,
    get_pid_func: Optional[Callable[..., Any]] = None,
    export_dir_func: Callable[..., Any] = export_dir_start,
    parse_iter_func: Callable[..., Any] = parse_export_dir_as_path_iter,
    stream_factory: Callable[..., Any] = httpx.stream,
) -> None:
    """
    执行单路径目录树导出子进程主体

    :param params: 子进程参数
    :param result_queue: 父进程结果队列
    :param client_factory: 测试可注入客户端工厂
    :param get_pid_func: 测试可注入路径 ID 函数
    :param export_dir_func: 测试可注入导出提交函数
    :param parse_iter_func: 测试可注入解析函数
    :param stream_factory: 测试可注入 HTTP stream 工厂
    """
    context = ExportDirLogContext(
        request_id=params["request_id"],
        pan_path=params["pan_path"],
        local_path=params["local_path"],
        status_timeout=params["status_timeout"],
        lock_wait_timeout=params["lock_wait_timeout"],
        watchdog_timeout=params["watchdog_timeout"],
    )
    output_path = Path(params["output_path"])
    export_result: Optional[Dict[str, Any]] = None
    client: Any = None
    request_kwargs: Dict[str, Any] = {}
    try:
        if client_factory is None:
            from ...core.p115_client import create_client

            client_factory = create_client
        if get_pid_func is None:
            from ...core.p115 import get_pid_by_path

            get_pid_func = get_pid_by_path

        client = client_factory(
            params["cookies"],
            default_timeout=params.get("default_timeout"),
            slow_timeout=params.get("slow_timeout"),
        )
        request_kwargs = dict(params.get("request_kwargs") or {})
        download_timeout = httpx.Timeout(**params["download_timeout"])
        lock_path = Path(params["lock_path"])

        with acquire_export_dir_lock(
            lock_path=lock_path,
            timeout_seconds=params["lock_wait_timeout"],
            context=context,
        ):
            context.log("resolve_cid_start")
            cid = get_pid_func(
                client=client,
                path=params["pan_path"],
                mkdir=True,
                update_cache=False,
                by_cache=False,
                request_timeout=10,
            )
            if cid == -1:
                raise FileNotFoundError(f"网盘路径不存在: {params['pan_path']}")
            context.log("resolve_cid_done", cid=cid)

            context.log("export_submit_start", cid=cid)
            export_id = export_dir_func(
                client,
                file_ids=cid,
                target=0,
                layer_limit=0,
                **request_kwargs,
            )
            context.log("export_submit_done", export_id=export_id)

            export_result = wait_export_dir_result_with_log(
                client=client,
                export_id=export_id,
                timeout_seconds=params["status_timeout"],
                context=context,
                request_kwargs=request_kwargs,
            )
            pickcode = export_result["pick_code"]
            file_id = export_result.get("file_id")
            context.log(
                "export_result_done",
                export_id=export_id,
                file_id=file_id,
                pickcode=pickcode,
            )

            context.log("download_url_start", pickcode=pickcode)
            try:
                url = client.download_url(
                    pickcode,
                    use_web_api=True,
                    **request_kwargs,
                )
            except OSError:
                url = client.download_url(pickcode, **request_kwargs)
            context.log("download_url_done", pickcode=pickcode)

            headers = _merge_download_headers(client, url, request_kwargs)
            context.log("download_stream_start", timeout=params["download_timeout"])
            with stream_factory(
                "GET",
                str(url),
                headers=headers,
                timeout=download_timeout,
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                items = parse_iter_func(
                    _iter_response_text_lines(response),
                    escape=params["escape_func"],
                )
                count = _write_export_dir_items(items, output_path, context)
            context.log("download_stream_done", item_count=count)
    except Exception as exc:
        context.log(
            "failed",
            "目录树导出失败",
            level="error",
            exception_type=type(exc).__name__,
            exception=str(exc),
        )
        result_queue.put(
            {
                "status": "error",
                "request_id": params["request_id"],
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "traceback": traceback.format_exc(limit=20),
                "elapsed": context.elapsed(),
            }
        )
        return
    finally:
        if client and export_result and export_result.get("file_id"):
            try:
                client.fs_delete(export_result["file_id"], **request_kwargs)
                context.log("remote_cleanup_done", file_id=export_result["file_id"])
            except Exception as exc:
                context.log(
                    "failed",
                    "清理 115 导出文件失败，将继续返回主结果",
                    level="warning",
                    exception_type=type(exc).__name__,
                    exception=str(exc),
                )

    result_queue.put(
        {
            "status": "ok",
            "request_id": params["request_id"],
            "output_path": str(output_path),
            "elapsed": context.elapsed(),
        }
    )


def _queue_get_result(result_queue: Queue) -> Optional[Dict[str, Any]]:
    """
    从 multiprocessing queue 读取子进程结果

    :param result_queue: 结果队列
    :return: 结果字典或 None
    """
    try:
        return result_queue.get(timeout=1)
    except queue.Empty:
        return None


def run_worker_with_watchdog(
    params: Dict[str, Any],
    context: ExportDirLogContext,
    worker_target: Callable[[Dict[str, Any], Queue], None] = export_dir_worker_main,
) -> Dict[str, Any]:
    """
    以子进程执行 worker 并施加 watchdog

    :param params: 子进程参数
    :param context: 父进程日志上下文
    :param worker_target: 子进程入口
    :return: 子进程结果
    :raises TimeoutError: watchdog 超时
    :raises RuntimeError: 子进程失败或无结果退出
    """
    result_queue: Queue = Queue(maxsize=1)
    process = Process(target=worker_target, args=(params, result_queue))
    process.start()
    process.join(params["watchdog_timeout"])
    if process.is_alive():
        context.log(
            "watchdog_timeout",
            "单路径目录树导出超过 watchdog，终止子进程",
            level="error",
            pid=process.pid,
        )
        process.terminate()
        process.join(DEFAULT_EXPORT_DIR_TERMINATE_GRACE_SECONDS)
        if process.is_alive():
            context.log(
                "watchdog_timeout",
                "子进程 terminate 后仍未退出，执行 kill",
                level="error",
                pid=process.pid,
            )
            process.kill()
            process.join()
        raise TimeoutError(
            f"目录树导出 watchdog 超时: {params['watchdog_timeout']:.0f}s"
        )

    result = _queue_get_result(result_queue)
    if result is None:
        raise RuntimeError(
            f"目录树导出子进程无结果退出，exitcode={process.exitcode}"
        )
    if result.get("status") != "ok":
        raise RuntimeError(
            f"目录树导出失败: {result.get('exception_type')}: {result.get('exception')}"
        )
    return result


def build_export_dir_watchdog_params(
    *,
    request_id: str,
    pan_path: str,
    local_path: str,
    cookies: str,
    plugin_temp_path: Path,
    status_timeout_config: Any,
    default_timeout: Optional[Dict[str, Any]],
    slow_timeout: Optional[Dict[str, Any]],
    request_kwargs: Dict[str, Any],
    escape_func: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """
    构建目录树导出 watchdog 参数

    :param request_id: 单次目录树导出 request_id
    :param pan_path: 115 网盘路径
    :param local_path: 本地媒体库路径
    :param cookies: 115 Cookie 字符串
    :param plugin_temp_path: 插件临时目录
    :param status_timeout_config: 用户配置的目录树导出状态超时
    :param default_timeout: 普通请求超时配置
    :param slow_timeout: 慢操作请求超时配置
    :param request_kwargs: 115 请求参数
    :param escape_func: 115 导出路径转义函数
    :return: 子进程参数字典
    """
    status_timeout = resolve_export_dir_status_timeout(status_timeout_config)
    lock_wait_timeout = resolve_export_dir_lock_wait_timeout(status_timeout)
    watchdog_timeout = resolve_export_dir_watchdog_timeout(
        status_timeout,
        lock_wait_timeout,
    )
    output_dir = plugin_temp_path / "export_dir_watchdog"
    output_dir.mkdir(parents=True, exist_ok=True)
    return {
        "request_id": request_id,
        "pan_path": pan_path,
        "local_path": local_path,
        "cookies": cookies,
        "status_timeout": status_timeout,
        "lock_wait_timeout": lock_wait_timeout,
        "watchdog_timeout": watchdog_timeout,
        "lock_path": str(plugin_temp_path / "export_dir.lock"),
        "output_path": str(output_dir / f"{request_id}.jsonl"),
        "default_timeout": default_timeout,
        "slow_timeout": slow_timeout,
        "download_timeout": build_download_timeout_config(slow_timeout),
        "request_kwargs": request_kwargs,
        "escape_func": escape_func or export_dir_custom_escape,
    }


def run_export_dir_with_watchdog(
    *,
    pan_path: str,
    local_path: str,
    cookies: str,
    plugin_temp_path: Path,
    status_timeout_config: Any,
    default_timeout: Optional[Dict[str, Any]],
    slow_timeout: Optional[Dict[str, Any]],
    request_kwargs: Dict[str, Any],
    escape_func: Optional[Callable[[str], str]] = None,
) -> Path:
    """
    执行受 watchdog 保护的单路径目录树导出

    :param pan_path: 115 网盘路径
    :param local_path: 本地媒体库路径
    :param cookies: 115 Cookie 字符串
    :param plugin_temp_path: 插件临时目录
    :param status_timeout_config: 用户配置的目录树导出状态超时
    :param default_timeout: 普通请求超时配置
    :param slow_timeout: 慢操作请求超时配置
    :param request_kwargs: 115 请求参数
    :param escape_func: 115 导出路径转义函数
    :return: 子进程输出 JSONL 路径
    """
    request_id = make_export_dir_request_id()
    params = build_export_dir_watchdog_params(
        request_id=request_id,
        pan_path=pan_path,
        local_path=local_path,
        cookies=cookies,
        plugin_temp_path=plugin_temp_path,
        status_timeout_config=status_timeout_config,
        default_timeout=default_timeout,
        slow_timeout=slow_timeout,
        request_kwargs=request_kwargs,
        escape_func=escape_func,
    )
    context = ExportDirLogContext(
        request_id=request_id,
        pan_path=pan_path,
        local_path=local_path,
        status_timeout=params["status_timeout"],
        lock_wait_timeout=params["lock_wait_timeout"],
        watchdog_timeout=params["watchdog_timeout"],
    )
    context.log("worker_start")
    if not _to_positive_float(status_timeout_config):
        context.log(
            "status_timeout_defaulted",
            "increment_sync_itertree_timeout_seconds 小于等于 0，使用 900 秒默认值",
            configured=status_timeout_config,
        )
    result = run_worker_with_watchdog(params=params, context=context)
    context.log("worker_done", output_path=result["output_path"])
    return Path(result["output_path"])
