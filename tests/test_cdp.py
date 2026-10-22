"""CDP 客户端的离线单测：python -m unittest tests.test_cdp -v

只测纯逻辑（帧编解码、cookie 归组、登录判定），不真的去起浏览器——
CI/无浏览器的环境也要能跑。
"""
from __future__ import annotations

import os
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assignmentradar import cdp  # noqa: E402


def _server_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """按 RFC6455 拼一条**服务端→客户端**的帧（不加掩码）。"""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


class TestFraming(unittest.TestCase):
    """WebSocket 帧的读取逻辑（掩码处理、长度前缀、控制帧跳过）。

    直接构造一个假的 _WebSocket 实例：把预置的字节塞进 `_buf`，并让 socket
    的 `recv` 永远返回空，从而在完全离线的情况下驱动 `recv_text`。
    """

    def _ws(self, raw: bytes) -> cdp._WebSocket:
        ws = object.__new__(cdp._WebSocket)
        ws._buf = raw

        class _Sock:
            def recv(self, _n):  # noqa: D401
                return b""

        ws._sock = _Sock()  # type: ignore[assignment]
        return ws

    def test_short_text_frame(self):
        ws = self._ws(_server_frame(b'{"id":1}'))
        self.assertEqual(ws.recv_text(), '{"id":1}')

    def test_two_byte_length_prefix(self):
        payload = b"x" * 300  # 126 以上的长度走 2 字节前缀
        ws = self._ws(_server_frame(payload))
        self.assertEqual(ws.recv_text(), "x" * 300)

    def test_eight_byte_length_prefix(self):
        payload = b"y" * 70000  # 65536 以上走 8 字节前缀
        ws = self._ws(_server_frame(payload))
        self.assertEqual(len(ws.recv_text()), 70000)

    def test_ping_is_skipped_then_text_returned(self):
        ping = _server_frame(b"hb", opcode=0x9)
        text = _server_frame(b"hello")
        ws = self._ws(ping + text)
        self.assertEqual(ws.recv_text(), "hello")

    def test_close_frame_returns_none(self):
        ws = self._ws(_server_frame(b"", opcode=0x8))
        self.assertIsNone(ws.recv_text())

    def test_two_frames_in_one_buffer(self):
        ws = self._ws(_server_frame(b"one") + _server_frame(b"two"))
        self.assertEqual(ws.recv_text(), "one")
        self.assertEqual(ws.recv_text(), "two")


class TestClientFraming(unittest.TestCase):
    """客户端帧**必须**带掩码——这是 RFC6455 的硬要求，服务端会拒绝裸帧。"""

    def test_text_frame_is_masked_and_roundtrips(self):
        ws = object.__new__(cdp._WebSocket)
        sent = bytearray()

        class _Sock:
            def sendall(self, data):  # noqa: D401
                sent.extend(data)

        ws._sock = _Sock()  # type: ignore[assignment]
        ws.send_text("hello")

        self.assertEqual(sent[0], 0x81)  # FIN + text
        masked = sent[1] & 0x80
        self.assertTrue(masked, "客户端帧必须设置掩码位")
        n = sent[1] & 0x7F
        self.assertEqual(n, 5)
        mask = sent[2:6]
        body = bytes(sent[6:])
        restored = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
        self.assertEqual(restored, b"hello")

    def test_long_frame_uses_two_byte_prefix(self):
        ws = object.__new__(cdp._WebSocket)
        sent = bytearray()

        class _Sock:
            def sendall(self, data):  # noqa: D401
                sent.extend(data)

        ws._sock = _Sock()  # type: ignore[assignment]
        ws.send_text("z" * 200)
        self.assertEqual(sent[1] & 0x7F, 126)
        n = struct.unpack(">H", sent[2:4])[0]
        self.assertEqual(n, 200)


