"""令牌通路（`wsapi`）与它接进轮询的方式。**全部离线**——不碰网络、不起浏览器。

这一套守的是 2026-10-10 加的那条通路：`MoodleSession` 大约 10~12 小时就死一次，
死了清单就停在旧数据上。令牌走平台自己的 REST 接口，不碰 cookie，于是会话死活
与数据新鲜度脱钩。回退是保底——HTML 路径一条都没删，令牌不可用时必须能自动退回去。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assignmentradar import attachments as att  # noqa: E402
from assignmentradar import moodle, wsapi  # noqa: E402
from assignmentradar.config import Config, load_config, set_ws_token  # noqa: E402
from assignmentradar.poller import App, PollSummary  # noqa: E402
from assignmentradar.store import Store  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, *, status=200, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """记录每次 POST 的参数，返回预设响应。"""

    def __init__(self, responder):
        self.headers: dict = {}
        self._responder = responder
        self.calls: list[dict] = []

    def post(self, url, data=None, timeout=None):
        payload = dict(data or {})
        self.calls.append({"url": url, "data": payload})
        return self._responder(url, payload)


def make_api(responder, token="TOK") -> wsapi.WsApi:
    cfg = Config(base_dir=Path("."), ws_token=token, request_delay_seconds=0)
    api = wsapi.WsApi(cfg)
    api.sess = FakeSession(responder)
    return api


def ok(payload):
    return lambda url, data: FakeResponse(payload)


def err(code):
    return lambda url, data: FakeResponse({"exception": "x", "errorcode": code, "message": "m"})


class TestCall(unittest.TestCase):
    def test_missing_token_is_unavailable(self):
        api = make_api(ok({}), token="")
        with self.assertRaises(wsapi.WsUnavailable):
            api.call("core_webservice_get_site_info")

    def test_invalidtoken_is_unavailable(self):
        """令牌被吊销 —— 整条通路不可用，调用方该回退。"""
        api = make_api(err("invalidtoken"))
        with self.assertRaises(wsapi.WsUnavailable):
            api.call("core_webservice_get_site_info")

    def test_accessexception_is_function_denied(self):
        """函数不在此服务里 —— 是**局部**问题，不等于整条通路不可用。"""
        api = make_api(err("accessexception"))
        with self.assertRaises(wsapi.WsFunctionDenied):
            api.call("mod_assign_get_assignments")
        self.assertTrue(issubclass(wsapi.WsFunctionDenied, wsapi.WsError))

    def test_other_errorcode_is_plain_error(self):
        api = make_api(err("invalidparameter"))
        with self.assertRaises(wsapi.WsError) as ctx:
            api.call("mod_assign_get_assignments")
        self.assertNotIsInstance(ctx.exception, wsapi.WsFunctionDenied)
        self.assertNotIsInstance(ctx.exception, wsapi.WsUnavailable)

    def test_network_error_is_unavailable(self):
        import requests

        def boom(url, data):
            raise requests.ConnectionError("down")

        api = make_api(boom)
        with self.assertRaises(wsapi.WsUnavailable):
            api.call("core_webservice_get_site_info")

    def test_http_403_is_unavailable(self):
        api = make_api(lambda u, d: FakeResponse(None, status=403, text="nope"))
        with self.assertRaises(wsapi.WsUnavailable):
            api.call("core_webservice_get_site_info")

    def test_non_json_is_error(self):
        api = make_api(lambda u, d: FakeResponse(None, text="<html>登录</html>"))
        with self.assertRaises(wsapi.WsError):
            api.call("core_webservice_get_site_info")

    def test_list_params_are_indexed(self):
        """Moodle REST 的数组参数是 `courseids[0]=…`，不是重复键。"""
        api = make_api(ok({"courses": []}))
        api.call("mod_assign_get_assignments", courseids=[7, 8])
        sent = api.sess.calls[0]["data"]
        self.assertEqual(sent["courseids[0]"], 7)
        self.assertEqual(sent["courseids[1]"], 8)
        self.assertEqual(sent["wsfunction"], "mod_assign_get_assignments")
        self.assertEqual(sent["moodlewsrestformat"], "json")

    def test_none_params_are_dropped(self):
        api = make_api(ok({}))
        api.call("core_enrol_get_users_courses", userid=None)
        self.assertNotIn("userid", api.sess.calls[0]["data"])


class TestCourses(unittest.TestCase):
    def _api(self, courses):
        def responder(url, data):
            fn = data["wsfunction"]
            if fn == "core_webservice_get_site_info":
                return FakeResponse({"userid": 100234, "username": "u"})
            return FakeResponse(courses)

        return make_api(responder)

    def test_semester_in_name_keeps_course(self):
        api = self._api([{"id": 11, "fullname": "Web 应用开发(26-27上学期)"},
                         {"id": 12, "fullname": "概率论与数理统计(25-26上学期)"}])
        got = api.courses("26-27上学期")
        self.assertEqual([c.course_id for c in got], [11])

    def test_running_course_without_semester_suffix_is_kept(self):
        """课名里没有学期后缀、但正在开课的，必须留下（HTML 路径靠侧边栏分组解决）。"""
        now = datetime.now().timestamp()
        api = self._api([{"id": 5, "fullname": "编译原理与方法",
                          "startdate": now - 86400, "enddate": now + 86400}])
        got = api.courses("26-27上学期")
        self.assertEqual([c.course_id for c in got], [5])

    def test_finished_course_without_suffix_is_dropped(self):
        api = self._api([{"id": 6, "fullname": "游戏引擎设计与开发（中英班）",
                          "startdate": 1000, "enddate": 2000}])
        self.assertEqual(api.courses("26-27上学期"), [])

    def test_site_frontpage_and_dupes_are_skipped(self):
        api = self._api([{"id": 1, "fullname": "首页(26-27上学期)"},
                         {"id": 9, "fullname": "课(26-27上学期)"},
                         {"id": 9, "fullname": "课(26-27上学期)"}])
        self.assertEqual([c.course_id for c in api.courses("26-27上学期")], [9])

    def test_no_semester_filter_keeps_everything(self):
        api = self._api([{"id": 6, "fullname": "旧课", "startdate": 1, "enddate": 2}])
        self.assertEqual(len(api.courses(None)), 1)

    def test_userid_is_cached(self):
        api = self._api([])
        api.courses(None)
        self.assertEqual(api.user_id, 100234)

    def test_missing_userid_raises(self):
        api = make_api(ok({"sitename": "x"}))
        with self.assertRaises(wsapi.WsError):
            api.courses(None)


class TestAssignments(unittest.TestCase):
    PAYLOAD = {
        "courses": [
            {
                "id": 19422,
                "fullname": "分布式系统设计(26-27上学期)",
                "assignments": [
                    {
                        "id": 86622,
                        "cmid": 853500,
                        "name": "实验1",
                        "duedate": 1789920000,
                        "introattachments": [
                            {"filename": "实验1.docx",
                             "fileurl": "https://moodle.scnu.edu.cn/webservice/pluginfile.php/1/a.docx"},
                        ],
                    },
                    {"id": 86623, "cmid": 853501, "name": "实验2", "duedate": 0},
                ],
            }
        ]
    }

    def test_field_mapping(self):
        api = make_api(ok(self.PAYLOAD))
        got = api.assignments([19422])
        a = got[19422][0]
        self.assertEqual(a.cmid, 853500)
        self.assertEqual(a.assign_id, 86622)
        self.assertEqual(a.course_id, 19422)
        self.assertEqual(a.name, "实验1")
        self.assertEqual(a.due_ts, datetime.fromtimestamp(1789920000))
        self.assertIn("mod/assign/view.php?id=853500", a.url)

    def test_zero_duedate_means_no_deadline(self):
        api = make_api(ok(self.PAYLOAD))
        self.assertIsNone(api.assignments([19422])[19422][1].due_ts)

    def test_attachments_carry_the_token(self):
        """`webservice/pluginfile.php` 靠 token 鉴权——不带 token 就是 403。"""
        api = make_api(ok(self.PAYLOAD))
        atts = api.assignments([19422])[19422][0].attachments
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0].name, "实验1.docx")
        self.assertIn("token=TOK", atts[0].url)

    def test_brief_and_detail_shapes(self):
        api = make_api(ok(self.PAYLOAD))
        a = api.assignments([19422])[19422][0]
        brief = a.brief()
        self.assertEqual((brief.cmid, brief.name), (853500, "实验1"))
        detail = a.detail(True)
        self.assertTrue(detail.submitted)
        self.assertEqual(detail.due_ts, a.due_ts)
        self.assertIn("2026年", detail.due_text or "")
        self.assertEqual(len(detail.attachments), 1)

    def test_empty_course_list_makes_no_request(self):
        api = make_api(ok({}))
        self.assertEqual(api.assignments([]), {})
        self.assertEqual(api.sess.calls, [])

    def test_broken_entries_are_skipped(self):
        api = make_api(ok({"courses": [{"id": 1, "assignments": [{"name": "无 id"}]}]}))
        self.assertEqual(api.assignments([1]), {1: []})


class TestSubmissionStatus(unittest.TestCase):
    def _api(self, payload):
        def responder(url, data):
            if isinstance(payload, str):
                return FakeResponse({"exception": "x", "errorcode": payload, "message": "m"})
            return FakeResponse(payload)

        return make_api(responder)

    def test_submitted_true(self):
        api = self._api({"lastattempt": {"submission": {"status": "submitted"}}})
        self.assertIs(api.submission_status(1), True)

    def test_new_is_false(self):
        api = self._api({"lastattempt": {"submission": {"status": "new"}}})
        self.assertIs(api.submission_status(1), False)

    def test_draft_counts_as_not_submitted(self):
        """和 HTML 路径一个取舍：草稿不算交。"""
        api = self._api({"lastattempt": {"submission": {"status": "draft"}}})
        self.assertIs(api.submission_status(1), False)

    def test_unknown_status_is_none(self):
        api = self._api({"lastattempt": {}})
        self.assertIsNone(api.submission_status(1))

    def test_function_denied_degrades_to_none(self):
        """该函数被服务裁剪时降级为"未知"，不该拖垮整条通路。"""
        api = self._api("accessexception")
        self.assertIsNone(api.submission_status(1))


class TestGrades(unittest.TestCase):
    PAYLOAD = {
        "usergrades": [{
            "courseid": 12223,
            "gradeitems": [
                {"id": 1, "itemname": "第三周作业", "itemmodule": "assign", "cmid": 480691,
                 "graderaw": 100.0, "grademax": 100, "gradeformatted": "100.00",
                 "gradedategraded": 1791790043, "gradedatesubmitted": 1789376294},
                {"id": 2, "itemname": "第四周作业", "itemmodule": "assign", "cmid": 484302,
                 "graderaw": None, "grademax": 100, "gradeformatted": "-",
                 "gradedategraded": None, "gradedatesubmitted": None},
                # 课程总分：cmid / itemname / itemmodule 全空
                {"id": 3, "itemname": None, "itemmodule": None, "cmid": None,
                 "graderaw": 20.0, "grademax": 100, "gradeformatted": "20.00",
                 "gradedategraded": None, "gradedatesubmitted": None},
                # 分数是字符串（Moodle 有时这么给）
                {"id": 4, "itemname": "第一周作业", "itemmodule": "assign", "cmid": 465488,
                 "graderaw": "90.81818", "grademax": 100, "gradeformatted": "90.82",
                 "gradedategraded": 1791000000, "gradedatesubmitted": None},
            ],
        }]
    }

    def _api(self, payload=None):
        def responder(url, data):
            fn = data["wsfunction"]
            if fn == "core_webservice_get_site_info":
                return FakeResponse({"userid": 100234})
            if fn == "core_enrol_get_users_courses":
                return FakeResponse([])
            return FakeResponse(payload if payload is not None else self.PAYLOAD)

        return make_api(responder)

    def test_field_mapping(self):
        got = self._api().grades(12223)
        self.assertEqual(len(got), 4)
        g = got[0]
        self.assertEqual((g.item_id, g.cmid, g.itemname), (1, 480691, "第三周作业"))
        self.assertEqual(g.grade_raw, 100.0)
        self.assertEqual(g.grade_max, 100.0)
        self.assertEqual(g.grade_text, "100.00")
        self.assertIsNotNone(g.graded_at)
        self.assertTrue(g.graded)
        self.assertFalse(g.is_course_total)

    def test_ungraded_item(self):
        g = self._api().grades(12223)[1]
        self.assertFalse(g.graded)
        self.assertEqual(g.grade_text, "-")
        self.assertIsNone(g.graded_at)

    def test_course_total_is_flagged(self):
        """课程总分和作业共用同一个结构，靠 cmid 分辨。"""
        g = self._api().grades(12223)[2]
        self.assertTrue(g.is_course_total)
        self.assertIsNone(g.cmid)
        self.assertIsNone(g.itemname)

    def test_string_grade_is_parsed(self):
        g = self._api().grades(12223)[3]
        self.assertAlmostEqual(g.grade_raw, 90.81818)

    def test_userid_is_fetched_when_missing(self):
        """不传 userid 会被判成"查看其他人的成绩"而拒绝，所以必须自动补上。"""
        api = self._api()
        api.grades(12223)  # 没先调 courses()
        sent = [c["data"] for c in api.sess.calls
                if c["data"]["wsfunction"] == "gradereport_user_get_grade_items"][0]
        self.assertEqual(sent["userid"], 100234)
        self.assertEqual(sent["courseid"], 12223)

    def test_empty_payload(self):
        self.assertEqual(self._api({"usergrades": []}).grades(1), [])


class TestWithToken(unittest.TestCase):
    def test_appends_with_question_mark(self):
        self.assertEqual(wsapi.with_token("https://x/a", "T"), "https://x/a?token=T")

    def test_appends_with_ampersand_when_query_present(self):
        self.assertEqual(wsapi.with_token("https://x/a?b=1", "T"), "https://x/a?b=1&token=T")

    def test_empty_inputs_are_passed_through(self):
        self.assertEqual(wsapi.with_token("", "T"), "")
        self.assertEqual(wsapi.with_token("https://x/a", ""), "https://x/a")


class TestSetWsToken(unittest.TestCase):
    def test_roundtrip_at_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            load_config(base)  # 生成模板（末尾有 [notify] / [course_dirs]）
            set_ws_token(base, "abc123")
            self.assertEqual(load_config(base).ws_token, "abc123")

    def test_empty_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                set_ws_token(Path(tmp), "   ")

    def test_whitespace_inside_is_rejected(self):
        """粘贴时多带了别的东西——当场拒绝，别写进去等运行时才报 invalidtoken。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                set_ws_token(Path(tmp), "abc def")


