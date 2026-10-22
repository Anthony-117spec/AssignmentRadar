"""平台页面解析：课程发现、作业列表、作业详情（截止时间/提交状态）。"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from .session import MoodleHTTP, NotEnrolled, SessionExpired, is_logged_in_html

log = logging.getLogger(__name__)


@dataclass
class Course:
    course_id: int
    name: str


@dataclass
class AssignmentBrief:
    cmid: int
    name: str
    url: str


@dataclass
class Attachment:
    """作业页上的一个可下载文件（老师发布的资料）。"""

    name: str
    url: str


@dataclass
class AssignmentDetail:
    due_ts: datetime | None = None
    due_text: str | None = None
    submitted: bool | None = None
    attachments: list[Attachment] = field(default_factory=list)


# ---------------------------------------------------------------- 日期解析

_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})")
_ZH = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_TIME = re.compile(r"(\d{1,2}):(\d{2})")
_EN = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
_EN_MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"]
    )
}


def parse_moodle_date(text: str | None) -> datetime | None:
    """解析 Moodle 页面上的日期文本（中文/英文/ISO），无时间时默认 23:59。"""
    if not text:
        return None
    iso = _ISO.search(text)
    if iso:
        y, m, d, hh, mm = (int(x) for x in iso.groups())
        try:
            return datetime(y, m, d, hh, mm)
        except ValueError:
            return None
    zh = _ZH.search(text)
    if zh:
        y, m, d = (int(x) for x in zh.groups())
        t = _TIME.search(text)
        hh, mm = (int(t.group(1)), int(t.group(2))) if t else (23, 59)
        try:
            return datetime(y, m, d, hh, mm)
        except ValueError:
            return None
    en = _EN.search(text)
    if en:
        day, mon, year = int(en.group(1)), en.group(2).lower(), int(en.group(3))
        if mon not in _EN_MONTHS:
            return None
        t = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", text, re.I)
        hh, mm = 23, 59
        if t:
            hh, mm = int(t.group(1)), int(t.group(2))
            ampm = (t.group(3) or "").lower()
            if ampm == "pm" and hh < 12:
                hh += 12
            elif ampm == "am" and hh == 12:
                hh = 0
        try:
            return datetime(year, _EN_MONTHS[mon], day, hh, mm)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 课程发现

_TIMELINE_WS = "core_course_get_enrolled_courses_by_timeline_classification"

# 平台把简介、教师、人数都塞进了 fullname，按标记截断出干净课名
_NAME_CUT_MARKERS = (" 暂无简介", " 教师:", " 教师：", " 老师:")
_SEMESTER_RE = re.compile(r"^\s*\d{2}-\d{2}\s*[上下]学期\s*$")


def clean_course_name(name: str) -> str:
    name = (name or "").strip()
    cuts = [name.index(m) for m in _NAME_CUT_MARKERS if m in name]
    if cuts:
        name = name[: min(cuts)]
    name = re.sub(r"\s*\d+\s*人在学.*$", "", name)
    if len(name) > 24 and " " in name:
        head = name.split(" ", 1)[0]  # 形如"WPS365教育版应用能力提升培训 课程简介..."
        if 6 <= len(head) <= 24:
            name = head
    return name.strip() or "未命名课程"


def parse_sidebar_courses(html: str, semester: str | None = None) -> list[Course]:
    """解析"我的课程"侧边栏（学校定制插件，服务端渲染，按学期分组）。

    这是教务系统同步的权威课程清单，比 AJAX 时间线准确——时间线里会混入
    校级培训课和往期课程。semester 指定学期分组名（如 "26-27上学期"），
    只返回该组课程；None 返回全部。找不到任何课程链接时返回空列表。
    """
    soup = BeautifulSoup(html, "html.parser")
    marker = soup.find(string=re.compile("我的课程"))
    if marker is not None:
        block = None
        cur = marker.parent
        for _ in range(8):  # 取能包住课程链接的最小祖先容器
            if cur is None:
                break
            if cur.find("a", href=re.compile(r"course/view\.php\?id=\d+")) is not None:
                block = cur
            cur = cur.parent
    else:
        block = None
    scope = block if block is not None else soup

    groups: dict[str, list[Course]] = {}
    current = ""
    for el in scope.descendants:
        if isinstance(el, str):
            text = el.strip()
            if _SEMESTER_RE.match(text):
                current = text
            continue
        if getattr(el, "name", None) != "a":
            continue
        href = el.get("href", "") or ""
        if "course/view.php?id=" not in href:
            continue
        qs = parse_qs(urlparse(href).query)
        try:
            cid = int(qs["id"][0])
        except (KeyError, ValueError):
            continue
        if cid <= 1:
            continue
        name = clean_course_name(el.get_text(" ", strip=True))
        groups.setdefault(current, []).append(Course(cid, name))

    if not groups:
        return []
    if semester:
        matched = {k: v for k, v in groups.items() if semester in k}
        if not matched:
            log.warning("侧边栏里没有找到学期分组 %r（有：%s）", semester, list(groups))
            return []
        groups = matched
    seen: set[int] = set()
    out: list[Course] = []
    for cs in groups.values():
        for c in cs:
            if c.course_id not in seen:
                seen.add(c.course_id)
                out.append(c)
    return out


def discover_courses(http: MoodleHTTP, semester: str | None = None) -> list[Course]:
    """发现要关注的课程。优先级：我的课程侧边栏（权威） > AJAX 时间线 > 页面卡片。"""
    resp = http.get(f"{http.base_url}/my/")
    # 网关失效时会发给游客一个"正常"的公开首页，必须先确认真登录了
    if not is_logged_in_html(resp.text):
        raise SessionExpired("/my/ 页面显示未登录（网关会话无效，处于游客态）")

    sidebar = parse_sidebar_courses(resp.text, semester)
    if sidebar:
        log.info("从『我的课程』侧边栏解析到 %d 门课程（semester=%r）", len(sidebar), semester)
        return sidebar
    log.info("侧边栏未解析到课程，回退 AJAX 时间线 + 页面卡片")

    courses: dict[int, Course] = {}
    m = re.search(r'"sesskey":"([0-9A-Za-z]+)"', resp.text)
    if m:
        try:
            courses.update(_courses_via_ajax(http, m.group(1)))
        except Exception as exc:
            log.warning("课程 AJAX 接口失败，回退解析 HTML 课程卡片：%s", exc)
    else:
        log.warning("页面里没找到 sesskey，只能解析 HTML 课程卡片")
    for course in parse_course_cards(resp.text):
        courses.setdefault(course.course_id, course)
    return list(courses.values())


def _courses_via_ajax(http: MoodleHTTP, sesskey: str) -> dict[int, Course]:
    url = f"{http.base_url}/lib/ajax/service.php?sesskey={sesskey}&info={_TIMELINE_WS}"
    payload = [
        {
            "index": 0,
            "methodname": _TIMELINE_WS,
            "args": {"offset": 0, "limit": 0, "classification": "all", "sort": "fullname"},
        }
    ]
    resp = http.post(
        url,
        json=payload,
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{http.base_url}/my/"},
    )
    data = resp.json()
    if not isinstance(data, list) or not data or data[0].get("error"):
        raise ValueError(f"AJAX 响应异常: {str(data)[:200]}")
    out: dict[int, Course] = {}
    for c in data[0].get("data", {}).get("courses", []):
        try:
            cid = int(c["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[cid] = Course(cid, clean_course_name(str(c.get("fullname") or c.get("shortname") or f"课程 {cid}")))
    return out


def parse_course_cards(html: str) -> list[Course]:
    """从任意页面 HTML 里提取课程链接（course/view.php?id=N）。"""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[int, Course] = {}
    for a in soup.select('a[href*="course/view.php?id="]'):
        qs = parse_qs(urlparse(a.get("href", "")).query)
        try:
            cid = int(qs["id"][0])
        except (KeyError, ValueError):
            continue
        if cid <= 1:  # 1 是站点首页本身
            continue
        name = a.get_text(" ", strip=True)
        if not name:
            heading = a.select_one("h5, h6, .coursename") or a.find_next(["h5", "h6"])
            name = heading.get_text(" ", strip=True) if heading else ""
        if name:
            out.setdefault(cid, Course(cid, clean_course_name(name)))
    return list(out.values())


# ---------------------------------------------------------------- 作业列表

def fetch_course_assignments(http: MoodleHTTP, course_id: int) -> list[AssignmentBrief]:
    resp = http.get(f"{http.base_url}/course/view.php?id={course_id}")
    if "/enrol/" in str(resp.url):
        # 课程还没"自助选课"加入，被挡在选课页
        raise NotEnrolled(f"course {course_id} 未加入（停在 {resp.url}）")
    return parse_course_assignments(resp.text)


def parse_course_assignments(html: str) -> list[AssignmentBrief]:
    """解析课程页里的作业活动（modtype_assign），测验/资源等一律忽略。"""
    soup = BeautifulSoup(html, "html.parser")
    out: list[AssignmentBrief] = []
    seen: set[int] = set()
    for li in soup.select("li.activity.modtype_assign, li.activity.assign"):
        a = li.select_one('a[href*="/mod/assign/view.php"]')
        if a is None:
            continue
        cmid = _extract_cmid(a.get("href", "")) or _extract_cmid(li.get("id", ""))
        if not cmid or cmid in seen:
            continue
        seen.add(cmid)
        name_el = li.select_one(".instancename")
        if name_el is not None:
            for hide in name_el.select(".accesshide"):
                hide.decompose()
            name = name_el.get_text(" ", strip=True)
        else:
            name = a.get_text(" ", strip=True)
        out.append(AssignmentBrief(cmid, name or f"作业 #{cmid}", a.get("href", "")))
    return out


def _extract_cmid(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"module-(\d+)", text)
    if m:
        return int(m.group(1))
    qs = parse_qs(urlparse(text).query)
    if "id" in qs:
        try:
            return int(qs["id"][0])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 作业详情

_DUE_LABELS = ("截止日期", "截止时间", "到期时间", "due date")
_STATUS_LABELS = ("提交状态", "作业状态", "submission status")
_REMAIN_RE = [
    re.compile(r"(?:仅剩|还剩)\s*(\d+)\s*天\s*(\d+)\s*小时"),
    re.compile(r"(?:仅剩|还剩)\s*(\d+)\s*小时\s*(\d+)\s*分钟"),
    re.compile(r"(?:仅剩|还剩)\s*(\d+)\s*天"),
    re.compile(r"(?:仅剩|还剩)\s*(\d+)\s*小时"),
    re.compile(r"(?:仅剩|还剩)\s*(\d+)\s*分钟"),
]


# 平台的 pluginfile 链接有两种：老师随作业发布的资料
# （`mod_assign/introattachment/…`、`mod_resource/content/…`），以及**你自己交上去的
# 文件**（`assignsubmission_file/submission_files/…`）。归档只收前者——后者是你的
# 作业，不是要下载的资料。
_SUBMISSION_MARK = "assignsubmission_file"


def parse_attachments(html: str) -> list[Attachment]:
    """挑出作业页里**老师发布的**可下载文件（排除自己提交的作业文件）。"""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Attachment] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="pluginfile.php"]'):
        href = a.get("href", "")
        if not href or href in seen or _SUBMISSION_MARK in href:
            continue
        seen.add(href)
        name = a.get_text(" ", strip=True)
        if not name:
            name = unquote(href.split("?", 1)[0].rsplit("/", 1)[-1])
        out.append(Attachment(name=name.strip(), url=href))
    return out


def fetch_assign_detail(http: MoodleHTTP, url: str) -> AssignmentDetail:
    resp = http.get(url)
    return parse_assign_detail(resp.text)


def parse_assign_detail(html: str) -> AssignmentDetail:
    """从作业详情页解析截止日期与提交状态。

    平台的主题把状态表里的截止行去掉了，但保留了 Moodle 5 的
    activity-dates 区块（截至: 2026年10月18日 星期日 00:00）；三层策略：
    activity-dates → 标准提交状态表（标准 Moodle 主题兼容）→ 剩余时间反推。
    """
    soup = BeautifulSoup(html, "html.parser")
    detail = AssignmentDetail()

    # 1) activity-dates 区块（Moodle 5 页面头部，在 region-main 之外）
    for item in soup.select(".activity-dates .date-item"):
        label_el = item.select_one(".date-item-label")
        label = label_el.get_text(strip=True) if label_el else ""
        value = item.get_text(" ", strip=True)
        if label:
            value = value.replace(label, "", 1).strip()
        if detail.due_text is None and ("截至" in label or "截止" in label or "due" in label.lower()):
            detail.due_text = value or None
            detail.due_ts = parse_moodle_date(value)

    # 2) 标准提交状态表：状态解析；截止仅在未找到时解析
    for table in soup.select("table"):
        for tr in table.select("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).rstrip(":： ").lower()
            value = cells[1].get_text(" ", strip=True)
            if detail.due_text is None and any(k in label for k in _DUE_LABELS):
                detail.due_text = value or None
                detail.due_ts = parse_moodle_date(value)
            elif any(k in label for k in _STATUS_LABELS):
                low = value.lower()
                if "已提交" in value or "submitted for grading" in low:
                    detail.submitted = True
                elif (
                    "未提交" in value
                    or "no attempt" in low
                    or "草稿" in value
                    or "not submitted" in low
                ):
                    detail.submitted = False

    # 3) 剩余时间反推（部分页面两处都没有截止行时；精度受轮询间隔限制）
    if detail.due_ts is None:
        text = soup.get_text(" ", strip=True)
        now = datetime.now()
        for pat in _REMAIN_RE:
            m = pat.search(text)
            if m:
                vals = [int(x) for x in m.groups()]
                if "天" in m.group(0) and "小时" in m.group(0):
                    delta = timedelta(days=vals[0], hours=vals[1])
                elif "小时" in m.group(0) and "分钟" in m.group(0):
                    delta = timedelta(hours=vals[0], minutes=vals[1])
                elif "天" in m.group(0):
                    delta = timedelta(days=vals[0])
                elif "小时" in m.group(0):
                    delta = timedelta(hours=vals[0])
                else:
                    delta = timedelta(minutes=vals[0])
                due = (now + delta).replace(second=0, microsecond=0)
                detail.due_ts = due
                detail.due_text = f"{due:%Y年%m月%d日 %H:%M}（按剩余时间推算）"
                break

    # 提交状态：activity-dates 之外的状态表（部分页面只有这一处）
    if detail.submitted is None:
        low = soup.get_text(" ", strip=True)
        if "已提交请评分" in low or "submitted for grading" in low:
            detail.submitted = True

    # 老师随作业发布的附件（与截止/状态同页，顺手一起解析）
    detail.attachments = parse_attachments(html)
    return detail
