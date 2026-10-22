"""终端作业列表：未交（按 DDL 紧迫度）/ 已交 两栏，供 CLI 渲染。"""
from __future__ import annotations

import re
from datetime import datetime

# 显示时把课程名里的学期后缀去掉，省宽度
_SEM_SUFFIX = re.compile(r"\(2\d-\d{2}[上下]学期\)\s*$")


def short_course(name: str) -> str:
    return _SEM_SUFFIX.sub("", name or "").strip() or (name or "")


def _from_iso(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def fmt_due(ts: str | None) -> str:
    d = _from_iso(ts)
    return d.strftime("%m-%d %H:%M") if d else "──"


def fmt_left(due: datetime, now: datetime) -> str:
    """剩余时间的人话描述（负数=已截止多久）。"""
    sec = (due - now).total_seconds()
    future = sec >= 0
    sec = abs(sec)
    days, rem = divmod(int(sec), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days >= 1:
        text = f"{days} 天 {hours} 小时"
    elif hours >= 1:
        text = f"{hours} 小时 {minutes} 分"
    else:
        text = f"{minutes} 分钟"
    return f"剩 {text}" if future else f"已过 {text}"


def build_lists(rows: list[dict], now: datetime, ignore_before: datetime | None = None) -> tuple[list, list, list, list]:
    """把作业分成四组（未交按紧迫度排序，已交按截止倒序）。

    返回 (已截止未交, 未截止未交, 截止未知未交, 已提交)。
    submitted != 1 一律算"未交"——平台对未提交的作业常常不给确认标记，
    宁可多提醒也不能漏。
    ignore_before：截止早于该时刻的视为历史遗留，不进任何列表。
    """
    overdue: list[tuple[dict, datetime]] = []
    upcoming: list[tuple[dict, datetime]] = []
    unknown: list[dict] = []
    submitted: list[tuple[dict, datetime | None]] = []

    for r in rows:
        due = _from_iso(r.get("due_ts"))
        if ignore_before is not None and due is not None and due < ignore_before:
            continue
        if r.get("submitted") == 1:
            submitted.append((r, due))
        elif due is None:
            unknown.append(r)
        elif due <= now:
            overdue.append((r, due))
        else:
            upcoming.append((r, due))

    # 已截止：最近截止的排最前（最可能还能补交）；未截止：越早截止越靠前
    overdue.sort(key=lambda x: x[1], reverse=True)
    upcoming.sort(key=lambda x: x[1])
    submitted.sort(key=lambda x: (x[1] is None, x[1] or datetime.min), reverse=True)
    return overdue, upcoming, unknown, submitted


def fmt_duration(seconds) -> str:
    """把刷新耗时说成人话：`30.4 秒` / `1 分 12 秒`；未知或非法值给空串。

    存进台账的是字符串（meta 表只存文本），所以这里要能吃 str / float / None。
    """
    try:
        sec = float(seconds)
    except (TypeError, ValueError):
        return ""
    if sec < 0:
        return ""
    if sec < 60:
        return f"{sec:.1f} 秒"
    minutes, rest = divmod(int(round(sec)), 60)
    return f"{minutes} 分 {rest} 秒"


def fmt_refresh_path(path: str | None) -> str:
    """刷新走的是哪条通路——用来解释"这次为什么快/慢"。"""
    return {"token": "令牌通路", "html": "页面通路"}.get((path or "").strip(), "")


def urgency_mark(due: datetime, now: datetime) -> tuple[str, str]:
    """(标记, 颜色名)：红=24h 内，黄=72h 内，绿=更远。"""
    hours = (due - now).total_seconds() / 3600
    if hours <= 24:
        return "⏰", "red"
    if hours <= 72:
        return "🔥", "yellow"
    return "  ", "green"


# ------------------------------------------------------------------ 评分

def is_graded(row: dict) -> bool:
    """这个评分项批改过没有。

    Moodle 对未批改的项给 `gradeformatted = "-"` 且不给评分时间；有的项（如课程总分）
    只有分数、没有评分时间。两者都算"已批改"。
    """
    if row.get("graded_at"):
        return True
    return row.get("grade_raw") is not None and (row.get("grade_text") or "-") != "-"


def fmt_score(row: dict) -> str:
    """分数显示文本：`100.00/100`。满分未知时只给分数。"""
    text = (row.get("grade_text") or "").strip()
    if not text or text == "-":
        raw = row.get("grade_raw")
        if raw is None:
            return "—"
        text = f"{raw:g}"
    max_grade = row.get("grade_max")
    return f"{text}/{max_grade:g}" if max_grade else text


def build_grade_rows(rows: list[dict], per_course: int = 3) -> list[tuple[str, list[dict]]]:
    """把评分项按课程分组，每门课只留**最近 per_course 条**。

    只收**已批改**且**挂在某个活动上**的项：
    * 未批改的（分数是 `"-"`）显示出来只是噪音；
    * **课程总分**那一项（`cmid` 为空）不是"某一次作业的评分"，不在这个列表里。

    返回 `[(课程名, [评分项, …]), …]`，课程之间按"最近一次评分时间"倒序，
    所以刚被批改的课排在最上面。
    """
    by_course: dict[str, list[dict]] = {}
    for r in rows:
        if not r.get("cmid"):  # 课程总分项
            continue
        if not is_graded(r):
            continue
        name = r.get("course_name") or f"课程 {r.get('course_id')}"
        by_course.setdefault(name, []).append(r)

    grouped: list[tuple[str, list[dict]]] = []
    for name, items in by_course.items():
        items.sort(key=lambda r: (r.get("graded_at") or "", r.get("item_id") or 0), reverse=True)
        grouped.append((name, items[:per_course]))
    # 最近有评分的课程排前面
    grouped.sort(key=lambda kv: kv[1][0].get("graded_at") or "", reverse=True)
    return grouped
