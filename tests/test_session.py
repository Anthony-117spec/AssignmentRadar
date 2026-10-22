"""SSO 续期/保活的离线单测：python -m unittest discover -s tests -v"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assignmentradar.config import load_config  # noqa: E402
from assignmentradar.poller import App  # noqa: E402
from assignmentradar.session import MoodleHTTP  # noqa: E402


def cookie(name: str, value: str, domain: str):
    return SimpleNamespace(name=name, value=value, domain=domain)


class TestCookieCandidates(unittest.TestCase):
    def test_full_harvest_first_then_ticket_combos(self):
        """首选整 jar 收获；同名 ticket 打架时逐个降级；SSO 内部/外域 cookie 剔除。"""
        jar = [
            cookie("my_client_ticket", "AAA111", ".scnu.edu.cn"),
            cookie("MoodleSession", "SESS1", "moodle.scnu.edu.cn"),
            cookie("my_client_ticket", "BBB222", "auth1.scnu.edu.cn"),
            cookie("JSESSIONID", "X", "sso.scnu.edu.cn"),  # SSO 内部会话，剔除
            cookie("server", "sso-server-1", "sso.scnu.edu.cn"),
            cookie("other", "1", "example.com"),  # 外域，剔除
        ]
        out = MoodleHTTP._cookie_candidates(jar)
        self.assertEqual(
            out[0],
            "my_client_ticket=AAA111; MoodleSession=SESS1; my_client_ticket=BBB222",
        )
        self.assertIn("MoodleSession=SESS1; my_client_ticket=AAA111", out)
        self.assertIn("MoodleSession=SESS1; my_client_ticket=BBB222", out)
        self.assertEqual(len(out), 3)

    def test_ticket_only_jar(self):
        out = MoodleHTTP._cookie_candidates([cookie("my_client_ticket", "CCC333", ".scnu.edu.cn")])
        self.assertEqual(out, ["my_client_ticket=CCC333"])

    def test_empty_jar(self):
        self.assertEqual(MoodleHTTP._cookie_candidates([]), [])


class TestKeepaliveSso(unittest.TestCase):
    """保活只走到 SSO 确认页为止；死亡判定只认统一身份认证自己的登录页。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(Path(self.tmp.name))
        self.http = MoodleHTTP(self.cfg)
        self.http.sso_cookie = "JSESSIONID=x"
        self.gets: list[str] = []

    def _patch_get(self, final_url: str):
        from unittest.mock import patch

        def fake_get(self_sess, url, **kw):
            self.gets.append(url)
            return SimpleNamespace(url=final_url)

        return patch("requests.Session.get", fake_get)

    def test_no_sso_cookie_returns_none(self):
        self.http.sso_cookie = ""
        self.assertIsNone(self.http.keepalive_sso())
        self.assertEqual(self.gets, [])

    def test_alive_when_auth_page_reachable(self):
        with self._patch_get("https://sso.scnu.edu.cn/AccountService/openapi/auth.html?x=1"):
            self.assertTrue(self.http.keepalive_sso())
        self.assertEqual(len(self.gets), 1)

    def test_alive_when_gateway_lets_through(self):
        with self._patch_get("https://moodle.scnu.edu.cn/my/"):
            self.assertTrue(self.http.keepalive_sso())

    def test_dead_when_bounced_to_sso_login_page(self):
        with self._patch_get(
            "https://sso.scnu.edu.cn/AccountService/user/login.html"
        ):
            self.assertFalse(self.http.keepalive_sso())

    def test_network_error_never_reports_dead(self):
        from unittest.mock import patch

        def boom(self_sess, url, **kw):
            raise RuntimeError("network down")

        with patch("requests.Session.get", boom):
            self.assertTrue(self.http.keepalive_sso())  # 抖动≠死亡，不触发断线预警

    def test_throttled_within_interval(self):
        with self._patch_get("https://sso.scnu.edu.cn/AccountService/openapi/auth.html"):
            self.assertTrue(self.http.keepalive_sso())
            self.assertTrue(self.http.keepalive_sso())  # 限频窗口内不再发请求
        self.assertEqual(len(self.gets), 1)


