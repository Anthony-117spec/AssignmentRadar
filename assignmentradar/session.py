"""HTTP 层：cookie 会话、限速、会话过期检测、SSO 自动续期。

平台网关（rump_frontend）会频繁轮换 MoodleSession，静态复制的 cookie
存活时间很短。只要用户在 config 里提供 sso.scnu.edu.cn 的会话 cookie，
本模块就能在会话失效时自动沿统一身份认证的 OAuth 链路重新登录网关，
全程不接触账号密码。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from .config import Config, parse_cookie_header

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

SSO_HOST = "sso.scnu.edu.cn"
# SSO 自己的会话 cookie，续期完成后不应带去平台
_SSO_INTERNAL = {"JSESSIONID", "machineSerial", "cookie_session_id", "server"}


class SessionExpired(Exception):
    """登录会话已失效，且（或）自动续期未能完成。"""


class NotEnrolled(Exception):
    """课程存在但当前账号尚未点"自助选课"加入，看不到课程内容。"""


def is_logged_in_html(html: str) -> bool:
    """判定页面是否处于真实登录态。

    两个标记任一即可：logout 链接；或 M.cfg 里的 userid >= 2（游客是 0/1）。
    """
    if re.search(r"login/logout\.php", html):
        return True
    m = re.search(r'"userid"\s*:\s*"?(\d+)"?', html)
    return bool(m) and int(m.group(1)) >= 2


class MoodleHTTP:
    """带 cookie、请求间隔、429 退避和登录页检测的 HTTP 客户端。"""

    # 两次主动续命（keepalive）的最小间隔（秒）
    KEEPALIVE_MIN_INTERVAL = 3600

    def __init__(self, cfg: Config):
        self.base_url = cfg.base_url
        self.delay = max(0.5, cfg.request_delay_seconds)
        self.sso_cookie = cfg.sso_cookie
        # 续期成功后由外部（poller）持久化新 cookie，避免下次再走一遍
        self.on_cookies_changed = None  # callable(moodle_cookie_str)
        self.on_sso_cookies_changed = None  # callable(sso_cookie_str)，SSO 侧轮换时持久化
        self._last_sso_touch = 0.0  # 上次真正走 OAuth 链路的时间（保活限频用）
        self.last_renew_status = "no_sso"  # 最近一次续期的结束状态
        self.sess = self._build_session(cfg.cookie)

    def _build_session(self, cookie_str: str) -> requests.Session:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            }
        )
        domain = urlparse(self.base_url).hostname or ""
        for name, value in parse_cookie_header(cookie_str).items():
            s.cookies.set(name, value, domain=domain)
        return s

    # ------------------------------------------------------------ 请求

    @staticmethod
    def _retry_after_seconds(resp: requests.Response) -> float:
        """RFC 9110：`Retry-After` 可以是秒数，**也可以是 HTTP-date**。

        网关真发过 date 形式。旧代码直接 `int(...)`，遇到 date 就 ValueError
        把整轮轮询炸掉（不是这一门课失败，是全部）。无法解析时退回 60 秒。
        """
        raw = (resp.headers.get("Retry-After") or "").strip()
        if not raw:
            return 60.0
        try:
            return min(max(float(raw), 1.0), 300.0)
        except ValueError:
            pass
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return 60.0
        if dt is None:
            return 60.0
        if dt.tzinfo is None:  # 没给时区按 GMT 算（RFC 要求 HTTP-date 必须带）
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (dt - datetime.now(timezone.utc)).total_seconds()
        return min(max(secs, 1.0), 300.0)

    def _get_with_retry(self, url: str, *, timeout: float,
                        headers: dict | None = None,
                        attempts: int = 3) -> requests.Response:
        """连接级错误（断网 / 超时 / DNS）重试。

        这类错误以前直接往上抛，一次网络抖动就废掉整轮轮询——课程发现走到
        一半断网，后面十几门课的详情全丢。重试两次、间隔 1.5s/3s 就够了。
        """
        last: Exception | None = None
        for i in range(attempts):
            try:
                return self.sess.get(url, timeout=timeout, headers=headers)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
                if i < attempts - 1:
                    wait = 1.5 * (i + 1)
                    log.warning("请求失败（%s），%.1f 秒后重试：%s",
                                type(exc).__name__, wait, url)
                    time.sleep(wait)
        assert last is not None
        raise last

    def get(self, url: str, *, referer: str | None = None) -> requests.Response:
        headers = {"Referer": referer} if referer else None
        resp: requests.Response | None = None
        for _ in range(3):
            resp = self._get_with_retry(url, timeout=30, headers=headers)
            if resp.status_code != 429:
                break
            wait = self._retry_after_seconds(resp)
            log.warning("被限流(429)，等待 %s 秒后重试", wait)
            time.sleep(wait)
        self._check_expired(resp)
        assert resp is not None
        resp.raise_for_status()
        time.sleep(self.delay)
        return resp

    def get_file(self, url: str) -> requests.Response:
        """下载附件用：**不做"登录页"判定**。

        附件链接有两种：带 token 的 `webservice/pluginfile.php`（靠 token 鉴权，不需要
        cookie），以及从 HTML 页面解析出来的 `pluginfile.php`（靠 cookie）。前者在 token
        失效时服务端回 403，若走 `get()`，`_check_expired` 会把它**误判成会话过期**，
        进而触发一整轮重新登录——那是错误归因。附件下载失败就是失败，
        `attachments.download_one` 会记成 failed；"被弹到登录页"由它自己的
        `_looks_like_html` 单独识别，不需要在这里兜。
        """
        resp: requests.Response | None = None
        for _ in range(3):
            resp = self._get_with_retry(url, timeout=60)
            if resp.status_code != 429:
                break
            wait = self._retry_after_seconds(resp)
            log.warning("被限流(429)，等待 %s 秒后重试", wait)
            time.sleep(wait)
        assert resp is not None
        resp.raise_for_status()
        time.sleep(self.delay)
        return resp

    def post(self, url: str, **kwargs) -> requests.Response:
        resp = self.sess.post(url, timeout=30, **kwargs)
        self._check_expired(resp)
        resp.raise_for_status()
        time.sleep(self.delay)
        return resp

    @staticmethod
    def _check_expired(resp: requests.Response | None) -> None:
        """最终落在登录页（SSO 登录页或 /rump_frontend/login/）即视为会话过期。"""
        if resp is None:
            return
        if resp.status_code in (401, 403):
            raise SessionExpired(f"HTTP {resp.status_code}: {resp.url}")
        final_url = str(resp.url).lower()
        if "/login" in final_url:
            raise SessionExpired(f"被重定向到登录页: {resp.url}")

    # ------------------------------------------------------------ 自动续期

    def reestablish(self, debug_path=None) -> bool:
        """沿 SSO OAuth 链路重新登录网关。成功返回 True 并重建会话。

        结束状态记在 self.last_renew_status（sso_dead / no_code / no_cookies /
        verify_failed / request_failed / ok），供 keepalive 区分"SSO 真死了"
        和"链路没走通但不代表 SSO 失效"。
        """
        self._last_sso_touch = time.monotonic()
        self.last_renew_status = "no_sso"
        if not self.sso_cookie:
            log.info("未配置 sso_cookie，无法自动续期")
            return False
        self.last_renew_status = "no_cookies"
        # 干净起步：不带任何平台旧 cookie（旧 ticket 会让网关放行为游客、跳过登录）
        s = self._build_session("")
        for name, value in parse_cookie_header(self.sso_cookie).items():
            s.cookies.set(name, value, domain=SSO_HOST)
        steps: list[str] = []
        try:
            login_entry = f"{self.base_url}/rump_frontend/login/?next={self.base_url}/my/"
            r = s.get(login_entry, timeout=30, allow_redirects=True)
            steps.append(f"登录跳转落点 {r.url}")
            r.raise_for_status()
            final = str(r.url)
            if SSO_HOST in urlparse(final).netloc:
                if "/login" in final:
                    log.info("SSO 会话无效（被弹回登录页 %s）", final)
                    steps.append("SSO 会话已失效：服务端不识别这套 sso_cookie"
                                 "（常见是会话过期，或复制时浏览器里本就未登录）")
                    self.last_renew_status = "sso_dead"
                    self._save_debug(debug_path, r.text, steps)
                    return False
                # SSO 的"登录确认页"（gotoApp→fastlogin）：提取 app_id 代为确认
                m_fast = re.search(r"fastlogin\.html\?app_id=(\d+)", r.text)
                if m_fast:
                    m_ru = re.search(r"[?&]redirect_url=([^&]+)", final)
                    fast_url = (
                        f"{urlparse(final).scheme}://{urlparse(final).netloc}"
                        f"/AccountService/openapi/fastlogin.html?app_id={m_fast.group(1)}"
                    )
                    if m_ru:
                        fast_url += f"&redirect_url={m_ru.group(1)}"
                    steps.append("SSO 登录确认页 → 自动确认")
                    r = s.get(fast_url, timeout=30, allow_redirects=True)
                    steps.append(f"fastlogin 落点 {r.url}")
                    r.raise_for_status()
                    final = str(r.url)
                # 其他形式的授权页：尝试提取带 code 的跳转地址
                if SSO_HOST in urlparse(final).netloc:
                    m = re.search(r'https?://[^"\'\s]+(?:code=|loginFromOauth2)[^"\'\s]*', r.text)
                    if not m:
                        log.info("SSO 授权页未找到 code 跳转（可能需要手动授权），停在 %s", final)
                        self.last_renew_status = "no_code"
                        self._save_debug(debug_path, r.text, steps)
                        return False
                    r = s.get(m.group(0).replace("&amp;", "&"), timeout=30, allow_redirects=True)
                    steps.append(f"code 回跳落点 {r.url}")
                    r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("续期请求失败：%s", exc)
            self.last_renew_status = "request_failed"
            return False

        # 网关登录态未必只靠 my_client_ticket 一个 cookie——浏览器里的登录态
        # 是整套 cookie（10-03 实测：链路走完但逐个 ticket 单测 /my/ 都"未登录"）。
        # 所以首选整 jar 的收获组合；同名 ticket 打架时再降级为
        # "其余 cookie + 单个 ticket"逐个实测，取真正能登录的那个。
        candidates = self._cookie_candidates(s.cookies)
        if not candidates:
            log.info("续期流程走完但没有收获任何网关 cookie")
            self.last_renew_status = "no_cookies"
            self._save_debug(debug_path, r.text, steps)
            return False
        r2 = None
        working = None
        ok = False
        for combo in candidates:
            label = next(
                (p.split("=", 1)[1][:6] for p in combo.split("; ")
                 if p.startswith("my_client_ticket=")),
                "无",
            )
            try:
                test = self._build_session(combo)
                r2 = test.get(f"{self.base_url}/my/", timeout=30, allow_redirects=True)
                ok = is_logged_in_html(r2.text) and "/login" not in str(r2.url).lower()
                steps.append(f"复核 /my/（ticket {label}…）→ {'已登录' if ok else '未登录'}")
            except requests.RequestException as exc:
                steps.append(f"复核请求失败：{exc}")
                ok = False
            if ok:
                working = combo
                break
        log.info("自动续期：%s", "；".join(steps))
        if not ok:
            self.last_renew_status = "verify_failed"
            self._save_debug(debug_path, r2.text if r2 is not None else r.text, steps)
            return False

        self.last_renew_status = "ok"
        self.sess = self._build_session(working)
        log.info("SSO 自动续期成功")
        # SSO 侧如有轮换，把最新值也持久化（仅成功时，避免拿匿名会话覆盖好会话）
        sso_now = "; ".join(
            f"{c.name}={c.value}" for c in s.cookies if SSO_HOST in (c.domain or "")
        )
        if sso_now and sso_now != self.sso_cookie and self.on_sso_cookies_changed:
            try:
                self.on_sso_cookies_changed(sso_now)
                self.sso_cookie = sso_now
            except Exception:
                log.exception("持久化 SSO cookie 失败（不影响本轮使用）")
        if self.on_cookies_changed:
            try:
                self.on_cookies_changed(working)
            except Exception:
                log.exception("持久化新 cookie 失败（不影响本轮使用）")
        return True

    # ------------------------------------------------------------ SSO 保活

    def keepalive_sso(self) -> bool | None:
        """轻触一次 SSO，保住会话不过期（"太久不活动就作废"的场景）。

        只走到 SSO 确认页为止（2 个请求）：网关登录入口 302 过去、确认页 200
        返回即证明 SSO 会话存活——不点 fastlogin、不消耗授权码，也不重跑那条
        目前仍无法在服务端复现浏览器效果的回调链路。
        返回 None = 未配置 sso_cookie；False = SSO 会话已死（网关会话不受影响）；
        True = SSO 会话可用（本次已续命，或仍在限频窗口内）。
        """
        if not self.sso_cookie:
            return None
        now = time.monotonic()
        if now - self._last_sso_touch < self.KEEPALIVE_MIN_INTERVAL:
            return True
        self._last_sso_touch = now
        s = self._build_session("")
        for name, value in parse_cookie_header(self.sso_cookie).items():
            s.cookies.set(name, value, domain=SSO_HOST)
        try:
            r = s.get(f"{self.base_url}/rump_frontend/login/?next={self.base_url}/my/",
                      timeout=30, allow_redirects=True)
        except Exception as exc:
            log.warning("SSO 保活请求失败：%s", exc)
            return True  # 网络抖动等≠会话死亡，别误报
        # SSO 死亡的唯一标志：被弹到统一身份认证自己的登录页
        # （确认页 openapi/auth.html = 存活；网关直通 /my/ = 网关会话有效）
        final = str(r.url).lower()
        alive = "accountservice/user/login" not in final
        if alive:
            log.info("SSO 会话保活正常（确认页可达）")
        else:
            log.info("SSO 保活发现会话已失效（被弹回登录页 %s）", r.url)
        return alive

    @staticmethod
    def _save_debug(debug_path, html: str, steps: list[str]) -> None:
        if debug_path is None:
            return
        try:
            html = re.sub(r'"sesskey":"[0-9A-Za-z]+"', '"sesskey":"REDACTED"', html)
            html = re.sub(r'"userid"\s*:\s*"?\d+"?', '"userid":"REDACTED"', html)
            debug_path.write_text(
                "\n".join(steps) + "\n\n" + html, encoding="utf-8"
            )
            log.info("续期调试页已保存：%s", debug_path)
        except Exception:
            log.exception("保存续期调试页失败")

    @staticmethod
    def _cookie_candidates(jar) -> list[str]:
        """从 OAuth 链路的 cookie jar 组出候选的网关 cookie 集（完整组合优先）。"""
        harvest_all = MoodleHTTP._harvest_from(jar)
        tickets = [c.value for c in jar if c.name == "my_client_ticket"]
        others = [
            p for p in (harvest_all.split("; ") if harvest_all else [])
            if not p.startswith("my_client_ticket=")
        ]
        candidates: list[str] = []
        if harvest_all:
            candidates.append(harvest_all)
        for t in dict.fromkeys(tickets):
            combo = "; ".join(others + [f"my_client_ticket={t}"])
            if combo not in candidates:
                candidates.append(combo)
        return candidates

    @staticmethod
    def _harvest_from(jar) -> str:
        """收集 jar 里网关域（scnu.edu.cn 但非 SSO 域）的全部 cookie。"""
        parts = []
        for c in jar:
            domain = (c.domain or "").lower()
            if SSO_HOST in domain or c.name in _SSO_INTERNAL:
                continue
            if "scnu.edu.cn" not in domain:
                continue
            parts.append(f"{c.name}={c.value}")
        return "; ".join(parts)
