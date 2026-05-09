import unittest
from pathlib import Path

from utils.increment_path_pair import (
    build_path_pair_index,
    normalize_extensions,
    select_pan_path_for_local_path,
)


class IncrementPathPairTest(unittest.TestCase):
    def test_normalize_extensions_lowercase_dot_and_skip_empty(self):
        self.assertEqual(
            normalize_extensions(["mkv", ".MP4", " ass ", ""]),
            {".mkv", ".mp4", ".ass"},
        )

    def test_selects_correct_source_when_tree_lines_are_misaligned(self):
        pair_index = build_path_pair_index(
            [
                ("/strm/B/MovieB.strm", "/pan/B/MovieB.mkv"),
                ("/strm/A/MovieA.strm", "/pan/A/MovieA.mkv"),
            ]
        )

        decision = select_pan_path_for_local_path(
            local_path="/strm/A/MovieA.strm",
            pan_paths=pair_index["/strm/A/MovieA.strm"],
            media_extensions=[".mkv", ".mp4"],
            download_extensions=[".ass", ".srt"],
            auto_download_mediainfo=True,
        )

        self.assertTrue(decision.should_process)
        self.assertEqual(decision.pan_path, "/pan/A/MovieA.mkv")

    def test_rejects_strm_target_mapped_to_ass_source(self):
        decision = select_pan_path_for_local_path(
            local_path="/strm/Movie.strm",
            pan_paths=["/pan/Movie.ass"],
            media_extensions=[".mkv", ".mp4"],
            download_extensions=[".ass", ".srt"],
            auto_download_mediainfo=True,
        )

        self.assertFalse(decision.should_process)
        self.assertIn("本地 STRM 目标必须对应媒体源文件", decision.reason)

    def test_rejects_ass_target_mapped_to_mkv_source(self):
        decision = select_pan_path_for_local_path(
            local_path="/strm/Movie.ass",
            pan_paths=["/pan/Movie.mkv"],
            media_extensions=[".mkv", ".mp4"],
            download_extensions=[".ass", ".srt"],
            auto_download_mediainfo=True,
        )

        self.assertFalse(decision.should_process)
        self.assertIn("媒体信息目标必须对应相同后缀源文件", decision.reason)

    def test_duplicate_movie_strm_keeps_first_source_and_reports_count(self):
        pair_index = build_path_pair_index(
            [
                ("/strm/Movie.strm", "/pan/Movie.mkv"),
                ("/strm/Movie.strm", "/pan/Movie.mp4"),
            ]
        )

        decision = select_pan_path_for_local_path(
            local_path="/strm/Movie.strm",
            pan_paths=pair_index["/strm/Movie.strm"],
            media_extensions=[".mkv", ".mp4"],
            download_extensions=[".ass"],
            auto_download_mediainfo=True,
        )

        self.assertTrue(decision.should_process)
        self.assertEqual(decision.pan_path, "/pan/Movie.mkv")
        self.assertEqual(decision.duplicate_count, 2)

    def test_missing_mapping_is_rejected_without_pan_path(self):
        decision = select_pan_path_for_local_path(
            local_path="/strm/Missing.strm",
            pan_paths=[],
            media_extensions=[".mkv", ".mp4"],
            download_extensions=[".ass"],
            auto_download_mediainfo=True,
        )

        self.assertFalse(decision.should_process)
        self.assertIsNone(decision.pan_path)
        self.assertIn("无法根据本地目标路径找到网盘源路径", decision.reason)


class TestIncrementSourceGuard(unittest.TestCase):
    def test_increment_generation_no_longer_uses_line_number_pairing(self):
        increment_path = (
            Path(__file__).resolve().parents[1]
            / "helper"
            / "strm"
            / "increment.py"
        )
        source = increment_path.read_text(encoding="utf-8")

        self.assertNotIn("compare_trees_lines", source)
        self.assertNotIn("get_path_by_line_number(line)", source)


if __name__ == "__main__":
    unittest.main()
