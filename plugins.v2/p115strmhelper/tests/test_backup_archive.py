"""真实 tar 文件的多根恢复、旧包兼容、原子写入与边界验证"""

import ast
import re
import tarfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from types import SimpleNamespace
from typing import List, Optional, Tuple
from unittest import TestCase
from unittest.mock import Mock

from utils.backup_archive import backup_sources, backup_tar_writer, restore_backup_archive


def _archive(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if isinstance(data, bytes):
                info.size = len(data)
                archive.addfile(info, BytesIO(data))
            else:
                info.type, info.linkname = data
                archive.addfile(info)


def _load_helper(root):
    source = Path(__file__).resolve().parents[1] / "helper/backup/__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BackupStrmHelper")
    names = {"_collect_backup_entries", "_create_tar_gz", "_extract_tar_gz", "restore_from_local", "restore_from_cloud",
             "_safe_task_name", "_legacy_task_name", "_matches_backup", "_clean_old_backups"}
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    tracker = SimpleNamespace(begin_phase=lambda *args: None, mark_logged=lambda *args: None,
                              overall_ratio=lambda value: value, should_log_scan=lambda *args: False,
                              should_log_pack=lambda *args, **kwargs: False)
    ns = {"Path": Path, "List": List, "Tuple": Tuple, "Optional": Optional, "P115Client": object,
          "logger": Mock(), "backup_sources": backup_sources, "backup_tar_writer": backup_tar_writer,
          "re": re, "sha256": sha256,
          "restore_backup_archive": restore_backup_archive, "tarfile_open": tarfile.open,
          "NamedTemporaryFile": NamedTemporaryFile, "perf_counter": lambda: 1.0,
          "_BackupProgressTracker": lambda *args, **kwargs: tracker,
          "BackupPhaseWeight": SimpleNamespace(SCAN_START=0, SCAN_END=0.2, PACK_START=0.2, PACK_END_LOCAL=1),
          "StringUtils": SimpleNamespace(format_size=str),
          "configer": SimpleNamespace(PLUGIN_TEMP_PATH=root, get_user_agent=lambda: "test"),
          "HttpxClient": Mock()}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), ns)
    helper = ns[cls.name]
    helper._log_backup_progress = Mock()
    helper._format_duration = staticmethod(lambda _: "time")
    return helper, ns