# ------------------------------------------------------------------ 接线


class FakeWs:
    """令牌通路替身。"""

    def __init__(self, *, courses=None, by_course=None, submitted=None,
                 raise_on_courses=None, raise_on_status=None):
        self._courses = courses if courses is not None else [
            moodle.Course(101, "分布式系统设计(26-27上学期)"),
        ]
        self._by_course = by_course if by_course is not None else {
            101: [wsapi.WsAssignment(cmid=1, assign_id=11, course_id=101,
                                     name="实验5", due_ts=datetime(2026, 10, 12),
                                     url="https://m/x")]
        }
        self._submitted = submitted
        self._raise_on_courses = raise_on_courses
        self._raise_on_status = raise_on_status
        self.status_calls: list[int] = []

    def courses(self, semester=None):
        if self._raise_on_courses:
            raise self._raise_on_courses
        return list(self._courses)

    def assignments(self, course_ids):
        return {k: list(v) for k, v in self._by_course.items()}

    def submission_status(self, assign_id):
        self.status_calls.append(assign_id)
        if self._raise_on_status:
            raise self._raise_on_status
        return self._submitted


class RecordingHttp:
    """HTML 通路的替身：只记录有没有被用到。"""

    base_url = "https://moodle.test"

    def __init__(self):
        self.used = 0

    def get(self, url, **kw):
        self.used += 1
        raise RuntimeError("HTML 通路被调用了")

    def post(self, url, **kw):
        self.used += 1
        raise RuntimeError("HTML 通路被调用了")

    def keepalive_sso(self):
        return None


class PollerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.cfg = load_config(base)
        self.cfg.browser_refresh_hours = 0  # 别真去起浏览器
        self.cfg.ws_token = "TOK"
        self.cfg.assignment_dirs = None
        self.http = RecordingHttp()

    def tearDown(self):
        self._tmp.cleanup()

    def app(self, ws):
        app = App(self.cfg, notify_enabled=False)
        app.ws = ws
        app.http = self.http
        return app


class TestPollerUsesTokenPath(PollerCase):
    def test_token_path_is_used_and_html_never_touched(self):
        app = self.app(FakeWs())
        s = app.poll_once()
        self.assertTrue(s.ok)
        self.assertEqual(s.courses, 1)
        self.assertEqual(s.assignments, 1)
        self.assertEqual(self.http.used, 0, "配了令牌就不该碰 HTML 通路")
        self.assertEqual(app.store.get_meta("last_poll_path"), "token")
        app.shutdown()

    def test_new_assignment_lands_in_the_store(self):
        app = self.app(FakeWs(submitted=False))
        app.poll_once()
        row = app.store.get_assignment(1)
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "实验5")
        self.assertEqual(row["due_ts"], "2026-10-12T00:00:00")
        self.assertEqual(row["submitted"], 0)
        app.shutdown()

    def test_submission_status_failure_does_not_break_the_round(self):
        app = self.app(FakeWs(raise_on_status=wsapi.WsError("boom")))
        s = app.poll_once()
        self.assertTrue(s.ok, "提交状态拿不到，不该让整轮失败")
        self.assertEqual(app.store.get_assignment(1)["submitted"], None)
        app.shutdown()


