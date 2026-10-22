"""平台 web service（REST）通路：**不依赖会话**的作业数据源。

为什么需要它
------------
原有通路是"爬 HTML"，而 HTML 需要 `MoodleSession`——平台那个会话大约 10~12 小时
就死一次，死掉之后清单就停在旧数据上（用户 2026-10-10 报的"数据更新于凌晨"）。
会话过期后 `requests` 又走不通登录链路（网关改过入口、回调不升级票据），
于是只能人工 `radar browser login`。这是整条链上最难受的一环。

`wstoken` 换掉了这一环：它是平台**自己发的**长期令牌（在 `/user/managetoken.php`
创建，可随时吊销），配合 `webservice/rest/server.php` 直接返回结构化 JSON。
一次轮询只要 1（课程）+ 1（作业）+ N（提交状态，只查复查队列里那十几个）个请求，
**且完全不碰 cookie**。

覆盖范围
--------
实测（2026-10-10，服务开放 833 个函数）：

* `core_webservice_get_site_info`        → userid（取"我的课程"要用）
* `core_enrol_get_users_courses`         → 课程列表
* `mod_assign_get_assignments`           → 作业名 / `duedate` / `cmid` / **`introattachments`**
* `mod_assign_get_submission_status`     → 我的提交状态（学生可用）
* `core_course_get_contents`             → 课程文件（暂未用）

**取不到的**：`mod_assign_get_submissions`（批量）需要 `mod/assign:grade`，教师权限。
整页截图也只能靠真实浏览器——那是渲染，不是数据。

错误分类（这个分法决定了要不要回退，别混）
------------------------------------------
* `WsUnavailable`   —— 令牌通路整体不可用（没配 token / `invalidtoken` / 网络不通）。
                       调用方应**回退到 HTML 路径**。
* `WsFunctionDenied`—— 该函数不在此服务里（`accessexception`）。是**局部**问题：
                       能降级就降级（如提交状态拿不到就记 NULL），不必整条放弃。
* `WsError`         —— 其它（参数写错、服务端异常）。是我们自己的 bug，记日志即可。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

from . import moodle
from .config import Config
from .session import USER_AGENT

log = logging.getLogger(__name__)

# 令牌失效 / 服务被裁剪 —— 这两种都意味着"这条路现在走不通了"
_UNAVAILABLE_CODES = {"invalidtoken", "invalidtokenid"}
_DENIED_CODE = "accessexception"

#: 截止时间文案格式。HTML 路径显示的是页面原文，令牌路径要自己造一句同样风格的。
DUE_FMT = "%Y年%m月%d日 %H:%M"


class WsUnavailable(Exception):
    """令牌通路整体不可用 —— 调用方应回退到 HTML 路径。"""


class WsError(Exception):
    """令牌通路可用，但这次调用失败（服务端异常 / 参数问题）。"""


class WsFunctionDenied(WsError):
    """该函数不在此服务里（accessexception）——局部问题，能降级就降级。"""


@dataclass
class WsAssignment:
    """`mod_assign_get_assignments` 里一条作业，外加它所在课程。"""

    cmid: int
    assign_id: int
    course_id: int
    name: str
    due_ts: datetime | None
    url: str
    attachments: list[moodle.Attachment] = field(default_factory=list)

    def brief(self) -> moodle.AssignmentBrief:
        return moodle.AssignmentBrief(self.cmid, self.name, self.url)

    @property
    def due_text(self) -> str | None:
        return self.due_ts.strftime(DUE_FMT) if self.due_ts else None

    def detail(self, submitted: bool | None) -> moodle.AssignmentDetail:
        return moodle.AssignmentDetail(
            due_ts=self.due_ts,
            due_text=self.due_text,
            submitted=submitted,
            attachments=list(self.attachments),
        )


def _num(value) -> float | None:
    """Moodle 的分数可能是数字，也可能是字符串（`"90.81818"`）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(ts) -> datetime | None:
    """Unix 秒 → 本地时间；0 / 空 / 非法都算"没有"。"""
    try:
        n = int(ts or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class GradeItem:
    """平台成绩册里的一个评分项。

    `cmid` 非空时能和雷达台账里的作业**直接对上**；**课程总分**那一项的
    `cmid` / `itemname` / `itemmodule` 都是空的（Moodle 用同一个结构表示"整门课的成绩"），
    调用方按 `cmid is None` 分辨。
    """

    item_id: int
    course_id: int
    cmid: int | None
    itemname: str | None
    itemmodule: str | None
    grade_raw: float | None
    grade_max: float | None
    grade_text: str | None
    graded_at: datetime | None
    submitted_at: datetime | None

    @property
    def is_course_total(self) -> bool:
        return self.cmid is None and not self.itemname

    @property
    def graded(self) -> bool:
        """批改过没有。Moodle 对未评分的项给 `"-"`，同时不给评分时间。"""
        if self.graded_at is not None:
            return True
        return self.grade_raw is not None and self.grade_text not in (None, "-")


def _due_from_ts(value) -> datetime | None:
    """Moodle 的 `duedate` 是 Unix 秒；0 表示"没有截止时间"。"""
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return None


def with_token(url: str, token: str) -> str:
    """给 pluginfile 链接挂上 token —— `webservice/pluginfile.php` 靠它鉴权，不需要 cookie。"""
    if not url or not token:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}token={token}"