class TestBackupArchive(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "backup.tar.gz"

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_basenames_restore_to_original_paths_even_after_reordering(self):
        roots = [self.root / "a/Movies", self.root / "b/Movies"]
        for index, root in enumerate(roots):
            root.mkdir(parents=True)
            (root / "单引号'剧集.strm").write_text(f"source-{index}", encoding="utf-8")
        helper, _ = _load_helper(self.root)
        ok, error = helper._create_tar_gz([str(root) for root in roots], self.archive)
        self.assertTrue(ok, error)
        for root in roots:
            (root / "单引号'剧集.strm").write_text("changed", encoding="utf-8")
        ok, error = helper.restore_from_local(str(self.archive), [str(root) for root in reversed(roots)])
        self.assertTrue(ok, error)
        for index, root in enumerate(roots):
            self.assertEqual((root / "单引号'剧集.strm").read_text(encoding="utf-8"), f"source-{index}")

    def test_new_archive_can_relocate_each_root_in_configured_order(self):
        roots = [self.root / "old/A", self.root / "old/B"]
        for root in roots:
            root.mkdir(parents=True)
        with backup_tar_writer(self.archive, roots) as archive:
            for index in range(2):
                info = tarfile.TarInfo(f"source-{index}/file.strm")
                info.size = 1
                archive.addfile(info, BytesIO(bytes([65 + index])))
        targets = [self.root / "new-a/Library", self.root / "new-b/Library"]
        restore_backup_archive(self.archive, targets)
        self.assertEqual((targets[0] / "file.strm").read_bytes(), b"A")
        self.assertEqual((targets[1] / "file.strm").read_bytes(), b"B")

    def test_legacy_multi_parent_restore_does_not_use_only_first_parent(self):
        _archive(self.archive, [("A/a.strm", b"a"), ("B/b.strm", b"b")])
        targets = [self.root / "north/A", self.root / "south/B"]
        helper, _ = _load_helper(self.root)
        ok, error = helper.restore_from_local(str(self.archive), [str(path) for path in targets])
        self.assertTrue(ok, error)
        self.assertEqual((targets[1] / "b.strm").read_bytes(), b"b")
        self.assertFalse((self.root / "north/B").exists())

    def test_ambiguous_legacy_roots_are_rejected(self):
        _archive(self.archive, [("Movies/a.strm", b"a")])
        with self.assertRaisesRegex(ValueError, "同名源目录"):
            restore_backup_archive(self.archive, [self.root / "a/Movies", self.root / "b/Movies"])

    def test_missing_source_does_not_replace_previous_backup(self):
        self.archive.write_bytes(b"previous-good-backup")
        helper, _ = _load_helper(self.root)
        ok, _ = helper._create_tar_gz([str(self.root / "missing")], self.archive)
        self.assertFalse(ok)
        self.assertEqual(self.archive.read_bytes(), b"previous-good-backup")

    def test_partial_archive_failure_keeps_previous_backup(self):
        source = self.root / "source"
        source.mkdir()
        self.archive.write_bytes(b"previous-good-backup")
        with self.assertRaises(OSError), backup_tar_writer(self.archive, [source]):
            raise OSError("write interrupted")
        self.assertEqual(self.archive.read_bytes(), b"previous-good-backup")
        self.assertEqual(list(self.root.glob(".p115-backup-*.part")), [])

    def test_empty_valid_source_is_distinct_from_missing_source(self):
        source = self.root / "source"
        source.mkdir()
        with backup_tar_writer(self.archive, [source]):
            pass
        target = self.root / "restored"
        restore_backup_archive(self.archive, [target])
        self.assertTrue(target.is_dir())

    def test_self_backup_and_overlapping_roots_are_rejected(self):
        source = self.root / "source"
        child = source / "child"
        child.mkdir(parents=True)
        with self.assertRaises(ValueError):
            backup_sources([source], source / "backup.tar.gz")
        with self.assertRaises(ValueError):
            backup_sources([source, child])

    def test_task_names_cannot_collide_after_filename_sanitizing(self):
        helper, _ = _load_helper(self.root)
        first, second = helper._safe_task_name("A/B"), helper._safe_task_name("A_B")
        self.assertNotEqual(first, second)
        filename = first + "_20260914_000000.tar.gz"
        self.assertTrue(helper._matches_backup(filename, "A/B"))
        self.assertFalse(helper._matches_backup(filename, "A_B"))
        self.assertFalse(helper._matches_backup("A_B_20260914_000000.tar.gz", "A", include_legacy=True))
        self.assertLess(len(helper._safe_task_name("汉" * 500).encode("utf-8")), 200)

    def test_retention_preserves_other_tasks_directories_and_legacy_archives(self):
        helper, _ = _load_helper(self.root)
        prefix = helper._safe_task_name("A")
        old = f"{prefix}_20260914_000000.tar.gz"
        newest = f"{prefix}_20260914_010000.tar.gz"
        foreign = f"{helper._safe_task_name('A_B')}_20260914_020000.tar.gz"
        legacy = "A_20260914_030000.tar.gz"
        directory = f"{prefix}_20260914_040000.tar.gz"
        for name in (old, newest, foreign, legacy):
            (self.root / name).write_bytes(b"keep")
        (self.root / directory).mkdir()
        (self.root / directory / "keep").write_bytes(b"keep")
        self.assertEqual(helper._clean_old_backups(self.root, "A", 1), 1)
        self.assertFalse((self.root / old).exists())
        for name in (newest, foreign, legacy, directory):
            self.assertTrue((self.root / name).exists())

    def test_bad_member_is_rejected_before_any_existing_file_changes(self):
        target = self.root / "A"
        target.mkdir()
        (target / "keep.strm").write_bytes(b"original")
        for bad in ("../escape", "/absolute", "unknown/file", "A/../escape"):
            with self.subTest(bad=bad):
                _archive(self.archive, [("A/keep.strm", b"replacement"), (bad, b"bad")])
                with self.assertRaises(ValueError):
                    restore_backup_archive(self.archive, [target])
                self.assertEqual((target / "keep.strm").read_bytes(), b"original")

    def test_existing_symlink_cannot_redirect_restore_outside_target(self):
        target, outside = self.root / "A", self.root / "outside"
        target.mkdir()
        outside.mkdir()
        try:
            (target / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("当前平台不能创建符号链接")
        _archive(self.archive, [("A/link/escape.strm", b"bad")])
        with self.assertRaises(ValueError):
            restore_backup_archive(self.archive, [target])
        self.assertFalse((outside / "escape.strm").exists())

    def test_internal_hardlink_restores_content_and_cyclic_link_is_rejected(self):
        target = self.root / "A"
        _archive(self.archive, [("A/file.strm", b"data"), ("A/link.strm", (tarfile.LNKTYPE, "A/file.strm"))])
        restore_backup_archive(self.archive, [target])
        self.assertEqual((target / "link.strm").read_bytes(), b"data")
        _archive(self.archive, [("A/x", (tarfile.SYMTYPE, "y")), ("A/y", (tarfile.SYMTYPE, "x"))])
        with self.assertRaisesRegex(ValueError, "循环链接"):
            restore_backup_archive(self.archive, [target])

    def test_parent_file_conflict_is_detected_before_writing(self):
        _archive(self.archive, [("A/parent", b"file"), ("A/parent/child", b"child")])
        with self.assertRaises(ValueError):
            restore_backup_archive(self.archive, [self.root / "A"])
        self.assertFalse((self.root / "A").exists())

    def test_failed_cloud_restore_removes_partial_download(self):
        helper, ns = _load_helper(self.root)
        instance = helper()
        instance._storage_name = "P115"
        instance._storage_chain = Mock()
        instance._storage_chain.list_files.return_value = [SimpleNamespace(name="backup.tar.gz", type="file", pickcode="pick")]
        response = Mock()

        def chunks(**kwargs):
            yield b"partial"
            raise OSError("download interrupted")

        response.iter_bytes.side_effect = chunks
        from unittest.mock import MagicMock
        client = MagicMock()
        client.__enter__.return_value.stream.return_value.__enter__.return_value = response
        ns["HttpxClient"].return_value = client
        ok, _ = instance.restore_from_cloud("/backup.tar.gz", [str(self.root / "target")], Mock())
        self.assertFalse(ok)
        self.assertEqual(list((self.root / "restore").iterdir()), [])
