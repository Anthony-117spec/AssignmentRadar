"""用真实浏览器给会话续命：SSO 还活着时，用户一个字都不用输。

为什么需要它
------------
平台的会话大约 10~12 小时就过期，而 `requests` 那条自动续期链路走不通：

* 网关改过登录入口——`/rump_frontend/login/` 现在直接返回 **404**（实测）；
* 就算入口还在，回调也不会把匿名票据升级成登录态（第 4.3 节那个"最后一公里"）。

**但真实浏览器能走通**：网关认浏览器。所以只要私有配置目录里的 SSO 会话还活着，
让浏览器访问一次平台，网关就会**静默**重新登录——用户不用输任何东西。

本模块就做这一步：无头打开私有目录 → 导航平台 → 取回新会话写回 config。
SSO 也过期了就如实返回失败，由上层提示用户跑 `radar browser login`（那是唯一的出路）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config, set_cookie

log = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    ok: bool
    reason: str = ""
    cookies: int = 0


def refresh_via_browser(cfg: Config, *, timeout: float = 60.0) -> RefreshResult:
    """无头打开私有配置目录的浏览器，导航平台，把拿到的会话写回 config。

    成功时 `cfg.cookie` / `cfg.sso_cookie` 会就地更新（调用方据此重建 HTTP 会话）。
    任何一步不可用都只返回失败原因，**不抛异常**——续期失败不该打断轮询。
    """
    from .browser import find_browser
    from .cdp import BrowserSession, CdpError

    exe = find_browser()
    if exe is None:
        return RefreshResult(False, "没找到 Chrome / Edge")

    session = BrowserSession(cfg.base_dir, cfg.base_url, executable=exe)
    try:
        session.start(headless=True, timeout=timeout)
    except CdpError as exc:
        session.stop()  # 兜底：别让半启动的 Chrome 占着 profile
        return RefreshResult(False, f"起浏览器失败：{exc}")

    try:
        snap = session.snapshot()  # 导航 /my/ 并判定登录态
        if not snap.logged_in or not snap.cookie:
            return RefreshResult(False, "浏览器里没有登录态（SSO 也过期了）")
        count = set_cookie(cfg.base_dir, snap.cookie)
        cfg.cookie = snap.cookie
        if snap.sso_cookie:
            set_cookie(cfg.base_dir, snap.sso_cookie, field="sso_cookie")
            cfg.sso_cookie = snap.sso_cookie
        log.info("已用浏览器静默续期（%d 个条目）", count)
        return RefreshResult(True, "已取回新会话", count)
    except CdpError as exc:
        return RefreshResult(False, f"读取失败：{exc}")
    finally:
        session.stop()
