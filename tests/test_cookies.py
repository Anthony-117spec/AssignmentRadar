"""浏览器 cookie 提取的离线单测：python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assignmentradar import cookies  # noqa: E402


def _make_profile(root: Path, plain: list[tuple[str, str, str]] | None = None,
                  encrypted: list[tuple[str, str, bytes]] | None = None) -> None:
    """铺一个最小的 Chromium profile：Local State + cookie 库。

    `plain` 走 value 列（明文），`encrypted` 走 encrypted_value 列（密文）。
    """
    (root / "Local State").write_text(
        json.dumps({"os_crypt": {"encrypted_key": "RFBBUEk="}, "profile": {"last_used": "Default"}}),
        encoding="utf-8",
    )
    db_dir = root / "Default" / "Network"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "Cookies")
    conn.execute(
        "create table cookies (host_key text, name text, encrypted_value blob, value text)"
    )
    for host, name, value in plain or []:
        conn.execute("insert into cookies values (?,?,?,?)", (host, name, b"", value))
    for host, name, blob in encrypted or []:
        conn.execute("insert into cookies values (?,?,?,?)", (host, name, blob, None))
    conn.commit()
    conn.close()


class TestHarvestSelection(unittest.TestCase):
    """选 cookie 时按域过滤——平台会话与统一身份认证的会话不能混。"""

    def test_moodle_picks_moodle_and_shared_domains(self):
        detail = {
            "moodle.scnu.edu.cn": {"MoodleSession": "MS1"},
            ".scnu.edu.cn": {"my_client_ticket": "TK1"},
            "sso.scnu.edu.cn": {"JSESSIONID": "SSO1", "cookie_session_id": "SID1"},
            "example.com": {"MoodleSession": "NOPE"},  # 外域，必须丢弃
        }
        got = cookies._moodle_cookie({}, detail)
        self.assertEqual(got, "MoodleSession=MS1; my_client_ticket=TK1")
        self.assertNotIn("SSO1", got)
        self.assertNotIn("NOPE", got)

    def test_sso_picks_only_sso_domain(self):
        detail = {
            "moodle.scnu.edu.cn": {"MoodleSession": "MS1"},
            "sso.scnu.edu.cn": {"JSESSIONID": "SSO1", "cookie_session_id": "SID1"},
        }
        got = cookies._sso_cookie({}, detail)
        self.assertEqual(got, "JSESSIONID=SSO1; cookie_session_id=SID1")
        self.assertNotIn("MS1", got)

    def test_no_entries_returns_none(self):
        self.assertIsNone(cookies._moodle_cookie({}, {}))
        self.assertIsNone(cookies._sso_cookie({}, {}))

    def test_analytics_cookies_are_filtered_out(self):
        """平台上挂着 Matomo 统计与主题偏好，这些混进请求头只会变脏，必须剔除。"""
        detail = {
            "moodle.scnu.edu.cn": {
                "MoodleSession": "MS1",
                "_pk_id.3.98ff": "12b6230372591c7a.1791391077.",
                "_pk_ses.3.98ff": "1",
                "theme_style": "dark",
            },
        }
        got = cookies._moodle_cookie({}, detail)
        self.assertEqual(got, "MoodleSession=MS1")
        self.assertNotIn("_pk_", got)
        self.assertNotIn("theme_style", got)


class TestFindCookieDb(unittest.TestCase):
    """cookie 库位置随 Chromium 版本漂移，两条路径都要认。"""

    def test_default_network_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "Default" / "Network" / "Cookies"
            db.parent.mkdir(parents=True)
            db.write_bytes(b"x")
            self.assertEqual(cookies._find_cookie_db(root), db)

    def test_falls_back_to_active_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Local State").write_text(
                json.dumps({"profile": {"last_used": "Profile 3"}}), encoding="utf-8"
            )
            db = root / "Profile 3" / "Network" / "Cookies"
            db.parent.mkdir(parents=True)
            db.write_bytes(b"x")
            self.assertEqual(cookies._find_cookie_db(root), db)

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cookies._find_cookie_db(Path(tmp)))


class TestInspectProfiles(unittest.TestCase):
    """体检要如实反映「浏览器里有会话 / 没会话 / 库读不到」三种状态。"""

    def _patch_profiles(self, pairs):
        from unittest.mock import patch

        return patch.object(cookies, "browser_profiles", lambda: pairs)

    def test_reports_logged_in_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_profile(root, plain=[("moodle.scnu.edu.cn", "MoodleSession", "MS1")])
            with self._patch_profiles([("Chrome", root)]), \
                 self._patch_master_key():
                got = cookies.inspect_profiles()
            self.assertEqual(len(got), 1)
            self.assertTrue(got[0].readable)
            self.assertTrue(got[0].has_moodle_session)
            self.assertEqual(got[0].cookie_count, 1)

    def test_reports_not_logged_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_profile(root, plain=[("example.com", "x", "1")])
            with self._patch_profiles([("Chrome", root)]), \
                 self._patch_master_key():
                got = cookies.inspect_profiles()
            self.assertTrue(got[0].readable)
            self.assertFalse(got[0].has_moodle_session)
            self.assertEqual(got[0].cookie_count, 0)

    def test_reports_unreadable_when_key_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_profile(root, plain=[("moodle.scnu.edu.cn", "MoodleSession", "MS1")])
            from unittest.mock import patch

            with self._patch_profiles([("Chrome", root)]), \
                 patch.object(cookies, "_master_key", side_effect=OSError("no dpapi")):
                got = cookies.inspect_profiles()
            self.assertFalse(got[0].readable)

    def test_sso_login_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_profile(root, plain=[
                ("sso.scnu.edu.cn", "JSESSIONID", "SSO1"),
                ("sso.scnu.edu.cn", "cookie_session_id", "SID1"),
            ])
            with self._patch_profiles([("Chrome", root)]), \
                 self._patch_master_key():
                got = cookies.inspect_profiles()
            self.assertTrue(got[0].has_sso)
            self.assertFalse(got[0].has_moodle_session)

    def _patch_master_key(self):
        from unittest.mock import patch

        return patch.object(cookies, "_master_key", lambda root: b"\x00" * 32)


class TestDecryptValue(unittest.TestCase):
    """只处理 v10；v20（App-Bound）必须安全地返回 None，而不是抛异常。"""

    def test_v20_returns_none(self):
        self.assertIsNone(cookies._decrypt_value(b"\x00" * 32, b"v20" + b"x" * 40))

    def test_garbage_returns_none(self):
        self.assertIsNone(cookies._decrypt_value(b"\x00" * 32, b"v10" + b"x" * 40))


class TestTempCopyCleanup(unittest.TestCase):
    """读 cookie 的临时副本必须删掉——里面是明文登录凭证。

    2026-10-27 发版前审计发现 `_copy_cookie_db` 把库复制到 %TEMP% 后从不清理，
    每跑一次 import 就在磁盘上多留一份可读的 cookie。
    """

    def test_read_profile_removes_temp_copy(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            root.mkdir()
            _make_profile(root, plain=[("moodle.scnu.edu.cn", "MoodleSession", "MS1")])
            created: list[Path] = []
            real_mkdtemp = tempfile.mkdtemp

            def tracking_mkdtemp(*args, **kwargs):
                path = real_mkdtemp(*args, **kwargs)
                created.append(Path(path))
                return path

            with mock.patch("assignmentradar.cookies.tempfile.mkdtemp", tracking_mkdtemp), \
                    mock.patch.object(cookies, "_master_key", lambda _root: b"\x00" * 32):
                jar = cookies._read_profile("Chrome", root)

            self.assertIsNotNone(jar)
            self.assertEqual(jar.values.get("MoodleSession"), "MS1")
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0].exists(), "明文 cookie 副本不能留在 %TEMP%")


if __name__ == "__main__":
    unittest.main()
