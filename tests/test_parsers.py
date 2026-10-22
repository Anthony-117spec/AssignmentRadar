"""解析器与存储的离线单元测试：python -m unittest discover -s tests -v"""
from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assignmentradar.config import load_config, parse_cookie_header, set_cookie  # noqa: E402
from assignmentradar.moodle import (  # noqa: E402
    clean_course_name,
    parse_assign_detail,
    parse_course_assignments,
    parse_course_cards,
    parse_moodle_date,
    parse_sidebar_courses,
)
from assignmentradar.store import Store  # noqa: E402

COURSE_HTML = """
<html><body><div id="region-main"><ul class="sections">
<li class="activity assign modtype_assign" id="module-1001">
  <div class="activityinstance">
    <a class="aalink" href="https://moodle.scnu.edu.cn/mod/assign/view.php?id=1001">
      <span class="instancename">作业一：文献综述<span class="accesshide"> 作业</span></span>
    </a>
  </div>
</li>
<li class="activity quiz modtype_quiz" id="module-1002">
  <a href="https://moodle.scnu.edu.cn/mod/quiz/view.php?id=1002">
    <span class="instancename">第一章测验</span></a>
</li>
<li class="activity assign modtype_assign" id="module-1003">
  <a href="https://moodle.scnu.edu.cn/mod/assign/view.php?id=1003">
    <span class="instancename">作业二</span></a>
</li>
</ul></div></body></html>
"""

ASSIGN_ZH = """
<html><body><div id="region-main"><table class="generaltable">
<tr><th class="cell c0" scope="row">提交状态</th><td class="cell c1">未提交</td></tr>
<tr><th class="cell c0" scope="row">评分状态</th><td class="cell c1">未评分</td></tr>
<tr><th class="cell c0" scope="row">截止日期</th><td class="cell c1">2026年10月15日 星期四 23:59</td></tr>
<tr><th class="cell c0" scope="row">剩余时间</th><td class="cell c1">12 天 4 小时</td></tr>
</table></div></body></html>
"""

ASSIGN_EN = """
<html><body><div id="region-main"><table class="generaltable">
<tr><th scope="row">Submission status</th><td>Submitted for grading</td></tr>
<tr><th scope="row">Due date</th><td>Thursday, 15 October 2026, 11:59 PM</td></tr>
</table></div></body></html>
"""

# 平台（Moodle 5）实际结构：截止行从状态表里去掉，日期在 activity-dates 区块
ASSIGN_LIRU_PENDING = """
<html><body><div id="region-main">
<div class="activity-dates" class="activity-dates">
  <div class="date-item"><span class="date-item-label">已开启:</span> 2026年09月28日 星期一 00:00</div>
  <div class="date-item"><span class="date-item-label">截至:</span> 2026年10月18日 星期日 00:00</div>
</div>
<h3>作业状态</h3>
<div class="submissionsummarytable"><table class="generaltable">
<tr><th class="cell c0" scope="row">作业状态</th><td class="cell c1">未提交</td></tr>
<tr><th class="cell c0" scope="row">剩余时间</th><td class="cell c1">仅剩 14 天 8 小时</td></tr>
</table></div>
</div></body></html>
"""

ASSIGN_LIRU_NODATE = """
<html><body><div id="region-main">
<h3>作业状态</h3>
<div class="submissionsummarytable"><table class="generaltable">
<tr><th class="cell c0" scope="row">作业状态</th><td class="cell c1">已提交请评分</td></tr>
<tr><th class="cell c0" scope="row">剩余时间</th><td class="cell c1">作业早交 3 分钟 8 秒</td></tr>
</table></div>
</div></body></html>
"""

MY_HTML = """
<html><body>
<a href="https://moodle.scnu.edu.cn/course/view.php?id=101"><h6 class="coursename">2026秋-数据结构</h6></a>
<a href="/course/view.php?id=102">操作系统</a>
<a href="/course/view.php?id=1">站点首页</a>
</body></html>
"""