class TestPollerFallsBack(PollerCase):
    def test_unavailable_token_falls_back_to_html(self):
        app = self.app(FakeWs(raise_on_courses=wsapi.WsUnavailable("令牌被吊销")))
        app.poll_once()
        self.assertGreater(self.http.used, 0, "令牌不可用必须回退 HTML")
        self.assertIn("令牌被吊销", app.store.get_meta("ws_last_error") or "")
        # 清单标题行靠这个 meta 解释"这次为什么慢"，回退时必须如实记成 html
        self.assertEqual(app.store.get_meta("last_poll_path"), "html")
        app.shutdown()

    def test_plain_ws_error_also_falls_back(self):
        app = self.app(FakeWs(raise_on_courses=wsapi.WsError("服务端异常")))
        app.poll_once()
        self.assertGreater(self.http.used, 0)
        app.shutdown()

    def test_no_token_means_html_from_the_start(self):
        self.cfg.ws_token = ""
        app = App(self.cfg, notify_enabled=False)
        app.http = self.http
        self.assertIsNone(app.ws)
        app.poll_once()
        self.assertGreater(self.http.used, 0)
        app.shutdown()


class TestPollDuration(PollerCase):
    """端到端耗时：既进 PollSummary（于是 poll_log 里天然留了历史），
    也单独进 meta（读缓存的清单标题行显示它）。"""

    def test_duration_is_measured_and_reported(self):
        app = self.app(FakeWs())
        s = app.poll_once()
        self.assertTrue(s.ok)
        self.assertGreater(s.duration, 0.0, "端到端耗时必须量到")
        self.assertIn("用时", s.describe(), "耗时要说人话，直接进 describe")
        self.assertAlmostEqual(
            float(app.store.get_meta("last_poll_duration")), s.duration, places=1
        )
        app.shutdown()

    def test_failed_round_still_reports_duration(self):
        """失败的轮询尤其要看耗时——61 秒那种多半是卡在浏览器续期的调试端口超时。"""
        s = PollSummary(ok=False, duration=61.4)
        s.errors.append("会话过期，自动续期失败")
        text = s.describe()
        self.assertIn("轮询未完成", text)
        self.assertIn("用时 61.4 秒", text)

    def test_no_duration_means_no_noise(self):
        """量不到耗时（duration=0）时不该硬塞一个"用时 0.0 秒"进输出。"""
        self.assertNotIn("用时", PollSummary(ok=True, courses=3).describe())


