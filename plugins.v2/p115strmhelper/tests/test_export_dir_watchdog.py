import codecs
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from multiprocessing import Queue
from pathlib import Path


class _FakeLogger:
    def __init__(self):
        self.records = []

    def info(self, message):
        self.records.append(("info", str(message)))

    def error(self, message):
        self.records.append(("error", str(message)))

    def warning(self, message):
        self.records.append(("warning", str(message)))

    def debug(self, message):
        self.records.append(("debug", str(message)))


class _FakeTimeout:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeResponse:
    def __init__(self, text):
        self._payload = codecs.BOM_UTF16_LE + text.encode("utf-16-le")

    def iter_bytes(self):
        midpoint = len(self._payload) // 2
        yield self._payload[:midpoint]
        yield self._payload[midpoint:]

    def raise_for_status(self):
        return None


class _FakeStream:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeUrl:
    headers = {"x-url": "1"}

    def __str__(self):
        return "https://example.test/tree.txt"


class _FakeClient:
    def __init__(self):
        self.headers = {"x-client": "1"}
        self.deleted = []
        self.status_calls = 0

    def fs_export_dir_status(self, export_id, **kwargs):
        self.status_calls += 1
        return {
            "data": {
                "export_id": str(export_id),
                "file_id": "file-1",
                "file_name": "tree.txt",
                "pick_code": "pick-1",
            }
        }

    def download_url(self, pickcode, **kwargs):
        return _FakeUrl()

    def fs_delete(self, file_id, **kwargs):
        self.deleted.append(file_id)
        return {"state": True}


def _sleeping_worker(params, result_queue):
    time.sleep(2)


