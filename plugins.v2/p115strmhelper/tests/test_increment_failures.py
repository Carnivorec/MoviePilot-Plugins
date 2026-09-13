"""增量失败状态与本地扫描线程生命周期的行为回归测试"""

import ast
import time
from collections import deque
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple
from unittest import TestCase
from unittest.mock import Mock


class TreeFailure(Exception):
    """模拟目录树导出或本地扫描失败"""


def _load_class(relative_path, class_name, methods, namespace):
    source = Path(__file__).resolve().parents[1] / relative_path
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[class_name]


def _helper():
    history = Mock()
    namespace = {
        "Path": Path, "Optional": Optional, "List": List, "Tuple": Tuple, "Dict": Dict,
        "deque": deque, "Thread": Thread, "perf_counter": time.perf_counter,
        "sleep": lambda _: None, "logger": Mock(), "sentry_manager": Mock(),
        "ItertreeInternalError": TreeFailure, "StrmExecHistoryManager": history,
        "configer": SimpleNamespace(increment_sync_second_level_dir_scan=False),
        "settings": SimpleNamespace(CACHE_BACKEND_TYPE="redis"),
    }
    cls = _load_class("helper/strm/increment.py", "IncrementSyncStrmHelper", {
        "generate_strm_files", "get_generate_total", "get_sync_error",
        "__generate_local_tree", "__wait_generate_local_tree",
    }, namespace)
    helper = cls()
    helper.sync_failures = {}
    helper._local_scan_error = None
    helper.strm_count = helper.strm_fail_count = helper.mediainfo_count = 0
    helper.mediainfo_fail_count = helper.remove_unless_strm_count = 0
    helper.total_iterated = helper.api_count = 0
    helper.elapsed_time = 0.0
    helper.strm_fail_dict = {}
    helper.mediainfo_fail_dict = []
    helper.auto_download_mediainfo = False
    helper.download_mediaext = [".srt"]
    helper.download_mediainfo_list = []
    helper.remove_unless_strm = False
    helper.local_tree = Mock()
    helper.local_strm_tree = Mock()
    helper.local_tree_path = Path("/unused-local-tree")
    helper.pan_to_local_tree_path = Path("/unused-pan-tree")
    helper.strm_exec_history_kind = "increment"
    helper.mediainfodownloader = Mock()
    helper.mediainfodownloader.batch_auto_downloader.return_value = (0, 0, [])
    helper._IncrementSyncStrmHelper__generate_pan_tree = Mock()
    helper._generate_additions = Mock()
    return helper, history


