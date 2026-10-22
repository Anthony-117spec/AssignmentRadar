#!/usr/bin/env python3
"""AssignmentRadar — Moodle 作业雷达。

用法:
    radar.bat run                托盘常驻 + 定时轮询 + 本地面板（默认，可省略 run）
    radar.bat check              验证会话，列出课程与作业（只读）
    radar.bat list               终端展示作业清单（零请求，秒开）
    radar.bat import --sso       从浏览器取回会话（免手抄，推荐）
    radar.bat set-token          配置 web service 令牌（配了就不怕会话过期）
    radar.bat attach             归档老师附件 + 作业页截图
    radar.bat install            注册开机自启
    radar.bat --version          查看版本

完整说明见 docs/manual.md，架构与设计取舍见 docs/architecture.md。
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    from assignmentradar.cli import run_cli

    raise SystemExit(run_cli(BASE_DIR))


if __name__ == "__main__":
    main()
