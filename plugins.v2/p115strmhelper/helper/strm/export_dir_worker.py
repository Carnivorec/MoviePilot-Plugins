"""目录树导出的独立进程入口，不加载 MoviePilot 或插件服务"""

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _peak_rss_bytes() -> Optional[int]:
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform == "darwin" else peak * 1024
    except ImportError:
        return None


class _JsonLogHandler(logging.Handler):
    def emit(self, record):
        """以 JSON 行实时输出日志，不与标准输出的结果混用"""
        print(
            json.dumps(
                {"level": record.levelname.lower(), "message": record.getMessage()},
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )


class _ResultWriter:
    def __init__(self):
        self.result = None

    def put(self, result: Dict[str, Any]) -> None:
        """输出导出结果及子进程内存峰值"""
        self.result = result
        result["worker_pid"] = os.getpid()
        result["worker_peak_rss_bytes"] = _peak_rss_bytes()
        print(json.dumps(result, ensure_ascii=False), flush=True)


def main() -> int:
    """
    从 stdin 接收导出参数并运行 worker，--probe 仅检查导入与进程隔离

    :return int: 正常完成为 0，导出失败为 1
    """
    writer = _ResultWriter()
    try:
        params = {} if "--probe" in sys.argv[1:] else json.load(sys.stdin)
        here = Path(__file__).resolve()
        worker = _load_module("p115_export_worker_logic", here.with_name("export_dir_watchdog.py"))
        client_module = _load_module("p115_export_client", here.parents[2] / "core/p115_client.py")
        from p115client import util
        from p115client.tool.attr import get_id_to_path

        logger = logging.getLogger("p115_export_worker")
        logger.setLevel(logging.INFO)
        logger.handlers = [_JsonLogHandler()]
        logger.propagate = False
        worker.logger = logger
        if "--probe" in sys.argv[1:]:
            writer.put(
                {
                    "status": "ok",
                    "app_loaded": any(
                        name == "app" or name.startswith("app.") for name in sys.modules
                    ),
                    "cache_lock_pids": [
                        getattr(cache._lock, "_creator_pid", os.getpid())
                        for cache in vars(util).values()
                        if isinstance(cache, util.LockedJsonKV)
                    ],
                }
            )
            return 0

        def get_directory_id(*, client, path, request_timeout=10, **kwargs):
            request_kwargs = dict(params.get("request_kwargs") or {})
            request_kwargs["timeout"] = request_timeout
            return get_id_to_path(
                client, path=path, ensure_file=False, refresh=True, **request_kwargs
            )

        params["escape_func"] = worker.export_dir_custom_escape
        worker.export_dir_worker_main(
            params,
            writer,
            client_factory=client_module.create_client,
            get_pid_func=get_directory_id,
        )
        return 0 if writer.result and writer.result.get("status") == "ok" else 1
    except Exception as exc:
        if writer.result is None:
            writer.put(
                {"status": "error", "exception_type": type(exc).__name__, "exception": str(exc)}
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
