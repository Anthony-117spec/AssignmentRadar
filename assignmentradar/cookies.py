"""从本机 Chrome/Edge 的 cookie 库里，把平台的会话 cookie 直接取出来。

为什么需要它
------------
续期「最后一公里」（见 session.py 注释）时，网关只认「真实浏览器」的会话；
而人工复制又容易出现两个坑：① 找不到带 `MoodleSession` 的请求（平台整站被
网关 `rump_frontend` 前置，未登录时任何请求都只有 `my_client_ticket`）；
② 两个域的 cookie 长得很像，极易贴混。既然用户「本来就在浏览器里登录着」，
那就直接去读那份已经在用的会话——比让用户手抄可靠得多。

技术要点
--------
Chromium 的 cookie 分层加密：
1. `Local State` → `os_crypt.encrypted_key`，base64 解码后前 5 字节是 `DPAPI`；
2. 去掉该前缀，交给 Windows `CryptUnprotectData`（**同用户、同会话**即可解密，
   不需要管理员权限、不需要外部库）；
3. 每条 cookie 是 `v10` + 12 字节 nonce + 密文 + 16 字节 tag，用 AES-256-GCM 解。

⚠ 本模块的**能力边界**（2026-10-08 实测）
---------------------------------------
Chrome 从 127 版起引入了 **App-Bound Encryption**：新写入的 cookie 密文前缀变成
`v20`，其密钥由 `Local State` 的 `app_bound_encrypted_key` 包着，而那层包装用的是
**系统级 DPAPI**——解密需要 `LOCAL_SYSTEM` 权限。于是：

* 本模块对 `v20` cookie **彻底失效**（`_decrypt_value` 对它们返回 None）；
* 这不是 bug，是安全边界；不要试图提权去解。
* **正确的替代方案是 `cdp.py`**：通过浏览器调试协议让浏览器自己把 cookie 明文
  交出来，完全绕开加密。`radar.bat import` 在直读拿不到会话时会自动改走 CDP。

只读、绝不写用户的浏览器数据：以 `mode=ro` 打开 cookie 库的副本，读完即弃。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 浏览器配置目录里 cookie 库的相对位置（新版在 Network/ 下，旧版在根目录）
_COOKIE_RELATIVE = (
    "Default/Network/Cookies",
    "Default/Cookies",
    "Profile 1/Network/Cookies",
    "Profile 2/Network/Cookies",
)


def browser_profiles() -> list[tuple[str, Path]]:
    """返回本机可用的 (浏览器名, 用户数据根目录) 列表。

    `RADAR_BROWSER_DATA` 可指定自定义的 User Data 根目录（例如 portable 浏览器）。
    """
    out: list[tuple[str, Path]] = []
    env = os.environ.get("RADAR_BROWSER_DATA")
    if env and Path(env).is_dir():
        out.append(("自定义", Path(env)))
    local = Path.home() / "AppData" / "Local"
    for name, rel in (
        ("Chrome", "Google/Chrome/User Data"),
        ("Edge", "Microsoft/Edge/User Data"),
    ):
        root = local / rel
        if (root / "Local State").is_file():
            out.append((name, root))
    return out


def _active_profile(root: Path) -> str:
    """读出该浏览器当前实际使用的 profile 目录名（`Local State` 的 last_used）。"""
    try:
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
        name = state.get("profile", {}).get("last_used")
        if name and (root / name).is_dir():
            return name
    except Exception:
        pass
    return "Default"


@dataclass
class CookieJar:
    browser: str
    profile: str
    values: dict[str, str]  # 展平后的 "name=value" 对（含所有域，调用方自行筛选）


# ------------------------------------------------------------ DPAPI


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI 解保护：同用户下即可解密浏览器写下的密钥。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError(f"CryptUnprotectData 失败（错误码 {ctypes.GetLastError()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _master_key(profile_root: Path) -> bytes:
    state = json.loads((profile_root / "Local State").read_text(encoding="utf-8"))
    enc = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if enc[:5] != b"DPAPI":
        raise ValueError("Local State 里的 encrypted_key 不是预期的 DPAPI 封装")
    return _dpapi_unprotect(enc[5:])


def _decrypt_value(key: bytes, blob: bytes) -> str | None:
    """解一条 cookie；v20（App-Bound）等无法处理的形态返回 None。"""
    if blob[:3] in (b"v20", b"v21"):
        return None
    if blob[:3] != b"v10":
        # 极少见的：老版本直接用 DPAPI 包住明文
        try:
            return _dpapi_unprotect(blob).decode("utf-8")
        except Exception:
            return None
    try:
        from Crypto.Cipher import AES

        nonce, ct, tag = blob[3:15], blob[15:-16], blob[-16:]
        pt = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
    except Exception:
        log.debug("cookie 解密失败", exc_info=True)
        return None
    try:
        return pt.decode("utf-8")
    except UnicodeDecodeError:
        # Chromium 早期版本在明文前附 32 字节绑定哈希
        return pt[32:].decode("utf-8", errors="replace")


# ------------------------------------------------------------ 读取


def _find_cookie_db(profile_root: Path) -> Path | None:
    """在 profile 下找 cookie 库；找不到再看该浏览器当前实际使用的 profile。"""
    for rel in _COOKIE_RELATIVE:
        cand = profile_root / rel
        if cand.is_file():
            return cand
    active = _active_profile(profile_root)
    for rel in ("Network/Cookies", "Cookies"):
        cand = profile_root / active / rel
        if cand.is_file():
            return cand
    hits = sorted(profile_root.glob("*/Network/Cookies"))
    return hits[0] if hits else None


def _copy_cookie_db(db: Path) -> Path | None:
    """把 cookie 库复制到临时目录再读。

    浏览器会以独占方式打开这个库，`shutil.copy2` 会撞上 WinError 32；但 SQLite
    的在线备份接口能穿过共享锁把内容读出来，所以这里用 `Connection.backup` 复制，
    读不到时再退回 `copy2`。

    调用方读完**必须删掉副本所在目录**——里面是明文 cookie，等同于登录凭证。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="radar-cookies-"))
    copy = tmp_dir / "Cookies"
    src = dst = None
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(copy)
        with dst:
            src.backup(dst)
        return copy
    except Exception:
        log.debug("在线备份失败，退回文件复制", exc_info=True)
    finally:
        for conn in (dst, src):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    try:
        shutil.copy2(db, copy)
        return copy
    except Exception as exc:
        log.warning("cookie 库复制失败：%s", exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None


def _read_profile(name: str, root: Path) -> CookieJar | None:
    db = _find_cookie_db(root)
    if db is None:
        return None
    try:
        key = _master_key(root)
    except Exception as exc:
        log.warning("%s 的主密钥读取失败：%s", name, exc)
        return None
    copy = _copy_cookie_db(db)
    if copy is None:
        return None
    values: dict[str, str] = {}
    detail: dict[str, dict[str, str]] = {}
    rows = None
    try:
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "select host_key, name, encrypted_value, value from cookies "
                "where host_key like '%scnu.edu.cn'"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("%s 的 cookie 库读取失败：%s", name, exc)
    finally:
        # 明文 cookie 副本读完即弃，绝不留在 %TEMP% 里
        shutil.rmtree(copy.parent, ignore_errors=True)
    if rows is None:
        return None
    for host, cname, enc, plain in rows:
        val = plain or _decrypt_value(key, enc)
        if not val:
            continue
        detail.setdefault(host, {})[cname] = val
        values[cname] = val
    jar = CookieJar(browser=name, profile=root.name, values=values)
    jar.detail = detail  # type: ignore[attr-defined]
    return jar


@dataclass
class ProfileStatus:
    """某个浏览器 profile 里平台会话的状态（供 CLI 给出精确提示）。"""

    browser: str
    root: Path
    readable: bool
    has_moodle_session: bool
    has_sso: bool
    cookie_count: int


def inspect_profiles() -> list[ProfileStatus]:
    """体检：每个浏览器 profile 是否可读、是否已登录平台。"""
    out = []
    for name, root in browser_profiles():
        db = _find_cookie_db(root)
        if db is None:
            out.append(ProfileStatus(name, root, readable=False,
                                     has_moodle_session=False, has_sso=False, cookie_count=0))
            continue
        jar = _read_profile(name, root)
        if jar is None:
            out.append(ProfileStatus(name, root, readable=False,
                                     has_moodle_session=False, has_sso=False, cookie_count=0))
            continue
        detail = getattr(jar, "detail", {})
        moodle_hosts = {h for h in detail if "moodle.scnu.edu.cn" in h or h in (".scnu.edu.cn", "scnu.edu.cn")}
        has_moodle = any("MoodleSession" in detail.get(h, {}) for h in moodle_hosts)
        has_sso = any("JSESSIONID" in detail.get(h, {}) or "cookie_session_id" in detail.get(h, {})
                      for h in detail if "sso.scnu.edu.cn" in h)
        out.append(ProfileStatus(name, root, readable=True,
                                 has_moodle_session=has_moodle, has_sso=has_sso,
                                 cookie_count=len(jar.values)))
    return out


def read_scnu_cookies() -> list[CookieJar]:
    """列出本机所有浏览器 profile 里与 scnu.edu.cn 相关的 cookie。"""
    jars = []
    for name, root in browser_profiles():
        jar = _read_profile(name, root)
        if jar is not None:
            jars.append(jar)
    return jars


# ------------------------------------------------------------ 组装


# 只认真正参与身份认证的 cookie 名。平台上还挂着 Matomo 统计（`_pk_*`）、
# 主题偏好（`theme_*`）等一堆与登录无关的条目，混进去只会让请求头变脏。
_AUTH_NAMES = {
    "MoodleSession",        # 平台自身的会话（host-only，moodle.scnu.edu.cn）
    "my_client_ticket",     # 统一身份网关的票据（Domain=scnu.edu.cn）
    "JSESSIONID",           # SSO 侧 Java 会话
    "cookie_session_id",    # SSO 侧会话标识
    "machineSerial",        # SSO 侧设备标识
    "login_type",           # SSO 侧登录方式
    "login_user",           # SSO 侧登录用户名
}


def _moodle_cookie(values: dict[str, str], detail: dict[str, dict[str, str]]) -> str | None:
    """按域挑出适合发给 moodle.scnu.edu.cn 的 cookie 串。"""
    parts: list[str] = []
    for host in ("moodle.scnu.edu.cn", ".scnu.edu.cn", "scnu.edu.cn",
                 "auth1.scnu.edu.cn"):
        for cname, val in detail.get(host, {}).items():
            if cname not in _AUTH_NAMES:
                continue
            entry = f"{cname}={val}"
            if entry not in parts:
                parts.append(entry)
    return "; ".join(parts) if parts else None


def _sso_cookie(values: dict[str, str], detail: dict[str, dict[str, str]]) -> str | None:
    parts: list[str] = []
    for host in ("sso.scnu.edu.cn", "auth1.scnu.edu.cn"):
        for cname, val in detail.get(host, {}).items():
            if cname not in _AUTH_NAMES:
                continue
            entry = f"{cname}={val}"
            if entry not in parts:
                parts.append(entry)
    return "; ".join(parts) if parts else None


def harvest(prefer: str | None = None) -> list[tuple[CookieJar, str | None, str | None]]:
    """返回 [(jar, moodle_cookie, sso_cookie)]；两者都为 None 的条目会被过滤。"""
    out = []
    for jar in read_scnu_cookies():
        if prefer and jar.browser.lower() != prefer.lower():
            continue
        detail = getattr(jar, "detail", {})
        m = _moodle_cookie(jar.values, detail)
        s = _sso_cookie(jar.values, detail)
        if m or s:
            out.append((jar, m, s))
    return out
