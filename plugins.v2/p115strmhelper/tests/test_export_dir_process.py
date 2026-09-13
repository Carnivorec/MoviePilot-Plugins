"""独立导出解释器、匿名管道、日志和硬超时的真实子进程测试"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread, enumerate as enumerate_threads
from unittest import TestCase, skipIf
from unittest.mock import Mock, patch


_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "export_process_under_test", _ROOT / "helper/strm/export_dir_process.py"
)
_process = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_process)


class TestIsolatedExportProcess(TestCase):
    def setUp(self):
        self.context = Mock()
        self.logger = Mock()

    def _run(self, script, timeout=3):
        return _process.run_export_process(
            {"watchdog_timeout": timeout, "cookies": "private-cookie-not-for-argv"},
            self.context,
            self.logger,
            command=[sys.executable, "-I", "-c", script],
        )

    def tearDown(self):
        self.assertFalse(
            any(thread.name == "P115ExportLogReader" for thread in enumerate_threads())
        )

    def test_real_worker_imports_no_moviepilot_and_owns_its_cache_locks(self):
        result = _process.run_export_process(
            {"watchdog_timeout": 15},
            self.context,
            self.logger,
            command=[
                sys.executable,
                "-I",
                str(_ROOT / "helper/strm/export_dir_worker.py"),
                "--probe",
            ],
        )
        self.assertFalse(result["app_loaded"])
        self.assertNotEqual(result["worker_pid"], os.getpid())
        self.assertEqual(set(result["cache_lock_pids"]), {result["worker_pid"]})
        if result["worker_peak_rss_bytes"] is not None:
            self.assertLess(result["worker_peak_rss_bytes"], 256 * 1024 * 1024)

    def test_large_error_payload_and_logs_do_not_block(self):
        script = """
import json, sys
params = json.load(sys.stdin)
for _ in range(100):
    print(json.dumps({'level': 'info', 'message': '日志' * 4096}), file=sys.stderr, flush=True)
print(json.dumps({'status': 'error', 'exception_type': 'ValueError', 'exception': 'large error', 'traceback': 'x' * 262144}))
"""
        with self.assertRaisesRegex(RuntimeError, "large error"):
            self._run(script)
        self.assertEqual(self.logger.info.call_count, 100)
        self.assertNotIn("private-cookie", str(self.logger.mock_calls))

    def test_logs_arrive_before_worker_finishes(self):
        received = Event()
        self.logger.info.side_effect = lambda message: received.set()
        result = []
        script = """
import json, sys, time
json.load(sys.stdin)
print(json.dumps({'level': 'info', 'message': 'started'}), file=sys.stderr, flush=True)
time.sleep(0.3)
print(json.dumps({'status': 'ok'}))
"""
        runner = Thread(target=lambda: result.append(self._run(script)))
        runner.start()
        self.assertTrue(received.wait(2))
        self.assertTrue(runner.is_alive())
        runner.join(3)
        self.assertEqual(result[0]["status"], "ok")

    @skipIf(sys.platform == "win32", "需要 POSIX SIGTERM")
    def test_timeout_kills_worker_that_ignores_terminate(self):
        created = []
        original = subprocess.Popen

        def start(*args, **kwargs):
            instance = original(*args, **kwargs)
            created.append(instance)
            return instance

        script = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(10)"
        with (
            patch.object(_process.subprocess, "Popen", side_effect=start),
            patch.object(_process, "PROCESS_STOP_GRACE_SECONDS", 0.05),
            self.assertRaises(TimeoutError),
        ):
            self._run(script, timeout=0.2)
        self.assertIsNotNone(created[0].poll())
        self.assertNotIn("private-cookie", str(created[0].args))

    def test_partial_json_then_hang_remains_bounded(self):
        with self.assertRaises(TimeoutError):
            self._run(
                "import sys,time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(10)",
                timeout=0.2,
            )

    def test_nonzero_exit_cannot_claim_success(self):
        with self.assertRaisesRegex(RuntimeError, "exitcode=7"):
            self._run("import json; print(json.dumps({'status':'ok'})); raise SystemExit(7)")

    def test_failed_process_start_does_not_leave_log_reader(self):
        with self.assertRaises(OSError):
            _process.run_export_process(
                {"watchdog_timeout": 1},
                self.context,
                self.logger,
                command=["/missing/p115-export-worker"],
            )

    def test_nonstandard_callable_is_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "标准目录树路径转义"):
            _process.run_export_process(
                {"escape_func": lambda text: text, "watchdog_timeout": 1}, self.context, self.logger
            )
