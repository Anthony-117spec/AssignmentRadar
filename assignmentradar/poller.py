"""轮询编排：课程发现 → 作业 diff → 详情复查 → 提醒 → 通知。"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from . import moodle, wsapi
from .config import Config, load_config, set_cookie
from .notify import Notifier
from .session import MoodleHTTP, SessionExpired
from .store import Store

log = logging.getLogger(__name__)

# ---- 详情复查节奏 ----------------------------------------------------------
# 作业详情（截止时间 + 提交状态）不是每轮都抓，而是错峰复查，避免把学校服务器
# 当玩具。两档：
#   * 常规作业：每 DETAIL_MAX_AGE 复查一次，捕捉老师改截止时间；
#   * 临近截止（含刚过截止）且尚未提交的：每 NEAR_DEADLINE_MAX_AGE 复查一次——
#     学生大多卡在截止前最后几小时交作业，交完会立刻打开清单确认，这时状态必须新鲜。
DETAIL_MAX_AGE = timedelta(hours=24)
NEAR_DEADLINE_WINDOW = timedelta(hours=12)
NEAR_DEADLINE_MAX_AGE = timedelta(minutes=20)

# 浏览器静默续期：两次尝试之间的最小间隔。SSO 已死时，每轮都去起一次浏览器纯属浪费。
BROWSER_REFRESH_MIN_GAP = timedelta(minutes=30)


@dataclass
class PollSummary:
    courses: int = 0
    skipped: int = 0
    assignments: int = 0
    new: int = 0
    due_changed: int = 0
    reminders: int = 0
    attachments: int = 0
    ok: bool = False
    #: 这一轮**端到端**用了多少秒（含会话续期、浏览器静默续期这些"不在轮询主体里"的等待）。
    #: 0 = 还没量到。它同时进 `describe()`，所以 poll_log 里天然留了历史，不用改表结构。
    duration: float = 0.0
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.ok:
            text = "轮询未完成：" + ("；".join(self.errors) or "未知错误")
        else:
            extra = f"，未加入跳过 {self.skipped} 门" if self.skipped else ""
            extra += f"，附件归档 {self.attachments} 个" if self.attachments else ""
            text = (
                f"课程 {self.courses} 门，作业 {self.assignments} 个，"
                f"新作业 {self.new}，截止变更 {self.due_changed}，临近提醒 {self.reminders}{extra}"
            )
        # 失败的轮询尤其要看耗时：61 秒那种多半是卡在浏览器续期的调试端口超时
        return text + (f"，用时 {self.duration:.1f} 秒" if self.duration else "")


class App:
    """一个进程一个实例：承载轮询循环、共享状态与回调。"""

    def __init__(self, cfg: Config, *, notify_enabled: bool = True,
                 quiet_session_notify: bool = False):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.http = MoodleHTTP(cfg)
        # 令牌通路（可选）：配了 ws_token 就优先走它——它不依赖会话，
        # 所以不会随 MoodleSession 过期而变旧。None 表示没配，直接走 HTML 路径。
        self.ws = wsapi.WsApi(cfg) if (cfg.ws_token or "").strip() else None
        self.http.on_cookies_changed = self._persist_cookies
        self.http.on_sso_cookies_changed = self._persist_sso_cookies
        self.notifier = Notifier(cfg) if ((cfg.cookie or cfg.sso_cookie) and notify_enabled) else None
        # list 命令的临时刷新用：会话过期时不弹 toast（避免连按轰炸），
        # 新作业/变更/提醒照常发
        self.quiet_session_notify = quiet_session_notify
        self.session_ok: bool | None = None  # None=未知 True=有效 False=过期
        self._sso_dead_notified = False  # SSO 死亡预警只弹一次，恢复后重新武装
        self.paused = False
        self.last_poll: datetime | None = None
        #: 本轮 poll_once 的起始单调时刻；`_finish` 用它算端到端耗时。
        #: 用 monotonic 而不是 wall clock——系统时间被改/校时都不会把耗时算成负数。
        self._poll_started: float | None = None
        self.listeners: list = []  # fn(app)，托盘等用于刷新图标
        self._stop = False
        # 轮询不可重入：托盘"立即轮询"起的是新线程，与循环里的定时轮询可能撞上。
        # 两轮并发 = 重复通知 + 抢着写 cookie + 可能同时起两个无头浏览器。
        self._poll_lock = threading.Lock()

    def _persist_cookies(self, cookie_str: str) -> None:
        """自动续期拿到的新 cookie 写回 config，重启后不用重新续期。"""
        try:
            set_cookie(self.cfg.base_dir, cookie_str)
            self.cfg.cookie = cookie_str
            log.info("已把续期后的会话 cookie 写回 config.toml")
        except Exception:
            log.exception("写回 cookie 失败")

    def _persist_sso_cookies(self, sso_str: str) -> None:
        """SSO 侧发生轮换时把最新值写回 config。"""
        try:
            set_cookie(self.cfg.base_dir, sso_str, field="sso_cookie")
            self.cfg.sso_cookie = sso_str
            log.info("已把轮换后的 SSO cookie 写回 config.toml")
        except Exception:
            log.exception("写回 SSO cookie 失败")

    def _noise_cutoff(self) -> datetime | None:
        raw = (self.cfg.ignore_due_before or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            log.warning("ignore_due_before 格式应为 YYYY-MM-DD，收到 %r，已忽略", raw)
            return None

    def _is_noise(self, due_iso: str | None) -> bool:
        """截止早于 ignore_due_before 的历史遗留作业。"""
        cut = self._noise_cutoff()
        if cut is None or not due_iso:
            return False
        try:
            return datetime.fromisoformat(due_iso) < cut
        except ValueError:
            return False

    # ------------------------------------------------------------ 生命周期

    def shutdown(self) -> None:
        self._stop = True
        try:
            self.store.close()
        except Exception:
            pass

    def notify_listeners(self) -> None:
        for fn in list(self.listeners):
            try:
                fn(self)
            except Exception:
                log.exception("状态回调失败")

    # ------------------------------------------------------------ 轮询

    def poll_once(self) -> PollSummary:
        """一轮轮询；会话过期时依次尝试：SSO 自动续期 → 重读 config → **浏览器静默续期**。

        最后那道"浏览器静默续期"是 2026-10-10 加的：`requests` 走不通登录链路
        （网关改过入口、且回调不升级票据），但**真实浏览器能走通**。只要私有配置目录
        里的 SSO 会话还活着，打开一次平台就能静默重新登录——用户一个字都不用输。
        """
        if self._poll_lock.locked():
            # 手动轮询撞上定时轮询：跳过这一次，而不是并发跑两轮。
            log.info("上一轮轮询尚未结束，跳过本次（手动）触发")
            self._poll_started = time.monotonic()
            s = PollSummary()
            s.errors.append("上一轮轮询仍在进行，本次触发已跳过")
            self._finish(s, ok=False)
            return s
        with self._poll_lock:
            return self._poll_once_locked()

    def _poll_once_locked(self) -> PollSummary:
        self._poll_started = time.monotonic()
        self._maybe_proactive_browser_refresh()
        try:
            return self._poll_body()
        except SessionExpired:
            if self.http.reestablish():
                try:
                    return self._poll_body()
                except SessionExpired:
                    pass
            if self._reload_session_from_config():
                try:
                    return self._poll_body()
                except SessionExpired:
                    pass
            if self._browser_refresh():
                try:
                    return self._poll_body()
                except SessionExpired:
                    pass
            self._handle_expired()
            s = PollSummary()
            s.errors.append(
                "会话过期，自动续期失败（跑一次 radar browser login 即可恢复）"
            )
            self._finish(s, ok=False)
            return s

    # -------------------------------------------------- 浏览器静默续期

    def _browser_refresh(self, *, min_gap: timedelta = BROWSER_REFRESH_MIN_GAP) -> bool:
        """无头打开私有目录的浏览器，静默续一次会话；成功则重建 HTTP 会话。

        **限流用的是"上次尝试时间"，不是"上次成功时间"**：SSO 已死时续期必然失败，
        若只在成功时记录，就会变成每轮都想再起一次浏览器——纯属浪费。
        """
        now = datetime.now()
        last = self.store.get_meta("browser_refresh_at")
        if last and min_gap:
            try:
                if now - datetime.fromisoformat(last) < min_gap:
                    return False
            except ValueError:
                pass
        self.store.set_meta("browser_refresh_at", now.isoformat(timespec="seconds"))

        from .relogin import refresh_via_browser

        try:
            result = refresh_via_browser(self.cfg)
        except Exception:
            # 异常不是"未成功"：debug 级在 INFO 日志下永久不可见，
            # 续期坏了会表现得像"只是没续上"，白排查。
            log.warning("浏览器续期异常", exc_info=True)
            return False
        if not result.ok:
            log.info("浏览器续期未成功：%s", result.reason)
            return False
        return self._reload_session_from_config()

    def _maybe_proactive_browser_refresh(self) -> None:
        """会话临期前先静默续一次，免得等到过期才发现。

        间隔由 `browser_refresh_hours` 控制（0 = 只在过期后才补救）。
        定期用浏览器走一遍登录，同时也**让 SSO 会话保持活跃**——这是把"每天都要
        重新登录"变成"基本不用管"的关键。

        **但走令牌通路时直接跳过。** 那条路下会话只影响作业页截图，而截图是"你要才做"
        的事；SSO 已死时这一步要等浏览器调试端口超时（实测 **61 秒**）才回来，
        拿它拖累每一轮轮询不划算——清单会在打开时现场刷一轮，用户能直接感觉到慢。
        """
        if self.ws is not None:
            return
        hours = getattr(self.cfg, "browser_refresh_hours", 0)
        if hours <= 0:
            return
        if self._browser_refresh(min_gap=timedelta(hours=hours)):
            log.info("已在会话过期前静默续期")

    def _reload_session_from_config(self) -> bool:
        """用户可能刚更新了 config 里的 cookie：重读并重建会话，返回是否有变化。"""
        try:
            fresh = load_config(self.cfg.base_dir)
        except Exception:
            log.exception("重读配置失败")
            return False
        if fresh.cookie == self.cfg.cookie and fresh.sso_cookie == self.cfg.sso_cookie:
            return False
        log.info("检测到 config 里的 cookie 已更新，重载会话后重试")
        self.cfg.cookie = fresh.cookie
        self.cfg.sso_cookie = fresh.sso_cookie
        self.http = MoodleHTTP(self.cfg)
        self.http.on_cookies_changed = self._persist_cookies
        self.http.on_sso_cookies_changed = self._persist_sso_cookies
        return True

    def _poll_body(self) -> PollSummary:
        """一轮轮询的主体：**优先走令牌通路**（不依赖会话），不可用则回退 HTML。

        令牌通路是 2026-10-10 加的，专治那个反复咬人的毛病：`MoodleSession` 大约
        10~12 小时就死一次，死了清单就停在旧数据上（"数据更新于凌晨"）。token 走的是
        平台自己的 REST 接口，**完全不碰 cookie**，于是会话死活与数据新鲜度脱钩了。

        回退是**保底**：没配 `ws_token`、令牌被吊销、服务被裁剪、网络不通，都自动回到
        原来的 HTML 路径——旧路一条都没删。`WsUnavailable` / `WsError` 之外的异常
        故意不吞：那多半是我们自己的 bug，该露出来。
        """
        if self.ws is not None:
            try:
                s = self._poll_body_ws()
                self.store.set_meta("last_poll_path", "token")
                return s
            except (wsapi.WsUnavailable, wsapi.WsError) as exc:
                log.warning("令牌通路不可用（%s），本次回退到 HTML 路径", exc)
                self.store.set_meta("ws_last_error", str(exc)[:300])
                # 只放弃本轮：下一轮仍会再试（可能只是一次网络抖动）
        self.store.set_meta("last_poll_path", "html")
        return self._poll_body_html()

    def _poll_body_ws(self) -> PollSummary:
        """令牌通路的轮询主体：两次批量调用拿课程与作业，提交状态只查复查队列。"""
        s = PollSummary()
        baseline = self.store.get_meta("baseline_done") != "1"

        courses = self.ws.courses(self.cfg.semester)
        self.session_ok = True  # 数据通路正常；会话现在只影响截图
        s.courses = len(courses)
        if not courses:
            s.errors.append("令牌通路没返回任何课程（服务权限或学期过滤可能有问题）")

        by_course = self.ws.assignments([c.course_id for c in courses])

        for course in courses:
            self.store.upsert_course(course.course_id, course.name)
            course_name = self.store.course_name(course.course_id) or course.name
            for a in by_course.get(course.course_id, []):
                s.assignments += 1
                if self.store.get_assignment(a.cmid) is not None:
                    continue
                detail = self._ws_detail(a, s)
                # 首次运行只建基线不轰炸通知；正常轮询则立即通知
                self.store.insert_assignment(
                    a.cmid, course.course_id, a.name, a.url,
                    notified_new=True, detail=detail,
                )
                if not baseline:
                    s.new += 1
                    # 历史遗留（截止早于 ignore_due_before）只入库，不打扰
                    if self.notifier and not self._is_noise(
                        detail.due_ts.isoformat(timespec="seconds") if detail.due_ts else None
                    ):
                        self.notifier.new_assignment(
                            course_name, a.name, detail.due_text, a.url,
                        )
            # 附件：批量数据里就带着老师附件（`introattachments`），全部走一遍。
            # 已存在的文件会跳过，所以正常情况**一个请求都不发**——这是附件归档的
            # 老规矩（幂等），也正是它敢挂在每轮轮询上的原因。
            for a in by_course.get(course.course_id, []):
                if a.attachments:
                    self._archive_attachments(a.detail(None), course_name, a.name, s)

            # 评分：一门课一次调用，把成绩册同步进台账——这样清单里的评分和作业一样
            # 不依赖会话。失败只记日志：评分拿不到不该影响作业监控本身。
            try:
                for g in self.ws.grades(course.course_id):
                    self.store.upsert_grade(g)
            except Exception as exc:
                log.warning("同步评分失败 course_id=%s：%s", course.course_id, exc)

        self._refresh_stale_details_ws(s, by_course)
        self._send_reminders(s)

        self.store.set_meta("baseline_done", "1")
        s.ok = True
        self._keep_sso_alive()
        self._finish(s, ok=True)
        return s

    def _ws_detail(self, a: wsapi.WsAssignment, s: PollSummary):
        """令牌通路的作业详情：截止时间来自批量数据，提交状态单独查一次。"""
        try:
            submitted = self.ws.submission_status(a.assign_id)
        except Exception as exc:
            log.warning("取提交状态失败 cmid=%s：%s", a.cmid, exc)
            submitted = None  # 拿不到就记未知，不猜——和 HTML 路径同一取舍
        return a.detail(submitted)

    def _refresh_stale_details_ws(self, s: PollSummary, by_course: dict) -> None:
        """令牌通路的错峰复查。与 HTML 路径有两点不同：

        1. **截止时间不需要额外请求** —— 批量数据里已经带着全部作业的截止时间，可以
           逐条比对（HTML 路径每 24 小时只能抽查十几个），老师改截止能更快被发现。
        2. 提交状态仍要逐份调用，所以**只查复查队列**（临近截止的优先），
           两档节奏与 HTML 路径一致。
        """
        flat = {a.cmid: a for items in by_course.values() for a in items}
        if not flat:
            return

        # ① 截止时间：零额外请求地比对全部。**只写 due**，不碰 submitted / last_checked
        #    （用 update_detail 会把 last_checked 推到当下，那条作业就会被踢出复查队列
        #    最长 24 小时，提交状态反而变旧）。
        try:
            rows = self.store.assignments_with_course()
        except Exception:
            log.exception("读取台账失败")
            rows = []
        for row in rows:
            a = flat.get(row["cmid"])
            if a is None:
                continue
            new_due = a.due_ts.isoformat(timespec="seconds") if a.due_ts else None
            if new_due == row["due_ts"]:
                continue
            if (row["due_ts"] or row["due_text"]) and self.notifier and not self._is_noise(new_due):
                course_name = self.store.course_name(row["course_id"]) or f"课程 {row['course_id']}"
                self.notifier.due_changed(
                    course_name, row["name"], row["due_text"], a.due_text, row["url"]
                )
                s.due_changed += 1
            # 截止变了，旧提醒作废，允许按新截止再次提醒
            self.store.clear_reminders(row["cmid"])
            self.store.update_due(row["cmid"], a.due_ts, a.due_text)

        # ② 提交状态：只查复查队列
        try:
            near = self.store.assignments_near_deadline(
                NEAR_DEADLINE_WINDOW, NEAR_DEADLINE_MAX_AGE
            )
            stale = self.store.assignments_needing_detail(DETAIL_MAX_AGE)
        except Exception:
            log.exception("查询待复查作业失败")
            return
        seen: set[int] = set()
        for row in [*near, *stale]:  # 临近的优先，同一条只取一次
            if row["cmid"] in seen:
                continue
            seen.add(row["cmid"])
            a = flat.get(row["cmid"])
            if a is None:
                continue
            self.store.update_detail(row["cmid"], self._ws_detail(a, s))

    def _poll_body_html(self) -> PollSummary:
        s = PollSummary()
        baseline = self.store.get_meta("baseline_done") != "1"
        try:
            courses = moodle.discover_courses(self.http, semester=self.cfg.semester)
        except SessionExpired:
            raise
        except Exception as exc:
            log.exception("课程发现失败")
            s.errors.append(f"课程发现失败：{exc}")
            self._finish(s, ok=False)
            return s

        self.session_ok = True
        s.courses = len(courses)
        if not courses:
            s.errors.append("未发现任何课程（页面结构可能变化，或账号下确实没有课程）")

        for course in courses:
            try:
                briefs = moodle.fetch_course_assignments(self.http, course.course_id)
            except moodle.NotEnrolled as exc:
                # 课程还没"自助选课"加入：跳过且不入库，加入后下一轮自动出现
                log.info("%s", exc)
                s.skipped += 1
                continue
            except SessionExpired:
                raise
            except Exception as exc:
                s.errors.append(f"课程《{course.name}》解析失败：{exc}")
                continue

            self.store.upsert_course(course.course_id, course.name)
            course_name = self.store.course_name(course.course_id) or course.name
            for brief in briefs:
                s.assignments += 1
                if self.store.get_assignment(brief.cmid) is None:
                    detail = self._safe_detail(brief.url, s)
                    # 首次运行只建基线不轰炸通知；正常轮询则立即通知
                    self.store.insert_assignment(
                        brief.cmid, course.course_id, brief.name, brief.url,
                        notified_new=True, detail=detail,
                    )
                    self._archive_attachments(detail, course_name, brief.name, s)
                    if not baseline:
                        s.new += 1
                        # 历史遗留（截止早于 ignore_due_before）只入库，不打扰
                        if self.notifier and not self._is_noise(
                            detail.due_ts.isoformat(timespec="seconds") if detail and detail.due_ts else None
                        ):
                            self.notifier.new_assignment(
                                course_name, brief.name,
                                detail.due_text if detail else None, brief.url,
                            )

        self._refresh_stale_details(s)
        self._send_reminders(s)

        self.store.set_meta("baseline_done", "1")
        s.ok = True
        self._keep_sso_alive()
        self._finish(s, ok=True)
        return s

    def _archive_attachments(self, detail, course_name: str, assignment_name: str,
                             s: PollSummary) -> None:
        """把作业附件按「课程 / 作业」归档到本地。

        已存在的文件会跳过，所以正常情况下**一个请求都不发**——只有真正新出现的
        附件才产生流量。任何失败都只记日志，绝不影响轮询本身。
        """
        root = self.cfg.homework_path
        if root is None or not detail or not detail.attachments:
            return
        if not getattr(self.cfg, "download_attachments_on_poll", True):
            return
        try:
            from . import attachments as att

            items = [
                att.Item(course_name, assignment_name, a.name, a.url)
                for a in detail.attachments
            ]
            summary = att.download(self.http, items, root,
                                   course_dirs=self.cfg.course_dirs)
            s.attachments += summary.saved
            if summary.saved:
                log.info("已归档 %d 个附件到 %s", summary.saved, root)
        except Exception:
            log.warning("附件归档失败（不影响轮询）", exc_info=True)

    def _keep_sso_alive(self) -> None:
        """SSO 会话约一天就悄悄死掉，死透后"自动续期"随之报废，下次网关会话
        一过期就只能人工介入。趁每轮轮询成功主动续命（内部限频）；发现 SSO
        已死时弹一次预警，让用户趁 moodle 会话还活着就把 SSO cookie 换新。"""
        try:
            alive = self.http.keepalive_sso()
        except AttributeError:
            return  # 测试替身等没有该方法的 HTTP 层：跳过
        except Exception:
            log.warning("SSO 保活异常（不影响本轮轮询）", exc_info=True)
            return
        if alive is False:
            if not self._sso_dead_notified:
                self._sso_dead_notified = True
                if self.notifier and not self.quiet_session_notify:
                    self.notifier.sso_expired()
        elif alive:
            self._sso_dead_notified = False

    # ------------------------------------------------------------ 子步骤

    def _safe_detail(self, url: str, s: PollSummary):
        """抓详情页；瞬时失败自动重试一次，仍失败则记入本轮错误。"""
        for attempt in range(2):
            try:
                return moodle.fetch_assign_detail(self.http, url)
            except SessionExpired:
                raise
            except Exception as exc:
                if attempt == 0:
                    time.sleep(3)
                    continue
                s.errors.append(f"详情获取失败 {url}：{exc}")
                return None

    def _refresh_stale_details(self, s: PollSummary) -> None:
        """错峰复查作业详情：既捕捉老师改截止时间，也捕捉**提交状态的变化**。

        两档节奏见模块顶部的常量。临近截止的作业复查得更勤，是为了不让学生在
        「刚交完就看清单」时被过期的「未提交」吓到。
        """
        try:
            near = self.store.assignments_near_deadline(
                NEAR_DEADLINE_WINDOW, NEAR_DEADLINE_MAX_AGE
            )
            stale = self.store.assignments_needing_detail(DETAIL_MAX_AGE)
        except Exception:
            log.exception("查询待复查作业失败")
            return

        rows: list = []
        seen: set[int] = set()
        for row in [*near, *stale]:  # 临近的优先，同一条只取一次
            if row["cmid"] in seen:
                continue
            seen.add(row["cmid"])
            rows.append(row)

        for row in rows:
            detail = self._safe_detail(row["url"], s)
            if detail is None:
                continue
            new_due = detail.due_ts.isoformat(timespec="seconds") if detail.due_ts else None
            had_due = bool(row["due_ts"] or row["due_text"])
            if new_due != row["due_ts"]:
                if had_due and self.notifier and not self._is_noise(new_due):
                    course_name = self.store.course_name(row["course_id"]) or f"课程 {row['course_id']}"
                    self.notifier.due_changed(
                        course_name, row["name"], row["due_text"], detail.due_text, row["url"]
                    )
                    s.due_changed += 1
                # 截止变了，旧提醒作废，允许按新截止再次提醒
                self.store.clear_reminders(row["cmid"])
            self.store.update_detail(row["cmid"], detail)
            # 顺手把（可能新出现的）附件归档——已存在的会跳过
            course_name = self.store.course_name(row["course_id"]) or f"课程 {row['course_id']}"
            self._archive_attachments(detail, course_name, row["name"], s)

    def _send_reminders(self, s: PollSummary) -> None:
        if not self.notifier:
            return
        now = datetime.now()
        for hours in dict.fromkeys(self.cfg.deadline_reminder_hours):
            try:
                rows = self.store.pending_reminders(hours, now)
            except Exception:
                log.exception("查询临近截止作业失败")
                continue
            for row in rows:
                course_name = self.store.course_name(row["course_id"]) or f"课程 {row['course_id']}"
                self.notifier.reminder(course_name, row["name"], row["due_text"], hours, row["url"])
                self.store.record_reminder(row["cmid"], hours)
                s.reminders += 1

    def _handle_expired(self) -> None:
        if self.session_ok is not False and self.notifier and not self.quiet_session_notify:
            self.notifier.session_expired(self.cfg.base_url)
        self.session_ok = False
        self.last_poll = datetime.now()
        self.notify_listeners()

    def _finish(self, s: PollSummary, *, ok: bool) -> None:
        # 耗时要在 describe() **之前**量好：它既进 describe（于是 poll_log 里天然留了历史，
        # 不用给表加列），也单独写进 meta 供"读缓存"的清单页显示。
        if self._poll_started is not None:
            s.duration = max(0.0, time.monotonic() - self._poll_started)
            self._poll_started = None
            self.store.set_meta("last_poll_duration", f"{s.duration:.1f}")
        s.ok = ok
        self.last_poll = datetime.now()
        self.store.set_meta("last_poll", self.last_poll.isoformat(timespec="seconds"))
        if ok:  # 只有成功巡逻才算"数据更新于"；失败只算"最后检查"
            self.store.set_meta("last_success", self.last_poll.isoformat(timespec="seconds"))
        detail = s.describe()
        if s.errors:
            detail += "；" + "；".join(s.errors)
        self.store.poll_log(ok, detail)
        self.notify_listeners()

    # ------------------------------------------------------------ 循环

    def run_forever(self) -> None:
        log.info("开始轮询，间隔约 %s 分钟", self.cfg.poll_interval_minutes)
        fails = 0  # 连续失败轮次，用于退避
        try:
            self.poll_once()
        except Exception:
            log.exception("首轮轮询异常")
        while not self._stop:
            if self.paused:
                if self._sleep(30):
                    break
                continue
            # 连续失败时退避（最多退到 1/4）：网关挂整晚时还按原频率敲门，
            # 只会把限流惹起来。成功一轮即复位。
            base = max(1, self.cfg.poll_interval_minutes) * 60
            factor = 0.5 ** min(fails, 2)
            delay = base * random.uniform(0.9, 1.1) * factor
            if self._sleep(delay):
                break
            if self.paused:
                continue
            try:
                s = self.poll_once()
                fails = 0 if s.ok else fails + 1
            except Exception:
                log.exception("轮询出现未捕获异常")
                fails += 1

    def _sleep(self, seconds: float) -> bool:
        """可被 stop 打断的睡眠；返回 True 表示收到停止信号。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop:
                return True
            time.sleep(min(1.0, max(0.05, end - time.monotonic())))
        return self._stop

    # ------------------------------------------------------------ 面板数据

    def api_state(self) -> dict:
        rows = self.store.assignments_with_course()
        cutoff = self._noise_cutoff()
        courses: dict[int, dict] = {}
        ignored = 0
        for r in rows:
            if cutoff is not None and r["due_ts"]:
                try:
                    if datetime.fromisoformat(r["due_ts"]) < cutoff:
                        ignored += 1
                        continue
                except ValueError:
                    pass
            c = courses.setdefault(
                r["course_id"], {"id": r["course_id"], "name": r["course_name"] or f"课程 {r['course_id']}", "assignments": []}
            )
            c["assignments"].append(
                {
                    "cmid": r["cmid"],
                    "name": r["name"],
                    "url": r["url"],
                    "due_ts": r["due_ts"],
                    "due_text": r["due_text"],
                    "submitted": None if r["submitted"] is None else bool(r["submitted"]),
                    "first_seen": r["first_seen"],
                }
            )
        return {
            "ok": True,
            "session_ok": self.session_ok,
            # 内存里的 self.last_poll 重启后是 None，而耗时是从 meta 读的——不兜底就会
            # 显示成"最后检查：尚未运行（用时 27.2 秒）"这种自相矛盾的话。回落到 meta。
            "last_poll": (
                self.last_poll.isoformat(timespec="seconds") if self.last_poll
                else self.store.get_meta("last_poll")
            ),
            # 最近一轮的端到端耗时（秒，字符串）与走的是哪条通路——面板的 meta 行显示它们
            "last_duration": self.store.get_meta("last_poll_duration"),
            "last_path": self.store.get_meta("last_poll_path"),
            "now": datetime.now().isoformat(timespec="seconds"),
            "interval_minutes": self.cfg.poll_interval_minutes,
            "ignored_count": ignored,
            "courses": [courses[k] for k in sorted(courses)],
        }