class TestIncrementFailureReporting(TestCase):
    def test_exhausted_directory_retries_are_recorded_as_failure(self):
        helper, history = _helper()
        helper._IncrementSyncStrmHelper__generate_pan_tree.side_effect = TreeFailure("export failed")
        helper.generate_strm_files("/local#/pan")
        helper.get_generate_total()
        self.assertEqual(helper._IncrementSyncStrmHelper__generate_pan_tree.call_count, 3)
        helper._generate_additions.assert_not_called()
        record = history.append_run.call_args.kwargs
        self.assertFalse(record["success"])
        self.assertIn("export failed", record["error"])
        self.assertEqual(record["stats"]["directory_fail_count"], 1)

    def test_successful_retry_does_not_leave_a_failure(self):
        helper, history = _helper()
        helper._IncrementSyncStrmHelper__generate_pan_tree.side_effect = [TreeFailure("temporary"), None]
        helper.generate_strm_files("/local#/pan")
        helper.get_generate_total()
        helper._generate_additions.assert_called_once()
        self.assertTrue(history.append_run.call_args.kwargs["success"])
        self.assertIsNone(helper.get_sync_error())

    def test_invalid_path_is_reported_and_other_paths_continue(self):
        helper, history = _helper()
        helper.generate_strm_files("missing-separator\n/#/pan\n/local#/pan")
        helper.get_generate_total()
        helper._generate_additions.assert_called_once()
        self.assertEqual(len(helper.sync_failures), 2)
        self.assertFalse(history.append_run.call_args.kwargs["success"])

    def test_local_scan_error_prevents_generation_from_partial_tree(self):
        helper, history = _helper()
        helper.local_tree.scan_directory_to_tree.side_effect = OSError("unreadable local directory")
        helper.generate_strm_files("/local#/pan")
        helper.get_generate_total()
        helper._generate_additions.assert_not_called()
        self.assertEqual(helper.local_tree.scan_directory_to_tree.call_count, 3)
        self.assertIn("unreadable local directory", history.append_run.call_args.kwargs["error"])

    def test_file_failures_are_not_recorded_as_success(self):
        helper, history = _helper()
        helper.strm_fail_count = 1
        helper.get_generate_total()
        self.assertFalse(history.append_run.call_args.kwargs["success"])
        self.assertIn("STRM 生成失败 1 个", helper.get_sync_error())

    def test_batch_download_exception_keeps_failure_details(self):
        helper, history = _helper()
        helper.mediainfodownloader.batch_auto_downloader.side_effect = OSError("download failed")
        with self.assertRaises(OSError):
            helper.generate_strm_files("/local#/pan")
        helper.get_generate_total()
        self.assertIn("download failed", history.append_run.call_args.kwargs["error"])
        self.assertFalse(history.append_run.call_args.kwargs["success"])

    def test_failed_export_waits_for_scanner_before_reusing_trees(self):
        helper, _ = _helper()
        scanning = Event()
        previous_scanners = []
        original_start = helper._IncrementSyncStrmHelper__generate_local_tree

        def scan(**kwargs):
            scanning.set()
            time.sleep(0.03)
            scanning.clear()

        def start(**kwargs):
            previous_scanners.append(scanning.is_set())
            return original_start(**kwargs)

        def export(**kwargs):
            self.assertTrue(scanning.wait(1))
            raise TreeFailure("export failed while scanning")

        helper.local_tree.scan_directory_to_tree.side_effect = scan
        helper._IncrementSyncStrmHelper__generate_local_tree = start
        helper._IncrementSyncStrmHelper__generate_pan_tree.side_effect = export
        helper.generate_strm_files("/local-a#/pan-a\n/local-b#/pan-b")
        self.assertEqual(previous_scanners, [False] * 6)
        self.assertFalse(scanning.is_set())


class TestIncrementServiceFailure(TestCase):
    def test_failure_reaches_scheduler_and_releases_sync_guard(self):
        helper = Mock()
        helper.get_generate_total.return_value = (0, 0, 0, 0, 0)
        helper.get_sync_error.return_value = "目录同步失败"
        namespace = {
            "logger": Mock(), "IncrementSyncStrmHelper": Mock(return_value=helper),
            "configer": SimpleNamespace(get_config=lambda key: False if key == "notify" else "configured"),
            "sentry_manager": SimpleNamespace(capture_all_class_exceptions=lambda cls: cls),
        }
        source = Path(__file__).resolve().parents[1] / "service/__init__.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        class_name = next(node.name for node in tree.body if isinstance(node, ast.ClassDef)
                          and any(isinstance(method, ast.FunctionDef) and method.name == "_run_increment_sync"
                                  for method in node.body))
        cls = _load_class("service/__init__.py", class_name,
                          {"increment_sync_strm_files", "_run_increment_sync"}, namespace)
        service = cls()
        service.client = service.mediainfodownloader = Mock()
        service._sync_state_lock = Lock()
        service._full_sync_running = service._increment_sync_running = False
        service._full_sync_pending = True
        service.full_sync_strm_files = Mock()
        with self.assertRaisesRegex(RuntimeError, "目录同步失败"):
            service.increment_sync_strm_files()
        self.assertFalse(service._increment_sync_running)
        helper.get_generate_total.assert_called_once()
        service.full_sync_strm_files.assert_called_once()
