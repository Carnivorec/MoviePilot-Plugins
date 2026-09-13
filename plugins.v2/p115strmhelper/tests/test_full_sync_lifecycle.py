"""全量同步的真实线程回收、Rust 写入兼容、失败记录和目录树隔离测试"""

import ast
import json
import os
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from itertools import batched
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Thread, Lock
from types import SimpleNamespace
from typing import Any, Dict, Generator, List, Optional, Set, Tuple
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4


class MemoryTree:
    """按真实目录树命名保存条目，检查析构是否影响其他实例"""

    values = {}

    def __init__(self, path):
        self.path = str(path)
        self.values.setdefault(self.path, [])

    def clear(self):
        self.values[self.path] = []


def _load_class(path, name, methods, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class TestFullSyncLifecycle(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.threads = []
        self.config = SimpleNamespace(
            user_rmt_mediaext="mkv", user_download_mediaext="srt",
            full_sync_auto_download_mediainfo_enabled=False, pan_transfer_enabled=False,
            pan_transfer_paths="", full_sync_overwrite_mode="always", full_sync_remove_unless_strm=False,
            full_sync_cleanup_confirm_mode="none", full_sync_media_server_refresh_enabled=False,
            full_sync_mediaservers=[], full_sync_media_server_refresh_delay=0,
            strm_generate_blacklist=[], mediainfo_download_whitelist=[], mediainfo_download_blacklist=[],
            PLUGIN_TEMP_PATH=self.root, full_sync_strm_log=False, full_sync_process_rust=False,
            full_sync_process_num=1, full_sync_min_file_size=0,
            get_config=lambda key: {"full_sync_iter_function": "iter_files_with_path_skim", "full_sync_batch_num": 100}[key],
            get_ios_ua_app=lambda **kwargs: {},
        )
        self.history = Mock()

        def make_thread(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            self.threads.append((thread, kwargs.get("target").__name__))
            return thread

        self.namespace = {
            "P115Client": object, "MediaInfoDownloader": object, "Optional": Optional, "Dict": Dict,
            "Any": Any, "List": List, "Set": Set, "Tuple": Tuple, "Generator": Generator,
            "contextmanager": contextmanager, "configer": self.config, "uuid4": uuid4,
            "logger": Mock(), "FileDbHelper": Mock(), "MediaServerRefresh": Mock(return_value=SimpleNamespace(enabled=False)),
            "StrmUrlGetter": Mock(return_value=SimpleNamespace(get_strm_url=lambda *args: "http://example.invalid/play")),
            "AutomatonUtils": SimpleNamespace(build_automaton=lambda _: None), "Queue": Queue, "Path": Path,
            "DirectoryTree": MemoryTree, "Thread": make_thread, "perf_counter": time.perf_counter,
            "ThreadPoolExecutor": ThreadPoolExecutor, "as_completed": as_completed, "batched": batched,
            "sleep": lambda _: None, "get_pid_by_path": Mock(return_value=42), "Empty": Empty,
            "CBase64": SimpleNamespace(encode=lambda _: "path-key"), "Processor": Mock(), "PackedResult": object,
            "rust_core_version": "test", "dumps": lambda value: json.dumps(value).encode(),
            "iter_files_with_path_skim": Mock(return_value=[{"path": "/pan/movie.mkv", "name": "movie.mkv"}]),
            "iter_files_with_path": Mock(), "StrmExecHistoryManager": self.history, "makedirs": os.makedirs,
            "PathUtils": SimpleNamespace(sanitize_path_parts=lambda path: path),
            "StrmGenerater": SimpleNamespace(get_strm_filename=lambda path: path.stem + ".strm"),
            "sentry_manager": Mock(), "settings": SimpleNamespace(CACHE_BACKEND_TYPE="redis"),
            "ProcessResult": namedtuple("ProcessResult", "status path message data path_entry"),
        }
        source = Path(__file__).resolve().parents[1] / "helper/strm/full/__init__.py"
        self.cls = _load_class(source, "FullSyncStrmHelper", {
            "__init__", "__del__", "_clean_tree", "__base_no_logger", "__base_has_logger",
            "generate_strm_files", "_generate_strm_files", "_io_writer_session", "__io_writer_worker",
            "__flush_write_buffer", "get_sync_error", "get_generate_total", "result_print",
        }, self.namespace)
        self.helper = self._new_helper()

    def _new_helper(self):
        downloader = Mock()
        downloader.batch_auto_downloader.return_value = (0, 0, [])
        helper = self.cls(object(), downloader)
        helper.strm_exec_history_kind = "full"
        helper._FullSyncStrmHelper__process_db_item = lambda batch, folders, files: (folders, files)
        helper._flush_deferred_strm_cleanup_batch = Mock()
        return helper

    def tearDown(self):
        # 对照旧代码时也回收本测试创建的线程，不遗留阻塞 worker
        for thread, role in self.threads:
            if thread.is_alive() and role == "__io_writer_worker":
                self.helper.write_queue.put(None)
        for thread, _ in self.threads:
            thread.join(2)
        self.directory.cleanup()

    def _assert_threads_stopped(self):
        self.assertTrue(self.threads)
        self.assertFalse(any(thread.is_alive() for thread, _ in self.threads))

    def test_directory_lookup_failure_reaps_workers_and_records_failure(self):
        self.namespace["get_pid_by_path"].side_effect = OSError("lookup failed")
        self.assertFalse(self.helper.generate_strm_files(f"{self.root}/target#/pan"))
        self._assert_threads_stopped()
        self.assertFalse(self.history.append_run.call_args.kwargs["success"])

    def test_malformed_path_does_not_leave_threads_running(self):
        with self.assertRaises(IndexError):
            self.helper.generate_strm_files("missing-separator")
        self._assert_threads_stopped()
        self.assertFalse(self.history.append_run.call_args.kwargs["success"])

    def test_rust_results_still_write_files_and_close_threads(self):
        self.config.full_sync_process_rust = True
        self.namespace["Processor"].return_value.process_batch.return_value = SimpleNamespace(
            fail_results=[], download_results=[], skip_results=[], strm_results=[SimpleNamespace(
                path_in_pan="/pan/movie.mkv", pickcode="pick", original_file_name="movie.mkv")],
        )
        target = self.root / "target"
        self.assertTrue(self.helper.generate_strm_files(f"{target}#/pan"))
        self.assertEqual((target / "movie.strm").read_text(), "http://example.invalid/play")
        self.assertEqual(self.helper.strm_count, 1)
        self._assert_threads_stopped()

    def test_old_destructor_cannot_clear_new_full_sync_trees(self):
        newer = self._new_helper()
        key = str(newer.pan_tree_path)
        MemoryTree.values[key] = ["/new/movie.strm"]
        self.helper.__del__()
        self.assertEqual(MemoryTree.values[key], ["/new/movie.strm"])
        self.assertNotEqual(self.helper.local_tree_path, newer.local_tree_path)

    def test_file_failure_marks_history_failed(self):
        self.helper.strm_fail_count = 1
        self.helper.get_generate_total()
        self.assertFalse(self.history.append_run.call_args.kwargs["success"])

    def test_partial_thread_start_failure_reaps_started_workers(self):
        original = self.namespace["Thread"]

        def start(*args, **kwargs):
            if len(self.threads) == 2:
                raise RuntimeError("cannot start thread")
            return original(*args, **kwargs)

        self.namespace["Thread"] = start
        with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
            self.helper.generate_strm_files(f"{self.root}/target#/pan")
        self._assert_threads_stopped()
        self.assertEqual(self.helper.result_queue.unfinished_tasks, 0)

    def test_service_propagates_full_failure_and_releases_guard(self):
        helper = Mock()
        helper.get_generate_total.return_value = (0, 0, 0, 0, 0, 0)
        helper.get_sync_error.return_value = "全量失败"
        ns = {"logger": Mock(), "FullSyncStrmHelper": Mock(return_value=helper),
              "configer": SimpleNamespace(get_config=lambda key: False if key == "notify" else "configured"),
              "sentry_manager": SimpleNamespace(capture_all_class_exceptions=lambda cls: cls)}
        source = Path(__file__).resolve().parents[1] / "service/__init__.py"
        cls = _load_class(source, "ServiceHelper", {"full_sync_strm_files", "_run_full_sync"}, ns)
        service = cls()
        service.client = service.mediainfodownloader = object()
        service._sync_state_lock = Lock()
        service._full_sync_running = service._increment_sync_running = False
        with self.assertRaisesRegex(RuntimeError, "全量失败"):
            service.full_sync_strm_files()
        self.assertFalse(service._full_sync_running)