class TestParsers(unittest.TestCase):
    def test_course_assignments(self):
        briefs = parse_course_assignments(COURSE_HTML)
        self.assertEqual([b.cmid for b in briefs], [1001, 1003])
        self.assertEqual(briefs[0].name, "作业一：文献综述")  # accesshide 已剥离
        self.assertEqual(briefs[1].url, "https://moodle.scnu.edu.cn/mod/assign/view.php?id=1003")

    def test_assign_detail_zh(self):
        d = parse_assign_detail(ASSIGN_ZH)
        self.assertEqual(d.due_ts, datetime(2026, 10, 15, 23, 59))
        self.assertFalse(d.submitted)

    def test_assign_detail_en(self):
        d = parse_assign_detail(ASSIGN_EN)
        self.assertEqual(d.due_ts, datetime(2026, 10, 15, 23, 59))
        self.assertTrue(d.submitted)

    def test_assign_detail_empty(self):
        d = parse_assign_detail("<html><body><p>nothing</p></body></html>")
        self.assertIsNone(d.due_ts)
        self.assertIsNone(d.submitted)

    def test_assign_detail_liru(self):
        d = parse_assign_detail(ASSIGN_LIRU_PENDING)
        self.assertEqual(d.due_ts, datetime(2026, 10, 18, 0, 0))
        self.assertFalse(d.submitted)

    def test_assign_detail_liru_submitted_no_due(self):
        d = parse_assign_detail(ASSIGN_LIRU_NODATE)
        self.assertIsNone(d.due_ts)  # 已交且无截止行、剩余时间非"仅剩"→不推算
        self.assertTrue(d.submitted)

    def test_dates(self):
        self.assertEqual(parse_moodle_date("2026年10月15日 08:05"), datetime(2026, 10, 15, 8, 5))
        self.assertEqual(parse_moodle_date("2026年10月15日"), datetime(2026, 10, 15, 23, 59))
        self.assertEqual(parse_moodle_date("2026-10-15 08:05"), datetime(2026, 10, 15, 8, 5))
        self.assertEqual(parse_moodle_date("15 October 2026, 12:30 AM"), datetime(2026, 10, 15, 0, 30))
        self.assertIsNone(parse_moodle_date("没有日期"))
        self.assertIsNone(parse_moodle_date(""))

    def test_course_cards(self):
        courses = parse_course_cards(MY_HTML)
        ids = {c.course_id for c in courses}
        self.assertEqual(ids, {101, 102})

    def test_sidebar_courses(self):
        semester_html = """
        <div class="block_my_courses"><div class="title">我的课程</div><ul>
          <li><span>25-26下学期</span>
            <ul><li><a href="/course/view.php?id=900">Java SE 程序设计（25级）</a></li></ul>
          </li>
          <li><span>26-27上学期</span>
            <ul>
              <li><a href="/course/view.php?id=20101">Web 应用开发(26-27上学期)</a></li>
              <li><a href="/course/view.php?id=20102">数据库系统技术(26-27上学期)</a></li>
              <li><a href="/course/view.php?id=20103">分布式系统设计(26-27上学期)</a></li>
              <li><a href="/course/view.php?id=20104">数字视觉基础(26-27上学期)</a></li>
              <li><a href="/course/view.php?id=20105">编译原理与方法</a></li>
              <li><a href="/course/view.php?id=20106">计算机网络基础</a></li>
            </ul>
          </li>
        </ul></div>
        """
        got = parse_sidebar_courses(semester_html, "26-27上学期")
        self.assertEqual([(c.course_id, c.name) for c in got], [
            (20101, "Web 应用开发(26-27上学期)"),
            (20102, "数据库系统技术(26-27上学期)"),
            (20103, "分布式系统设计(26-27上学期)"),
            (20104, "数字视觉基础(26-27上学期)"),
            (20105, "编译原理与方法"),
            (20106, "计算机网络基础"),
        ])
        self.assertEqual(len(parse_sidebar_courses(semester_html)), 7)  # 不过滤 = 全部
        self.assertEqual(parse_sidebar_courses(semester_html, "27-28上学期"), [])  # 不存在的学期
        self.assertEqual(parse_sidebar_courses("<p>没有侧边栏</p>"), [])

    def test_clean_course_name(self):
        self.assertEqual(
            clean_course_name("毛泽东思想和中国特色社会主义理论体系概论(26-27上学期) 暂无简介 教师: 王老师 3464 人在学"),
            "毛泽东思想和中国特色社会主义理论体系概论(26-27上学期)",
        )
        self.assertEqual(clean_course_name("平台操作指南"), "平台操作指南")
        self.assertEqual(
            clean_course_name("大学生劳动教育理论和实践(1)(25-26上学期) 暂无简介 7934 人在学"),
            "大学生劳动教育理论和实践(1)(25-26上学期)",
        )
        # 没有标记、描述混在名字里：按空格截断
        self.assertEqual(clean_course_name("一流课程建设与交流 为促进我校一流课程建设与交流 学校征集"), "一流课程建设与交流")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_roundtrip(self):
        self.store.upsert_course(101, "数据结构")
        self.assertEqual(self.store.course_name(101), "数据结构")

        from assignmentradar.moodle import AssignmentDetail

        detail = AssignmentDetail(due_ts=datetime(2026, 10, 15, 23, 59), due_text="2026年10月15日", submitted=False)
        self.store.insert_assignment(1001, 101, "作业一", "http://x/1001", notified_new=True, detail=detail)
        row = self.store.get_assignment(1001)
        self.assertEqual(row["due_ts"], "2026-10-15T23:59:00")

        # 临近提醒：距截止 23h59m，落在 24h 窗口内
        now = datetime(2026, 10, 15, 0, 0)
        self.assertEqual(len(self.store.pending_reminders(24, now)), 1)
        self.store.record_reminder(1001, 24)
        self.assertEqual(len(self.store.pending_reminders(24, now)), 0)  # 已提醒
        # 已提交的不提醒
        detail.submitted = True
        self.store.update_detail(1001, detail)
        self.store.clear_reminders(1001)
        self.assertEqual(len(self.store.pending_reminders(24, now)), 0)

        # 24h 复查队列：刚插入过详情，不应进入
        self.assertEqual(len(self.store.assignments_needing_detail(timedelta(hours=24))), 0)

    def test_baseline_flow(self):
        self.store.upsert_course(9, "C")
        self.store.insert_assignment(1, 9, "A", "u", notified_new=True)
        self.store.set_meta("baseline_done", "1")
        self.assertEqual(self.store.get_meta("baseline_done"), "1")


