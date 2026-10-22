"""系统托盘：打开面板 / 立即轮询 / 暂停 / 退出，图标颜色反映会话状态。"""
from __future__ import annotations

import logging
import threading
import webbrowser

log = logging.getLogger(__name__)


def _icon_image(color: tuple[int, int, int]):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), outline=(28, 35, 51, 255), width=5)
    d.ellipse((20, 20, 44, 44), fill=color + (255,))
    return img


class Tray:
    def __init__(self, app, dashboard_url: str | None):
        import pystray

        self.app = app
        self.url = dashboard_url
        self._images = {
            "ok": _icon_image((58, 158, 99)),
            "bad": _icon_image((214, 69, 69)),
            "unknown": _icon_image((152, 160, 179)),
        }
        # 用 --no-panel 启动时没有面板，菜单里就不出现"打开作业面板"这一项
        items = []
        if dashboard_url:
            items.append(pystray.MenuItem("打开作业面板", self._open_dashboard, default=True))
        items.extend(
            [
                pystray.MenuItem("立即轮询", self._poll_now, default=not dashboard_url),
                pystray.MenuItem(
                    lambda item: "恢复轮询" if self.app.paused else "暂停轮询",
                    self._toggle_pause,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._quit),
            ]
        )
        menu = pystray.Menu(*items)
        self.icon = pystray.Icon(
            "AssignmentRadar", self._images["unknown"], "AssignmentRadar · Moodle 作业雷达", menu
        )
        app.listeners.append(self._on_state)

    # ------------------------------------------------------------ 回调

    def _on_state(self, app) -> None:
        key = "ok" if app.session_ok else ("bad" if app.session_ok is False else "unknown")
        state_text = {True: "会话正常", False: "会话过期", None: "尚未检查"}[app.session_ok]
        try:
            self.icon.icon = self._images[key]
            self.icon.title = f"AssignmentRadar · {state_text}"
            if hasattr(self.icon, "update_icon"):
                self.icon.update_icon()
        except Exception:
            pass

    def _open_dashboard(self, icon=None, item=None) -> None:
        if self.url:
            webbrowser.open(self.url)

    def _poll_now(self, icon=None, item=None) -> None:
        threading.Thread(target=self.app.poll_once, name="manual-poll", daemon=True).start()

    def _toggle_pause(self, icon=None, item=None) -> None:
        self.app.paused = not self.app.paused
        self._on_state(self.app)

    def _quit(self, icon=None, item=None) -> None:
        self.app.shutdown()
        icon.stop()

    def run(self) -> None:
        self.icon.run()