class TestPickCookies(unittest.TestCase):
    """按域把 cookie 归组成可以被 moodle / sso 接受的串。"""

    def test_pick_moodle_hosts(self):
        cookies = [
            {"domain": "moodle.scnu.edu.cn", "name": "MoodleSession", "value": "MS1"},
            {"domain": ".scnu.edu.cn", "name": "my_client_ticket", "value": "TK1"},
            {"domain": "example.com", "name": "MoodleSession", "value": "NOPE"},
        ]
        got = cdp._pick(cookies, cdp._MOODLE_HOSTS)
        self.assertIn("MoodleSession=MS1", got)
        self.assertIn("my_client_ticket=TK1", got)
        self.assertNotIn("NOPE", got)

    def test_pick_sso_hosts(self):
        cookies = [
            {"domain": "sso.scnu.edu.cn", "name": "JSESSIONID", "value": "J1"},
            {"domain": "sso.scnu.edu.cn", "name": "cookie_session_id", "value": "S1"},
        ]
        got = cdp._pick(cookies, cdp._SSO_HOSTS)
        self.assertIn("JSESSIONID=J1", got)
        self.assertIn("cookie_session_id=S1", got)

    def test_analytics_cookies_are_dropped(self):
        cookies = [
            {"domain": "moodle.scnu.edu.cn", "name": "MoodleSession", "value": "MS1"},
            {"domain": "moodle.scnu.edu.cn", "name": "_pk_id.3.98ff", "value": "track"},
            {"domain": "moodle.scnu.edu.cn", "name": "theme_style", "value": "dark"},
        ]
        got = cdp._pick(cookies, cdp._MOODLE_HOSTS)
        self.assertNotIn("_pk", got)
        self.assertNotIn("theme_style", got)

    def test_no_match_returns_none(self):
        cookies = [{"domain": "example.com", "name": "MoodleSession", "value": "x"}]
        self.assertIsNone(cdp._pick(cookies, cdp._MOODLE_HOSTS))

    def test_duplicate_entries_collapsed(self):
        cookies = [
            {"domain": "moodle.scnu.edu.cn", "name": "MoodleSession", "value": "MS1"},
            {"domain": ".scnu.edu.cn", "name": "MoodleSession", "value": "MS1"},
        ]
        got = cdp._pick(cookies, cdp._MOODLE_HOSTS)
        self.assertEqual(got, "MoodleSession=MS1")


