import itertools
import sys
import types
import unittest
from pathlib import Path

from utils.sync_lock import (
    STRM_SYNC_TASK_FULL,
    STRM_SYNC_TASK_INCREMENT,
    StrmSyncRunGuard,
)


class StrmSyncRunGuardTest(unittest.TestCase):
    def test_full_running_blocks_increment_until_release(self):
        guard = StrmSyncRunGuard()

        self.assertTrue(guard.acquire("全量STRM生成"))
        self.assertEqual(guard.current_task, "全量STRM生成")
        self.assertFalse(guard.acquire("增量STRM生成"))

        guard.release()

        self.assertTrue(guard.acquire("增量STRM生成"))
        self.assertEqual(guard.current_task, "增量STRM生成")
        guard.release()

    def test_increment_running_blocks_full_until_release(self):
        guard = StrmSyncRunGuard()

        self.assertTrue(
            guard.acquire("增量STRM生成", task_kind=STRM_SYNC_TASK_INCREMENT)
        )
        self.assertEqual(guard.current_task, "增量STRM生成")
        self.assertEqual(guard.current_task_kind, STRM_SYNC_TASK_INCREMENT)
        self.assertFalse(guard.acquire("全量STRM生成"))

        self.assertEqual(guard.release(), STRM_SYNC_TASK_INCREMENT)

        self.assertTrue(guard.acquire("全量STRM生成", task_kind=STRM_SYNC_TASK_FULL))
        self.assertEqual(guard.current_task, "全量STRM生成")
        self.assertEqual(guard.current_task_kind, STRM_SYNC_TASK_FULL)
        guard.release()

    def test_pending_full_sync_flag_merges_duplicate_requests(self):
        guard = StrmSyncRunGuard()

        self.assertFalse(guard.pending_full_sync)
        self.assertTrue(guard.mark_full_sync_pending())
        self.assertTrue(guard.pending_full_sync)
        self.assertFalse(guard.mark_full_sync_pending())
        self.assertTrue(guard.clear_full_sync_pending())
        self.assertFalse(guard.pending_full_sync)

    def test_pending_full_sync_runner_reserves_once(self):
        guard = StrmSyncRunGuard()

        self.assertFalse(guard.reserve_full_sync_runner())
        guard.mark_full_sync_pending()
        self.assertTrue(guard.reserve_full_sync_runner())
        self.assertFalse(guard.reserve_full_sync_runner())
        guard.finish_full_sync_runner()
        self.assertTrue(guard.reserve_full_sync_runner())


class ServiceEntryGuardSourceTest(unittest.TestCase):
    def test_sync_entries_check_guard_before_core_helper_creation(self):
        service_path = (
            Path(__file__).resolve().parents[1] / "service" / "__init__.py"
        )
        source = service_path.read_text(encoding="utf-8")

        full_func = source[source.index("    def full_sync_strm_files") :]
        full_func = full_func[: full_func.index("    def start_full_sync")]
        self.assertLess(
            full_func.index(
                "if not self._enter_strm_sync_task(task_name, STRM_SYNC_TASK_FULL):"
            ),
            full_func.index("FullSyncStrmHelper("),
        )
        self.assertIn("finally:", full_func)
        self.assertIn("self._leave_strm_sync_task(task_name)", full_func)

        increment_func = source[source.index("    def increment_sync_strm_files") :]
        increment_func = increment_func[: increment_func.index("    def hdhive_checkin_scheduler_tick")]
        self.assertLess(
            increment_func.index(
                "if not self._enter_strm_sync_task(task_name, STRM_SYNC_TASK_INCREMENT):"
            ),
            increment_func.index("IncrementSyncStrmHelper("),
        )
        self.assertIn("finally:", increment_func)
        self.assertIn("self._leave_strm_sync_task(task_name)", increment_func)

    def test_increment_checks_full_priority_before_acquiring_guard(self):
        service_path = (
            Path(__file__).resolve().parents[1] / "service" / "__init__.py"
        )
        source = service_path.read_text(encoding="utf-8")
        increment_func = source[source.index("    def increment_sync_strm_files") :]
        increment_func = increment_func[: increment_func.index("    def hdhive_checkin_scheduler_tick")]

        self.assertLess(
            increment_func.index("if self._should_skip_increment_for_full_priority():"),
            increment_func.index(
                "if not self._enter_strm_sync_task(task_name, STRM_SYNC_TASK_INCREMENT):"
            ),
        )

    def test_full_entry_registers_pending_when_increment_holds_guard(self):
        service_path = (
            Path(__file__).resolve().parents[1] / "service" / "__init__.py"
        )
        source = service_path.read_text(encoding="utf-8")
        enter_func = source[source.index("    def _enter_strm_sync_task") :]
        enter_func = enter_func[: enter_func.index("    def _start_pending_full_sync_if_needed")]

        self.assertIn("task_kind == STRM_SYNC_TASK_FULL", enter_func)
        self.assertIn("running_task_kind == STRM_SYNC_TASK_INCREMENT", enter_func)
        self.assertIn("self.strm_sync_guard.mark_full_sync_pending()", enter_func)

    def test_pending_full_sync_blocks_new_increment_and_replays_after_release(self):
        service_path = (
            Path(__file__).resolve().parents[1] / "service" / "__init__.py"
        )
        source = service_path.read_text(encoding="utf-8")
        skip_func = source[
            source.index("    def _should_skip_increment_for_full_priority") :
        ]
        skip_func = skip_func[: skip_func.index("    def _enter_strm_sync_task")]
        leave_func = source[source.index("    def _leave_strm_sync_task") :]
        leave_func = leave_func[: leave_func.index("    def _create_mediainfo_downloader_for_task")]
        replay_func = source[source.index("    def _run_pending_full_sync") :]
        replay_func = replay_func[: replay_func.index("    def _leave_strm_sync_task")]

        self.assertIn("self.strm_sync_guard.pending_full_sync", skip_func)
        self.assertIn("跳过本次增量", skip_func)
        self.assertIn("released_task_kind == STRM_SYNC_TASK_INCREMENT", leave_func)
        self.assertIn("self._start_pending_full_sync_if_needed()", leave_func)
        self.assertIn("self.full_sync_strm_files(_from_pending=True)", replay_func)


