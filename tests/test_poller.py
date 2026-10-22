"""端到端轮询测试：用假 HTTP 走完 poll_once 全流程（离线，不访问真实站点）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assignmentradar.config import load_config  # noqa: E402
from assignmentradar import relogin  # noqa: E402
from assignmentradar.poller import App  # noqa: E402

_ORIG_REFRESH = None


def setUpModule():
    """整个模块默认**不碰真实浏览器**：把"浏览器静默续期"替成永远失败。

    否则凡是走到"会话过期"分支的用例都会真的去起一次 Chrome（约 30 秒），
    既慢又算不上离线测试。想验证续期行为的用例自己再 patch 一次即可
    （见 `TestBrowserRefresh`）。
    """
    global _ORIG_REFRESH
    _ORIG_REFRESH = relogin.refresh_via_browser
    relogin.refresh_via_browser = lambda cfg, **kw: relogin.RefreshResult(False, "测试环境不启用")


def tearDownModule():
    if _ORIG_REFRESH is not None:
        relogin.refresh_via_browser = _ORIG_REFRESH

MY_HTML = ('<script>var M = {"sesskey":"AbCdEf123456"};</script>'
           '<a href="https://moodle.test/login/logout.php?sesskey=AbCdEf123456">退出</a>')


def course_html(assign_ids):
    lis = "".join(
        f'<li class="activity assign modtype_assign" id="module-{i}">'
        f'<a href="https://moodle.test/mod/assign/view.php?id={i}">'
        f'<span class="instancename">作业{i}</span></a></li>'
        for i in assign_ids
    )
    return f"<html><body>{lis}</body></html>"


def detail_html(due_text=None, submitted=None):
    rows = ""
    if submitted is not None:
        status = "已提交供评分" if submitted else "未提交"
        rows += f'<tr><th>提交状态</th><td>{status}</td></tr>'
    if due_text:
        rows += f'<tr><th>截止日期</th><td>{due_text}</td></tr>'
    return f"<html><body><table>{rows}</table></body></html>"


@dataclass
class FakeResponse:
    text: str = ""
    url: str = "https://moodle.test/ok"
    status_code: int = 200
    content: bytes = b""

    def json(self):
        import json

        return json.loads(self.text)

    def raise_for_status(self):
        pass


class FakeHTTP:
    """按 URL 前缀分发页面；测试可随时替换页面内容。

    `blobs` 用于附件等二进制响应（前缀匹配，优先于 `pages`）。
    """

    base_url = "https://moodle.test"

    def __init__(self):
        self.pages: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}
        self.ajax: list | None = None
        self.hits: list[str] = []

    def get(self, url, **kw):
        self.hits.append(url)
        path = url[len(self.base_url):] if url.startswith(self.base_url) else url
        for prefix, blob in self.blobs.items():
            if path.startswith(prefix):
                return FakeResponse(text="", url=url, content=blob)
        for prefix, html in self.pages.items():
            if path.startswith(prefix):
                return FakeResponse(text=html, url=url, content=html.encode("utf-8"))
        raise AssertionError(f"意外请求：{url}")

    def post(self, url, **kw):
        assert "lib/ajax/service.php" in url
        return FakeResponse(text=self.ajax_text, url=url)

    def reestablish(self):
        return True

    @property
    def ajax_text(self):
        import json

        return json.dumps(self.ajax or [])


class FakeNotifier:
    def __init__(self):
        self.events: list[tuple] = []

    def new_assignment(self, course, name, due, url):
        self.events.append(("new", course, name))

    def due_changed(self, course, name, old, new, url):
        self.events.append(("due", course, name, old, new))

    def reminder(self, course, name, due, hours, url):
        self.events.append(("reminder", course, name, hours))

    def session_expired(self, base_url):
        self.events.append(("expired",))


class TestPoller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = load_config(base)
        self.cfg.poll_interval_minutes = 120
        self.cfg.browser_refresh_hours = 0  # 这些测试不碰浏览器
        self.http = FakeHTTP()
        self.http.ajax = [
            {"error": False, "data": {"courses": [
                {"id": 101, "fullname": "数据结构"},
                {"id": 102, "fullname": "操作系统"},
            ]}}
        ]
        self.http.pages = {
            "/my/": MY_HTML,
            "/course/view.php?id=101": course_html([1001]),
            "/course/view.php?id=102": course_html([1003]),
            "/mod/assign/view.php?id=1001": detail_html("2026年10月15日 23:59", False),
            "/mod/assign/view.php?id=1003": detail_html(),
        }
        self.app = App(self.cfg)
        self.app.http = self.http
        self.notifier = FakeNotifier()
        self.app.notifier = self.notifier

    def tearDown(self):
        self.app.shutdown()
        self.tmp.cleanup()

    def test_poll_once_is_not_reentrant(self):
        """轮询进行中再触发（托盘"立即轮询"起的就是新线程）必须直接跳过。

        并发两轮 = 重复通知 + 抢写 cookie，还可能同时起两个无头浏览器。
        """
        with self.app._poll_lock:  # 模拟"定时轮询正跑着"
            s = self.app.poll_once()
        self.assertFalse(s.ok)
        self.assertTrue(any("仍在进行" in e for e in s.errors))
        self.assertEqual(self.http.hits, [])  # 一个请求都没发
        self.assertEqual(self.notifier.events, [])

    def test_full_flow(self):
        # —— 第一轮：建基线，不通知 ——
        s1 = self.app.poll_once()
        self.assertTrue(s1.ok)
        self.assertEqual((s1.courses, s1.assignments, s1.new), (2, 2, 0))
        self.assertEqual(self.notifier.events, [])
        row = self.app.store.get_assignment(1001)
        self.assertEqual(row["due_ts"], "2026-10-15T23:59:00")
        self.assertEqual(row["notified_new"], 1)  # 基线不轰炸

        # —— 第二轮：课程 101 多了一个 2 小时后截止的新作业 ——
        soon = (datetime.now() + timedelta(hours=2)).strftime("%Y年%m月%d日 %H:%M")
        self.http.pages["/course/view.php?id=101"] = course_html([1001, 1004])
        self.http.pages["/mod/assign/view.php?id=1004"] = detail_html(soon, False)
        s2 = self.app.poll_once()
        self.assertEqual(s2.new, 1)
        self.assertIn(("new", "数据结构", "作业1004"), self.notifier.events)
        self.assertIn("reminder", [e[0] for e in self.notifier.events])  # 临近 24h 提醒

        # 提醒不重复发
        n_reminders = sum(1 for e in self.notifier.events if e[0] == "reminder")
        self.app.poll_once()
        n_reminders2 = sum(1 for e in self.notifier.events if e[0] == "reminder")
        self.assertEqual(n_reminders, n_reminders2)

        # —— 第三轮：老师改了 1001 的截止时间（强制进入 24h 复查队列）——
        self.app.store._conn.execute(
            "UPDATE assignments SET last_checked='2020-01-01T00:00:00' WHERE cmid=1001"
        )
        self.app.store._conn.commit()
        self.http.pages["/mod/assign/view.php?id=1001"] = detail_html("2026年11月01日 12:00", False)
        s3 = self.app.poll_once()
        self.assertEqual(s3.due_changed, 1)
        due_events = [e for e in self.notifier.events if e[0] == "due"]
        self.assertEqual(len(due_events), 1)
        self.assertEqual(due_events[0][4], "2026年11月01日 12:00")
        row = self.app.store.get_assignment(1001)
        self.assertEqual(row["due_ts"], "2026-11-01T12:00:00")

        # —— 会话过期 ——
        from assignmentradar.session import SessionExpired

        class ExpiredHTTP(FakeHTTP):
            reestablish_called = False

            def get(self, url, **kw):
                raise SessionExpired("被重定向到登录页: https://sso.scnu.edu.cn/user/login.html")

            def reestablish(self):
                self.reestablish_called = True
                return False  # 模拟 SSO cookie 也失效

        expired = ExpiredHTTP()
        self.app.http = expired
        s = self.app.poll_once()
        self.assertFalse(s.ok)
        self.assertFalse(self.app.session_ok)
        self.assertTrue(expired.reestablish_called)  # 触发过自动续期尝试
        self.assertIn("expired", [e[0] for e in self.notifier.events])

    def test_api_state(self):
        self.app.poll_once()
        st = self.app.api_state()
        self.assertTrue(st["ok"])
        self.assertEqual(len(st["courses"]), 2)
        names = {c["name"] for c in st["courses"]}
        self.assertEqual(names, {"数据结构", "操作系统"})


class TestDetailRefreshNeverFreezes(unittest.TestCase):
    """回归（2026-10-08 真事故）：作业过了截止时间后，提交状态就再也不更新了。

    场景：学生卡在截止前一小时交作业 → 雷达截止前最后一次抓取记下「未提交」→
    截止一过，该作业被 `due_ts >= now` 过滤掉、永久排除在复查之外 → 清单里
    永远显示「未提交」，把学生吓一跳（"我明明交了！"）。

    修法：复查队列必须包含「已截止但尚未提交」的作业；另外临近截止的那批
    用更短的间隔复查，保证「刚交完就看清单」时状态是新鲜的。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = load_config(base)
        self.cfg.browser_refresh_hours = 0  # 这些测试不碰浏览器
        self.http = FakeHTTP()
        self.http.ajax = [{"error": False, "data": {"courses": [
            {"id": 101, "fullname": "分布式系统设计"},
        ]}}]
        self.overdue = (datetime.now() - timedelta(days=3)).strftime("%Y年%m月%d日 %H:%M")
        self.http.pages = {
            "/my/": MY_HTML,
            "/course/view.php?id=101": course_html([1001]),
            # 截止已过、页面上没有明确的提交状态标记 → submitted=None
            "/mod/assign/view.php?id=1001": detail_html(self.overdue, None),
        }
        self.app = App(self.cfg)
        self.app.http = self.http
        self.app.notifier = None

    def tearDown(self):
        self.app.shutdown()
        self.tmp.cleanup()

    def _age_detail(self, when="2020-01-01T00:00:00") -> None:
        """把 last_checked 拨回过去，模拟「很久没复查过」。"""
        self.app.store._conn.execute(
            "UPDATE assignments SET last_checked=? WHERE cmid=1001", (when,)
        )
        self.app.store._conn.commit()

    def test_overdue_unsubmitted_stays_in_refresh_queue(self):
        self.app.poll_once()
        self._age_detail()
        rows = self.app.store.assignments_needing_detail(timedelta(hours=24))
        self.assertEqual([r["cmid"] for r in rows], [1001],
                         "已截止但未提交的作业必须留在复查队列里（否则状态被永久冻结）")

    def test_overdue_submitted_drops_out_of_queue(self):
        self.app.poll_once()
        self.app.store._conn.execute("UPDATE assignments SET submitted=1 WHERE cmid=1001")
        self.app.store._conn.commit()
        self._age_detail()
        rows = self.app.store.assignments_needing_detail(timedelta(hours=24))
        self.assertEqual(rows, [], "已截止且已提交的不该再复查——没有新信息可获取")

    def test_future_unsubmitted_still_refreshed(self):
        """未截止的照常复查，用来捕捉老师改截止时间。"""
        self.http.pages["/mod/assign/view.php?id=1001"] = detail_html(
            (datetime.now() + timedelta(days=5)).strftime("%Y年%m月%d日 %H:%M"), False)
        self.app.poll_once()
        self._age_detail()
        rows = self.app.store.assignments_needing_detail(timedelta(hours=24))
        self.assertEqual([r["cmid"] for r in rows], [1001])

    def test_late_submission_is_picked_up_end_to_end(self):
        """完整复现用户场景：过期作业补交后，下一轮复查应把状态改成「已提交」。"""
        self.app.poll_once()
        self.assertIsNone(self.app.store.get_assignment(1001)["submitted"])

        # 学生补交了作业：页面出现「已提交供评分」
        self.http.pages["/mod/assign/view.php?id=1001"] = detail_html(self.overdue, True)
        self._age_detail()

        self.app.poll_once()
        self.assertEqual(self.app.store.get_assignment(1001)["submitted"], 1)

    def test_near_deadline_is_refreshed_even_when_recently_checked(self):
        """临近截止的作业用短间隔复查：刚交完立刻看清单也能看到正确状态。"""
        soon = (datetime.now() + timedelta(hours=3)).strftime("%Y年%m月%d日 %H:%M")
        self.http.pages["/mod/assign/view.php?id=1001"] = detail_html(soon, None)
        self.app.poll_once()

        # 学生交了作业，但上次复查就在 30 分钟前（常规 24h 队列不会收它）
        self.http.pages["/mod/assign/view.php?id=1001"] = detail_html(soon, True)
        self._age_detail((datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds"))

        near = self.app.store.assignments_near_deadline(
            timedelta(hours=12), timedelta(minutes=20))
        self.assertEqual([r["cmid"] for r in near], [1001],
                         "临近截止的未提交作业应进入短间隔复查队列")

        self.app.poll_once()
        self.assertEqual(self.app.store.get_assignment(1001)["submitted"], 1)

    def test_near_deadline_queue_excludes_submitted_and_distant(self):
        soon = (datetime.now() + timedelta(hours=3)).strftime("%Y年%m月%d日 %H:%M")
        self.http.pages["/mod/assign/view.php?id=1001"] = detail_html(soon, False)
        self.app.poll_once()
        self._age_detail()

        near = self.app.store.assignments_near_deadline(
            timedelta(hours=12), timedelta(minutes=20))
        self.assertEqual([r["cmid"] for r in near], [1001])

        # 已提交 → 移出短间隔队列
        self.app.store._conn.execute("UPDATE assignments SET submitted=1 WHERE cmid=1001")
        self.app.store._conn.commit()
        self.assertEqual(
            self.app.store.assignments_near_deadline(timedelta(hours=12), timedelta(minutes=20)),
            [],
        )

        # 截止还在很远 → 也不进短间隔队列
        self.app.store._conn.execute(
            "UPDATE assignments SET submitted=0, due_ts=? WHERE cmid=1001",
            ((datetime.now() + timedelta(days=10)).isoformat(timespec="seconds"),),
        )
        self.app.store._conn.commit()
        self.assertEqual(
            self.app.store.assignments_near_deadline(timedelta(hours=12), timedelta(minutes=20)),
            [],
        )


class TestAttachmentArchiving(unittest.TestCase):
    """轮询顺手把老师随作业发布的附件按「课程 / 作业」归档到本地。

    归档是幂等的：文件已存在就不发请求，所以挂在每轮轮询上代价极小。
    """

    ATT_URL = ("https://moodle.test/pluginfile.php/1/mod_assign/"
               "introattachment/0/handout.pdf")
    SUB_URL = ("https://moodle.test/pluginfile.php/2/assignsubmission_file/"
               "submission_files/9/mine.pdf")
    PAGE = (
        '<html><body>'
        '<div id="intro">'
        f'<a href="{ATT_URL}">handout.pdf</a>'
        f'<a href="{SUB_URL}">mine.pdf</a>'
        '</div>'
        '<table><tr><th>截止日期</th><td>2026年11月01日 23:59</td></tr></table>'
        '</body></html>'
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = load_config(base)
        self.cfg.homework_dir = "homework"
        self.cfg.browser_refresh_hours = 0  # 这些测试不碰浏览器
        self.http = FakeHTTP()
        self.http.ajax = [{"error": False, "data": {"courses": [
            {"id": 101, "fullname": "分布式系统设计"},
        ]}}]
        self.http.pages = {
            "/my/": MY_HTML,
            "/course/view.php?id=101": course_html([1001]),
            "/mod/assign/view.php?id=1001": self.PAGE,
        }
        self.http.blobs = {
            "/pluginfile.php/1/mod_assign/introattachment/0/handout.pdf": b"%PDF-1.4 data",
        }
        self.app = App(self.cfg)
        self.app.http = self.http
        self.app.notifier = None
        self.root = base / "homework"

    def tearDown(self):
        self.app.shutdown()
        self.tmp.cleanup()

    def test_new_assignment_attachment_is_archived(self):
        s = self.app.poll_once()
        self.assertEqual(s.attachments, 1)
        found = list(self.root.rglob("handout.pdf"))
        self.assertEqual(len(found), 1, f"未找到归档文件（目录树：{list(self.root.rglob('*'))}）")
        self.assertEqual(found[0].read_bytes(), b"%PDF-1.4 data")
        # 目录结构：homework/<课程>/<作业>/handout.pdf
        self.assertEqual(found[0].parent.parent.parent, self.root)
        self.assertIn("分布式系统", str(found[0].parent.parent))

    def test_own_submission_is_not_archived(self):
        self.app.poll_once()
        self.assertEqual(list(self.root.rglob("mine.pdf")), [],
                         "自己交上去的作业不该被当成资料归档")

    def test_second_poll_does_not_refetch_attachment(self):
        self.app.poll_once()
        # 让台账里的详情过期，逼出一次详情复查（复查会再次走到归档逻辑）
        self.app.store._conn.execute(
            "UPDATE assignments SET last_checked='2020-01-01T00:00:00' WHERE cmid=1001")
        self.app.store._conn.commit()
        self.http.hits.clear()
        self.app.poll_once()
        self.assertNotIn(self.ATT_URL, self.http.hits,
                         "文件已在本地，复查时不该再下载一遍")

    def test_disabled_by_config(self):
        self.cfg.download_attachments_on_poll = False
        s = self.app.poll_once()
        self.assertEqual(s.attachments, 0)
        self.assertEqual(list(self.root.rglob("*")), [])


class TestBrowserRefresh(unittest.TestCase):
    """会话过期时用**真实浏览器**静默续期。

    背景（2026-10-10）：`requests` 那条续期链路走不通——网关改过登录入口
    （`/rump_frontend/login/` 现在 404），回调也不升级票据。但真实浏览器能走通，
    所以只要私有目录里的 SSO 还活着，用户一个字都不用输。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = load_config(base)
        self.cfg.browser_refresh_hours = 4
        self.http = FakeHTTP()
        self.http.ajax = [{"error": False, "data": {"courses": [
            {"id": 101, "fullname": "软件系统"},
        ]}}]
        self.http.pages = {
            "/my/": MY_HTML,
            "/course/view.php?id=101": course_html([1001]),
            "/mod/assign/view.php?id=1001": detail_html("2026年11月01日 23:59", False),
        }
        self.app = App(self.cfg)
        self.app.http = self.http
        self.app.notifier = None
        self._orig = relogin.refresh_via_browser

    def tearDown(self):
        relogin.refresh_via_browser = self._orig
        self.app.shutdown()
        self.tmp.cleanup()

    def _patch(self, *, ok=True, cookie="MoodleSession=NEWVALUE"):
        """替身：真续期会写 config，这里也照做，否则 _reload_session_from_config 看不到变化。"""
        calls: list = []

        def fake(cfg, **kw):
            calls.append(1)
            if not ok:
                return relogin.RefreshResult(False, "SSO 也过期了")
            from assignmentradar.config import set_cookie

            set_cookie(cfg.base_dir, cookie)
            cfg.cookie = cookie
            return relogin.RefreshResult(True, "已取回新会话", 1)

        relogin.refresh_via_browser = fake
        return calls

    def test_expired_session_recovers_via_browser(self):
        """核心场景：轮询撞上会话过期 → 浏览器静默续期 → 本轮自动恢复，无需人工。"""
        expired = {"on": True}
        orig_get = self.http.get

        def get(url, **kw):
            if expired["on"] and "/my/" in url:
                from assignmentradar.session import SessionExpired

                raise SessionExpired("被重定向到登录页")
            return orig_get(url, **kw)

        self.http.get = get  # type: ignore[assignment]
        self.app.http = self.http
        calls = self._patch()

        # 续期成功后放行（模拟浏览器拿到了新会话）
        def fake(cfg, **kw):
            calls.append(1)
            from assignmentradar.config import set_cookie

            set_cookie(cfg.base_dir, "MoodleSession=NEWVALUE")
            cfg.cookie = "MoodleSession=NEWVALUE"
            expired["on"] = False
            return relogin.RefreshResult(True, "已取回新会话", 1)

        relogin.refresh_via_browser = fake

        s = self.app.poll_once()
        self.assertTrue(s.ok, f"应当靠浏览器续期救回来，实际：{s.errors}")
        self.assertEqual(len(calls), 1)

    def test_proactive_refresh_runs_when_interval_elapsed(self):
        calls = self._patch()
        self.app.poll_once()
        self.assertEqual(len(calls), 1, "没记录过续期时间时，应主动续一次")
        self.assertIsNotNone(self.app.store.get_meta("browser_refresh_at"))

    def test_proactive_refresh_throttled_within_interval(self):
        calls = self._patch()
        self.app.store.set_meta("browser_refresh_at",
                                datetime.now().isoformat(timespec="seconds"))
        self.app.poll_once()
        self.assertEqual(calls, [], "刚续过就不该再起浏览器")

    def test_proactive_disabled_when_zero(self):
        self.cfg.browser_refresh_hours = 0
        calls = self._patch()
        self.app.poll_once()
        self.assertEqual(calls, [], "设成 0 时只在过期后才补救")

    def test_failed_refresh_does_not_break_poll(self):
        calls = self._patch(ok=False)
        s = self.app.poll_once()
        self.assertTrue(s.ok, "续期失败不该影响本轮正常轮询")
        self.assertEqual(calls, [1], "失败也要记录尝试时间，避免每轮都起浏览器")

    def test_attempt_gap_prevents_hammering(self):
        """SSO 已死时不能每轮都去起一次浏览器。"""
        calls = self._patch(ok=False)
        self.app.store.set_meta("browser_refresh_at",
                                datetime.now().isoformat(timespec="seconds"))
        self.assertFalse(self.app._browser_refresh())
        self.assertEqual(calls, [], "30 分钟内不该重复尝试")


if __name__ == "__main__":
    unittest.main()