class ExportDirWatchdogTest(unittest.TestCase):
    def setUp(self):
        self._saved_modules = {}
        self.fake_logger = _FakeLogger()
        self._install_stubs()
        module_path = (
            Path(__file__).resolve().parents[1]
            / "helper"
            / "strm"
            / "export_dir_watchdog.py"
        )
        spec = importlib.util.spec_from_file_location(
            "test_export_dir_watchdog_module",
            module_path,
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["test_export_dir_watchdog_module"] = module
        spec.loader.exec_module(module)
        self.module = module

    def tearDown(self):
        sys.modules.pop("test_export_dir_watchdog_module", None)
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _save_module(self, name):
        if name not in self._saved_modules:
            self._saved_modules[name] = sys.modules.get(name)

    def _set_module(self, name, module):
        self._save_module(name)
        sys.modules[name] = module

    def _install_stubs(self):
        app = types.ModuleType("app")
        app_log = types.ModuleType("app.log")
        app_log.logger = self.fake_logger
        self._set_module("app", app)
        self._set_module("app.log", app_log)

        p115client = types.ModuleType("p115client")
        p115client.check_response = lambda response: response
        self._set_module("p115client", p115client)

        p115client_tool = types.ModuleType("p115client.tool")
        p115client_tool_export_dir = types.ModuleType("p115client.tool.export_dir")
        p115client_tool_export_dir.export_dir = lambda *args, **kwargs: 100

        def fake_parse_iter(lines, escape=None):
            for line in lines:
                value = line.rstrip("\n")
                if value:
                    yield escape(value) if escape else value

        p115client_tool_export_dir.export_dir_parse_iter_path = fake_parse_iter
        p115client_tool_export_dir.parse_export_dir_as_path_iter = fake_parse_iter
        self._set_module("p115client.tool", p115client_tool)
        self._set_module("p115client.tool.export_dir", p115client_tool_export_dir)

        httpx = types.ModuleType("httpx")
        httpx.Timeout = _FakeTimeout
        httpx.Response = _FakeResponse
        httpx.stream = object()
        self._set_module("httpx", httpx)

    def _context(self):
        return self.module.ExportDirLogContext(
            request_id="req-test",
            pan_path="/pan",
            local_path="/local",
            status_timeout=1,
            lock_wait_timeout=1,
            watchdog_timeout=1,
        )

    def test_resolve_status_timeout_uses_positive_config(self):
        self.assertEqual(self.module.resolve_export_dir_status_timeout(300), 300.0)

    def test_resolve_status_timeout_defaults_for_zero_negative_and_missing(self):
        default = self.module.DEFAULT_EXPORT_DIR_STATUS_TIMEOUT_SECONDS
        self.assertEqual(self.module.resolve_export_dir_status_timeout(0), default)
        self.assertEqual(self.module.resolve_export_dir_status_timeout(-1), default)
        self.assertEqual(self.module.resolve_export_dir_status_timeout(None), default)

    def test_lock_acquire_immediately_and_release_logs_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "export_dir.lock"
            with self.module.acquire_export_dir_lock(lock_path, 1, self._context()):
                self.assertTrue(lock_path.exists())

        joined = "\n".join(message for _, message in self.fake_logger.records)
        self.assertIn("request_id=req-test", joined)
        self.assertIn("pan_path=/pan", joined)
        self.assertIn("phase=lock_acquired", joined)
        self.assertIn("phase=lock_released", joined)

    @unittest.skipIf(sys.platform.startswith("win"), "fcntl 语义仅在 Linux 下测试")
    def test_lock_wait_retries_and_times_out(self):
        import fcntl

        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "export_dir.lock"
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                with self.assertRaises(TimeoutError):
                    with self.module.acquire_export_dir_lock(
                        lock_path,
                        0.05,
                        self._context(),
                        poll_seconds=0.01,
                        tick_seconds=0.01,
                    ):
                        pass
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

        joined = "\n".join(message for _, message in self.fake_logger.records)
        self.assertIn("phase=lock_wait_tick", joined)
        self.assertIn("phase=lock_timeout", joined)

    def test_watchdog_terminates_hanging_worker(self):
        params = {"watchdog_timeout": 0.1}
        with self.assertRaises(TimeoutError):
            self.module.run_worker_with_watchdog(
                params=params,
                context=self._context(),
                worker_target=_sleeping_worker,
            )

        joined = "\n".join(message for _, message in self.fake_logger.records)
        self.assertIn("phase=watchdog_timeout", joined)

    def test_worker_success_writes_export_items_and_cleans_remote_file(self):
        client = _FakeClient()
        response = _FakeResponse("Root\nRoot/Movie.mkv\n")
        stream = _FakeStream(response)
        export_calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            params = {
                "request_id": "req-worker",
                "pan_path": "/pan",
                "local_path": "/local",
                "status_timeout": 3,
                "lock_wait_timeout": 1,
                "watchdog_timeout": 5,
                "lock_path": str(temp_path / "export_dir.lock"),
                "output_path": str(temp_path / "result.jsonl"),
                "download_timeout": {"connect": 1, "pool": 1, "read": 1, "write": 1},
                "request_kwargs": {"headers": {"x-req": "1"}},
                "escape_func": lambda value: value,
                "cookies": "UID=1",
                "default_timeout": None,
                "slow_timeout": None,
            }
            result_queue = Queue(maxsize=1)
            self.module.export_dir_worker_main(
                params,
                result_queue,
                client_factory=lambda *args, **kwargs: client,
                get_pid_func=lambda **kwargs: 88,
                export_dir_func=lambda *args, **kwargs: export_calls.append((args, kwargs)) or 99,
                parse_iter_func=lambda lines, escape=None: (line.rstrip("\n") for line in lines),
                stream_factory=stream,
            )
            result = result_queue.get(timeout=1)
            output = [json.loads(line) for line in (temp_path / "result.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result["status"], "ok")
        self.assertEqual(output, ["Root", "Root/Movie.mkv"])
        self.assertEqual(client.deleted, ["file-1"])
        self.assertTrue(stream.calls)
        self.assertEqual(export_calls[0][1]["file_ids"], 88)
        self.assertEqual(export_calls[0][1]["target"], 0)
        self.assertNotIn("export_file_ids", export_calls[0][1])
        self.assertNotIn("target_pid", export_calls[0][1])

    def test_log_context_includes_required_fields(self):
        context = self._context()
        context.log("download_stream_start", timeout="1s")
        message = self.fake_logger.records[-1][1]
        self.assertIn("request_id=req-test", message)
        self.assertIn("pan_path=/pan", message)
        self.assertIn("local_path=/local", message)
        self.assertIn("phase=download_stream_start", message)
        self.assertIn("elapsed=", message)
        self.assertIn("timeout=1s", message)


if __name__ == "__main__":
    unittest.main()
