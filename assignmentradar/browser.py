"""定位本机浏览器可执行文件与私有配置目录。

历史沿革（重要）
----------------
本模块原先实现的是「`--dump-dom` 无头刷新」路线：起一个无头 Chrome 抓 DOM，
再从私有配置目录的 cookie 库里解出会话。这条路在 2026-10-08 被证实**走不通**：

* 新版 Chrome 对 cookie 启用了 App-Bound 加密（密文前缀 `v20`，密钥被**系统级**
  DPAPI 包着），直读 cookie 库无论如何都解不开；
* `--cookie-output-file` 在当前 Chromium 里已不存在。

于是读取会话的职责整体移交给了 `cdp.py`（浏览器调试协议——让浏览器**自己**
把 cookie 明文交出来，绕开加密）。本模块现在只保留两件与协议无关的事：

1. 找到 Chrome/Edge 的可执行文件（`find_browser`）；
2. 约定私有配置目录的位置（`profile_dir`）。

设计约束（与项目现有风格一致）
------------------------------
1. 绝不静默弹窗：可见窗口只在用户显式运行 `radar.bat browser login` 时出现。
2. 找不到浏览器不算错误：返回 None，让上层给出「装一个浏览器」的提示。
3. 只用标准库，不给项目增加依赖。
"""
from __future__ import annotations

import os
from pathlib import Path

# 浏览器可执行文件的常见位置（Windows）。必要时可用环境变量 RADAR_BROWSER 覆盖。
_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

# 私有配置目录名（放在 data/ 下，已随 data/ 一起被 gitignore）。
# 与 cdp.PROFILE_DIRNAME 保持一致——两边指向同一个目录，可互相接力。
PROFILE_DIRNAME = "browser_profile"


def find_browser() -> str | None:
    """找到本机可用的 Chrome/Edge 可执行文件。找不到返回 None（不是错误）。"""
    env = os.environ.get("RADAR_BROWSER")
    if env and Path(env).is_file():
        return env
    for cand in _CHROME_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def profile_dir(base_dir: Path) -> Path:
    """私有配置目录：会话落在这里，绝不碰用户日常浏览器。"""
    return base_dir / "data" / PROFILE_DIRNAME
