"""以全新解释器运行目录树导出，避免复制 MoviePilot 的堆和线程状态"""

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Optional


PROCESS_STOP_GRACE_SECONDS = 10.0


def run_export_process(
    params: Dict[str, Any],
    context: Any,
    logger: Any,
    *,
    command: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    经匿名管道传递参数和结果，实时转发日志，并限制独立导出进程的总运行时间

    :param params (Dict): 导出参数，Cookie 仅通过 stdin 传输
    :param context (Any): 阶段日志上下文
    :param logger (Any): MoviePilot 日志入口
    :param command (List): 测试可注入子进程命令

    :return Dict: 成功结果
    """
    payload = dict(params)
    escape_func = payload.pop("escape_func", None)
    if (
        escape_func is not None
        and getattr(escape_func, "__name__", "") != "export_dir_custom_escape"
    ):
        raise ValueError("独立导出进程仅支持标准目录树路径转义")
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    worker_command = command or [
        sys.executable,
        "-I",
        str(Path(__file__).with_name("export_dir_worker.py")),
    ]
    read_fd, write_fd = os.pipe()
    log_stream = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
    process = None
    reader = None
    reader_started = False

    def forward_logs() -> None:
        for line in log_stream:
            line = line.rstrip("\r\n")
            if not line:
                continue
            try:
                record = json.loads(line)
                level = record.get("level", "info")
                if level not in ("info", "warning", "error", "debug"):
                    level = "info"
                message = record.get("message", "")
            except (ValueError, AttributeError):
                level, message = "warning", line
            try:
                getattr(logger, level)(message)
            except Exception:
                # 日志后端异常时仍排空管道，避免把子进程堵在日志输出上
                continue

    try:
        process = subprocess.Popen(
            worker_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=write_fd,
            text=True,
            encoding="utf-8",
            errors="replace",
            close_fds=True,
        )
        os.close(write_fd)
        write_fd = None
        reader = Thread(target=forward_logs, name="P115ExportLogReader", daemon=True)
        reader.start()
        reader_started = True
        context.log("isolated_worker_start", pid=process.pid)
        try:
            stdout, _ = process.communicate(serialized, timeout=params["watchdog_timeout"])
        except subprocess.TimeoutExpired as exc:
            context.log("watchdog_timeout", "独立导出进程超时", level="error", pid=process.pid)
            raise TimeoutError(
                f"目录树导出 watchdog 超时: {params['watchdog_timeout']:.0f}s"
            ) from exc
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            raise RuntimeError(
                f"目录树导出子进程无有效结果，exitcode={process.returncode}"
            ) from exc
        if not isinstance(result, dict):
            raise RuntimeError("目录树导出子进程返回结果格式错误")
        if result.get("status") != "ok":
            raise RuntimeError(
                f"目录树导出失败: {result.get('exception_type')}: {result.get('exception')}"
            )
        if process.returncode != 0:
            raise RuntimeError(f"目录树导出子进程异常退出，exitcode={process.returncode}")
        peak = result.get("worker_peak_rss_bytes")
        context.log(
            "isolated_worker_done",
            pid=process.pid,
            peak_memory_mib=round(peak / 1024 / 1024, 1) if peak else None,
        )
        return result
    finally:
        if write_fd is not None:
            os.close(write_fd)
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=PROCESS_STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    context.log(
                        "worker_kill",
                        "独立进程未响应 terminate，执行 kill",
                        level="warning",
                        pid=process.pid,
                    )
                    process.kill()
                    process.wait(timeout=PROCESS_STOP_GRACE_SECONDS)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
        if reader_started:
            reader.join(timeout=PROCESS_STOP_GRACE_SECONDS)
        if not reader_started or not reader.is_alive():
            log_stream.close()
