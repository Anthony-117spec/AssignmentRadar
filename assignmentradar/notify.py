"""通知发送：Windows Toast + ntfy + Server酱，全部走后台队列，绝不阻塞轮询。"""
from __future__ import annotations

import logging
import queue
import threading
import webbrowser
from dataclasses import dataclass

import requests

from .config import Config

log = logging.getLogger(__name__)


@dataclass
class Message:
    title: str
    body: str
    url: str | None = None


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._queue: queue.Queue[Message | None] = queue.Queue()
        self._toast_fn = self._load_toast() if cfg.notify.toast else None
        self._worker = threading.Thread(target=self._run, name="notify", daemon=True)
        self._worker.start()

    @staticmethod
    def _load_toast():
        try:
            from win11toast import toast

            return toast
        except Exception as exc:
            log.warning("win11toast 不可用，系统通知将只写入日志：%s", exc)
            return None

    def _run(self) -> None:
        while True:
            msg = self._queue.get()
            if msg is None:
                return
            try:
                self._send(msg)
            except Exception:
                log.exception("通知发送失败：%s", msg.title)
            finally:
                self._queue.task_done()

    def _send(self, msg: Message) -> None:
        log.info("通知 | %s | %s | %s", msg.title, msg.body, msg.url or "-")
        if self._toast_fn is not None:
            self._toast(msg)
        n = self.cfg.notify
        if n.ntfy_enabled and n.ntfy_topic:
            self._send_ntfy(msg)
        if n.serverchan_enabled and n.serverchan_sendkey:
            self._send_serverchan(msg)

    def _toast(self, msg: Message) -> None:
        def _on_click(*_args, **_kwargs):
            if msg.url:
                webbrowser.open(msg.url)

        try:
            # toast() 是同步函数（内部自跑事件循环）；on_click 参数异常时降级为纯展示
            try:
                self._toast_fn(msg.title, msg.body, duration="short", on_click=_on_click)
            except TypeError:
                self._toast_fn(msg.title, msg.body, duration="short")
        except Exception as exc:
            log.warning("Windows 通知失败：%s", exc)

    def _send_ntfy(self, msg: Message) -> None:
        payload: dict = {
            "topic": self.cfg.notify.ntfy_topic,
            "title": msg.title,
            "message": msg.body,
            "tags": ["bookmark"],
        }
        if msg.url:
            payload["click"] = msg.url
        try:
            requests.post(self.cfg.notify.ntfy_server, json=payload, timeout=15)
        except Exception as exc:
            log.warning("ntfy 推送失败：%s", exc)

    def _send_serverchan(self, msg: Message) -> None:
        desp = msg.body + (f"\n\n[打开作业页面]({msg.url})" if msg.url else "")
        try:
            requests.post(
                f"https://sctapi.ftqq.com/{self.cfg.notify.serverchan_sendkey}.send",
                data={"title": msg.title, "desp": desp},
                timeout=15,
            )
        except Exception as exc:
            log.warning("Server酱 推送失败：%s", exc)

    # ------------------------------------------------------------ 高层事件

    def new_assignment(self, course_name: str, name: str, due_text: str | None, url: str) -> None:
        due = f"，截止：{due_text}" if due_text else ""
        self._queue.put(Message(f"新作业 · {course_name}", f"{name}{due}", url))

    def due_changed(
        self, course_name: str, name: str, old_text: str | None, new_text: str | None, url: str
    ) -> None:
        self._queue.put(
            Message(
                f"截止时间变更 · {course_name}",
                f"{name}：{old_text or '未设置'} → {new_text or '未设置'}",
                url,
            )
        )

    def reminder(self, course_name: str, name: str, due_text: str | None, hours: int, url: str) -> None:
        self._queue.put(
            Message(
                f"即将截止 · {course_name}",
                f"《{name}》将在 {hours} 小时内截止（{due_text or '截止时间未知'}），快去看看",
                url,
            )
        )

    def session_expired(self, base_url: str) -> None:
        self._queue.put(
            Message(
                "AssignmentRadar：登录已过期",
                "自动续期失败，提醒已暂停。请在浏览器重新登录平台后运行 radar.bat import"
                "（一条命令即可恢复；若 SSO 也过期，用 radar.bat import --sso）",
                base_url,
            )
        )

    def sso_expired(self) -> None:
        self._queue.put(
            Message(
                "AssignmentRadar：SSO 会话已失效",
                "moodle 会话目前仍正常，但自动续期已不可用。请在浏览器登录统一身份认证、"
                "确认进入个人页后运行 radar.bat import --sso，以免下次轮询彻底断线",
            )
        )