class _FakeNotifier:
    def __init__(self):
        self.events: list[str] = []

    def sso_expired(self):
        self.events.append("sso_expired")


class TestPollerSsoKeepalive(unittest.TestCase):
    """轮询成功路径上的保活钩子：SSO 死亡只预警一次，恢复后重新武装。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = App(load_config(Path(self.tmp.name)), notify_enabled=False)
        self.addCleanup(self.app.shutdown)
        self.notifier = _FakeNotifier()
        self.app.notifier = self.notifier
        self.app.http = SimpleNamespace(keepalive_sso=lambda: None)

    def _set_alive(self, value):
        self.app.http = SimpleNamespace(keepalive_sso=lambda: value)

    def test_notify_once_until_recovered(self):
        self._set_alive(False)
        self.app._keep_sso_alive()
        self.assertEqual(self.notifier.events, ["sso_expired"])
        self.app._keep_sso_alive()
        self.assertEqual(self.notifier.events, ["sso_expired"])  # 不重复轰炸

        self._set_alive(True)
        self.app._keep_sso_alive()
        self.assertEqual(self.notifier.events, ["sso_expired"])

        self._set_alive(False)  # 再次死亡 → 重新预警
        self.app._keep_sso_alive()
        self.assertEqual(self.notifier.events, ["sso_expired", "sso_expired"])

    def test_none_means_feature_off(self):
        self._set_alive(None)
        self.app._keep_sso_alive()
        self.assertEqual(self.notifier.events, [])

    def test_quiet_mode_suppresses_toast(self):
        self.app.quiet_session_notify = True
        self._set_alive(False)
        self.app._keep_sso_alive()
        self.assertEqual(self.notifier.events, [])
        self.assertTrue(self.app._sso_dead_notified)  # 状态照记，只是不弹

    def test_keepalive_exception_never_raises(self):
        def boom():
            raise RuntimeError("network down")

        self.app.http = SimpleNamespace(keepalive_sso=boom)
        self.app._keep_sso_alive()  # 不应抛出
        self.assertEqual(self.notifier.events, [])


class TestRetryAfterParsing(unittest.TestCase):
    """Retry-After 两种合法形式都必须认：秒数 与 HTTP-date。

    以前只 `int(...)`，服务端发 date 形式时 ValueError 直接把整轮轮询炸掉
    （不是一门课失败，是全部课程那一轮全丢）。
    """

    @staticmethod
    def _resp(value: str):
        from assignmentradar.session import MoodleHTTP

        return SimpleNamespace(headers={"Retry-After": value}) if value \
            else SimpleNamespace(headers={})

    def test_seconds_form(self):
        from assignmentradar.session import MoodleHTTP

        self.assertEqual(MoodleHTTP._retry_after_seconds(self._resp("120")), 120.0)

    def test_seconds_form_is_clamped(self):
        from assignmentradar.session import MoodleHTTP

        self.assertEqual(MoodleHTTP._retry_after_seconds(self._resp("0")), 1.0)
        self.assertEqual(MoodleHTTP._retry_after_seconds(self._resp("99999")), 300.0)

    def test_http_date_form(self):
        from datetime import datetime, timedelta, timezone

        from assignmentradar.session import MoodleHTTP

        future = datetime.now(timezone.utc) + timedelta(seconds=90)
        header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        got = MoodleHTTP._retry_after_seconds(self._resp(header))
        self.assertTrue(60.0 <= got <= 91.0, got)

    def test_http_date_in_the_past_clamps_to_one(self):
        from assignmentradar.session import MoodleHTTP

        got = MoodleHTTP._retry_after_seconds(self._resp("Wed, 21 Oct 2015 07:28:00 GMT"))
        self.assertEqual(got, 1.0)

    def test_missing_or_garbage_falls_back_to_60(self):
        from assignmentradar.session import MoodleHTTP

        self.assertEqual(MoodleHTTP._retry_after_seconds(self._resp("")), 60.0)
        self.assertEqual(MoodleHTTP._retry_after_seconds(self._resp("soon")), 60.0)


if __name__ == "__main__":
    unittest.main()
