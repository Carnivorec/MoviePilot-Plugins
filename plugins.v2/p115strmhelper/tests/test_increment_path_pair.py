"""增量路径关联的行为回归测试，不访问 MoviePilot 或网盘"""

import ast
from pathlib import Path
from typing import Optional
from unittest import TestCase
from unittest.mock import Mock

from utils.increment_path_pair import PathPairIndex


class MemoryTree:
    """提供与目录树相同的集合比较接口"""

    def __init__(self, paths=()):
        self.paths = list(paths)

    def clear(self):
        self.paths.clear()

    def generate_tree_from_list(self, paths, append=False):
        if not append:
            self.clear()
        self.paths.extend(paths)

    def compare_trees(self, other):
        yield from (path for path in self.paths if path not in other.paths)


def load_helper():
    """加载实际新增和建树方法，替换网盘与文件写入依赖"""
    source = Path(__file__).resolve().parents[1] / "helper/strm/increment.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                and node.name in ("_generate_additions", "__generate_pan_tree")]
    namespace = {"Path": Path, "Optional": Optional, "logger": Mock(),
                 "sentry_manager": Mock(), "ItertreeInternalError": RuntimeError,
                 "sleep": lambda _: None}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
    helper = namespace[cls.name]()
    helper.path_pairs = PathPairIndex()
    helper.pan_to_local_tree = MemoryTree()
    helper.pan_to_local_strm_tree = MemoryTree()
    helper.local_tree = MemoryTree()
    helper.strm_fail_count = 0
    helper.strm_fail_dict = {}
    helper.total_iterated = 0
    helper.rmt_mediaext = ["mkv", ".MP4"]
    helper.download_mediaext = [".ass", ".srt"]
    helper.auto_download_mediainfo = True
    helper._IncrementSyncStrmHelper__handle_addition_path = Mock()
    return helper


class TestPathPairIndex(TestCase):
    """验证唯一来源和类型契约"""

    def setUp(self):
        self.index = PathPairIndex()

    def resolve(self, target):
        return self.index.resolve(target, ["mkv", ".MP4"], [".ass"], True)

    def test_source_order_does_not_change_episode(self):
        self.index.add("/local/E11.strm", "/pan/E11.mkv")
        self.index.add("/local/E10.strm", "/pan/E10.mkv")
        self.assertEqual(self.resolve("/local/E11.strm"), "/pan/E11.mkv")

    def test_missing_mapping_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "找不到"):
            self.resolve("/local/E11.strm")

    def test_two_different_sources_are_rejected_in_both_orders(self):
        for sources in [("a.mkv", "a.mp4"), ("a.mp4", "a.mkv")]:
            self.index.clear()
            for source in sources:
                self.index.add("a.strm", source)
            with self.assertRaisesRegex(ValueError, "多个网盘源"):
                self.resolve("a.strm")

    def test_repeated_identical_export_is_not_ambiguous(self):
        self.index.add("a.strm", "a.MP4")
        self.index.add("a.strm", "a.MP4")
        self.assertEqual(self.resolve("a.strm"), "a.MP4")

    def test_video_and_subtitle_cannot_be_cross_written(self):
        for target, source in [("a.strm", "a.ass"), ("a.ass", "a.mkv"), ("a.ass", "a.srt")]:
            with self.subTest(target=target, source=source):
                self.index.clear()
                self.index.add(target, source)
                with self.assertRaisesRegex(ValueError, "类型不兼容"):
                    self.resolve(target)

    def test_subtitle_requires_download_enabled(self):
        self.index.add("a.ass", "a.ass")
        self.assertEqual(self.resolve("a.ass"), "a.ass")
        with self.assertRaises(ValueError):
            self.index.resolve("a.ass", ["mkv"], ["ass"], False)

    def test_unicode_apostrophe_and_newline_are_preserved(self):
        target = "/本地/it's\n剧集/第11集.strm"
        source = "/网盘/it's\n剧集/第11集.mkv"
        self.index.add(target, source)
        self.assertEqual(self.resolve(target), source)


