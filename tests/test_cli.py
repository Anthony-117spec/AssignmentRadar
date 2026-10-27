"""CLI 辅助逻辑的离线单测：单实例锁。

背景（2026-10-09 真事故）：`radar run` 原来只靠"探测面板端口是否被占用"来判重，
而这是**竞态**的——两个实例可能同时发现"还没人监听"，于是双双启动、一起跑了三个多小时，
造成双倍轮询、双倍通知、并发写同一个 SQLite。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assignmentradar.cli import InstanceLock, _needs_setup  # noqa: E402


class TestInstanceLock(unittest.TestCase):
    def test_first_acquire_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = InstanceLock(Path(tmp) / "radar.lock")
            self.assertTrue(lock.acquire())
            lock.release()

    def test_second_instance_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radar.lock"
            first, second = InstanceLock(path), InstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire(), "已有实例持锁时不该再放行第二个")
            first.release()
            self.assertTrue(second.acquire(), "前一个释放后应当能拿到")
            second.release()

    def test_lock_file_records_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radar.lock"
            lock = InstanceLock(path)
            self.assertTrue(lock.acquire())
            self.assertEqual(path.read_text(encoding="ascii").strip(), str(os.getpid()))
            lock.release()

    def test_stale_file_does_not_lock_you_out(self):
        """崩溃残留的锁文件只是垃圾——锁由 OS 持有，文件本身不该把人挡在外面。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radar.lock"
            path.write_text("99999")  # 上次崩溃留下的、早已不存在的 PID
            lock = InstanceLock(path)
            self.assertTrue(lock.acquire())
            lock.release()

    def test_context_manager_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radar.lock"
            with InstanceLock(path) as lock:
                self.assertIsNotNone(lock._fh)
            again = InstanceLock(path)
            self.assertTrue(again.acquire())
            again.release()

    def test_release_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = InstanceLock(Path(tmp) / "radar.lock")
            lock.acquire()
            lock.release()
            lock.release()  # 再放一次不该炸


class TestInstanceLockAcrossProcesses(unittest.TestCase):
    """跨进程才是真实场景。

    同进程里两个句柄各自 open 都能成功，正好掩盖了 Windows 的真实行为：
    **只要别的进程持着锁，连 open 都会抛 PermissionError**。2026-10-09 实测踩到——
    `acquire()` 里的 open 写在 try 外面，于是第二个实例不是被礼貌拒绝，
    而是带着堆栈直接崩掉。
    """

    def test_second_process_is_refused_without_crashing(self):
        import subprocess
        import time

        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "radar.lock"
            holder_src = (
                "import sys, time\n"
                f"sys.path.insert(0, r'{root}')\n"
                "from pathlib import Path\n"
                "from assignmentradar.cli import InstanceLock\n"
                f"lock = InstanceLock(Path(r'{lock_path}'))\n"
                "print('ACQUIRED' if lock.acquire() else 'REFUSED', flush=True)\n"
                "time.sleep(3)\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", holder_src],
                                    stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ACQUIRED")
                time.sleep(0.4)
                # 这里必须优雅返回 False；抛 PermissionError 就是回归
                second = InstanceLock(lock_path)
                self.assertFalse(second.acquire(), "第二个进程应被拒绝")
            finally:
                proc.wait(timeout=20)
                if proc.stdout:
                    proc.stdout.close()

    def test_lock_is_free_again_after_holder_exits(self):
        import subprocess
        import time

        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "radar.lock"
            holder_src = (
                "import sys, time\n"
                f"sys.path.insert(0, r'{root}')\n"
                "from pathlib import Path\n"
                "from assignmentradar.cli import InstanceLock\n"
                f"lock = InstanceLock(Path(r'{lock_path}'))\n"
                "print('ACQUIRED' if lock.acquire() else 'REFUSED', flush=True)\n"
                "time.sleep(0.6)\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", holder_src],
                                    stdout=subprocess.PIPE, text=True)
            self.assertEqual(proc.stdout.readline().strip(), "ACQUIRED")
            proc.wait(timeout=20)
            if proc.stdout:
                proc.stdout.close()
            time.sleep(0.3)
            after = InstanceLock(lock_path)
            self.assertTrue(after.acquire(), "持有者退出后应能正常启动")
            after.release()


class TestNeedsSetup(unittest.TestCase):
    """只配 SSO 也算"配置过"。

    2026-10-27 发版前审计发现 `_needs_setup` 只看 cookie / ws_token，
    只配 sso_cookie 的用户启动 `run` 会被当成首次运行、打印引导后直接退出。
    """

    def test_sso_only_counts_as_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.toml").write_text(
                'sso_cookie = "JSESSIONID=x"\n', encoding="utf-8"
            )
            self.assertFalse(_needs_setup(Path(tmp)))

    def test_empty_config_needs_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.toml").write_text(
                'cookie = ""\nsso_cookie = ""\nws_token = ""\n', encoding="utf-8"
            )
            self.assertTrue(_needs_setup(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