class TestConfig(unittest.TestCase):
    def test_cookie_parsing(self):
        c = parse_cookie_header("MoodleSession=abc123; foo=bar; junk")
        self.assertEqual(c, {"MoodleSession": "abc123", "foo": "bar"})

    def test_set_cookie_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = load_config(base)  # 会生成模板，默认 120 分钟
            self.assertEqual(cfg.poll_interval_minutes, 120)
            set_cookie(base, "MoodleSession=abc; other=xyz")
            cfg2 = load_config(base)
            self.assertEqual(cfg2.cookie, "MoodleSession=abc; other=xyz")
            self.assertEqual(cfg2.poll_interval_minutes, 120)  # 其他配置不被破坏

    def test_new_field_lands_before_the_first_table(self):
        """新字段必须写在表头之前，否则会被 TOML 当成某张表的子键，静默读不到。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            load_config(base)  # 生成模板（末尾有 [notify] / [course_dirs]）
            set_cookie(base, "MoodleSession=abc", field="brand_new_field")
            raw = tomllib.loads((base / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(raw.get("brand_new_field"), "MoodleSession=abc")
            # 关键：不能落到任何一张表里面
            for table, body in raw.items():
                if isinstance(body, dict):
                    self.assertNotIn("brand_new_field", body, f"跑进了 [{table}]")

    def test_existing_field_is_replaced_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            load_config(base)
            set_cookie(base, "MoodleSession=first")
            set_cookie(base, "MoodleSession=second")
            raw = tomllib.loads((base / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(raw["cookie"], "MoodleSession=second")
            self.assertEqual(raw["cookie"].count("MoodleSession"), 1)

    def test_does_not_touch_a_same_named_key_inside_a_table(self):
        """顶层替换不能误伤表里的同名字段。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            load_config(base)
            text = (base / "config.toml").read_text(encoding="utf-8")
            text = text.replace("[notify]", '[notify]\ncollide = "table-value"')
            (base / "config.toml").write_text(text, encoding="utf-8")
            set_cookie(base, "MoodleSession=abc", field="collide")
            raw = tomllib.loads((base / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(raw["collide"], "MoodleSession=abc")
            self.assertEqual(raw["notify"]["collide"], "table-value")


from assignmentradar.views import (  # noqa: E402
    build_grade_rows,
    build_lists,
    fmt_duration,
    fmt_left,
    fmt_refresh_path,
    fmt_score,
    is_graded,
    short_course,
)


class TestGradeStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def _item(item_id=1, course_id=101, cmid=1001, raw=90.0, text="90.00",
              graded=datetime(2026, 10, 1, 10, 0)):
        from types import SimpleNamespace

        return SimpleNamespace(
            item_id=item_id, course_id=course_id, cmid=cmid, itemname="作业一",
            itemmodule="assign", grade_raw=raw, grade_max=100.0, grade_text=text,
            graded_at=graded, submitted_at=None,
        )

    def test_upsert_updates_instead_of_duplicating(self):
        """老师改分、补评分都要能跟上——同一个 item_id 只应有一条。"""
        self.store.upsert_grade(self._item())
        self.store.upsert_grade(self._item(raw=95.0, text="95.00"))
        rows = self.store.grades_with_course()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["grade_raw"], 95.0)
        self.assertEqual(rows[0]["grade_text"], "95.00")

    def test_graded_only_drops_ungraded(self):
        self.store.upsert_grade(self._item(item_id=1))
        self.store.upsert_grade(self._item(item_id=2, cmid=1002, raw=None, text="-", graded=None))
        self.assertEqual(len(self.store.grades_with_course(graded_only=True)), 1)
        self.assertEqual(len(self.store.grades_with_course()), 2)

    def test_course_name_is_joined(self):
        self.store.upsert_course(101, "数据结构")
        self.store.upsert_grade(self._item())
        self.assertEqual(self.store.grades_with_course()[0]["course_name"], "数据结构")


class TestGradeViews(unittest.TestCase):
    @staticmethod
    def _row(item_id, cmid, raw, graded_at, course="课A", text=None, mx=100.0):
        if text is None:
            text = f"{raw:.2f}" if raw is not None else "-"
        return {
            "item_id": item_id, "cmid": cmid, "course_id": 1, "course_name": course,
            "itemname": f"作业{item_id}", "grade_raw": raw, "grade_max": mx,
            "grade_text": text, "graded_at": graded_at,
        }

    def test_is_graded(self):
        self.assertTrue(is_graded({"graded_at": "2026-10-01T00:00:00"}))
        self.assertTrue(is_graded({"graded_at": None, "grade_raw": 90, "grade_text": "90.00"}))
        self.assertFalse(is_graded({"graded_at": None, "grade_raw": None, "grade_text": "-"}))

    def test_fmt_score(self):
        self.assertEqual(fmt_score(self._row(1, 10, 100.0, None, text="100.00")), "100.00/100")
        self.assertEqual(fmt_score({"grade_text": "-", "grade_raw": None, "grade_max": 100}), "—")

    def test_course_total_is_excluded(self):
        """课程总分不是"某一次作业的评分"，不该混进这个列表。"""
        self.assertEqual(build_grade_rows([self._row(1, None, 20.0, "2026-10-01T10:00:00")]), [])

    def test_ungraded_is_excluded(self):
        self.assertEqual(build_grade_rows([self._row(1, 10, None, None)]), [])

    def test_only_latest_three_per_course(self):
        rows = [self._row(i, 100 + i, 90.0, f"2026-10-0{i}T10:00:00") for i in range(1, 6)]
        got = build_grade_rows(rows, per_course=3)
        self.assertEqual(len(got), 1)
        self.assertEqual([r["item_id"] for r in got[0][1]], [5, 4, 3])

    def test_courses_sorted_by_most_recent_grade(self):
        rows = [self._row(1, 10, 90.0, "2026-10-01T10:00:00", course="旧课"),
                self._row(2, 20, 90.0, "2026-10-09T10:00:00", course="新课")]
        self.assertEqual([name for name, _ in build_grade_rows(rows)], ["新课", "旧课"])


class TestViews(unittest.TestCase):
    def _row(self, cmid, name, due=None, submitted=None):
        return {"cmid": cmid, "name": name, "course_name": "测试课(26-27上学期)",
                "due_ts": due, "submitted": submitted, "url": "u"}

    def test_build_lists_ordering(self):
        from datetime import datetime, timedelta

        now = datetime(2026, 10, 3, 12, 0)
        rows = [
            self._row(1, "远期未交", (now + timedelta(days=10)).isoformat(timespec="seconds"), None),
            self._row(2, "临近未交", (now + timedelta(hours=5)).isoformat(timespec="seconds"), None),
            self._row(3, "已截止未交", (now - timedelta(days=1)).isoformat(timespec="seconds"), None),
            self._row(4, "更早截止未交", (now - timedelta(days=5)).isoformat(timespec="seconds"), None),
            self._row(5, "未知截止", None, None),
            self._row(6, "已交A", (now - timedelta(days=2)).isoformat(timespec="seconds"), True),
            self._row(7, "已交B", (now - timedelta(days=1)).isoformat(timespec="seconds"), True),
            self._row(8, "确认未交", (now + timedelta(days=1)).isoformat(timespec="seconds"), False),
        ]
        overdue, upcoming, unknown, submitted = build_lists(rows, now)
        # 已截止组：最近截止的排最前（最可能还能补交）
        self.assertEqual([r["name"] for r, _ in overdue], ["已截止未交", "更早截止未交"])
        self.assertEqual([r["name"] for r, _ in upcoming], ["临近未交", "确认未交", "远期未交"])
        self.assertEqual([r["name"] for r in unknown], ["未知截止"])
        self.assertEqual([r["name"] for r, _ in submitted], ["已交B", "已交A"])  # 最近提交在前

    def test_fmt_left(self):
        from datetime import datetime, timedelta

        now = datetime(2026, 10, 3, 12, 0)
        self.assertEqual(fmt_left(now + timedelta(days=2, hours=3), now), "剩 2 天 3 小时")
        self.assertEqual(fmt_left(now - timedelta(hours=25), now), "已过 1 天 1 小时")
        self.assertEqual(fmt_left(now + timedelta(minutes=30), now), "剩 30 分钟")

    def test_short_course(self):
        self.assertEqual(short_course("数据结构(26-27上学期)"), "数据结构")
        self.assertEqual(short_course("编译原理与方法"), "编译原理与方法")

    def test_fmt_duration(self):
        # 秒级保留一位小数：30.4 秒 / 0.6 秒
        self.assertEqual(fmt_duration(30.4), "30.4 秒")
        self.assertEqual(fmt_duration("0.6"), "0.6 秒")
        self.assertEqual(fmt_duration(59.94), "59.9 秒")
        # 跨过一分钟改说"几分几秒"
        self.assertEqual(fmt_duration(60), "1 分 0 秒")
        self.assertEqual(fmt_duration(72.4), "1 分 12 秒")
        self.assertEqual(fmt_duration(125), "2 分 5 秒")
        # 未知/非法一律给空串——调用方据此决定"整段不显示"，而不是显示一个假的 0
        for bad in (None, "", "abc", -1, "  "):
            self.assertEqual(fmt_duration(bad), "", f"{bad!r} 应给空串")

    def test_fmt_refresh_path(self):
        self.assertEqual(fmt_refresh_path("token"), "令牌通路")
        self.assertEqual(fmt_refresh_path("html"), "页面通路")
        # 没记录过 / 脏值 → 空串（不显示通路那一截）
        for bad in (None, "", "   ", "???"):
            self.assertEqual(fmt_refresh_path(bad), "", f"{bad!r} 应给空串")


if __name__ == "__main__":
    unittest.main()
