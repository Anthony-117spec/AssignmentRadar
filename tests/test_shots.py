"""作业页整页截图的离线单测：python -m unittest tests.test_shots -v"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assignmentradar import shots  # noqa: E402
from assignmentradar.cdp import CdpError, CookieSnapshot  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


class FakeBrowser:
    """替身：记录导航与截图，可模拟失败/掉登录。"""

    def __init__(self, *, logged_in=True, fail_urls=(), drop_after=None, png=PNG):
        self._logged_in = logged_in
        self.fail_urls = set(fail_urls)
        self.drop_after = drop_after  # 截够 N 张后掉登录
        self.png = png
        self.navigated: list[str] = []
        self.shots = 0

    def navigate(self, url, *, settle=0.0):
        self.navigated.append(url)
        if url in self.fail_urls:
            raise CdpError("模拟导航失败")

    def screenshot(self, *, full_page=True):
        self.shots += 1
        # drop_after=N：存下 N 张之后会话失效（下一份作业的登录预检就会失败）
        if self.drop_after is not None and self.shots >= self.drop_after:
            self._logged_in = False
        return self.png

    def snapshot(self, *, navigate=True):
        return CookieSnapshot(cookie="MoodleSession=x", logged_in=self._logged_in,
                              url="https://moodle.test/x")


class TestDestFor(unittest.TestCase):
    def test_screenshot_lives_next_to_attachments(self):
        got = shots.dest_for(Path("D:/我的作业"), "Web 应用开发(26-27上学期)", "实验四 ArkUI开发")
        self.assertEqual(got, Path("D:/我的作业") / "Web 应用开发" / "实验四 ArkUI开发" / "_作业页截图.png")

    def test_honours_course_mapping(self):
        got = shots.dest_for(Path("D:/我的作业"), "Web 应用开发(26-27上学期)",
                             "实验四", {"Web 应用开发": "web"})
        self.assertEqual(got.parent.parent.name, "web")

    def test_name_starts_with_underscore_so_it_sorts_first(self):
        self.assertTrue(shots.SHOT_NAME.startswith("_"))


class TestLoggedIn(unittest.TestCase):
    def test_true_when_browser_logged_in(self):
        self.assertTrue(shots.logged_in(FakeBrowser(logged_in=True)))

    def test_false_when_logged_out(self):
        self.assertFalse(shots.logged_in(FakeBrowser(logged_in=False)))

    def test_false_when_cdp_blows_up(self):
        class Broken:
            def snapshot(self, *, navigate=True):
                raise CdpError("连不上")

        self.assertFalse(shots.logged_in(Broken()))


class TestCapture(unittest.TestCase):
    TASKS = [
        shots.ShotTask("Web 应用开发(26-27上学期)", "实验三 Ability开发", "u1"),
        shots.ShotTask("Web 应用开发(26-27上学期)", "实验四 ArkUI开发", "u2"),
    ]
    MAP = {"Web 应用开发": "web"}

    def test_saves_one_screenshot_per_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            b = FakeBrowser()
            s = shots.capture(b, self.TASKS, root, course_dirs=self.MAP)
            self.assertEqual((s.saved, s.skipped, s.failed), (2, 0, 0))
            for task in self.TASKS:
                self.assertTrue(shots.dest_for(root, task.course, task.assignment, self.MAP).is_file())
            self.assertEqual(b.navigated, ["u1", "u2"])

    def test_second_run_skips_without_navigating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shots.capture(FakeBrowser(), self.TASKS, root, course_dirs=self.MAP)
            b2 = FakeBrowser()
            s2 = shots.capture(b2, self.TASKS, root, course_dirs=self.MAP)
            self.assertEqual((s2.saved, s2.skipped), (0, 2))
            self.assertEqual(b2.navigated, [], "已存在就该跳过，连导航都省了")

    def test_force_recaptures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shots.capture(FakeBrowser(), self.TASKS, root, course_dirs=self.MAP)
            s2 = shots.capture(FakeBrowser(), self.TASKS, root, course_dirs=self.MAP, force=True)
            self.assertEqual(s2.saved, 2)

    def test_aborts_when_session_drops_midway(self):
        """会话中途失效时不能继续存——后面的截图全是登录页，留着反而误导。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = shots.capture(FakeBrowser(drop_after=1), self.TASKS, root, course_dirs=self.MAP)
            self.assertEqual(s.saved, 1)
            self.assertTrue(s.aborted, "应当标记中断原因")
            self.assertFalse(
                shots.dest_for(root, self.TASKS[1].course, self.TASKS[1].assignment, self.MAP).exists(),
                "掉登录后不该再存第二张",
            )

    def test_navigation_failure_is_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = shots.capture(FakeBrowser(fail_urls={"u1"}), self.TASKS, root,
                              course_dirs=self.MAP)
            self.assertEqual((s.saved, s.failed), (1, 1))

    def test_empty_png_counts_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = shots.capture(FakeBrowser(png=b""), self.TASKS, Path(tmp), course_dirs=self.MAP)
            self.assertEqual(s.failed, 2)
            self.assertEqual(s.saved, 0)

    def test_describe_reports_abort(self):
        s = shots.ShotSummary(saved=1, skipped=2, aborted="会话已失效")
        self.assertIn("中断", s.describe())


if __name__ == "__main__":
    unittest.main()
