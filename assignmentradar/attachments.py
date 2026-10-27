"""作业附件归档：把老师随作业发布的附件按「课程 / 作业」下载到本地。

为什么单独一个模块
------------------
「识别哪些链接是附件」是页面解析的事（`moodle.parse_attachments`），
「把它们按科目摆到磁盘上」是文件系统的事——本模块只管后者，两边互不污染。

目录约定
--------
    <homework_dir>/<科目文件夹>/<作业名>/<附件名>

`<科目文件夹>` 优先取 `config.toml` 的 `[course_dirs]` 映射——学生往往**已经**按科目
建好了文件夹（`D:/我的作业/web`、`.../db`…），映射能让附件直接进那个现成的
文件夹，而不是另起一个名字对不上的新文件夹。没映射的课程才按课名自动新建。

作业再分一层，避免同一门课不同作业出现同名文件互相覆盖。

设计要点
--------
* **幂等**：目标文件已存在就跳过（`--force` 可强制重下）。所以随时可重跑，
  也可以挂在每轮轮询上，代价极小——正常情况下一个请求都不发。
* **文件名安全**：替换 Windows 非法字符、去掉结尾的点和空格、限长，
  并挡住 `..` 之类的路径穿越。
* **不认登录页**：会话过期时平台会把 pluginfile 请求重定向到登录页，
  返回的是 HTML 而不是文件。这种一律判为失败，绝不把 HTML 存成附件。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import moodle, wsapi

log = logging.getLogger(__name__)

# 默认归档目录（相对项目根目录）
DEFAULT_DIRNAME = "homework"

# Windows 文件名里不允许出现的字符，外加控制字符
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 令牌通路的 pluginfile 链接把 ws_token 放在 query 里；日志里必须抹掉，
# 否则一次下载失败就会把长期令牌原样写进 radar.log。
_TOKEN_QUERY = re.compile(r"(?i)([?&]token=)[^&\s]+")


def _redact_url(url: str) -> str:
    return _TOKEN_QUERY.sub(r"\1<redacted>", url or "")

# 会话过期时平台会把这些请求重定向到登录页，返回 HTML——绝不能当成附件存下来
_HTML_HEAD = (b"<!doctype", b"<html", b"<?xml")


def safe_name(text: str, *, fallback: str = "未命名", maxlen: int = 80) -> str:
    """把任意字符串收拾成安全的文件/目录名（跨平台）。"""
    name = _ILLEGAL.sub("_", (text or "").strip())
    name = name.replace("\n", " ").replace("\r", " ")
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")  # Windows 不允许结尾是点或空格
    if name in ("", ".", ".."):
        return fallback
    if len(name) > maxlen:
        # 保住扩展名，截断主干
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            keep = maxlen - len(ext) - 1
            name = stem[:keep].rstrip(". ") + "." + ext
        else:
            name = name[:maxlen].rstrip(". ")
    return name or fallback


def normalize_course(name: str) -> str:
    """去掉课名末尾的学期后缀：`Web 应用开发(26-27上学期)` → `Web 应用开发`。

    平台的课名带着学期分组，每学期都变；映射表的键按"裸课名"写，换学期就不用改。
    """
    return re.sub(r"[(（][^)）]*[)）]\s*$", "", (name or "").strip()).strip()


def resolve_subject(course: str, course_dirs: dict[str, str] | None = None) -> tuple[str, bool]:
    """把平台课程名解析成本地科目文件夹名。

    返回 `(文件夹名, 是否来自显式映射)`。先按完整课名查，再按去掉学期后缀的裸课名查；
    都没命中就退回裸课名（会在归档根目录下新建一个同名文件夹）。
    """
    mapping = course_dirs or {}
    for key in (course, normalize_course(course)):
        folder = mapping.get(key)
        if folder and str(folder).strip():
            return safe_name(str(folder), fallback=key or "未命名课程"), True
    fallback = normalize_course(course) or course
    return safe_name(fallback, fallback="未命名课程"), False


def subject_folder(course: str, course_dirs: dict[str, str] | None = None) -> str:
    """只要文件夹名。"""
    return resolve_subject(course, course_dirs)[0]


def dest_for(root: Path, course: str, assignment: str, filename: str,
             course_dirs: dict[str, str] | None = None) -> Path:
    """算出一个附件应该落到哪里：<root>/<科目文件夹>/<作业>/<文件>。"""
    return (
        root
        / subject_folder(course, course_dirs)
        / safe_name(assignment, fallback="未命名作业")
        / safe_name(filename, fallback="attachment")
    )


@dataclass
class DownloadSummary:
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    paths: list[Path] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.saved + self.skipped + self.failed

    def describe(self) -> str:
        parts = [f"新下载 {self.saved}", f"已存在跳过 {self.skipped}"]
        if self.failed:
            parts.append(f"失败 {self.failed}")
        return "，".join(parts)


def _looks_like_html(blob: bytes) -> bool:
    head = blob[:64].lstrip().lower()
    return any(head.startswith(mark) for mark in _HTML_HEAD)


def _fetch(http, url: str) -> bytes:
    """取回文件内容。

    优先用 `MoodleHTTP.get_file`：它**不做"登录页"判定**。带 token 的
    `webservice/pluginfile.php` 在 token 失效时回 403，若走 `get()`，
    `_check_expired` 会把它误判成"会话过期"，进而触发一整轮重新登录——错误归因。
    （测试替身只有 `get` 时退回它。）
    """
    getter = getattr(http, "get_file", None) or http.get
    return getter(url).content


def download_one(http, url: str, dest: Path, *, force: bool = False) -> str:
    """下载单个附件；返回 'saved' / 'skipped' / 'failed'。"""
    if dest.exists() and not force:
        return "skipped"
    try:
        blob = _fetch(http, url)
    except Exception:
        log.warning("附件下载失败：%s", _redact_url(url), exc_info=True)
        return "failed"
    if not blob:
        log.warning("附件内容为空：%s", _redact_url(url))
        return "failed"
    if _looks_like_html(blob):
        # 多半是会话过期被弹到登录页——不能把 HTML 当附件存
        log.warning("附件请求返回的是网页（会话可能已过期）：%s", _redact_url(url))
        return "failed"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 先写 .part 再原子替换：直接 write_bytes 中途失败会留下半份文件，
    # 下次运行看到 dest.exists() 就永远跳过了，归档里一直是一份坏文件。
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, dest)
    except OSError as exc:
        log.warning("附件写入失败 %s：%s", dest, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return "failed"
    return "saved"


@dataclass
class Item:
    """一个待归档的附件（带上它属于哪门课、哪个作业）。"""

    course: str
    assignment: str
    name: str
    url: str


def _collect_via_ws(ws, *, semester=None, course_filter=None, only_cmids=None,
                    on_event=None, on_assignment=None) -> list[Item]:
    """令牌通路的收集：附件来自 API 的 `introattachments`，一个作业页都不用抓。

    与 HTML 路径的契约完全一致——`on_assignment` 依然对**每份作业**回调一次
    （含没有附件的），截图清单靠它攒。
    """
    say = on_event or (lambda _m: None)
    items: list[Item] = []
    courses = ws.courses(semester)
    by_course = ws.assignments([c.course_id for c in courses])
    for course in courses:
        if course_filter and course_filter not in course.name:
            continue
        for a in by_course.get(course.course_id, []):
            if only_cmids is not None and a.cmid not in only_cmids:
                continue
            if on_assignment:
                on_assignment(course.name, a.name, a.url)
            for att in a.attachments:
                items.append(Item(course.name, a.name, att.name, att.url))
    if not items:
        say("  （令牌通路：本学期没有找到老师发布的附件）")
    return items


def collect(http, *, semester: str | None = None,
            course_filter: str | None = None,
            only_cmids: set[int] | None = None,
            on_event=None,
            on_assignment=None,
            ws=None) -> list[Item]:
    """走一遍课程与作业，收集所有老师附件。

    `course_filter` 按子串筛选课程名；`only_cmids` 限定只处理这些作业（用于"只下未交的"）。
    `on_assignment(course, assignment, url)` 会在**每份作业**上回调一次（含没有附件的），
    调用方可以借它攒出"所有作业页"的清单——整页截图要的就是这份清单。

    `ws`（`WsApi`）非空时优先走**令牌通路**：不依赖会话，附件来自 API 的
    `introattachments`。不可用时自动退回页面解析（旧路保底）。
    """
    if ws is not None:
        try:
            return _collect_via_ws(
                ws, semester=semester, course_filter=course_filter,
                only_cmids=only_cmids, on_event=on_event, on_assignment=on_assignment,
            )
        except (wsapi.WsUnavailable, wsapi.WsError) as exc:
            if on_event:
                on_event(f"  ! 令牌通路不可用（{exc}），改用页面解析")

    def say(msg: str) -> None:
        if on_event:
            on_event(msg)

    items: list[Item] = []
    courses = moodle.discover_courses(http, semester=semester)
    for course in courses:
        if course_filter and course_filter not in course.name:
            continue
        try:
            briefs = moodle.fetch_course_assignments(http, course.course_id)
        except moodle.NotEnrolled:
            continue
        except Exception as exc:
            say(f"  ! {course.name}：作业列表失败（{exc}）")
            continue
        for brief in briefs:
            if only_cmids is not None and brief.cmid not in only_cmids:
                continue
            if on_assignment:
                on_assignment(course.name, brief.name, brief.url)
            try:
                detail = moodle.fetch_assign_detail(http, brief.url)
            except Exception as exc:
                say(f"  ! {brief.name}：详情失败（{exc}）")
                continue
            for att in detail.attachments:
                items.append(Item(course.name, brief.name, att.name, att.url))
    return items


def download(http, items: list[Item], root: Path, *, force: bool = False,
             course_dirs: dict[str, str] | None = None,
             on_event=None) -> DownloadSummary:
    """把收集到的附件逐个下载到 <root>/<科目文件夹>/<作业>/ 下。"""
    summary = DownloadSummary()
    for it in items:
        dest = dest_for(root, it.course, it.assignment, it.name, course_dirs)
        status = download_one(http, it.url, dest, force=force)
        if status == "saved":
            summary.saved += 1
            summary.paths.append(dest)
            if on_event:
                on_event(f"  + {dest.relative_to(root)}")
        elif status == "skipped":
            summary.skipped += 1
        else:
            summary.failed += 1
            if on_event:
                on_event(f"  ! 下载失败：{it.course} / {it.assignment} / {it.name}")
    return summary
