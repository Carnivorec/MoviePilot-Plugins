"""关联文件清理的文件名边界和媒体保护测试"""

import ast
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock


class TestRelatedFileCleanup(TestCase):
    def test_cleanup_preserves_similar_names_and_real_media(self):
        source = Path(__file__).resolve().parents[1] / "utils/path.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PathRemoveUtils")
        cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in {"clean_related_files", "iter_related_files"}]
        namespace = {"Path": Path, "logger": Mock(), "settings": SimpleNamespace(RMT_MEDIAEXT=[".mkv", ".mp4"])}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
        with TemporaryDirectory() as root:
            directory = Path(root)
            removed = ["Film 1.nfo", "Film 1.zh.srt", "Film 1-poster.jpg"]
            preserved = ["Film 10.srt", "Other Film 1.srt", "Film 1.strm", "Film 1.mkv", "Film 1-trailer.mp4"]
            for name in removed + preserved:
                (directory / name).write_text("content", encoding="utf-8")
            namespace["PathRemoveUtils"].clean_related_files(directory / "Film 1.strm")
            self.assertEqual(sorted(path.name for path in directory.iterdir()), sorted(preserved))

    def test_life_candidate_loops_preserve_other_episodes_and_literal_brackets(self):
        source = Path(__file__).resolve().parents[1] / "utils/path.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PathRemoveUtils")
        cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "iter_related_files"]
        namespace = {"Path": Path, "settings": SimpleNamespace(RMT_MEDIAEXT=[".mkv", ".mp4"])}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
        life_source = source.parents[1] / "helper/life/client.py"
        life_tree = ast.parse(life_source.read_text(encoding="utf-8"))
        candidate_loops = [node for node in ast.walk(life_tree) if isinstance(node, ast.For)
                           and isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Attribute)
                           and node.iter.func.attr == "iter_related_files"]
        self.assertEqual(len(candidate_loops), 3)
        with TemporaryDirectory() as root:
            directory = Path(root)
            related = {"Show[1] E1.zh.srt", "Show[1] E1-poster.jpg"}
            preserved = {"Show[1] E10.srt", "Show[1] E1.mkv", "Show[1] E1.strm", "Show[1] E1-trailer.mp4"}
            for name in related | preserved:
                (directory / name).write_text("content", encoding="utf-8")
            for loop in candidate_loops:
                namespace.update(old_path=directory / "Show[1] E1.mkv", old_local_path=directory / "Show[1] E1.mkv")
                candidates = eval(compile(ast.Expression(loop.iter), str(life_source), "eval"), namespace)
                self.assertEqual({path.name for path in candidates}, related)
