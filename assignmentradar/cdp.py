"""通过 Chrome DevTools Protocol 读取浏览器会话 cookie。

为什么需要它（而不是直接解 cookie 库）
--------------------------------------
Chrome 从 127 版起对 cookie 启用了 **App-Bound Encryption**：cookie 密文前缀从
`v10` 变成 `v20`，密钥由 `Local State` 的 `app_bound_encrypted_key` 包着，且那层
包装用的是 **系统级 DPAPI**——解密需要 `LOCAL_SYSTEM` 权限。也就是说：

* 旧的 `v10` 路线（`cookies.py` 里的 DPAPI + AES-GCM）对 `v20` **彻底失效**；
* 想硬解得提权，既脆弱又不该做。

而 Chrome 自己就能把 cookie 明文交出来——只要通过 **DevTools Protocol**（CDP）。
本模块就是这条路的实现：起一个 Chrome 实例（或附着到已开的实例），用
`Network.getAllCookies` 取回 cookie，**完全绕开加密问题**，不需要任何权限。

唯一约束
--------
Chrome 出于安全考虑，**拒绝对「默认」用户数据目录开远程调试**（会直接报
"DevTools remote debugging requires a non-default data directory"）。
所以本模块使用**项目私有目录** `data/browser_profile`——这正好也符合
"绝不碰用户日常浏览器"的设计原则。

实现只用到标准库：起进程用 `subprocess`，CDP 用 `socket` 手写 WebSocket 帧
（协议很简单：客户端帧必须掩码，服务端帧不掩码），HTTP 探测用 `urllib`。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .browser import PROFILE_DIRNAME

log = logging.getLogger(__name__)

# 可用的调试端口范围（挑一个空闲的即可）
_PORT_RANGE = range(9333, 9360)

# CDP 命令的默认超时（秒）
_CDP_TIMEOUT = 30


class CdpError(RuntimeError):
    """CDP 通信失败（起不来、连不上、命令报错）。"""


# ---------------------------------------------------------------- WebSocket


class _WebSocket:
    """极简 WebSocket 客户端：只够收发 JSON 文本帧（CDP 只用到这些）。

    协议要点：
    * 客户端→服务端的数据帧**必须**加掩码，服务端→客户端**必须**不加；
    * 长度 < 126 直接写在第二字节，< 65536 用 126 + 2 字节，否则 127 + 8 字节；
    * 这里只处理文本帧（opcode 1）与关闭帧（opcode 8），忽略 ping/pong。
    """

    def __init__(self, url: str, timeout: float = _CDP_TIMEOUT):
        u = urlparse(url)
        self._sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port or 80}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(req.encode())
        # 读完 HTTP 101 响应头（以空行结束）
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CdpError("WebSocket 握手失败：连接被关闭")
            buf += chunk
        if b"101" not in buf.split(b"\r\n", 1)[0]:
            raise CdpError(f"WebSocket 握手失败：{buf.split(b'\r\n', 1)[0]!r}")
        self._buf = buf.split(b"\r\n\r\n", 1)[1]

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + opcode 1(text)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str | None:
        """读一条文本帧；非文本帧（如 ping）被跳过；连接关闭返回 None。"""
        while True:
            while len(self._buf) < 2:
                chunk = self._sock.recv(65536)
                if not chunk:
                    return None
                self._buf += chunk
            b0, b1 = self._buf[0], self._buf[1]
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            off = 2
            if n == 126:
                while len(self._buf) < 4:
                    self._buf += self._sock.recv(65536)
                n = struct.unpack(">H", self._buf[2:4])[0]
                off = 4
            elif n == 127:
                while len(self._buf) < 10:
                    self._buf += self._sock.recv(65536)
                n = struct.unpack(">Q", self._buf[2:10])[0]
                off = 10
            while len(self._buf) < off + n:
                chunk = self._sock.recv(65536)
                if not chunk:
                    return None
                self._buf += chunk
            payload = self._buf[off:off + n]
            self._buf = self._buf[off + n:]
            if opcode == 0x8:
                return None
            if opcode != 0x1:
                continue  # ping/pong 等，忽略
            return payload.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


class CdpSession:
    """一条 CDP 连接，带自增的请求 id 与简单的同步等待。"""

    def __init__(self, ws_url: str, timeout: float = _CDP_TIMEOUT):
        self._ws = _WebSocket(ws_url, timeout=timeout)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self._ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            text = self._ws.recv_text()
            if text is None:
                raise CdpError(f"CDP 连接在等待 {method} 响应时关闭")
            try:
                msg = json.loads(text)
            except Exception:
                continue  # CDP 也会推事件，非 JSON 的忽略
            if msg.get("id") != mid:
                continue  # 事件或其他响应，跳过
            if "error" in msg:
                raise CdpError(f"{method} 失败：{msg['error'].get('message')}")
            return msg.get("result", {})

    def evaluate(self, expression: str) -> str:
        """在页面里执行一段 JS，返回其字符串结果。"""
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return (res.get("result") or {}).get("value", "")

    def close(self) -> None:
        self._ws.close()


# ---------------------------------------------------------------- 浏览器进程


def _free_port() -> int:
    for port in _PORT_RANGE:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise CdpError("没有找到空闲的调试端口")


def _cdp_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _find_page_target(port: int, prefer_host: str | None = None) -> dict | None:
    """取一个 page 类型的调试目标（browser 目标不支持 Network 域）。

    私有 profile 里可能残留多个标签页，所以给一个 `prefer_host` 时优先挑网址里
    含该主机的那个——避免连到无关的空白页。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as resp:
            targets = json.load(resp)
    except Exception:
        return None
    pages = [t for t in targets
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    if prefer_host:
        for t in pages:
            if prefer_host in (t.get("url") or ""):
                return t
    return pages[0]


def _debug_ports() -> list[int]:
    """本机所有可用的 CDP 调试端口（有 /json/version 响应的那些）。"""
    out: list[int] = []
    for port in _PORT_RANGE:
        if _cdp_ready(port):
            out.append(port)
    return out


def profile_dir(base_dir: Path) -> Path:
    """私有 profile 目录（与 browser.py 共用同一个，两者可互相接力）。"""
    return base_dir / "data" / PROFILE_DIRNAME


def profile_exists(base_dir: Path) -> bool:
    """私有 profile 是否已经建过（说明用户至少登录过一次）。"""
    return (profile_dir(base_dir) / "Local State").is_file()


@dataclass
class CookieSnapshot:
    """一次 CDP 读取的结果。"""

    cookie: str | None = None            # 可直接发给 moodle 的 cookie 串
    sso_cookie: str | None = None        # 可直接发给 sso 的 cookie 串
    logged_in: bool = False
    url: str = ""
    raw: list[dict] = field(default_factory=list)


# 只收参与身份认证的条目；Matomo 统计、主题偏好等一律丢弃
_AUTH_NAMES = {
    "MoodleSession", "my_client_ticket", "JSESSIONID",
    "cookie_session_id", "machineSerial", "login_type", "login_user",
}
# 按域归组时，这三个域的 cookie 都可以发给 moodle
_MOODLE_HOSTS = ("moodle.scnu.edu.cn", ".scnu.edu.cn", "scnu.edu.cn", "auth1.scnu.edu.cn")
_SSO_HOSTS = ("sso.scnu.edu.cn", "auth1.scnu.edu.cn")


def _pick(cookies: list[dict], hosts: tuple[str, ...]) -> str | None:
    parts: list[str] = []
    for c in cookies:
        if c.get("name") not in _AUTH_NAMES:
            continue
        if not any(c.get("domain", "").endswith(h.lstrip(".")) or c.get("domain") in hosts
                   for h in hosts):
            continue
        entry = f"{c['name']}={c['value']}"
        if entry not in parts:
            parts.append(entry)
    return "; ".join(parts) if parts else None


class BrowserSession:
    """管理一个带调试端口的 Chrome 实例，并从中读取 cookie。

    用法：

        with BrowserSession(base_dir, base_url) as bs:
            snap = bs.snapshot()          # 若已登录，snap.cookie 就是会话
    """

    def __init__(self, base_dir: Path, base_url: str, *, executable: str | None = None):
        self.base_dir = base_dir
        self.base_url = base_url.rstrip("/")
        self.executable = executable
        self.profile = profile_dir(base_dir)
        self.port: int | None = None
        self._proc: subprocess.Popen | None = None
        self._cdp: CdpSession | None = None
        self._attached = False

    # ------------------------------------------------------------ 生命周期

    def start(self, url: str | None = None, *, headless: bool = True,
              timeout: float = 45, window: bool = False) -> None:
        """起一个 Chrome 实例并连上 CDP。

        `headless=False` 会开一个**可见**窗口（用于用户手动登录）；
        `window=True` 则把可见窗口挪到屏幕外（用于后台刷新，避免打扰）。
        """
        if self.executable is None:
            raise CdpError("未找到 Chrome / Edge 可执行文件")
        self.profile.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        args = [
            self.executable,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-features=Translate",
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={self.port}",
        ]
        if headless:
            args += ["--headless=new", "--disable-gpu"]
        elif window:
            args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
        else:
            args += ["--window-size=1180,860"]
        args.append(url or "about:blank")

        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            self._wait_ready(timeout, prefer_url=url or f"{self.base_url}/my/")
        except CdpError:
            # ⚠ 必须收拾干净再抛：半启动的 Chrome 会一直占着私有 profile，
            # 于是**之后每一次**启动都失败（报"profile 被占用"），而且看起来
            # 像是环境问题、很难联想到是上一次留下的残骸。
            self.stop()
            raise

    def attach(self, *, url: str | None = None) -> bool:
        """附着到已经开着的调试实例（例如 `radar browser login` 留下的窗口）。

        成功返回 True 并把 `port` 设成该实例的端口；没找到返回 False。
        """
        ports = _debug_ports()
        if not ports:
            return False
        self.port = ports[0]
        # 让浏览器打开目标页（尽量用已有的标签页，不新开）
        self._wait_ready(20, prefer_url=url)
        self._attached = True
        return True

    def _wait_ready(self, timeout: float, *, prefer_url: str | None = None) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _cdp_ready(self.port):
                break
            if self._proc is not None and self._proc.poll() is not None:
                raise CdpError("浏览器进程提前退出（可能是 profile 被占用）")
            time.sleep(0.4)
        else:
            raise CdpError("等待浏览器调试端口超时")

        host = self.base_url.split("//", 1)[-1].split("/", 1)[0]
        target = None
        while time.monotonic() < deadline and target is None:
            target = _find_page_target(self.port, prefer_host=host)
            if target is None:
                time.sleep(0.4)
        if target is None:
            raise CdpError("没有找到可用的页面调试目标")
        self._cdp = CdpSession(target["webSocketDebuggerUrl"])
        # 只在这一刻（用户还没开始输入）纠正一次落点；之后轮询绝不再导航。
        if prefer_url and host not in (target.get("url") or ""):
            self.navigate(prefer_url)

    def stop(self) -> None:
        if self._cdp is not None:
            self._cdp.close()
            self._cdp = None
        # 附着模式不杀别人的进程，只断开连接
        if not self._attached and self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()
        self._proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # ------------------------------------------------------------ 读取

    @property
    def cdp(self) -> CdpSession:
        if self._cdp is None:
            raise CdpError("还没有连接 CDP（先调用 start()）")
        return self._cdp

    def navigate(self, url: str, *, settle: float = 3.0) -> None:
        """把当前页面导航到 url 并等待加载完成。"""
        self.cdp.call("Page.navigate", {"url": url})
        self._wait_load(settle=settle)

    def _wait_load(self, *, settle: float = 3.0) -> None:
        """等页面加载完成：优先等 `Page.loadEventFired`，超时再退化为固定等待。"""
        # CDP 事件与响应共用一条流，收事件最简单的方式是轮询 document.readyState，
        # 既不需要事件订阅，也不用担心漏帧。
        deadline = time.monotonic() + max(settle, 5.0)
        while time.monotonic() < deadline:
            try:
                state = self.evaluate("document.readyState")
            except CdpError:
                state = ""
            if state in ("interactive", "complete") and not self.evaluate(
                "location.href === 'about:blank' ? '1' : '0'"
            ) == "1":
                break
            time.sleep(0.3)
        time.sleep(settle)

    def cookies(self) -> list[dict]:
        return self.cdp.call("Network.getAllCookies").get("cookies", [])

    def evaluate(self, expression: str) -> str:
        return self.cdp.evaluate(expression)

    def screenshot(self, *, full_page: bool = True) -> bytes:
        """截取当前页面，返回 PNG 字节。

        `full_page=True` 时用 `captureBeyondViewport` 让 Chrome 按**内容实际高度**出图，
        而不是只截视口那一屏——作业页往往很长，只截一屏根本看不出布置了什么。
        """
        params: dict = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
        res = self.cdp.call("Page.captureScreenshot", params)
        data = res.get("data")
        if not data:
            raise CdpError("截图返回空数据")
        return base64.b64decode(data)

    def snapshot(self, *, navigate: bool = True) -> CookieSnapshot:
        """读一次登录态与 cookie；`navigate=True` 时先访问 `/my/` 再读。

        登录态用页面内的两个铁证判定（与 `session.is_logged_in_html` 同一套逻辑）：
        出现 `login/logout.php`，或 `M.cfg.userid >= 2`。

        ⚠ **不要在用户正在输入时调用本方法**（它会导航，从而清空表单）。
        轮询等待登录请用 `wait_for_login()`，那条路只读不导航。
        """
        if navigate:
            self.navigate(f"{self.base_url}/my/")
        return self._read()

    def _read(self) -> CookieSnapshot:
        """**纯读取**：不导航、不改动页面，随时可安全调用。

        登录判定用的那段 JS 在任何页面上都安全（非 Moodle 页面上 `window.M` 不存在，
        表达式退化为 `'0|0|<当前网址>'`），所以轮询期间可以放心反复执行。
        """
        probe_url = f"{self.base_url}/my/"
        raw = self.cookies()
        js = (
            "(document.documentElement.outerHTML.includes('login/logout.php') ? '1' : '0')"
            " + '|' + ((window.M && M.cfg && M.cfg.userid) ? String(M.cfg.userid) : '0')"
            " + '|' + location.href"
        )
        try:
            verdict = self.evaluate(js)
        except CdpError:
            verdict = "0|0|"
        flag, _, userid = verdict.partition("|")
        userid, _, href = userid.partition("|")
        logged_in = flag == "1" or (userid.isdigit() and int(userid) >= 2)
        return CookieSnapshot(
            cookie=_pick(raw, _MOODLE_HOSTS),
            sso_cookie=_pick(raw, _SSO_HOSTS),
            logged_in=logged_in,
            url=href or probe_url,
            raw=raw,
        )

    def wait_for_login(self, *, timeout: int = 300, poll: float = 3.0,
                       on_tick=None) -> CookieSnapshot:
        """轮询等待用户完成登录（用于可见窗口模式）。

        每隔 `poll` 秒**只读一次**登录态（`_read()`，绝不导航）；一旦 `logged_in`
        为真就立刻返回快照。超时则返回最后一次读数（`logged_in` 为 False）。

        ⚠ 这里必须用 `_read()` 而不是 `snapshot()`：用户此刻可能正停在统一身份认证
        页面敲账号密码，任何一次导航都会把表单清空、让他白打一遍。
        """
        deadline = time.monotonic() + timeout
        snap = CookieSnapshot(url=f"{self.base_url}/my/")
        while time.monotonic() < deadline:
            try:
                snap = self._read()
            except CdpError as exc:
                log.debug("登录轮询失败：%s", exc)
            if snap.logged_in and snap.cookie:
                # 刚落地时网关可能还在补发 cookie，稍等一下再读一次拿到完整的会话。
                time.sleep(1.0)
                try:
                    final = self._read()
                    if final.cookie:
                        return final
                except CdpError:
                    pass
                return snap
            if on_tick is not None:
                on_tick(snap)
            time.sleep(poll)
        return snap
