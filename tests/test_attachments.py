"""作业附件归档的离线单测：python -m unittest tests.test_attachments -v"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assignmentradar import attachments as att  # noqa: E402
from assignmentradar import moodle  # noqa: E402


class FakeResponse:
    def __init__(self, blob: bytes = b"", text: str = ""):
        self.content = blob
        self.text = text or blob.decode("utf-8", "replace")


class FakeHTTP:
    """按 URL 分发：`blobs` 返回二进制（附件），`pages` 返回 HTML。"""

    base_url = "https://moodle.test"

    def __init__(self, blobs=None, pages=None):
        self.blobs = blobs or {}
        self.pages = pages or {}
        self.hits: list[str] = []

    def get(self, url, **kw):
        self.hits.append(url)
        if url in self.blobs:
            return FakeResponse(blob=self.blobs[url])
        if url in self.pages:
            return FakeResponse(text=self.pages[url])
        raise AssertionError(f"意外请求：{url}")


class TestSafeName(unittest.TestCase):
    def test_illegal_chars_replaced(self):
        self.assertEqual(att.safe_name('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_trailing_dot_and_space_stripped(self):
        self.assertEqual(att.safe_name("实验5.doc. "), "实验5.doc")
        self.assertEqual(att.safe_name("  名字   "), "名字")

    def test_path_traversal_neutralised(self):
        got = att.safe_name("../../etc/passwd")
        # 分隔符必须被抹掉，否则才可能穿越
        self.assertNotIn("/", got)
        self.assertNotIn("\\", got)
        self.assertNotEqual(got, "..")
        # 真正的安全属性：拼出来的路径不能跑出归档根目录
        root = Path("E:/hw")
        dest = att.dest_for(root, "课", "作业", "../../../../etc/passwd")
        self.assertTrue(dest.resolve().is_relative_to(root.resolve()),
                        f"附件路径逃出了归档根目录：{dest}")

    def test_empty_and_dots_fall_back(self):
        for bad in ("", "   ", ".", "..", "..."):
            self.assertEqual(att.safe_name(bad, fallback="无名"), "无名")

    def test_long_name_keeps_extension(self):
        got = att.safe_name("x" * 200 + ".pdf", maxlen=40)
        self.assertLessEqual(len(got), 40)
        self.assertTrue(got.endswith(".pdf"))


class TestDestFor(unittest.TestCase):
    def test_layout_is_course_then_assignment(self):
        got = att.dest_for(Path("E:/hw"), "分布式系统设计", "实验5", "实验5.doc")
        self.assertEqual(got, Path("E:/hw") / "分布式系统设计" / "实验5" / "实验5.doc")

    def test_course_name_is_sanitised(self):
        got = att.dest_for(Path("E:/hw"), "A/B", "C:D", "e?f.txt")
        self.assertEqual(got, Path("E:/hw") / "A_B" / "C_D" / "e_f.txt")

    def test_same_filename_in_two_assignments_does_not_collide(self):
        a = att.dest_for(Path("E:/hw"), "课", "作业一", "资料.pdf")
        b = att.dest_for(Path("E:/hw"), "课", "作业二", "资料.pdf")
        self.assertNotEqual(a, b)


class TestCourseMapping(unittest.TestCase):
    """平台课名 → 本地科目文件夹（学生已有的文件夹名字往往跟课名对不上）。"""

    MAP = {
        "Web 应用开发": "web",
        "数据库系统技术": "db",
        "编译原理与方法": "编译",
    }

    def test_normalize_strips_semester_suffix(self):
        self.assertEqual(att.normalize_course("Web 应用开发(26-27上学期)"), "Web 应用开发")
        self.assertEqual(att.normalize_course("Web 应用开发（26-27上学期）"), "Web 应用开发")
        self.assertEqual(att.normalize_course("计算机网络基础"), "计算机网络基础")
        self.assertEqual(att.normalize_course("  分布式系统  "), "分布式系统")

    def test_mapping_matches_without_semester_suffix(self):
        """键按裸课名写，换学期也不用改。"""
        folder, mapped = att.resolve_subject("Web 应用开发(26-27上学期)", self.MAP)
        self.assertEqual(folder, "web")
        self.assertTrue(mapped)

    def test_mapping_matches_exact_name_too(self):
        folder, mapped = att.resolve_subject("编译原理与方法", self.MAP)
        self.assertEqual(folder, "编译")
        self.assertTrue(mapped)

    def test_unmapped_course_falls_back_to_bare_name(self):
        folder, mapped = att.resolve_subject("数字视觉基础(26-27上学期)", self.MAP)
        self.assertEqual(folder, "数字视觉基础")
        self.assertFalse(mapped, "没配映射要如实汇报，好让 CLI 提醒用户")

    def test_no_mapping_at_all_is_safe(self):
        folder, mapped = att.resolve_subject("某课程(26-27上学期)", None)
        self.assertEqual(folder, "某课程")
        self.assertFalse(mapped)

    def test_mapped_folder_is_sanitised(self):
        folder, _ = att.resolve_subject("课(26-27上学期)", {"课": "a/b"})
        self.assertEqual(folder, "a_b")

    def test_blank_mapping_value_is_ignored(self):
        folder, mapped = att.resolve_subject("课(26-27上学期)", {"课": "   "})
        self.assertEqual(folder, "课")
        self.assertFalse(mapped)

    def test_dest_for_uses_the_mapping(self):
        got = att.dest_for(Path("D:/我的作业"), "Web 应用开发(26-27上学期)",
                           "实验四 ArkUI开发", "图.jpg", self.MAP)
        self.assertEqual(got, Path("D:/我的作业") / "web" / "实验四 ArkUI开发" / "图.jpg")


class TestParseAttachments(unittest.TestCase):
    PAGE = """
    <html><body>
      <div id="intro">
        <a href="https://moodle.test/pluginfile.php/1/mod_assign/introattachment/0/handout.pdf">
          handout.pdf</a>
        <a href="https://moodle.test/pluginfile.php/2/mod_resource/content/1/extra.docx">extra.docx</a>
      </div>
      <a href="https://moodle.test/pluginfile.php/3/assignsubmission_file/submission_files/9/mine.pdf">
        mine.pdf</a>
      <a href="https://moodle.test/pluginfile.php/1/mod_assign/introattachment/0/handout.pdf">
        handout.pdf</a>
    </body></html>
    """

    def test_keeps_teacher_files_and_drops_own_submission(self):
        got = moodle.parse_attachments(self.PAGE)
        names = [a.name for a in got]
        self.assertEqual(names, ["handout.pdf", "extra.docx"])
        self.assertNotIn("mine.pdf", names)

    def test_deduplicates_same_url(self):
        got = moodle.parse_attachments(self.PAGE)
        urls = [a.url for a in got]
        self.assertEqual(len(urls), len(set(urls)))

    def test_no_attachments_returns_empty(self):
        self.assertEqual(moodle.parse_attachments("<html><body>空</body></html>"), [])

    def test_name_falls_back_to_url_basename(self):
        html = ('<a href="https://moodle.test/pluginfile.php/1/mod_assign/'
                'introattachment/0/%E8%B5%84%E6%96%99.pdf"></a>')
        got = moodle.parse_attachments(html)
        self.assertEqual(got[0].name, "资料.pdf")


class TestDownloadOne(unittest.TestCase):
    def test_saves_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a" / "b.pdf"
            http = FakeHTTP(blobs={"u": b"%PDF-1.4 data"})
            self.assertEqual(att.download_one(http, "u", dest), "saved")
            self.assertEqual(dest.read_bytes(), b"%PDF-1.4 data")
            self.assertEqual(list(dest.parent.glob("*.part")), [], "原子写入不该留下临时文件")

    def test_skips_existing_without_any_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "b.pdf"
            dest.write_bytes(b"old")
            http = FakeHTTP(blobs={"u": b"new"})
            self.assertEqual(att.download_one(http, "u", dest), "skipped")
            self.assertEqual(http.hits, [], "已存在的文件不该产生任何请求")
            self.assertEqual(dest.read_bytes(), b"old")

    def test_force_redownloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "b.pdf"
            dest.write_bytes(b"old")
            http = FakeHTTP(blobs={"u": b"new"})
            self.assertEqual(att.download_one(http, "u", dest, force=True), "saved")
            self.assertEqual(dest.read_bytes(), b"new")

    def test_login_page_is_rejected(self):
        """会话过期时 pluginfile 会返回登录页 HTML——绝不能当附件存下来。"""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "b.pdf"
            http = FakeHTTP(blobs={"u": b"<!DOCTYPE html><html><body>login</body></html>"})
            self.assertEqual(att.download_one(http, "u", dest), "failed")
            self.assertFalse(dest.exists())

    def test_empty_body_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "b.pdf"
            http = FakeHTTP(blobs={"u": b""})
            self.assertEqual(att.download_one(http, "u", dest), "failed")
            self.assertFalse(dest.exists())

    def test_network_error_is_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "b.pdf"
            http = FakeHTTP()  # 任何请求都会 AssertionError
            self.assertEqual(att.download_one(http, "u", dest), "failed")


class TestDownload(unittest.TestCase):
    def test_archives_per_course_and_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = FakeHTTP(blobs={
                "https://moodle.test/f/1": b"%PDF-1.4 db",
                "https://moodle.test/f/2": b"PK\x03\x04 docx",
            })
            items = [
                att.Item("数据库系统技术", "第1章实验", "第1章.pdf", "https://moodle.test/f/1"),
                att.Item("数据库系统技术", "第2章实验", "第2章.pdf", "https://moodle.test/f/2"),
            ]
            s = att.download(http, items, root)
            self.assertEqual((s.saved, s.skipped, s.failed), (2, 0, 0))
            self.assertTrue((root / "数据库系统技术" / "第1章实验" / "第1章.pdf").is_file())
            self.assertTrue((root / "数据库系统技术" / "第2章实验" / "第2章.pdf").is_file())

    def test_mapping_routes_into_existing_subject_folder(self):
        """学生已有 web/ 这个科目文件夹时，附件要直接进去，而不是另起一个。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "web").mkdir()  # 已有的科目文件夹
            http = FakeHTTP(blobs={"https://moodle.test/f/1": b"jpgdata"})
            items = [att.Item("Web 应用开发(26-27上学期)", "实验四 ArkUI开发",
                              "图.jpg", "https://moodle.test/f/1")]
            s = att.download(http, items, root, course_dirs={"Web 应用开发": "web"})
            self.assertEqual(s.saved, 1)
            self.assertTrue((root / "web" / "实验四 ArkUI开发" / "图.jpg").is_file())
            # 没有多出一个以课名命名的新文件夹
            self.assertEqual([p.name for p in root.iterdir()], ["web"])

    def test_second_run_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = FakeHTTP(blobs={"https://moodle.test/f/1": b"data"})
            items = [att.Item("课", "作业", "a.bin", "https://moodle.test/f/1")]
            att.download(http, items, root)
            http.hits.clear()
            s2 = att.download(http, items, root)
            self.assertEqual((s2.saved, s2.skipped), (0, 1))
            self.assertEqual(http.hits, [], "重复归档不该再发请求")

    def test_summary_describe(self):
        s = att.DownloadSummary(saved=2, skipped=1)
        self.assertIn("新下载 2", s.describe())
        s.failed = 1
        self.assertIn("失败 1", s.describe())