class TestCollectViaWs(unittest.TestCase):
    def _ws(self, by_course):
        return FakeWs(courses=[moodle.Course(101, "课A"), moodle.Course(202, "课B")],
                      by_course=by_course)

    def test_on_assignment_fires_even_without_attachments(self):
        """截图清单靠它攒——没有附件的作业也必须回调。"""
        ws = self._ws({101: [wsapi.WsAssignment(1, 11, 101, "有附件", None, "u1",
                                                [moodle.Attachment("a.docx", "https://x?token=T")]),
                             wsapi.WsAssignment(2, 12, 101, "没附件", None, "u2")]})
        pages: list = []
        items = att.collect(None, ws=ws, on_assignment=lambda c, a, u: pages.append((c, a, u)))
        self.assertEqual(len(pages), 2)
        self.assertEqual([p[1] for p in pages], ["有附件", "没附件"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "a.docx")

    def test_only_cmids_filter(self):
        ws = self._ws({101: [wsapi.WsAssignment(1, 11, 101, "A", None, "u1", []),
                             wsapi.WsAssignment(2, 12, 101, "B", None, "u2", [])]})
        pages: list = []
        att.collect(None, ws=ws, only_cmids={2},
                    on_assignment=lambda c, a, u: pages.append(a))
        self.assertEqual(pages, ["B"])

    def test_course_filter(self):
        ws = self._ws({101: [wsapi.WsAssignment(1, 11, 101, "A", None, "u1", [])],
                       202: [wsapi.WsAssignment(2, 12, 202, "B", None, "u2", [])]})
        pages: list = []
        att.collect(None, ws=ws, course_filter="课B",
                    on_assignment=lambda c, a, u: pages.append(a))
        self.assertEqual(pages, ["B"])

    def test_unavailable_token_falls_back_to_html(self):
        """令牌不可用时必须真的走到 HTML 路径（替身故意炸，用来证明它被调用了）。"""
        class BadWs:
            def courses(self, semester=None):
                raise wsapi.WsUnavailable("no")

        class FakeHttp:
            base_url = "https://moodle.test"

            def __init__(self):
                self.used = 0

            def get(self, url, **kw):
                self.used += 1
                raise RuntimeError("html path")

        http = FakeHttp()
        with self.assertRaises(RuntimeError):
            att.collect(http, ws=BadWs())
        self.assertGreater(http.used, 0)


class TestStoreUpdateDue(unittest.TestCase):
    def test_only_due_changes(self):
        """`update_due` 绝不能碰 submitted / last_checked —— 否则会把作业踢出复查队列。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "s.db")
            store.insert_assignment(
                1, 9, "A", "u", notified_new=True,
                detail=moodle.AssignmentDetail(due_ts=datetime(2026, 10, 1), submitted=True),
            )
            store._conn.execute("UPDATE assignments SET last_checked='2020-01-01T00:00:00' WHERE cmid=1")
            store._conn.commit()

            store.update_due(1, datetime(2026, 10, 5), "新截止")
            row = store.get_assignment(1)
            self.assertEqual(row["due_ts"], "2026-10-05T00:00:00")
            self.assertEqual(row["due_text"], "新截止")
            self.assertEqual(row["submitted"], 1, "提交状态被覆盖了")
            self.assertEqual(row["last_checked"], "2020-01-01T00:00:00",
                             "last_checked 被推进了——那条作业会被踢出复查队列")
            store.close()


if __name__ == "__main__":
    unittest.main()