class TestIncrementIntegration(TestCase):
    """执行生产方法，验证错位不会传递到文件写入入口"""

    def test_reordered_difference_uses_exact_source_and_skips_existing(self):
        helper = load_helper()
        pairs = [("/local/E10.strm", "/pan/E10.mkv"), ("/local/E11.strm", "/pan/E11.mkv")]
        for target, source in pairs:
            helper.path_pairs.add(target, source)
        helper.pan_to_local_tree.paths = [pairs[1][0], pairs[0][0]]
        helper.local_tree.paths = [pairs[0][0]]
        helper._generate_additions()
        helper._IncrementSyncStrmHelper__handle_addition_path.assert_called_once_with(
            pan_path="/pan/E11.mkv", local_path="/local/E11.strm")
        self.assertEqual(helper.total_iterated, 1)

    def test_invalid_mapping_does_not_write_and_next_valid_file_continues(self):
        helper = load_helper()
        for target, source in [("bad.strm", "bad.ass"), ("conflict.strm", "a.mkv"),
                               ("conflict.strm", "b.mkv"), ("ok.strm", "ok.mkv")]:
            helper.path_pairs.add(target, source)
        helper.pan_to_local_tree.paths = ["missing.strm", "bad.strm", "conflict.strm", "ok.strm"]
        helper._generate_additions()
        helper._IncrementSyncStrmHelper__handle_addition_path.assert_called_once_with(
            pan_path="ok.mkv", local_path="ok.strm")

    def test_retry_discards_partial_mapping_and_previous_sync_root(self):
        helper = load_helper()
        helper.path_pairs.add("previous.strm", "previous.mkv")
        def failed_export():
            yield "partial.strm", "partial.mkv"
            raise OSError("Broken pipe")
        calls = iter([failed_export(), iter([("ok.strm", "ok.mkv")])])
        helper._IncrementSyncStrmHelper__itertree = lambda **kwargs: next(calls)
        helper._IncrementSyncStrmHelper__generate_pan_tree("/pan", "/local")
        self.assertEqual(helper.pan_to_local_tree.paths, ["ok.strm"])
        for stale in ("previous.strm", "partial.strm"):
            with self.assertRaises(ValueError):
                helper.path_pairs.resolve(stale, ["mkv"], [], False)
        helper._generate_additions()
        helper._IncrementSyncStrmHelper__handle_addition_path.assert_called_once_with(
            pan_path="ok.mkv", local_path="ok.strm")


class TestTaskTreeIsolation(TestCase):
    """旧任务析构不能删除新任务已经写入的目录树条目"""

    def test_old_destructor_during_new_tree_write_preserves_all_episodes(self):
        from itertools import cycle
        from types import SimpleNamespace
        from typing import Dict, List
        from uuid import uuid4

        source = Path(__file__).resolve().parents[1] / "helper/strm/increment.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                    and node.name in ("__init__", "__del__", "__generate_pan_tree", "_generate_additions")]
        storage = {}

        class SharedTree:
            def __init__(self, path):
                self.key = str(path)
                self.after_add = None

            def clear(self):
                storage[self.key] = []

            def generate_tree_from_list(self, paths, append=False):
                storage.setdefault(self.key, []).extend(paths)
                if self.after_add:
                    callback, self.after_add = self.after_add, None
                    callback()

            def compare_trees(self, other):
                yield from (p for p in storage.get(self.key, [])
                            if p not in storage.get(other.key, []))

        config = Mock()
        config.PLUGIN_TEMP_PATH = Path("/tmp/test-tree-isolation")
        config.get_config.side_effect = lambda key: {
            "PLUGIN_TEMP_PATH": config.PLUGIN_TEMP_PATH,
            "user_rmt_mediaext": "mkv", "user_download_mediaext": "ass",
        }.get(key, False)
        namespace = {"Path": Path, "Optional": Optional, "List": List, "Dict": Dict,
                     "P115Client": object, "MediaInfoDownloader": object,
                     "logger": Mock(), "sentry_manager": Mock(), "sleep": lambda _: None,
                     "ItertreeInternalError": RuntimeError, "cycle": cycle, "uuid4": uuid4,
                     "PathPairIndex": PathPairIndex, "configer": config, "DirectoryTree": SharedTree}
        for name in ("FileDbHelper", "DirectoryCache", "AutomatonUtils", "StrmUrlGetter", "MediaServerRefresh"):
            namespace[name] = Mock()
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
        helper_cls = namespace[cls.name]
        old, new = helper_cls(None, None), helper_cls(None, None)
        self.assertNotEqual(old.local_tree_path, new.local_tree_path)
        self.assertNotEqual(old.pan_to_local_tree_path, new.pan_to_local_tree_path)
        pairs = [(f"/local/E{i:02}.strm", f"/pan/E{i:02}.mkv") for i in (9, 10, 11)]
        new._IncrementSyncStrmHelper__itertree = lambda **kwargs: iter(pairs)
        new.pan_to_local_tree.after_add = old.__del__
        written = []
        new._IncrementSyncStrmHelper__handle_addition_path = lambda **kw: written.append(kw)
        new._IncrementSyncStrmHelper__generate_pan_tree("/pan", "/local")
        new._generate_additions()
        self.assertEqual(written, [{"local_path": local, "pan_path": pan} for local, pan in pairs])