def _attachments_from(raw, token: str) -> list[moodle.Attachment]:
    """`introattachments` → 雷达的 Attachment。**只收老师发布的**（这里天然不含
    学生自己交的文件——API 把它们分在别处）。"""
    out: list[moodle.Attachment] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("fileurl") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        name = str(item.get("filename") or "").strip()
        if not name:
            name = url.split("?", 1)[0].rsplit("/", 1)[-1]
        out.append(moodle.Attachment(name=name, url=with_token(url, token)))
    return out


class WsApi:
    """平台 web service 的极简客户端。只依赖 stdlib + requests。"""

    def __init__(self, cfg: Config):
        self.base_url = (cfg.base_url or "").rstrip("/")
        self.token = (cfg.ws_token or "").strip()
        # 与 HTML 路径同样的礼貌：相邻两次请求之间留出间隔。
        # 走 load_config 时它已经被夹到 >= 0.5 秒；直接构造 Config（测试）可以给 0。
        self.delay = max(0.0, float(cfg.request_delay_seconds or 0))
        self.user_id: int | None = None
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        })

    def configured(self) -> bool:
        return bool(self.token)

    # ------------------------------------------------------------ 调用

    def call(self, function: str, **params):
        """调一个 wsfunction，返回解析后的 JSON。"""
        if not self.token:
            raise WsUnavailable("未配置 ws_token")
        data: dict[str, object] = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }
        for key, value in params.items():
            if isinstance(value, (list, tuple, set)):
                for i, item in enumerate(value):
                    data[f"{key}[{i}]"] = item
            elif value is not None:
                data[key] = value
        try:
            resp = self.sess.post(
                f"{self.base_url}/webservice/rest/server.php", data=data, timeout=30
            )
        except requests.RequestException as exc:
            raise WsUnavailable(f"网络不通：{exc}") from exc
        if resp.status_code in (401, 403):
            raise WsUnavailable(f"HTTP {resp.status_code}（令牌可能已失效）")
        if resp.status_code >= 400:
            raise WsError(f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WsError(f"{function} 返回的不是 JSON：{resp.text[:120]!r}") from exc
        if isinstance(payload, dict) and payload.get("exception"):
            code = str(payload.get("errorcode") or "")
            message = f"{function}: {code} {payload.get('message')}"
            if code in _UNAVAILABLE_CODES:
                raise WsUnavailable(message)
            if code == _DENIED_CODE:
                raise WsFunctionDenied(message)
            raise WsError(message)
        if self.delay:
            time.sleep(self.delay)  # 对学校服务器友好一点，和 HTML 路径一个规矩
        return payload

    # ------------------------------------------------------------ 数据

    def courses(self, semester: str | None = None) -> list[moodle.Course]:
        """我的课程列表；`semester` 决定"哪些算本学期"（见 `_in_scope`）。

        顺手把 userid 缓存下来——`core_enrol_get_users_courses` 要用它。
        """
        info = self.call("core_webservice_get_site_info")
        try:
            self.user_id = int((info or {}).get("userid") or 0) or None
        except (TypeError, ValueError):
            self.user_id = None
        if not self.user_id:
            raise WsError("site_info 里没有 userid")

        raw = self.call("core_enrol_get_users_courses", userid=self.user_id)
        out: list[moodle.Course] = []
        seen: set[int] = set()
        for c in raw or []:
            try:
                cid = int(c["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if cid <= 1 or cid in seen:
                continue
            full = str(c.get("fullname") or c.get("shortname") or "")
            if semester and not self._in_scope(c, full, semester):
                continue
            seen.add(cid)
            out.append(moodle.Course(cid, moodle.clean_course_name(full or f"课程 {cid}")))
        return out

    @staticmethod
    def _in_scope(course: dict, fullname: str, semester: str) -> bool:
        """这门课算不算"本学期"。

        课名带学期后缀（`Web 应用开发(26-27上学期)`）是最直白的信号，但**不是全部**：
        有些正在上的课课名里根本没有后缀（`编译原理与方法`、`计算机网络基础`）。
        HTML 那条路靠"我的课程"侧边栏的**教务分组**解决，而 API 没有分组，于是改用
        课程自己的起止时间：`startdate <= now <= enddate` 就是"正在开"。

        2026-10-10 实测：只按课名过滤会得到 4 门，漏掉 2 门正在上的课；按起止时间
        得到 7 门——多出一门没被侧边栏收录的。宁可多留一门，也不能漏掉正在上的课
        （这与雷达"宁可多提醒，不能漏交"的一贯取舍一致）。
        """
        if semester in fullname:
            return True
        try:
            start = int(course.get("startdate") or 0)
            end = int(course.get("enddate") or 0)
        except (TypeError, ValueError):
            return False
        if start <= 0 or end <= 0:
            return False
        return start <= time.time() <= end

    def assignments(self, course_ids) -> dict[int, list[WsAssignment]]:
        """一次调用取回这些课程的全部作业（含截止时间与老师附件）。"""
        ids = [int(c) for c in (course_ids or [])]
        if not ids:
            return {}
        raw = self.call("mod_assign_get_assignments", courseids=ids)
        out: dict[int, list[WsAssignment]] = {}
        for c in (raw or {}).get("courses", []):
            try:
                cid = int(c.get("id") or 0)
            except (TypeError, ValueError):
                continue
            items: list[WsAssignment] = []
            for a in c.get("assignments", []) or []:
                try:
                    cmid = int(a["cmid"])
                    assign_id = int(a["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                items.append(
                    WsAssignment(
                        cmid=cmid,
                        assign_id=assign_id,
                        course_id=cid,
                        name=str(a.get("name") or "").strip() or f"作业 #{cmid}",
                        due_ts=_due_from_ts(a.get("duedate")),
                        url=f"{self.base_url}/mod/assign/view.php?id={cmid}",
                        attachments=_attachments_from(a.get("introattachments"), self.token),
                    )
                )
            out[cid] = items
        return out

    def submission_status(self, assign_id: int) -> bool | None:
        """我交没交。返回 True / False / None（拿不到就是 None，不猜）。

        该函数被服务裁剪时返回 None 而不是抛异常——提交状态拿不到，不该拖垮
        整条数据通路（课程和截止时间还是好的）。
        """
        try:
            st = self.call("mod_assign_get_submission_status", assignid=assign_id)
        except WsFunctionDenied:
            log.debug("mod_assign_get_submission_status 不在服务里，提交状态降级为未知")
            return None
        submission = (((st or {}).get("lastattempt") or {}).get("submission")) or {}
        status = str(submission.get("status") or "").strip().lower()
        if status == "submitted":
            return True
        if status in ("new", "draft"):
            return False
        return None

    def grades(self, course_id: int) -> list[GradeItem]:
        """一门课的全部评分项（含"课程总分"那一项，用 `is_course_total` 分辨）。

        两个坑：
        * **`userid` 必须传。** 不传会被判成"查看其他人的成绩"而拒绝
          （实测 `nopermissions 很抱歉，您目前没有执行此操作的权限（查看其他人的成绩）`）。
        * **没批改的项也会返回**，只是分数是 `"-"` 且没有评分时间——过滤交给调用方。
        """
        if not self.user_id:
            self.courses()  # 顺带把 userid 取回来
        raw = self.call(
            "gradereport_user_get_grade_items", courseid=course_id, userid=self.user_id
        )
        out: list[GradeItem] = []
        for usergrade in (raw or {}).get("usergrades", []) or []:
            for item in usergrade.get("gradeitems", []) or []:
                try:
                    item_id = int(item["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                try:
                    cmid = int(item["cmid"]) if item.get("cmid") else None
                except (TypeError, ValueError):
                    cmid = None
                text = item.get("gradeformatted")
                module = item.get("itemmodule")
                out.append(
                    GradeItem(
                        item_id=item_id,
                        course_id=course_id,
                        cmid=cmid,
                        itemname=(str(item.get("itemname") or "").strip() or None),
                        itemmodule=(str(module).strip() or None) if module else None,
                        grade_raw=_num(item.get("graderaw")),
                        grade_max=_num(item.get("grademax")),
                        grade_text=(str(text).strip() or None) if text is not None else None,
                        graded_at=_dt(item.get("gradedategraded")),
                        submitted_at=_dt(item.get("gradedatesubmitted")),
                    )
                )
        return out
