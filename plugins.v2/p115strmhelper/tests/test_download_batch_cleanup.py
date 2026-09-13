"""下载批次异常清理、重试保留和停止状态隔离测试"""

import ast
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Generator, List, Set
from unittest import TestCase
from unittest.mock import Mock

from p115client import check_response

def _downloader():
    source = Path(__file__).resolve().parents[1] / "helper/mediainfo_download/__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MediaInfoDownloader")
    names = {"_batch_fs_delete", "_flush_pending_deletes", "_batch_download_session",
             "batch_auto_downloader", "batch_auto_share_downloader"}
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        "Path": Path, "List": List, "Set": Set, "Generator": Generator,
        "contextmanager": contextmanager, "TYPE_TO_SUFFIXES": {2: [".jpg"]},
        "logger": Mock(), "time_sleep": lambda _: None, "check_response": check_response,
        "sentry_manager": SimpleNamespace(capture_all_class_exceptions=lambda cls: cls),
        "configer": SimpleNamespace(get_ios_ua_app=lambda **kwargs: {}),
    }
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
    downloader = namespace[cls.name]()
    downloader._batch_lock = Lock()
    downloader._pending_delete_scids = []
    downloader.client = Mock()
    downloader.client.fs_delete.return_value = {"state": True}
    downloader.stop_all_flag = True
    downloader.mediainfo_count = 7
    downloader.mediainfo_fail_count = 3
    downloader.mediainfo_fail_dict = ["old"]
    return downloader


class TestDownloadBatchCleanup(TestCase):
    def test_exception_cleans_temporary_directories_in_both_batch_entries(self):
        for share in (False, True):
            with self.subTest(share=share):
                downloader = _downloader()

                def fail(items):
                    downloader._pending_delete_scids.append(123)
                    raise OSError("batch interrupted")

                if share:
                    downloader.batch_share_subtitle_downloader = fail
                    run = downloader.batch_auto_share_downloader
                else:
                    downloader.batch_subtitle_downloader = fail
                    run = downloader.batch_auto_downloader
                with self.assertRaisesRegex(OSError, "batch interrupted"):
                    run([{"path": "/local/subtitle.srt"}])
                downloader.client.fs_delete.assert_called_once_with([123])
                self.assertEqual(downloader._pending_delete_scids, [])

    def test_failed_deletes_survive_until_next_batch_recovers(self):
        downloader = _downloader()
        downloader._pending_delete_scids = [123]
        downloader.client.fs_delete.side_effect = OSError("115 unavailable")
        downloader.batch_auto_downloader([])
        self.assertEqual(downloader._pending_delete_scids, [123])
        self.assertEqual(downloader.client.fs_delete.call_count, 3)
        downloader.client.fs_delete.side_effect = None
        downloader.batch_auto_share_downloader([])
        self.assertEqual(downloader._pending_delete_scids, [])
        self.assertEqual(downloader.client.fs_delete.call_count, 4)

    def test_share_batch_resets_stop_flag_and_statistics(self):
        downloader = _downloader()
        result = downloader.batch_auto_share_downloader([])
        self.assertEqual(result, (0, 0, []))
        self.assertFalse(downloader.stop_all_flag)

    def test_large_failed_delete_batch_is_retained_without_busy_loop(self):
        downloader = _downloader()
        downloader._pending_delete_scids = list(range(60))
        downloader.client.fs_delete.side_effect = OSError("115 unavailable")
        downloader._flush_pending_deletes(force=True)
        self.assertEqual(downloader._pending_delete_scids, list(range(60)))
        self.assertEqual(downloader.client.fs_delete.call_count, 3)

    def test_api_failure_response_keeps_pending_directory_ids(self):
        downloader = _downloader()
        downloader._pending_delete_scids = [123]
        downloader.client.fs_delete.return_value = {"state": False, "errno": 99}
        downloader._flush_pending_deletes(force=True)
        self.assertEqual(downloader._pending_delete_scids, [123])
