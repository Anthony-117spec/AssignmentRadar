"""SQLite 状态存储：课程、作业、已发提醒、轮询日志。"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    course_id   INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    cmid        INTEGER PRIMARY KEY,
    course_id   INTEGER NOT NULL,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    due_ts      TEXT,
    due_text    TEXT,
    submitted   INTEGER,
    first_seen  TEXT NOT NULL,
    last_checked TEXT,
    notified_new INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
    cmid    INTEGER NOT NULL,
    hours   INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (cmid, hours)
);
-- 评分：一条 = 平台成绩册里的一个评分项。
-- 用**独立表**而不是给 assignments 加列：已有 state.db 不用迁移（CREATE IF NOT EXISTS 就够），
-- 而且成绩册里还有"课程总分"这种不对应任何作业的项（cmid 为 NULL）。
CREATE TABLE IF NOT EXISTS grades (
    item_id      INTEGER PRIMARY KEY,
    course_id    INTEGER NOT NULL,
    cmid         INTEGER,
    itemname     TEXT,
    itemmodule   TEXT,
    grade_raw    REAL,
    grade_max    REAL,
    grade_text   TEXT,
    graded_at    TEXT,
    submitted_at TEXT,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS poll_log (
    ts     TEXT NOT NULL,
    ok     INTEGER NOT NULL,
    detail TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


class Store:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        # list 命令的自动刷新可能与常驻实例的轮询并发写库，等待而非报错
        self._conn.execute("PRAGMA busy_timeout = 10000")
        # WAL：读写不再互斥到"整个库锁住"的程度——常驻轮询在写时，`radar.bat list`
        # 的读能立刻拿到旧快照，而不是干等 busy_timeout 走完。模式是持久的，
        # 写一次就够；网络盘不支持时会失败，退回默认日志模式即可。
        try:
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode and str(mode[0]).lower() != "wal":
                log.warning("SQLite 未能切换到 WAL（当前 %s），继续用默认日志模式", mode[0])
        except sqlite3.Error as exc:
            log.warning("切换 WAL 失败，继续用默认日志模式：%s", exc)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    # ------------------------------------------------------------ courses

    def upsert_course(self, course_id: int, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO courses(course_id, name, first_seen, last_seen) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(course_id) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen",
                (course_id, name, _now(), _now()),
            )
            self._conn.commit()

    def course_name(self, course_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM courses WHERE course_id=?", (course_id,)
            ).fetchone()
            return row["name"] if row else None

    # ------------------------------------------------------------ assignments

    def get_assignment(self, cmid: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM assignments WHERE cmid=?", (cmid,)
            ).fetchone()

    def insert_assignment(
        self,
        cmid: int,
        course_id: int,
        name: str,
        url: str,
        *,
        notified_new: bool,
        detail=None,
    ) -> None:
        due_ts = _iso(detail.due_ts) if detail else None
        due_text = detail.due_text if detail else None
        submitted = None if detail is None or detail.submitted is None else int(detail.submitted)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO assignments"
                "(cmid, course_id, name, url, due_ts, due_text, submitted, first_seen, last_checked, notified_new) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cmid, course_id, name, url, due_ts, due_text, submitted,
                    _now(), _now() if detail else None, int(notified_new),
                ),
            )
            self._conn.commit()

    def update_detail(self, cmid: int, detail) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE assignments SET due_ts=?, due_text=?, submitted=?, last_checked=? WHERE cmid=?",
                (
                    _iso(detail.due_ts), detail.due_text,
                    None if detail.submitted is None else int(detail.submitted),
                    _now(), cmid,
                ),
            )
            self._conn.commit()

    def update_due(self, cmid: int, due_ts: datetime | None, due_text: str | None) -> None:
        """只改截止时间，**不碰 `submitted`，也不动 `last_checked`**。

        令牌通路一次批量调用就能拿到所有作业的截止时间，可以顺手修正截止变更；
        而提交状态要靠逐份调用，走的是另一条复查队列。两者**不能互相顶掉对方的进度**：
        `update_detail` 会把 `last_checked` 推到当前时刻，若拿它来做"只改截止"，
        那条作业就会被踢出复查队列最长 24 小时，提交状态反而变旧。
        """
        with self._lock:
            self._conn.execute(
                "UPDATE assignments SET due_ts=?, due_text=? WHERE cmid=?",
                (_iso(due_ts), due_text, cmid),
            )
            self._conn.commit()

    def assignments_with_course(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.*, c.name AS course_name FROM assignments a "
                "LEFT JOIN courses c ON c.course_id = a.course_id "
                "ORDER BY c.name, a.due_ts"
            ).fetchall()
            return [dict(r) for r in rows]

    def assignments_needing_detail(self, max_age: timedelta, limit: int = 15) -> list[sqlite3.Row]:
        """常规复查：超过 max_age 没查过，且「未截止 / 截止未知 / 已截止但尚未提交」。

        已截止且**已提交**的不再复查——那里已经没有新信息可获取了。

        ⚠ 这里**必须**包含「已截止但未提交」的作业。早期版本加了
        `due_ts >= now` 的过滤，等于把已截止作业永久排除在复查之外，于是
        「提交状态」会在截止那一刻被冻结（2026-10-08 实测踩坑：学生卡在截止前
        一小时交的作业，清单里一直显示「未提交」，把人吓一跳）。卡点交作业恰恰
        是最常见的情形，所以这条必须留着。
        """
        cutoff = (datetime.now() - max_age).isoformat(timespec="seconds")
        now = _now()
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM assignments a "
                "WHERE (a.last_checked IS NULL OR a.last_checked < :cutoff) "
                "  AND (a.due_ts IS NULL OR a.due_ts >= :now OR COALESCE(a.submitted, 0) = 0) "
                "ORDER BY COALESCE(a.last_checked, '1970-01-01T00:00:00') LIMIT :limit",
                {"cutoff": cutoff, "now": now, "limit": limit},
            ).fetchall()

    def assignments_near_deadline(
        self, window: timedelta, max_age: timedelta, limit: int = 15
    ) -> list[sqlite3.Row]:
        """截止时间落在 now ± window 内、且尚未提交的作业：用**更短**的 max_age 复查。

        学生大多卡在截止前最后几小时交作业，交完会立刻打开清单确认。这时若状态
        还是「未提交」，就会重演「我明明交了」的惊吓。所以这批作业的复查间隔要短
        得多——只在截止前后这一小段窗口内加密，平时不多打扰服务器。
        """
        now_dt = datetime.now()
        params = {
            "start": (now_dt - window).isoformat(timespec="seconds"),
            "end": (now_dt + window).isoformat(timespec="seconds"),
            "cutoff": (now_dt - max_age).isoformat(timespec="seconds"),
            "limit": limit,
        }
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM assignments a "
                "WHERE a.due_ts IS NOT NULL AND a.due_ts BETWEEN :start AND :end "
                "  AND COALESCE(a.submitted, 0) = 0 "
                "  AND (a.last_checked IS NULL OR a.last_checked < :cutoff) "
                "ORDER BY COALESCE(a.last_checked, '1970-01-01T00:00:00') LIMIT :limit",
                params,
            ).fetchall()

    # ------------------------------------------------------------ grades

    def upsert_grade(self, item) -> None:
        """写入/更新一个评分项。

        `item` 是 `wsapi.GradeItem`（这里只按鸭子类型用，测试替身不必真的构造它）。
        幂等：同一个 item_id 反复写只更新分数与时间——老师改分、补评分都能跟上。
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO grades(item_id, course_id, cmid, itemname, itemmodule,"
                " grade_raw, grade_max, grade_text, graded_at, submitted_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET "
                "  grade_raw=excluded.grade_raw, grade_max=excluded.grade_max,"
                "  grade_text=excluded.grade_text, graded_at=excluded.graded_at,"
                "  submitted_at=excluded.submitted_at, updated_at=excluded.updated_at",
                (
                    item.item_id, item.course_id, item.cmid, item.itemname, item.itemmodule,
                    item.grade_raw, item.grade_max, item.grade_text,
                    _iso(item.graded_at), _iso(item.submitted_at), _now(),
                ),
            )
            self._conn.commit()

    def grades_with_course(self, *, graded_only: bool = False) -> list[dict]:
        """评分项 + 课程名，按评分时间倒序。

        `graded_only=True` 只要**已经批改过**的（有评分时间，或有分数且不是 "-"）——
        终端那份"最近几次评分"要的就是这批；未批改的项显示出来只是噪音。
        """
        sql = (
            "SELECT g.*, c.name AS course_name FROM grades g "
            "LEFT JOIN courses c ON c.course_id = g.course_id "
        )
        if graded_only:
            sql += ("WHERE g.graded_at IS NOT NULL "
                    "OR (g.grade_raw IS NOT NULL AND COALESCE(g.grade_text, '-') <> '-') ")
        sql += "ORDER BY g.graded_at DESC, g.item_id DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    # ------------------------------------------------------------ reminders

    def pending_reminders(self, hours: int, now: datetime) -> list[sqlite3.Row]:
        """距截止 `hours` 小时内、未提交、未提醒过的作业。

        下界必须用**传入的 now**，不能拿 `_now()` 顶替：调用方传参考时刻
        （回放/测试/批量补发）时，两者不一致会让窗口整体错位。
        """
        horizon = (now + timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM assignments a "
                "WHERE a.due_ts IS NOT NULL AND a.due_ts > :now AND a.due_ts <= :horizon "
                "  AND COALESCE(a.submitted, 0) = 0 "
                "  AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.cmid = a.cmid AND r.hours = :hours) "
                "ORDER BY a.due_ts",
                {"now": now.isoformat(timespec="seconds"), "horizon": horizon, "hours": hours},
            ).fetchall()

    def record_reminder(self, cmid: int, hours: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO reminders(cmid, hours, sent_at) VALUES(?, ?, ?)",
                (cmid, hours, _now()),
            )
            self._conn.commit()

    def clear_reminders(self, cmid: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM reminders WHERE cmid=?", (cmid,))
            self._conn.commit()

    # ------------------------------------------------------------ poll log

    def poll_log(self, ok: bool, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO poll_log(ts, ok, detail) VALUES(?, ?, ?)",
                (_now(), int(ok), detail),
            )
            self._conn.commit()

    def latest_success_ts(self) -> str | None:
        """最近一次成功巡逻的时间（last_success 字段引入前的历史回填用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT ts FROM poll_log WHERE ok=1 ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return row["ts"] if row else None