class TestCollect(unittest.TestCase):
    PAGE = """
    <html><body><div id="intro">
      <a href="https://moodle.test/pluginfile.php/1/mod_assign/introattachment/0/h.pdf">h.pdf</a>
    </div></body></html>
    """

    def setUp(self):
        self._orig_disc = moodle.discover_courses
        self._orig_fetch = moodle.fetch_course_assignments
        moodle.discover_courses = lambda http, semester=None: [
            moodle.Course(7, "Web 应用开发"),
            moodle.Course(8, "编译原理与方法"),
        ]

        def fake_fetch(http, course_id):
            if course_id == 7:
                return [moodle.AssignmentBrief(
                    1001, "实验四", "https://moodle.test/mod/assign/view.php?id=1001")]
            return []

        moodle.fetch_course_assignments = fake_fetch

    def tearDown(self):
        moodle.discover_courses = self._orig_disc
        moodle.fetch_course_assignments = self._orig_fetch

    def test_collect_attaches_course_and_assignment(self):
        http = FakeHTTP(pages={"https://moodle.test/mod/assign/view.php?id=1001": self.PAGE})
        items = att.collect(http, semester=None)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].course, "Web 应用开发")
        self.assertEqual(items[0].assignment, "实验四")
        self.assertEqual(items[0].name, "h.pdf")

    def test_course_filter(self):
        http = FakeHTTP(pages={"https://moodle.test/mod/assign/view.php?id=1001": self.PAGE})
        self.assertEqual(att.collect(http, semester=None, course_filter="计算机网络"), [])
        self.assertEqual(len(att.collect(http, semester=None, course_filter="应用")), 1)

    def test_only_cmids_filter(self):
        http = FakeHTTP(pages={"https://moodle.test/mod/assign/view.php?id=1001": self.PAGE})
        self.assertEqual(att.collect(http, semester=None, only_cmids={9999}), [])
        self.assertEqual(len(att.collect(http, semester=None, only_cmids={1001})), 1)


class TestLogRedaction(unittest.TestCase):
    """令牌通路的附件 URL 带 `token=...`，日志里必须抹掉。"""

    def test_token_query_is_redacted(self):
        url = ("https://moodle.test/webservice/pluginfile.php/1/mod_assign/intro/a.pdf"
               "?token=SECRET123&forcedownload=1")
        redacted = att._redact_url(url)
        self.assertNotIn("SECRET123", redacted)
        self.assertIn("token=<redacted>", redacted)
        self.assertIn("forcedownload=1", redacted)


if __name__ == "__main__":
    unittest.main()
