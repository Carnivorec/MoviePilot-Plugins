"""存储返回值、快照完整性和下载落盘的行为测试，不连接网盘"""

import ast
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from types import SimpleNamespace
from typing import Dict, List, Optional
from unittest import TestCase
from unittest.mock import MagicMock, Mock


class FileItem(SimpleNamespace):
    """提供存储方法使用的文件项契约"""

    def __init__(self, **kwargs):
        values = dict(type="file", fileid="1", path="/movie.mkv", name="movie.mkv",
                      modify_time=None, size=0, pickcode="pick", storage="P115")
        super().__init__(**(values | kwargs))

    def model_dump(self, exclude=()):
        return {key: value for key, value in vars(self).items() if key not in exclude}


class StorageQueryError(Exception):
    """模拟宿主定义的存储查询异常"""


def _load(filename, class_name, methods, namespace):
    source = Path(__file__).resolve().parents[1] / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[class_name]()


def _api():
    namespace = {
        "Path": Path, "Optional": Optional, "List": List, "Dict": Dict, "FileItem": FileItem,
        "logger": Mock(), "get_ios_ua_app": lambda **_: {},
        "iter_files_with_path_skim": Mock(), "fs_files_iter": Mock(), "normalize_attr": lambda item: item,
        "StorageChain": Mock(), "StorageQueryError": StorageQueryError,
        "P115NotADirectoryError": NotADirectoryError, "NamedTemporaryFile": NamedTemporaryFile,
        "stream": MagicMock(), "RequestError": ConnectionError,
        "settings": SimpleNamespace(USER_AGENT="test", TEMP_PATH=Path("/unused")),
        "global_vars": SimpleNamespace(is_transfer_stopped=Mock(return_value=False)),
        "transfer_process": Mock(return_value=Mock()),
    }
    api = _load("p115_api.py", "P115Api", {"iter_files", "list", "download", "_get_cached_item"}, namespace)
    api._disk_name = "P115"
    api._id_cache = api._id_item_cache = Mock()
    api._list_rate_limiter = Mock()
    api._get_item_fail_records = {}
    api.client = Mock()
    api.client.download_url.return_value.geturl.return_value = "https://example.invalid/file"
    api.get_item = Mock(return_value=FileItem(size=6))
    return api, namespace


class TestRecursiveListing(TestCase):
    def test_stale_cache_pair_does_not_return_a_different_file(self):
        api, _ = _api()
        api._id_cache.get_id_by_dir.return_value = 12
        api._id_item_cache.get_item.return_value = dict(
            id=12, path="/old/movie.mkv", is_dir=False, size=6, modify_time=None, pickcode="pick",
        )
        self.assertIsNone(api._get_cached_item(Path("/new/movie.mkv")))
        api._id_item_cache.get_item.return_value["path"] = "/new/movie.mkv"
        self.assertEqual(api._get_cached_item(Path("/new/movie.mkv")).path, "/new/movie.mkv")

    def test_successful_recursive_listing_returns_collected_items(self):
        api, ns = _api()
        ns["iter_files_with_path_skim"].return_value = [dict(
            path="/movies/movie.mkv", is_dir=False, id=12, parent_id=10,
            size=6, pickcode="pick", name="movie.mkv",
        )]
        items = api.iter_files(FileItem(type="dir", path="/movies/", fileid="10"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].path, "/movies/movie.mkv")
        self.assertEqual(items[0].fileid, "12")

    def test_empty_directory_and_failed_listing_are_distinguishable(self):
        api, ns = _api()
        root = FileItem(type="dir", path="/", fileid="0")
        ns["iter_files_with_path_skim"].return_value = []
        self.assertEqual(api.iter_files(root), [])
        ns["iter_files_with_path_skim"].side_effect = OSError("offline")
        self.assertIsNone(api.iter_files(root))

    def test_strict_list_does_not_accept_failed_fallback_as_empty(self):
        api, ns = _api()
        root = FileItem(type="dir", path="/", fileid="0")
        ns["fs_files_iter"].side_effect = OSError("offline")
        fallback = ns["StorageChain"].return_value.list_files
        fallback.return_value = None
        with self.assertRaises(StorageQueryError):
            api.list(root, strict=True)
        fallback.side_effect = OSError("fallback offline")
        with self.assertRaises(StorageQueryError):
            api.list(root, strict=True)
        fallback.side_effect = None
        fallback.return_value = []
        self.assertEqual(api.list(root, strict=True), [])


class TestSnapshot(TestCase):
    def _storage(self):
        storage = _load("__init__.py", "P115Disk", {"snapshot_storage"}, {
            "_PluginBase": object, "Path": Path, "Optional": Optional, "Dict": Dict,
            "FileItem": FileItem, "logger": Mock(),
        })
        storage._disk_name = "P115"
        storage._p115_api = Mock()
        root = FileItem(type="dir", path="/movies/")
        storage._p115_api.get_item.return_value = root
        storage._p115_api.get_item_strict.return_value = root
        storage._p115_api.list.return_value = [
            FileItem(path="/movies/old.mkv", modify_time=10),
            FileItem(path="/movies/new.mkv", modify_time=20),
            FileItem(path="/movies/unknown.mkv", modify_time=None),
        ]
        return storage

    def test_initial_snapshot_includes_files_without_cutoff(self):
        storage = self._storage()
        result = storage.snapshot_storage("P115", Path("/movies"))
        self.assertEqual(len(result), 3)

    def test_incremental_snapshot_keeps_unknown_modification_times(self):
        storage = self._storage()
        result = storage.snapshot_storage("P115", Path("/movies"), last_snapshot_time=15)
        self.assertEqual(set(result), {"/movies/new.mkv", "/movies/unknown.mkv"})

    def test_snapshot_query_errors_do_not_become_empty_success(self):
        storage = self._storage()
        storage._p115_api.list.side_effect = StorageQueryError("incomplete")
        self.assertIsNone(storage.snapshot_storage("P115", Path("/movies")))
        storage._p115_api.get_item_strict.side_effect = StorageQueryError("offline")
        self.assertIsNone(storage.snapshot_storage("P115", Path("/movies")))


class TestAtomicDownload(TestCase):
    def _run(self, *, chunks=None, cancelled=False, failure=False):
        api, ns = _api()
        response = ns["stream"].return_value.__enter__.return_value

        def interrupted_chunks(**kwargs):
            yield b"partial"
            raise OSError("read interrupted")

        if failure:
            response.iter_bytes.side_effect = interrupted_chunks
        else:
            response.iter_bytes.return_value = iter(chunks or [b"new", b"data"])
        ns["global_vars"].is_transfer_stopped.return_value = cancelled
        with TemporaryDirectory() as root:
            target = Path(root) / "movie.mkv"
            target.write_bytes(b"original")
            result = api.download(FileItem(), Path(root))
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ["movie.mkv"])
            return result is not None, target.read_bytes()

    def test_success_replaces_target_after_complete_download(self):
        self.assertEqual(self._run(), (True, b"newdata"))

    def test_interrupted_download_keeps_existing_file_and_removes_partial(self):
        self.assertEqual(self._run(failure=True), (False, b"original"))

    def test_cancelled_download_keeps_existing_file_and_removes_partial(self):
        self.assertEqual(self._run(cancelled=True), (False, b"original"))

    def test_download_rejects_path_components_in_filename(self):
        api, _ = _api()
        self.assertIsNone(api.download(FileItem(name="../escape.mkv"), Path("/unused")))
        api.client.download_url.assert_not_called()