class TestProfilePaths(unittest.TestCase):
    """私有配置目录的定位。"""

    def test_profile_dir_under_data(self):
        got = cdp.profile_dir(Path("E:/AssignmentRadar"))
        self.assertEqual(got, Path("E:/AssignmentRadar") / "data" / "browser_profile")

    def test_profile_exists_requires_local_state(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertFalse(cdp.profile_exists(base))
            d = cdp.profile_dir(base)
            d.mkdir(parents=True)
            self.assertFalse(cdp.profile_exists(base), "空目录还不算已建")
            (d / "Local State").write_text("{}", encoding="utf-8")
            self.assertTrue(cdp.profile_exists(base))


class TestPortSelection(unittest.TestCase):
    """空闲端口的挑选在给定范围内，且能被真正 bind 到。"""

    def test_free_port_within_range(self):
        port = cdp._free_port()
        self.assertIn(port, cdp._PORT_RANGE)


class TestNoNavigationWhilePolling(unittest.TestCase):
    """回归：轮询登录期间**绝不能**导航。

    背景（2026-10-08 真事故）：`wait_for_login` 早期实现每 3 秒调一次 `snapshot()`，
    而 `snapshot()` 会导航回 `/my/`——用户正在统一身份认证页面敲账号密码，页面每
    3 秒被刷新一次，根本没法输入。修法是把「纯读取」`_read()` 与「导航」拆开。
    """

    def _bs(self, verdicts: list[str]):
        """造一个不启动浏览器的 BrowserSession，把导航/读取都换成记录桩。"""
        bs = cdp.BrowserSession(Path("E:/x"), "https://moodle.scnu.edu.cn",
                                executable="dummy.exe")
        calls: dict[str, list] = {"navigate": []}
        bs.navigate = lambda url, **kw: calls["navigate"].append(url)  # type: ignore[assignment]
        seq = list(verdicts)
        state = {"i": 0}

        def fake_eval(_expr: str) -> str:
            i = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return seq[i]

        bs.evaluate = fake_eval  # type: ignore[assignment]
        bs.cookies = lambda: [  # type: ignore[assignment]
            {"domain": "moodle.scnu.edu.cn", "name": "MoodleSession", "value": "MS"}
        ]
        return bs, calls

    def test_wait_for_login_never_navigates(self):
        # 先两拍停在 SSO 登录页，第三拍落到平台已登录
        verdicts = [
            "0|0|https://sso.scnu.edu.cn/AccountService/user/login.html",
            "0|0|https://sso.scnu.edu.cn/AccountService/user/login.html",
            "1|42|https://moodle.scnu.edu.cn/my/",
        ]
        bs, calls = self._bs(verdicts)
        snap = bs.wait_for_login(timeout=5, poll=0.01)
        self.assertTrue(snap.logged_in)
        self.assertEqual(calls["navigate"], [],
                         "轮询期间不得导航，否则会清空用户正在填的登录表单")

    def test_wait_for_login_times_out_without_navigating(self):
        bs, calls = self._bs(["0|0|https://sso.scnu.edu.cn/x"] * 50)
        snap = bs.wait_for_login(timeout=0.05, poll=0.01)
        self.assertFalse(snap.logged_in)
        self.assertEqual(calls["navigate"], [])

    def test_read_alone_does_not_navigate(self):
        bs, calls = self._bs(["0|0|https://moodle.scnu.edu.cn/?redirect=0"])
        snap = bs._read()
        self.assertEqual(calls["navigate"], [])
        self.assertEqual(snap.url, "https://moodle.scnu.edu.cn/?redirect=0")

    def test_snapshot_navigate_false_does_not_navigate(self):
        bs, calls = self._bs(["0|0|https://moodle.scnu.edu.cn/?redirect=0"])
        bs.snapshot(navigate=False)
        self.assertEqual(calls["navigate"], [])

    def test_snapshot_default_navigates_once_to_my(self):
        bs, calls = self._bs(["0|0|https://moodle.scnu.edu.cn/?redirect=0"])
        bs.snapshot()
        self.assertEqual(calls["navigate"], ["https://moodle.scnu.edu.cn/my/"])

    def test_guest_session_is_not_logged_in(self):
        bs, _ = self._bs(["0|0|https://moodle.scnu.edu.cn/?redirect=0"])
        snap = bs.snapshot(navigate=False)
        self.assertFalse(snap.logged_in, "游客也有 MoodleSession，不能误判为已登录")

    def test_logged_in_markers_detected(self):
        for verdict in ("1|0|https://moodle.scnu.edu.cn/my/",
                        "0|42|https://moodle.scnu.edu.cn/my/"):
            bs, _ = self._bs([verdict])
            self.assertTrue(bs.snapshot(navigate=False).logged_in, verdict)


class TestPageTargetSelection(unittest.TestCase):
    """`/json/list` 里挑标签页：优先匹配主机，避免连到残留的空白页。"""

    def _patch(self, payload):
        import json as _json

        class FakeResp:
            def __init__(self, data):
                self._data = _json.dumps(data).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        orig = cdp.urllib.request.urlopen
        cdp.urllib.request.urlopen = lambda url, timeout=None: FakeResp(payload)
        self.addCleanup(lambda: setattr(cdp.urllib.request, "urlopen", orig))

    def test_prefers_matching_host(self):
        self._patch([
            {"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://a"},
            {"type": "page", "url": "https://moodle.scnu.edu.cn/my/",
             "webSocketDebuggerUrl": "ws://b"},
        ])
        t = cdp._find_page_target(1, prefer_host="moodle.scnu.edu.cn")
        self.assertEqual(t["webSocketDebuggerUrl"], "ws://b")

    def test_falls_back_to_first_page(self):
        self._patch([
            {"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://a"},
            {"type": "service_worker", "url": "x", "webSocketDebuggerUrl": "ws://c"},
        ])
        t = cdp._find_page_target(1, prefer_host="moodle.scnu.edu.cn")
        self.assertEqual(t["webSocketDebuggerUrl"], "ws://a")

    def test_browser_target_is_never_chosen(self):
        self._patch([{"type": "browser", "url": "x", "webSocketDebuggerUrl": "ws://c"}])
        self.assertIsNone(cdp._find_page_target(1))

    def test_no_page_target_returns_none(self):
        self._patch([])
        self.assertIsNone(cdp._find_page_target(1))


if __name__ == "__main__":
    unittest.main()