class MediaInfoDownloaderStateTest(unittest.TestCase):
    def setUp(self):
        self._saved_modules = {}
        self._install_import_stubs()
        package_root = str(Path(__file__).resolve().parents[2])
        if package_root not in sys.path:
            sys.path.insert(0, package_root)
            self._added_path = package_root
        else:
            self._added_path = None

        sys.modules.pop("p115strmhelper.helper.mediainfo_download", None)
        from p115strmhelper.helper.mediainfo_download import MediaInfoDownloader

        self.MediaInfoDownloader = MediaInfoDownloader

    def tearDown(self):
        sys.modules.pop("p115strmhelper.helper.mediainfo_download", None)
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if self._added_path:
            try:
                sys.path.remove(self._added_path)
            except ValueError:
                pass

    def _save_module(self, name):
        if name not in self._saved_modules:
            self._saved_modules[name] = sys.modules.get(name)

    def _set_module(self, name, module):
        self._save_module(name)
        sys.modules[name] = module

    def _install_import_stubs(self):
        package_dir = Path(__file__).resolve().parents[1]
        p115_pkg = types.ModuleType("p115strmhelper")
        p115_pkg.__path__ = [str(package_dir)]
        self._set_module("p115strmhelper", p115_pkg)

        helper_pkg = types.ModuleType("p115strmhelper.helper")
        helper_pkg.__path__ = [str(package_dir / "helper")]
        self._set_module("p115strmhelper.helper", helper_pkg)

        core_pkg = types.ModuleType("p115strmhelper.core")
        core_pkg.__path__ = [str(package_dir / "core")]
        self._set_module("p115strmhelper.core", core_pkg)

        utils_pkg = types.ModuleType("p115strmhelper.utils")
        utils_pkg.__path__ = [str(package_dir / "utils")]
        self._set_module("p115strmhelper.utils", utils_pkg)

        if not hasattr(itertools, "batched"):
            def batched(iterable, n):
                iterator = iter(iterable)
                while batch := tuple(itertools.islice(iterator, n)):
                    yield batch

            itertools.batched = batched

        aiofiles = types.ModuleType("aiofiles")
        aiofiles.open = object()
        aiofiles_os = types.ModuleType("aiofiles.os")
        aiofiles_os.stat = object()
        self._set_module("aiofiles", aiofiles)
        self._set_module("aiofiles.os", aiofiles_os)

        httpx = types.ModuleType("httpx")
        httpx.AsyncClient = object
        httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
        httpx.LocalProtocolError = type("LocalProtocolError", (Exception,), {})
        httpx.RequestError = type("RequestError", (Exception,), {})
        httpx.stream = object()
        self._set_module("httpx", httpx)

        orjson = types.ModuleType("orjson")
        orjson.loads = lambda data: {}
        self._set_module("orjson", orjson)

        p115center = types.ModuleType("p115center")
        p115center.P115Center = type("P115Center", (), {})
        self._set_module("p115center", p115center)

        p115pickcode = types.ModuleType("p115pickcode")
        p115pickcode.pickcode_to_id = lambda pickcode: int(pickcode)
        self._set_module("p115pickcode", p115pickcode)

        p115client = types.ModuleType("p115client")
        p115client.P115Client = type("P115Client", (), {})
        p115client.check_response = lambda resp: resp
        self._set_module("p115client", p115client)

        p115client_const = types.ModuleType("p115client.const")
        p115client_const.TYPE_TO_SUFFIXES = {2: [".jpg", ".png"]}
        self._set_module("p115client.const", p115client_const)

        p115client_util = types.ModuleType("p115client.util")
        p115client_util.reduce_image_url_layers = lambda value: value
        self._set_module("p115client.util", p115client_util)

        p115client_tool = types.ModuleType("p115client.tool")
        p115client_tool_iterdir = types.ModuleType("p115client.tool.iterdir")
        p115client_tool_iterdir._iter_fs_files = lambda *args, **kwargs: iter(())
        p115client_tool_iterdir.iter_files = lambda *args, **kwargs: iter(())
        p115client_tool_iterdir.iter_files_with_path_skim = (
            lambda *args, **kwargs: iter(())
        )
        self._set_module("p115client.tool", p115client_tool)
        self._set_module("p115client.tool.iterdir", p115client_tool_iterdir)

        zstandard = types.ModuleType("zstandard")
        zstandard.ZstdCompressor = type("ZstdCompressor", (), {})
        zstandard.ZstdDecompressor = type("ZstdDecompressor", (), {})
        self._set_module("zstandard", zstandard)

        app = types.ModuleType("app")
        app_log = types.ModuleType("app.log")
        app_log.logger = _FakeLogger()
        self._set_module("app", app)
        self._set_module("app.log", app_log)

        config_module = types.ModuleType("p115strmhelper.core.config")
        config_module.configer = _FakeConfiger()
        self._set_module("p115strmhelper.core.config", config_module)

        cache_module = types.ModuleType("p115strmhelper.core.cache")
        cache_module.OofFastMiCache = type("OofFastMiCache", (), {})
        self._set_module("p115strmhelper.core.cache", cache_module)

        url_module = types.ModuleType("p115strmhelper.utils.url")
        url_module.Url = type("Url", (), {"of": staticmethod(lambda url, data: url)})
        self._set_module("p115strmhelper.utils.url", url_module)

        sentry_module = types.ModuleType("p115strmhelper.utils.sentry")
        sentry_module.sentry_manager = _FakeSentryManager()
        self._set_module("p115strmhelper.utils.sentry", sentry_module)

        exception_module = types.ModuleType("p115strmhelper.utils.exception")
        exception_module.DownloadValidationFail = type(
            "DownloadValidationFail", (Exception,), {}
        )
        self._set_module("p115strmhelper.utils.exception", exception_module)

        timeout_module = types.ModuleType("p115strmhelper.utils.p115_timeout")
        timeout_module.build_p115_request_kwargs = lambda **kwargs: {}
        self._set_module("p115strmhelper.utils.p115_timeout", timeout_module)

    def _new_downloader_stub(self):
        downloader = self.MediaInfoDownloader.__new__(self.MediaInfoDownloader)
        downloader.stop_all_flag = None
        downloader.mediainfo_count = 0
        downloader.mediainfo_fail_count = 0
        downloader.mediainfo_fail_dict = []
        downloader._pending_delete_scids = []
        downloader._pending_delete_task_types = []
        downloader.deleted_batches = []

        def fake_delete(scids, task_type="媒体信息文件下载"):
            downloader.deleted_batches.append((list(scids), task_type))

        downloader._batch_fs_delete = fake_delete
        downloader._closed = True
        return downloader

    def test_two_downloader_tasks_do_not_reset_each_other_state(self):
        first = self._new_downloader_stub()
        second = self._new_downloader_stub()

        first.mediainfo_count = 3
        first.mediainfo_fail_count = 1
        first.mediainfo_fail_dict = ["/a/movie.ass"]
        first._queue_pending_delete(101, "字幕")

        second._reset_download_run_state("普通媒体信息", [{"path": "/b/movie.srt"}])

        self.assertEqual(first.mediainfo_count, 3)
        self.assertEqual(first.mediainfo_fail_count, 1)
        self.assertEqual(first.mediainfo_fail_dict, ["/a/movie.ass"])
        self.assertEqual(first._pending_delete_scids, [101])
        self.assertEqual(first._pending_delete_task_types, ["字幕"])
        self.assertEqual(second.mediainfo_count, 0)
        self.assertEqual(second._pending_delete_scids, [])

    def test_multiple_subtitle_batches_flush_complete_delete_list(self):
        downloader = self._new_downloader_stub()

        for scid in [11, 22, 33]:
            downloader._queue_pending_delete(scid, "字幕")
            downloader._flush_pending_deletes()

        self.assertEqual(downloader.deleted_batches, [])

        downloader._flush_pending_deletes(force=True)

        self.assertEqual(downloader.deleted_batches, [([11, 22, 33], "字幕")])
        self.assertEqual(downloader._pending_delete_scids, [])


class _FakeLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def warn(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _FakeConfiger:
    PLUGIN_TEMP_PATH = Path("/tmp")
    p115center_license = ""

    @staticmethod
    def get_user_agent():
        return "fake-agent"

    @staticmethod
    def get_ios_ua_app(app=True):
        return {}


class _FakeSentryManager:
    @staticmethod
    def capture_all_class_exceptions(cls):
        return cls


if __name__ == "__main__":
    unittest.main()
