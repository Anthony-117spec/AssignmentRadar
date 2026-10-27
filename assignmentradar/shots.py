"""作业页整页截图：给每份作业留一张「布置页面」的完整截图。

为什么需要它
------------
归档是"程序替你读网页"——读漏了、读错了，学生从文件夹里根本看不出来。
留一张整页截图，相当于留了一份**原件复印件**：打开作业文件夹，附件和页面截图并排，
一眼就能核对「老师发的东西是不是都下来了、程序有没有漏读」。

实现要点
--------
* 截图必须靠**真实浏览器渲染**（`requests` 拿到的 HTML 没有版面），所以走 CDP。
* 因此需要一个**已登录的私有配置目录浏览器**——`radar.bat browser login` 登录过一次即可。
  会话过期时渲染出来的是登录页；那种截图毫无意义，所以**预检不通过就直接放弃**，
  绝不往归档里塞一堆登录页。
* 与附件一样是**幂等**的：同名截图已存在就跳过，重跑不花时间。
* 截图文件名以 `_` 开头，在文件夹里排在最前面，方便一眼看到。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import attachments as att
from .cdp import BrowserSession, CdpError

log = logging.getLogger(__name__)

# 以 _ 开头 → 在文件夹里排最前，打开就能看到
SHOT_NAME = "_作业页截图.png"

# 导航后多等一会儿，让图片/样式都渲染完再截图（整页截图最怕半张白图）
SETTLE_SECONDS = 1.5


@dataclass
class ShotTask:
    """一份作业的布置页面。"""

    course: str
    assignment: str
    url: str


@dataclass
class ShotSummary:
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    aborted: str = ""  # 非空表示中途放弃（例如会话失效）
    paths: list[Path] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"新截图 {self.saved}", f"已存在跳过 {self.skipped}"]
        if self.failed:
            parts.append(f"失败 {self.failed}")
        if self.aborted:
            parts.append(f"中断（{self.aborted}）")
        return "，".join(parts)


def dest_for(root: Path, course: str, assignment: str,
             course_dirs: dict[str, str] | None = None) -> Path:
    """截图与附件放在同一个作业文件夹里，文件名固定为 `_作业页截图.png`。"""
    return att.dest_for(root, course, assignment, SHOT_NAME, course_dirs)


def logged_in(browser: BrowserSession) -> bool:
    """预检：私有目录里的浏览器是否还登录着（只读，不导航）。"""
    try:
        return browser.snapshot(navigate=False).logged_in
    except CdpError:
        return False


def capture(browser: BrowserSession, tasks: list[ShotTask], root: Path, *,
            course_dirs: dict[str, str] | None = None,
            force: bool = False,
            settle: float = SETTLE_SECONDS,
            on_event=None) -> ShotSummary:
    """逐份作业导航并截整页图。

    调用方负责把 `browser` 起好（已连上 CDP），并保证私有目录已登录平台。
    中途发现会话掉了会立刻停下——宁可少几张，也不留一沓登录页冒充截图。
    """
    summary = ShotSummary()
    for task in tasks:
        dest = dest_for(root, task.course, task.assignment, course_dirs)
        if dest.exists() and not force:
            summary.skipped += 1
            continue
        try:
            browser.navigate(task.url, settle=settle)
            # 每一页都确认一次登录态：会话中途失效时，后面的截图会全是登录页
            if not logged_in(browser):
                summary.aborted = "会话已失效（截图会变成登录页）"
                if on_event:
                    on_event(f"  ! {summary.aborted}，停止截图")
                break
            blob = browser.screenshot(full_page=True)
        except CdpError as exc:
            log.warning("截图失败 %s：%s", task.url, exc)
            summary.failed += 1
            if on_event:
                on_event(f"  ! 截图失败：{task.course} / {task.assignment}（{exc}）")
            continue
        if not blob:
            summary.failed += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            tmp.write_bytes(blob)
            os.replace(tmp, dest)
        except OSError as exc:
            log.warning("截图写入失败 %s：%s", dest, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            summary.failed += 1
            continue
        summary.saved += 1
        summary.paths.append(dest)
        if on_event:
            on_event(f"  📷 {dest.relative_to(root)}")
    return summary
