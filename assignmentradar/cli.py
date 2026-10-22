"""命令行入口：run / once / check / set-cookie / install / uninstall。"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import sys
import threading
import tomllib
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import load_config, set_cookie
from .poller import App

log = logging.getLogger("radar")


def _setup_logging(base_dir: Path) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        (base_dir / "data").mkdir(exist_ok=True)
        fileh = RotatingFileHandler(
            base_dir / "data" / "radar.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except Exception:
        pass


_FIRST_RUN_GUIDE = """
还没有配置平台会话，先做这三步：

  1. 在浏览器里登录   https://moodle.scnu.edu.cn
  2. 取回会话         radar.bat import --sso
  3. 再启动           radar.bat run

更省事的做法是配一个 web service 令牌（配一次，长期有效，不怕会话过期）：
                     radar.bat set-token

本工具不接触账号密码，只读取你浏览器里已有的登录状态。
（确实想跳过这一步直接启动，加 --force）
"""


def _needs_setup(base_dir: Path) -> bool:
    """判断是不是"还没配置过"的首次运行。

    只读 config.toml，不走 load_config —— 后者在文件不存在时会写一份模板出来，
    那会让"首次运行"这件事在检测之前就消失。
    配置存在但解析失败时不拦（交给 load_config 去报真正的错）。
    """
    path = base_dir / "config.toml"
    if not path.exists():
        return True
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    cookie = str(raw.get("cookie") or "").strip()
    token = str(raw.get("ws_token") or "").strip()
    return not (cookie or token)


def _console_safe() -> None:
    """让输出在 GBK 控制台下也不会因为符号编码不了而崩掉。

    Windows 的 cmd 默认代码页是 936(GBK)，`✓`/`✗` 这类符号编不出 GBK 字节。
    直接连控制台时 Python 走 Unicode API 没问题，但**一旦重定向到文件或管道**
    （`radar.bat list > out.txt`、被别的脚本捕获输出）就会抛 UnicodeEncodeError，
    整个命令以 traceback 收场。降级成 `?` 也比崩掉强。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def run_cli(base_dir: Path) -> int:
    _console_safe()

    # --version 必须在"补 run"之前拦下：不带子命令时我们会默认按 run 处理，
    # 不先拦的话 `radar.py --version` 会被改写成 `run --version` 而报 unrecognized。
    if any(a in ("--version", "-V") for a in sys.argv[1:]):
        print(f"AssignmentRadar {__version__}")
        return 0

    parser = argparse.ArgumentParser(
        prog="radar.py", description="AssignmentRadar — Moodle 作业雷达"
    )
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="托盘常驻 + 定时轮询 + 本地面板（默认）")
    p_run.add_argument("--no-tray", action="store_true", help="不启动托盘，前台控制台运行")
    p_run.add_argument("--no-panel", action="store_true",
                       help="不启动网页面板（只要托盘 + 定时轮询；清单用 radar.bat list 看）")
    p_run.add_argument("--force", action="store_true",
                       help="跳过「还没配置会话」的检查，直接启动")

    p_loop = sub.add_parser(
        "loop", help="无头刷新循环：只轮询，不托盘/不面板/不弹窗（供一键启动器串起来跑）")

    p_once = sub.add_parser("once", help="只跑一轮轮询（调试用）")
    p_once.add_argument("--no-notify", action="store_true", help="本轮不发任何通知")

    p_check = sub.add_parser("check", help="验证登录：列出课程与作业（只读，不写库不发通知）")
    p_check.add_argument("--save-html", action="store_true",
                         help="把首页 HTML 脱敏后存到 data/debug_my.html（用于排查课程解析问题）")

    p_list = sub.add_parser("list", help="终端展示作业清单：未交（按 DDL 紧迫度）/ 已交")
    p_list.add_argument("--no-color", action="store_true", help="禁用彩色输出")
    p_list.add_argument(
        "--refresh", action="store_true",
        help="先现场刷新一轮再展示（约半分钟）。默认只读缓存、零请求、秒开")
    p_list.add_argument("--pause", action="store_true",
                        help="展示完等待回车再关闭（独立终端窗口弹出时用）")

    p_cookie = sub.add_parser("set-cookie", help="录入浏览器复制的 Cookie")
    p_cookie.add_argument("--cookie", help="直接传入 Cookie 字符串（省略则交互式粘贴）")
    p_cookie.add_argument("--sso", action="store_true", help="录入 sso.scnu.edu.cn 的会话 cookie（用于自动续期）")

    p_import = sub.add_parser(
        "import", help="从本机浏览器直接读取平台会话 cookie（免手抄，推荐）"
    )
    p_import.add_argument("--browser", help="只读某个浏览器（如 Chrome / Edge）")
    p_import.add_argument("--dry-run", action="store_true", help="只体检并显示将要写入的内容，不落盘")
    p_import.add_argument("--sso", action="store_true",
                          help="同时导入 SSO cookie（浏览器处于统一身份认证登录态时才有）")
    p_import.add_argument("--cdp", action="store_true",
                          help="强制改用浏览器调试协议读私有配置目录（绕开新版 Chrome 的 cookie 加密）")

    p_browser = sub.add_parser(
        "browser", help="浏览器模式：用真实浏览器完成登录并取回会话（推荐，绕开 cookie 加密）"
    )
    p_browser.add_argument("action", choices=["login", "probe", "status"],
                           help="login=打开窗口让你登录一次；probe=无头读一次会话；status=查看状态")
    p_browser.add_argument("--timeout", type=int, default=300,
                           help="login 时等待登录的最长秒数（默认 300）")

    p_token = sub.add_parser(
        "set-token", help="粘贴平台 web service token（配了它就不再怕会话过期）"
    )
    p_token.add_argument("--token", help="直接给出 token（不填则交互式粘贴）")
    p_token.add_argument(
        "--open", action=argparse.BooleanOptionalAction, default=True,
        help="交互式粘贴时自动帮你打开令牌创建页（默认开；不想开浏览器加 --no-open）")

    p_attach = sub.add_parser(
        "attach", help="下载老师随作业发布的附件，按「课程 / 作业」归档到本地"
    )
    p_attach.add_argument("--course", help="只处理名字含该关键词的课程")
    p_attach.add_argument("--pending", action="store_true",
                          help="只下载尚未提交的作业的附件")
    p_attach.add_argument("--force", action="store_true", help="已存在的文件也重新下载")
    p_attach.add_argument("--dry-run", action="store_true", help="只列出会下载什么，不落盘")
    p_attach.add_argument("--dir", help="覆盖归档目录（默认取 config.toml 的 homework_dir）")
    p_attach.add_argument("--no-shots", action="store_true",
                          help="不截作业布置页的整页图（默认会截，便于人工核对归档是否完整）")

    p_renew = sub.add_parser("renew", help="手动触发一次 SSO 自动续期（诊断用）")
    p_renew.add_argument("--debug", action="store_true",
                         help="把续期落点页面脱敏后存到 data/renew_debug.html")
    sub.add_parser("install", help="注册开机自启")
    sub.add_parser("uninstall", help="移除开机自启")

    # 不带子命令时默认按 run 处理（让 `radar.bat --no-tray` 这类写法也能工作）。
    # 但 `--help` / `-h` 必须留给顶层 parser：用户敲 `radar.py --help` 是想看
    # 全部子命令列表，而不是 run 自己的两个开关。
    argv = sys.argv[1:]
    if not argv:
        argv = ["run"]
    elif argv[0].startswith("-") and argv[0] not in ("--help", "-h"):
        argv = ["run"] + argv
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"
    _setup_logging(base_dir)

    if cmd == "set-token":
        return _cmd_set_token(base_dir, args)
    if cmd == "set-cookie":
        return _cmd_set_cookie(base_dir, args)
    if cmd == "import":
        return _cmd_import(base_dir, args)
    if cmd == "browser":
        return _cmd_browser(base_dir, args)
    if cmd == "attach":
        return _cmd_attach(base_dir, args)
    if cmd == "list":
        return _cmd_list(base_dir, args)
    if cmd == "renew":
        return _cmd_renew(base_dir, args)
    if cmd == "check":
        return _cmd_check(base_dir, args)
    if cmd == "once":
        return _cmd_once(base_dir, args)
    if cmd == "install":
        return _cmd_install(base_dir)
    if cmd == "uninstall":
        return _cmd_uninstall(base_dir)
    if cmd == "run":
        # 首次运行（还没配置任何会话/令牌）时，直接进托盘会让人一脸茫然：
        # 图标出现了，但里面什么都没有。这里先给三步引导。
        if not getattr(args, "force", False) and _needs_setup(base_dir):
            print(_FIRST_RUN_GUIDE)
            return 0
        return _cmd_run(base_dir, args)
    if cmd == "loop":
        return _cmd_loop(base_dir, args)
    parser.print_help()
    return 2


# ---------------------------------------------------------------- 子命令


def _read_multiline(prompt: str) -> str:
    """支持多行粘贴：空行结束；stdin 重定向时读全部。"""
    print(prompt)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip() and lines:
            break
        lines.append(line)
    return "\n".join(lines)


# cookie 条目名只在某一域出现，用于识别粘贴来源（两个域的 Cookie 长得很像，极易贴混）
_SSO_MARKERS = {"JSESSIONID", "cookie_session_id", "machineSerial", "login_type", "login_user"}


def _classify_cookie(raw: str) -> str:
    """猜测粘贴的 cookie 来自哪个域：sso / moodle / unknown。"""
    from .config import parse_cookie_header

    names = set(parse_cookie_header(raw))
    if "MoodleSession" in names:
        return "moodle"
    if names & _SSO_MARKERS:
        return "sso"
    return "unknown"


def _mask_secret(text: str) -> str:
    """给用户看一眼"确实收到了"，但不把凭证整串打在屏幕上。"""
    if len(text) <= 10:
        return "*" * len(text)
    return f"{text[:4]}…{text[-4:]}"


# 建令牌时「服务」下拉框该选哪一项。按 /user/managetoken.php 的实际下拉框核对过：
#   砺儒小课堂              ← 学校自建，作业/成绩相关函数齐全（首选）
#   Attendance / ai_box / sjj ← 同为学生自建的服务，可作备选
#   Moodle mobile web service ← 校方裁剪过，标准函数全被挡（accessexception），必失败
#
# 注：这里保留真实服务名是有意的——引导文案要让人能在下拉框里对上号，
# 换成"学校自建的那个"就退回到"让人自己猜"，而猜错的那一项恰好叫
# Moodle mobile web service（名字最像、却完全不能用）。README 与文档里
# 一律不写这个名称，只在终端提示里出现。
_TOKEN_PAGE = "https://moodle.scnu.edu.cn/user/managetoken.php"
_TOKEN_SERVICE = "砺儒小课堂"
_TOKEN_SERVICE_ALT = "Attendance、ai_box、sjj"
_TOKEN_SERVICE_BAD = "Moodle mobile web service"


def _cmd_set_token(base_dir: Path, args) -> int:
    """写入平台 web service token。

    token 在平台「安全密钥」页自己创建（`/user/managetoken.php`），不需要账号密码。
    ⚠ 服务选 `学校自建的服务`（学校自建）；**不要**选 `Moodle mobile web service`——
      校方把那个服务的标准函数全挡了（`accessexception`）。
    """
    from .config import set_ws_token
    from .wsapi import WsApi, WsError, WsUnavailable

    url = _TOKEN_PAGE
    token = (getattr(args, "token", None) or "").strip()

    if not token:
        print()
        print("配一个长期令牌，之后会话过期也不影响数据。全程不碰账号密码。")
        print()
        print("  1. 用【已登录学校平台】的浏览器打开下面这个页面")
        print(f"       {url}")
        print()
        print("  2. 点「创建令牌」，只填两栏：")
        print("       令牌名称 → 随便写，比如 radar")
        print("       有效期   → 留空（留空 = 一直有效）")
        print()
        print(f"  3. 「服务」下拉框里选【{_TOKEN_SERVICE}】 ← 就选这一项")
        print("       这是学校自建的服务，作业、成绩相关的函数都在里面。")
        print(f"       千万别选【{_TOKEN_SERVICE_BAD}】：名字最像，")
        print("       但被校方裁剪过，选了 100% 失败。")
        print(f"       下拉框里若还有 {_TOKEN_SERVICE_ALT}，也是学校")
        print(f"       自建的，可以当备选，但优先 {_TOKEN_SERVICE}。")
        print()
        print("  4. 点表单下方的保存按钮（「保存更改」或「创建」）")
        print("     页面会显示一串字符 —— 只显示这一次，立刻复制它。")
        print()
        print("  提醒：页面上已有的 Webservice-xxxxxx 那些，令牌值早就看不到了")
        print("        （只显示名称、服务、上次访问），所以只能新建一枚。")
        print()
        # 只有交互式终端才自动开浏览器：给了 --token / --no-open / 管道输入时都不打扰
        if getattr(args, "open", True) and sys.stdin.isatty():
            try:
                webbrowser.open(url)
                print("  （已帮你打开这个页面；没弹出来就手动复制上面的地址）")
                print()
            except Exception:
                pass
        try:
            token = getpass.getpass("粘贴令牌后回车（输入不回显，直接粘贴即可）：").strip()
        except Exception as exc:
            print(f"✗ 无法读取输入：{exc}")
            print("  换个终端再试，或者直接把令牌作为参数传进来：")
            print("    radar.bat set-token --token <你的令牌>")
            return 1
        if token:
            print(f"  收到 {len(token)} 个字符：{_mask_secret(token)}")

    if not token:
        print("✗ token 为空。")
        return 1

    try:
        set_ws_token(base_dir, token)
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ 已写入 config.toml 的 ws_token（{len(token)} 字符）")

    # 立刻验一次：写进去却用不了的 token，比不写更糟（下次轮询会静默回退，
    # 你会以为令牌通路在工作）
    cfg = load_config(base_dir)
    ws = WsApi(cfg)
    print("正在验证令牌…")
    try:
        courses = ws.courses(cfg.semester)
    except WsUnavailable as exc:
        print(f"✗ 令牌不可用：{exc}")
        print()
        print("  最可能的原因是「服务」选错了。回去重新建一枚，按这个顺序试：")
        print(f"    ① {_TOKEN_SERVICE}   ← 首选")
        print(f"    ② {_TOKEN_SERVICE_ALT}   ← 学校自建的其它服务")
        print(f"    唯一别选的是 {_TOKEN_SERVICE_BAD}。")
        print("  注意：已有的令牌看不到值，每次都要点「创建令牌」新建。")
        print(f"  页面：{url}")
        print()
        print("  不想折腾令牌也行——会话通路照样能用，只是大约每 10 小时要重取一次：")
        print("      radar.bat import --sso")
        return 1
    except WsError as exc:
        print(f"✗ 调用出错：{exc}")
        return 1
    print(f"✓ 令牌有效：读到 {len(courses)} 门本学期课程")
    if not courses:
        print()
        print("  0 门课通常是 config.toml 里 semester（学期分组名）和实际课程对不上，")
        print("  跟令牌本身无关。先跑 radar.bat check 看看能读到哪些课程。")
    for c in courses[:8]:
        print(f"    · {c.name}")
    if len(courses) > 8:
        print(f"    …还有 {len(courses) - 8} 门")
    print()
    print("之后轮询会优先走这条通路，不再依赖 cookie；会话只影响整页截图。")
    print("想立刻验证一遍：radar.bat list --refresh")
    return 0


def _cmd_set_cookie(base_dir: Path, args) -> int:
    want_sso = getattr(args, "sso", False)
    if want_sso:
        raw = getattr(args, "cookie", None) or _read_multiline(
            "请粘贴【已登录状态下】发给 sso.scnu.edu.cn 的请求的 Cookie 请求头整行：\n"
            "  F12 → 网络(Network) → 打开 https://sso.scnu.edu.cn → 点第一个 sso 文档请求\n"
            "  → 请求标头 → 复制 Cookie: 一整行（必须包含 JSESSIONID）\n"
            "（粘贴完回空行结束）"
        )
    else:
        raw = getattr(args, "cookie", None) or _read_multiline(
            "请粘贴【平台 moodle.scnu.edu.cn】请求的 Cookie 请求头整行：\n"
            "  F12 → 网络(Network) → 点一条发往 moodle.scnu.edu.cn 的【文档/Doc】请求\n"
            "  （或 应用程序 → Cookie → https://moodle.scnu.edu.cn 逐个复制）\n"
            "  → 请求标头 → 复制 Cookie: 一整行，原样粘贴即可\n"
            "  通常含 MoodleSession=…；也可能只有网关的 my_client_ticket=…，两种都收\n"
            "  ⚠ 别贴成统一身份认证 sso.scnu.edu.cn 的（那套含 JSESSIONID，要用 set-cookie --sso）\n"
            "（粘贴完回空行结束）"
        )

    target = "sso_cookie" if want_sso else "cookie"
    kind = _classify_cookie(raw)
    if kind == "sso" and not want_sso:
        # 最常见的翻车：把 SSO 的 Cookie 贴进了平台的字段——自动纠正到正确字段
        print("! 检测到粘贴的是统一身份认证（sso.scnu.edu.cn）的 Cookie（含 JSESSIONID 等条目），"
              "不是平台的。")
        print("  已自动写入 sso_cookie 字段（效果等同 radar set-cookie --sso），"
              "平台会话仍需另行复制。")
        target = "sso_cookie"
    elif kind == "moodle" and want_sso:
        print("! 检测到粘贴的是平台（moodle.scnu.edu.cn）的 Cookie（含 MoodleSession），"
              "不是统一身份认证的。")
        print("  已自动写入 cookie 字段（效果等同 radar set-cookie）。")
        target = "cookie"

    count = set_cookie(base_dir, raw, field=target)
    where = "SSO cookie（sso_cookie 字段）" if target == "sso_cookie" else "cookie（cookie 字段）"
    print(f"✓ 已写入 {count} 个 {where}到 config.toml（这是登录凭证，切勿外传 config.toml）")
    print("验证一下：python radar.py check   （会话过期时会自动尝试 SSO 续期）")
    return 0


def _cmd_import(base_dir: Path, args) -> int:
    """从本机浏览器的 cookie 库里直接把平台会话读出来写进 config.toml。

    这条路彻底绕开了手工复制：会话本来就在浏览器里，程序只是把它取出来，
    既不会贴错字段，也不用去猜哪个请求里才有 MoodleSession。

    当直读 cookie 库拿不到会话（新版 Chrome 的 App-Bound 加密会让 `v20` cookie
    解不开）时，自动改用浏览器调试协议走私有配置目录——CDP 让浏览器**自己**把
    cookie 交出来，完全绕开加密问题。
    """
    from .cookies import inspect_profiles, harvest

    prefer = getattr(args, "browser", None)
    dry = getattr(args, "dry_run", False)
    want_sso = getattr(args, "sso", False)

    if getattr(args, "cdp", False):
        return _import_via_cdp(base_dir, want_sso=want_sso, dry=dry)

    print("正在读取本机浏览器的 cookie 库…")
    statuses = inspect_profiles()
    if not statuses:
        print("✗ 没有找到 Chrome / Edge 的用户数据目录。")
        print("  可用 RADAR_BROWSER_DATA=<User Data 目录> 指定自定义位置。")
        return 1

    for st in statuses:
        if st.readable:
            marks = []
            if st.has_moodle_session:
                marks.append("平台已登录")
            if st.has_sso:
                marks.append("统一身份认证已登录")
            tail = "、".join(marks) if marks else "未登录平台"
            print(f"  · {st.browser}：{tail}（scnu 相关 cookie {st.cookie_count} 条）")
        else:
            print(f"  · {st.browser}：cookie 库读不到（多半是浏览器正在运行并独占该文件）")

    picked = harvest(prefer=prefer)
    # 优先挑带 MoodleSession 的那个 profile（真正的平台会话）
    best = None
    for jar, moodle, sso in picked:
        detail = getattr(jar, "detail", {})
        has_session = any(
            "MoodleSession" in detail.get(h, {})
            for h in detail if "moodle.scnu.edu.cn" in h or h in (".scnu.edu.cn", "scnu.edu.cn")
        )
        if has_session:
            best = (jar, moodle, sso)
            break
    if best is None and picked:
        best = picked[0]

    if best is None or not best[1]:
        # 直读失败：最常见的就是新版 Chrome 把所有 cookie 加密成了 v20
        # （App-Bound Encryption，密钥由系统级 DPAPI 包着，不越权解不开）。
        # 此时改用调试协议让浏览器自己交 cookie——这在私有配置目录里可行。
        unreadable = any(not st.readable for st in statuses)
        readable_but_empty = any(st.readable and st.cookie_count == 0 for st in statuses)
        if unreadable or readable_but_empty:
            print()
            print("· 直读 cookie 库没能拿到会话（新版 Chrome 的 App-Bound 加密会让"
                  " cookie 解不开）。")
            print("  改用浏览器调试协议再试一次…")
            return _import_via_cdp(base_dir, want_sso=want_sso, dry=dry)

        print()
        print("✗ 浏览器里没有找到可用的平台会话。")
        print("  请先在浏览器登录 https://moodle.scnu.edu.cn（登录的是哪个浏览器就读哪个），")
        print("  然后重新运行  python radar.py import")
        print("  （Edge 运行时其 cookie 库会被独占，读不到；此时请用 Chrome。）")
        print("  若仍读不到，可改用独立窗口登录一次：python radar.py browser login")
        return 1

    jar, moodle, sso = best
    if dry:
        print(f"\n[dry-run] 将写入 cookie 字段（来自 {jar.browser}）：\n  {_mask(moodle)}")
        if want_sso and sso:
            print(f"[dry-run] 将写入 sso_cookie 字段：\n  {_mask(sso)}")
        return 0

    n = set_cookie(base_dir, moodle)
    print(f"\n✓ 已从 {jar.browser} 读取平台会话并写入 config.toml（{n} 个条目）")
    print(f"  {_mask(moodle)}")
    if sso:
        n2 = set_cookie(base_dir, sso, field="sso_cookie")
        print(f"✓ 同时找到 SSO 会话，已写入 sso_cookie 字段（{n2} 个条目）")
    else:
        print("（未同时导入 SSO cookie；需要时加 --sso）")
    print("\n验证一下：python radar.py check")
    return 0


def _mask(cookie_str: str) -> str:
    """只露出条目名与前 4 个字符，避免把凭证完整打印到屏幕/日志里。"""
    out = []
    from .config import parse_cookie_header

    for name, value in parse_cookie_header(cookie_str).items():
        head = value[:4]
        out.append(f"{name}={head}…({len(value)}字符)")
    return "; ".join(out)


def _import_via_cdp(base_dir: Path, *, want_sso: bool, dry: bool) -> int:
    """走调试协议从私有配置目录取会话（绕开 cookie 加密问题的兜底路径）。

    HEADLESS 起私有配置目录的 Chrome → 读 /my/ → 若已登录，取出 cookie 落盘。
    若该目录从未登录过，则明确提示用户先运行 `radar browser login`。
    """
    from .browser import find_browser
    from .cdp import BrowserSession, CdpError
    from .config import load_config

    cfg = load_config(base_dir)
    exe = find_browser()
    if exe is None:
        print("✗ 未找到 Chrome / Edge，无法走调试协议。可用 RADAR_BROWSER=<路径> 指定。")
        return 1

    print("正在用调试协议读取私有配置目录里的会话…")
    bs = BrowserSession(base_dir, cfg.base_url, executable=exe)
    try:
        bs.start(f"{cfg.base_url}/my/", headless=True)
        snap = bs.snapshot()
    except CdpError as exc:
        print(f"✗ 调试协议读取失败：{exc}")
        print("  可改用  python radar.py browser login  打开窗口登录一次。")
        return 1
    finally:
        bs.stop()

    if not snap.logged_in or not snap.cookie:
        print("✗ 私有配置目录里还没有登录态。")
        print("  请先运行：python radar.py browser login")
        print("  （会打开一个独立窗口，登录一次即可；之后 import 就能自动读取。）")
        return 1

    print(f"✓ 取回平台会话（{snap.url}）")
    if dry:
        print(f"[dry-run] 将写入 cookie 字段：\n  {_mask(snap.cookie)}")
        if want_sso and snap.sso_cookie:
            print(f"[dry-run] 将写入 sso_cookie 字段：\n  {_mask(snap.sso_cookie)}")
        return 0

    n = set_cookie(base_dir, snap.cookie)
    print(f"✓ 已写入 config.toml（{n} 个条目）：{_mask(snap.cookie)}")
    if want_sso and snap.sso_cookie:
        n2 = set_cookie(base_dir, snap.sso_cookie, field="sso_cookie")
        print(f"✓ 同时找到 SSO 会话，已写入 sso_cookie 字段（{n2} 个条目）")
    elif want_sso:
        print("（私有配置目录里没有 SSO 会话；需要时在该窗口里也登录一次统一身份认证）")
    print("\n验证一下：python radar.py check")
    return 0


def _cmd_browser(base_dir: Path, args) -> int:
    """浏览器模式：用**真实浏览器**完成登录并取回会话。

    为什么是它
    ----------
    新版 Chrome 对 cookie 启用了 App-Bound 加密（密文以 `v20` 开头，密钥被系统级
    DPAPI 包着），直读 cookie 库根本解不开——这正是 `radar import` 在用户机器上
    失败的原因。但浏览器自己知道所有 cookie：只要给它一个调试端口，用
    `Network.getAllCookies` 就能拿到明文，完全不需要任何权限。

    Chrome 禁止对「默认」用户数据目录开远程调试，所以这里用**项目私有目录**
    `data/browser_profile`——顺带满足「绝不碰用户日常浏览器」的原则。
    """
    from .browser import find_browser
    from .cdp import BrowserSession, CdpError, profile_dir
    from .config import load_config

    cfg = load_config(base_dir)
    action = args.action
    exe = find_browser()
    prof = profile_dir(base_dir)

    if action == "status":
        print(f"浏览器可执行文件：{exe or '未找到（装 Chrome 或 Edge 即可）'}")
        print(f"私有配置目录：{prof}（{'已存在' if prof.exists() else '尚未创建'}）")
        if exe is None:
            return 1
        if not prof.exists():
            print("（还没有登录过。运行  python radar.py browser login  开一次窗口即可。）")
            return 0
        bs = BrowserSession(base_dir, cfg.base_url, executable=exe)
        try:
            bs.start(f"{cfg.base_url}/my/", headless=True)
            snap = bs.snapshot()
        except CdpError as exc:
            print(f"✗ 读取私有配置目录失败：{exc}")
            return 1
        finally:
            bs.stop()
        print(f"登录态：{'已登录' if snap.logged_in else '未登录（需要 browser login）'}")
        if snap.cookie:
            print(f"平台 cookie：{_mask(snap.cookie)}")
        if snap.sso_cookie:
            print(f"SSO cookie：{_mask(snap.sso_cookie)}")
        print(f"私有目录里的 scnu cookie 共 {len(snap.raw)} 条原始条目")
        return 0

    if exe is None:
        print("✗ 未找到 Chrome / Edge。可用 RADAR_BROWSER=<浏览器路径> 指定。")
        return 1

    if action == "login":
        return _browser_login(base_dir, cfg, exe, prof, timeout=getattr(args, "timeout", 300))

    if action == "probe":
        bs = BrowserSession(base_dir, cfg.base_url, executable=exe)
        try:
            bs.start(f"{cfg.base_url}/my/", headless=True)
            snap = bs.snapshot()
        except CdpError as exc:
            print(f"✗ 调试协议读取失败：{exc}")
            return 1
        finally:
            bs.stop()
        print(f"登录态：{'已登录' if snap.logged_in else '未登录/无会话'}")
        if snap.cookie:
            print(f"取回会话：{_mask(snap.cookie)}")
        else:
            print("（没有取回任何 cookie；先运行 python radar.py browser login 登录一次）")
        return 0 if (snap.logged_in and snap.cookie) else 1
    return 2


def _browser_login(base_dir: Path, cfg, exe: str, prof: Path, *, timeout: int) -> int:
    """打开可见窗口让用户登录一次；轮询到登录成功即自动取回会话。

    CDP 让整个过程可观测：窗口一登录成功，我们立刻读到 cookie 并落盘；
    用户不用手动关窗口，也不用再去猜有没有成功。
    """
    from .cdp import BrowserSession, CdpError

    print("即将打开一个独立的浏览器窗口（本工具自己的私有配置目录，")
    print("不会动你日常浏览器的任何数据）。")
    print("请在弹出的窗口里登录平台；登录成功后【无需任何操作】——")
    print("本程序会自动发现、收好会话，并关闭这个窗口。")
    print(f"（最多等待 {timeout} 秒）")

    bs = BrowserSession(base_dir, cfg.base_url, executable=exe)
    try:
        bs.start(f"{cfg.base_url}/my/", headless=False, timeout=60)
    except CdpError as exc:
        print(f"✗ 拉起浏览器失败：{exc}")
        return 1

    ticks = {"n": 0}

    def on_tick(snap) -> None:
        ticks["n"] += 1
        if ticks["n"] % 5 == 0:
            print(f"  …仍在等待登录（{snap.url}）")

    try:
        snap = bs.wait_for_login(timeout=timeout, on_tick=on_tick)
    finally:
        bs.stop()

    if not snap.logged_in or not snap.cookie:
        print("✗ 等待超时，仍未检测到登录。可以重试  python radar.py browser login")
        print("  或改用  python radar.py import（直接从日常浏览器读，若未被加密拦截）。")
        return 1

    n = set_cookie(base_dir, snap.cookie)
    print(f"✓ 登录成功，已取回会话并写入 config.toml（{n} 个条目）：{_mask(snap.cookie)}")
    if snap.sso_cookie:
        n2 = set_cookie(base_dir, snap.sso_cookie, field="sso_cookie")
        print(f"✓ 同时取到 SSO 会话，已写入 sso_cookie 字段（{n2} 个条目）")
    print("\n验证一下：python radar.py check")
    return 0


def _cmd_attach(base_dir: Path, args) -> int:
    """把老师随作业发布的附件按「科目 / 作业」归档到本地。

    只下载**老师发布的**文件——你自己交上去的作业（`assignsubmission_file`）会被
    自动排除，否则归档目录会被自己的作业塞满。

    科目文件夹优先取 `config.toml` 的 `[course_dirs]` 映射，这样附件能直接落进你
    已经建好的科目文件夹（`D:/我的作业/web` 之类），而不是另起一个名字对不上的。
    """
    from . import attachments as att
    from . import shots
    from .config import load_config
    from .session import MoodleHTTP, SessionExpired
    from .wsapi import WsApi

    cfg = load_config(base_dir)
    ws = WsApi(cfg) if (cfg.ws_token or "").strip() else None
    if ws is None and not cfg.cookie and not cfg.sso_cookie:
        print("✗ 既没有 cookie 也没有 ws_token，先运行：python radar.py import")
        print("  或者用 python radar.py set-token 配一个令牌（推荐：不受会话过期影响）")
        return 1

    root = Path(args.dir) if getattr(args, "dir", None) else cfg.homework_path
    if root is None:
        print("✗ config.toml 里的 homework_dir 为空，没有归档位置。")
        print("  填一个目录名（相对项目根目录）即可，例如 homework_dir = \"homework\"")
        return 1

    only_cmids = None
    if getattr(args, "pending", False):
        from .store import Store

        store = Store(cfg.db_path)
        try:
            rows = store.assignments_with_course()
        finally:
            store.close()
        only_cmids = {r["cmid"] for r in rows if not r["submitted"]}
        if not only_cmids:
            print("台账里没有「未提交」的作业（先跑一轮 python radar.py once 建立台账）。")
            return 0

    http = MoodleHTTP(cfg)
    via = "令牌通路" if ws is not None else "页面解析"
    print(f"正在扫描本学期的作业附件（{via}；归档到 {root}）…")
    pages: list[tuple[str, str, str]] = []
    try:
        items = att.collect(
            http,
            semester=cfg.semester,
            course_filter=getattr(args, "course", None),
            only_cmids=only_cmids,
            on_event=print,
            # 顺手攒出"所有作业页"的清单——整页截图要用
            on_assignment=lambda c, a, u: pages.append((c, a, u)),
            ws=ws,
        )
    except SessionExpired:
        print("✗ 会话已失效。先运行：python radar.py import")
        return 1

    want_shots = not getattr(args, "no_shots", False) and bool(pages)
    rc = 0

    if not items:
        print("没有找到老师发布的附件。")
    else:
        by_course: dict[str, int] = {}
        for it in items:
            by_course[it.course] = by_course.get(it.course, 0) + 1

        # 课程名 → 本地科目文件夹。学生往往已经按科目建好了文件夹，映射错了会散落一地，
        # 所以这里把解析结果明明白白打出来，并标出可疑项。
        print(f"\n共发现 {len(items)} 个附件，涉及 {len(by_course)} 门课：")
        warn_unmapped: list[str] = []
        warn_missing: list[str] = []
        for name, n in sorted(by_course.items()):
            folder, mapped = att.resolve_subject(name, cfg.course_dirs)
            mark = "映射" if mapped else "自动"
            print(f"  · {name}（{n} 个） → {folder}/  [{mark}]")
            if not mapped:
                warn_unmapped.append(f"{name} → {folder}")
            elif not (root / folder).is_dir():
                warn_missing.append(f"{name} → {root / folder}")

        if warn_unmapped:
            print("\n⚠ 这些课程没有配映射，会在归档目录下新建同名文件夹：")
            for line in warn_unmapped:
                print(f"    {line}")
            print("  想归到已有文件夹，请在 config.toml 的 [course_dirs] 里加一行，例如：")
            print(f'    "{att.normalize_course(warn_unmapped[0].split(" → ")[0])}" = "你的文件夹名"')
        if warn_missing:
            print("\n⚠ 这些映射指向的文件夹还不存在（拼错了？），本次会新建：")
            for line in warn_missing:
                print(f"    {line}")

        if getattr(args, "dry_run", False):
            print("\n[dry-run] 将归档到：")
            for it in items:
                rel = att.dest_for(root, it.course, it.assignment, it.name, cfg.course_dirs)
                print(f"  {rel.relative_to(root)}")
            if want_shots:
                print(f"\n[dry-run] 另外会为 {len(pages)} 份作业各截一张整页图：")
                for c, a, _u in pages:
                    rel = shots.dest_for(root, c, a, cfg.course_dirs)
                    print(f"  {rel.relative_to(root)}")
            return 0

        summary = att.download(http, items, root, force=getattr(args, "force", False),
                               course_dirs=cfg.course_dirs, on_event=print)
        print(f"\n✓ 归档完成：{summary.describe()}")
        print(f"  位置：{root}")
        print("  （每个科目一个文件夹，里面再按作业分；重跑会自动跳过已下载的文件）")
        if summary.failed:
            rc = 1

    if want_shots:
        if _capture_shots(base_dir, cfg, pages, root,
                          force=getattr(args, "force", False)) != 0:
            rc = 1
    elif getattr(args, "no_shots", False):
        print("（已跳过作业页截图；去掉 --no-shots 即可为每份作业留一张整页图）")

    return rc


def _capture_shots(base_dir: Path, cfg, pages: list[tuple[str, str, str]], root: Path,
                   *, force: bool) -> int:
    """给每份作业截一张「布置页面」整页图，便于人工核对归档是否完整。

    截图必须靠真实浏览器渲染，所以走私有配置目录的 CDP 会话——那需要
    `radar browser login` 登录过一次。任何一步不可用都**只提示、不报错**，
    不能因为"截不了图"就让附件归档看起来失败。
    """
    from . import shots
    from .browser import find_browser
    from .cdp import BrowserSession, CdpError

    exe = find_browser()
    if exe is None:
        print("\n⚠ 没找到 Chrome / Edge，跳过作业页截图。")
        return 0

    tasks = [shots.ShotTask(c, a, u) for c, a, u in pages]
    bs = BrowserSession(base_dir, cfg.base_url, executable=exe)
    try:
        bs.start(headless=True, timeout=60)  # 默认导航到 /my/，用于预检登录态
    except CdpError as exc:
        print(f"\n⚠ 起浏览器失败，跳过作业页截图：{exc}")
        return 0

    try:
        if not shots.logged_in(bs):
            print("\n⚠ 私有配置目录里没有登录态，跳过作业页截图。")
            print("  截图需要真实浏览器渲染，先跑一次：python radar.py browser login")
            return 0
        print(f"\n正在为 {len(tasks)} 份作业截整页图（每份约 2 秒）…")
        summary = shots.capture(bs, tasks, root, course_dirs=cfg.course_dirs,
                                force=force, on_event=print)
    finally:
        bs.stop()

    print(f"✓ 截图完成：{summary.describe()}")
    if summary.saved or summary.skipped:
        print("  （每份作业文件夹里都有 `_作业页截图.png`，可与附件对照核对）")
    return 1 if summary.failed else 0


def _explain_stale(errors: list[str]) -> None:
    """刷新失败时别只说"失败了"——直接把下一步该敲什么告诉用户。

    以前这里只提示"检查 sso_cookie 是否配置/有效"，但现实中九成的情况是**会话整个失效**，
    而自动续期因为网关改过登录入口已经走不通（`/rump_frontend/login/` 现在返回 404）。
    用户真正需要知道的是：跑一次 `radar browser login` 就能恢复。
    """
    joined = "；".join(errors)
    print("  （下面展示的是上次成功巡逻的旧数据）")
    if any(k in joined for k in ("会话", "续期", "登录", "cookie")):
        print()
        print("  会话已失效，自动续期也没能救回来（网关改过登录入口，这条路已经走不通）。")
        print("  恢复只要一条命令：")
        print("      radar browser login     # 弹一个独立窗口，登录一次即可")
        print("  登录成功后，本命令会立刻刷新，后台轮询也会自动跟上。")
    else:
        print("  具体原因看 data/radar.log 的最后几行。")


def _cmd_list(base_dir: Path, args) -> int:
    """终端作业清单：未交在前（按 DDL 紧迫度），已交在后。"""
    import os
    from datetime import datetime

    from .config import load_config
    from .store import Store
    from .views import (  # noqa: F401
        build_grade_rows, build_lists, fmt_due, fmt_duration, fmt_left, fmt_refresh_path,
        fmt_score, short_course, urgency_mark,
    )

    cfg = load_config(base_dir)
    ignore_before = None
    if cfg.ignore_due_before:
        try:
            ignore_before = datetime.fromisoformat(cfg.ignore_due_before)
        except ValueError:
            print(f"⚠ ignore_due_before 格式应为 YYYY-MM-DD，收到 {cfg.ignore_due_before!r}，已忽略")

    colors_enabled = sys.stdout.isatty() and not getattr(args, "no_color", False)
    if sys.platform == "win32" and colors_enabled:
        os.system("")  # 启用 Windows 控制台的 ANSI 转义支持
    c = {
        "red": "\033[31m", "yellow": "\033[33m", "green": "\033[32m",
        "cyan": "\033[36m", "dim": "\033[2m", "bold": "\033[1m", "reset": "\033[0m",
    } if colors_enabled else {k: "" for k in
        ("red", "yellow", "green", "cyan", "dim", "bold", "reset")}

    store = Store(cfg.db_path)
    now = datetime.now()
    last_poll_raw = store.get_meta("last_success") or store.latest_success_ts()
    # 最近一轮的耗时与通路：读缓存展示，也用来解释"这次为什么快/慢"
    last_duration = store.get_meta("last_poll_duration")
    last_path = store.get_meta("last_poll_path")

    # **默认只读缓存（零请求）**：后台循环（`radar run` / `radar loop`）每 2 小时刷
    # 一轮、把结果写进台账，打开清单不该让用户干等半分钟——实测首屏要 25~40 秒，
    # 正是要消灭的东西。想现场刷新就显式加 `--refresh`。
    if getattr(args, "refresh", False):
        print("ℹ 正在现场刷新一轮（约半分钟）…")
        from .poller import App

        app = App(cfg, quiet_session_notify=True)
        try:
            s = app.poll_once()
            print(("✓ " if s.ok else "✗ ") + s.describe())
            if not s.ok:
                _explain_stale(s.errors)
        except Exception as exc:
            print(f"✗ 刷新失败：{exc}")
            _explain_stale([str(exc)])
        finally:
            app.shutdown()
        # 刷新成功时 last_success 已更新；失败则保持旧值
        last_poll_raw = store.get_meta("last_success") or last_poll_raw
        # 耗时与通路**必须跟着重读**：否则刚刷完却在标题行显示上一轮的数字，
        # 正好把这个功能变成"看起来坏了"（实测踩到：刚跑完 25.8 秒，标题还写着 38.3 秒）。
        last_duration = store.get_meta("last_poll_duration") or last_duration
        last_path = store.get_meta("last_poll_path") or last_path

    stale = False
    if last_poll_raw:
        stale_age = (now - datetime.fromisoformat(last_poll_raw)).total_seconds()
        # 数据比两轮轮询还旧 = 后台显然没在正常刷新，值得显眼地提醒一次
        stale = stale_age > max(2 * cfg.poll_interval_minutes * 60, 3600)
    if stale:
        print("⚠ 下面是**过期数据**：后台刷新已经很久没能成功了，别拿它当现在的状态。")
        print("  想立刻刷新：radar list --refresh")

    rows = store.assignments_with_course()
    grade_rows = store.grades_with_course(graded_only=True)
    store.close()
    if not rows:
        print("还没有数据：先跑一轮 python radar.py once，或启动 run / loop（loop 是给"
              " start_cranium.bat 这类一键启动器用的无头刷新循环）。")
        return 1
    overdue, upcoming, unknown, submitted = build_lists(rows, now, ignore_before)
    ignored = len(rows) - (len(overdue) + len(upcoming) + len(unknown) + len(submitted))

    def line(prefix: str, color: str, r, due=None) -> None:
        left = fmt_left(due, now) if due else "截止时间未知"
        print(
            f"  {c[color]}{prefix}{c['reset']} {c['bold']}{fmt_due(r['due_ts'])}{c['reset']}"
            f" {c['dim']}│{c['reset']} {left:<14}"
            f" {c['dim']}│{c['reset']} {r['name']}"
            f" {c['dim']}— {short_course(r['course_name'])}{c['reset']}"
        )

    shown = last_poll_raw.replace("T", " ") if last_poll_raw else "从未"
    # 耗时挂在**标题行**上：既显眼，又不挤占作业正文。手动刷新和后台自动刷新写的是
    # 同一个 meta，所以这一行永远反映"最近一次刷新"——不论它是谁触发的。
    dur = fmt_duration(last_duration)
    stamp = ""
    if dur:
        detail = " · ".join(x for x in (dur, fmt_refresh_path(last_path)) if x)
        stamp = f"（用时 {detail}）"
    print(f"📋 作业清单 · 数据更新于 {shown}{stamp}")
    tail = f" · 已忽略历史 {ignored} 个" if ignored else ""
    print(
        f"未交 {len(overdue) + len(upcoming) + len(unknown)}"
        f"（已截止 {len(overdue)} / 进行中 {len(upcoming)} / 未知截止 {len(unknown)}）"
        f" · 已交 {len(submitted)}{tail}\n"
    )

    print(f"{c['red']}{c['bold']}❗ 未提交 · 已截止（可能还能补交，尽快确认）{c['reset']}")
    if overdue:
        for r, due in overdue:
            line("⚠ ", "red", r, due)
    else:
        print(f"  {c['dim']}（无）{c['reset']}")

    print(f"\n{c['bold']}📌 未提交 · 进行中（按截止时间升序）{c['reset']}")
    if upcoming:
        for r, due in upcoming:
            mark, color = urgency_mark(due, now)
            line(mark + " ", color, r, due)
    else:
        print(f"  {c['dim']}（无）{c['reset']}")

    if unknown:
        print(f"\n{c['bold']}❓ 未提交 · 截止时间未知（老师可能未设置，留意课程公告）{c['reset']}")
        for r in unknown:
            line("· ", "cyan", r)

    print(f"\n{c['cyan']}{c['bold']}✅ 已提交{c['reset']}"
          + (f"{c['dim']}（共 {len(submitted)} 个，只显示最近 3 个）{c['reset']}" if len(submitted) > 3 else ""))
    if submitted:
        for r, due in submitted[:3]:  # 已提交的不重要，只看最近的
            left = fmt_left(due, now) if due else "—"
            print(
                f"  {c['cyan']}✔{c['reset']} {fmt_due(r['due_ts'])}"
                f" {c['dim']}│{c['reset']} {left:<14}"
                f" {c['dim']}│{c['reset']} {r['name']}"
                f" {c['dim']}— {short_course(r['course_name'])}{c['reset']}"
            )
    else:
        print(f"  {c['dim']}（无）{c['reset']}")

    # ---- 评分：每门课最近 3 次批改（老师改得慢，所以列表通常很短）
    grouped = build_grade_rows(grade_rows, per_course=3)
    total_grades = sum(len(v) for _, v in grouped)
    if grouped:
        print(f"\n{c['bold']}📊 评分{c['reset']}"
              f"{c['dim']}（每门课最近 3 次 · 共 {total_grades} 条）{c['reset']}")
        for course, items in grouped:
            print(f"  {c['dim']}{short_course(course)}{c['reset']}")
            for g in items:
                print(
                    f"    {c['green']}✔{c['reset']} {fmt_score(g):<12}"
                    f" {c['dim']}│{c['reset']} {fmt_due(g['graded_at'])}"
                    f" {c['dim']}│{c['reset']} {g['itemname'] or '（未命名）'}"
                )
    else:
        hint = ("" if (cfg.ws_token or "").strip()
                else "（评分走令牌通路，先 python radar.py set-token）")
        print(f"\n{c['dim']}📊 评分（还没有批改记录）{hint}{c['reset']}")

    if getattr(args, "pause", False) and sys.stdin.isatty():
        try:
            input(f"\n{c['dim']}按回车关闭…{c['reset']}")
        except EOFError:
            pass
    return 0


def _cmd_renew(base_dir: Path, args=None) -> int:
    """手动触发一次自动续期，逐步打印诊断信息。"""
    from .session import MoodleHTTP

    cfg = load_config(base_dir)
    if not cfg.sso_cookie:
        print("✗ 尚未配置 SSO cookie。运行 python radar.py set-cookie --sso")
        return 1

    steps: list[str] = []
    debug_path = None
    if args is not None and getattr(args, "debug", False):
        debug_path = base_dir / "data" / "renew_debug.html"

    def on_cookies(cookie_str: str) -> None:
        set_cookie(base_dir, cookie_str)
        cfg.cookie = cookie_str
        steps.append(f"已把 {len(cookie_str)} 字符的新会话 cookie 写回 config.toml")

    def on_sso_cookies(sso_str: str) -> None:
        set_cookie(base_dir, sso_str, field="sso_cookie")
        steps.append("SSO cookie 有轮换，最新值已写回 config.toml")

    http = MoodleHTTP(cfg)
    http.on_cookies_changed = on_cookies
    http.on_sso_cookies_changed = on_sso_cookies
    print("正在沿 SSO OAuth 链路重新登录…")
    ok = http.reestablish(debug_path=debug_path)
    for line in steps:
        print("  •", line)
    if not ok:
        print("✗ 自动续期失败。常见原因：")
        print("  1. SSO 会话已失效（最常见）→ 浏览器登录统一身份认证、确认显示你的姓名后，重新 set-cookie --sso")
        print("  2. 粘贴的条目不全 → 请在 Application→Cookie 里逐条复制（必须包含 JSESSIONID）")
        if debug_path is not None:
            print(f"  3. 落点页面已脱敏保存到 {debug_path}，可发给开发者排查")
        print("详细日志见 data/radar.log")
        return 1
    print("✓ 自动续期成功，新的会话 cookie 已写入 config.toml")
    return _cmd_check(base_dir)


def _cmd_check(base_dir: Path, args=None) -> int:
    from . import moodle
    from .session import MoodleHTTP, SessionExpired

    cfg = load_config(base_dir)

    # 令牌通路自检放在最前面：配了令牌的用户，数据本来就不依赖 cookie，
    # 再拿"会话过期"去吓他是不对的。会话只影响整页截图。
    ws_ok = False
    if (cfg.ws_token or "").strip():
        from .wsapi import WsApi, WsError, WsUnavailable

        try:
            ws_courses = WsApi(cfg).courses(cfg.semester)
        except (WsUnavailable, WsError) as exc:
            print(f"△ 令牌通路不可用：{exc}")
            print("  （换成可用的服务重新 set-token 即可；下面继续按 cookie 检查）")
        else:
            ws_ok = True
            print(f"✓ 令牌通路正常：{len(ws_courses)} 门本学期课程（**数据不依赖 cookie**）")
            for c in ws_courses:
                print(f"  - {c.name}")
            print("  （cookie 只影响作业页整页截图，见用户手册 4.1 节）")
        print()

    if not cfg.cookie and not cfg.sso_cookie:
        if ws_ok:
            print("没有 cookie——数据通路没问题，但作业页整页截图用不了。")
            print("想要截图就运行：python radar.py browser login")
            return 0
        print("✗ 还没有配置 cookie，最快的方式：")
        print("  1. 浏览器登录平台 https://moodle.scnu.edu.cn")
        print("  2. 运行  python radar.py import   （直接读取浏览器里的会话，免手抄）")
        print("  强烈建议同时运行 python radar.py import --sso 一并配置自动续期")
        print("  更省事的办法：python radar.py set-token 配一个令牌，从此不怕会话过期")
        return 1
    http = MoodleHTTP(cfg)
    http.on_cookies_changed = lambda cs: set_cookie(base_dir, cs)
    try:
        try:
            courses = moodle.discover_courses(http, semester=cfg.semester)
        except SessionExpired:
            print("! 会话已失效，尝试 SSO 自动续期…")
            if not http.reestablish():
                if ws_ok:
                    print("△ 自动续期失败——但令牌通路正常，**数据不受影响**。")
                    print("  cookie 只影响作业页整页截图：想要截图就 python radar.py browser login")
                    return 0
                print("✗ 自动续期失败（sso_cookie 未配置或已过期）。")
                print("  最快恢复：浏览器重新登录平台，然后运行  python radar.py import")
                return 1
            courses = moodle.discover_courses(http, semester=cfg.semester)
    except SessionExpired:
        if ws_ok:
            print("△ 会话不可用（只影响作业页整页截图，不影响数据）。")
            print("  想要截图就运行：python radar.py browser login")
            return 0
        print("✗ cookie 无效或已过期。最快恢复：浏览器重新登录平台，")
        print("  然后运行  python radar.py import   （直接读取浏览器里的会话）")
        return 1
    except Exception as exc:
        print(f"✗ 访问失败：{exc}")
        return 1

    if args is not None and getattr(args, "save_html", False):
        try:
            resp = http.get(f"{cfg.base_url}/my/")
            html = re.sub(r'"sesskey":"[0-9A-Za-z]+"', '"sesskey":"REDACTED"', resp.text)
            html = re.sub(r'logintoken[^&"]*("value=")?[^"&]*', 'logintoken=REDACTED', html)
            debug_path = base_dir / "data" / "debug_my.html"
            debug_path.write_text(html, encoding="utf-8")
            print(f"（已保存脱敏首页 HTML 到 {debug_path}，可发给开发者排查课程解析）")
        except Exception as exc:
            print(f"（保存调试 HTML 失败：{exc}）")

    print(f"✓ 登录有效，发现 {len(courses)} 门课程（学期过滤：{cfg.semester or '无'}）：")
    total = 0
    skipped = 0
    for c in courses:
        try:
            briefs = moodle.fetch_course_assignments(http, c.course_id)
        except moodle.NotEnrolled:
            skipped += 1
            continue
        except Exception as exc:
            print(f"  - {c.name}（解析失败：{exc}）")
            continue
        total += len(briefs)
        tail = ""
        if briefs:
            names = "、".join(b.name for b in briefs[:3])
            tail = f"：{names}" + ("…" if len(briefs) > 3 else "")
        print(f"  - {c.name}（{len(briefs)} 个作业）{tail}")
    print(f"共 {total} 个作业；另有 {skipped} 门课程尚未点\"自助选课\"加入，已跳过。")
    print("一切正常，可以运行 python radar.py run 了。")
    return 0


def _cmd_once(base_dir: Path, args) -> int:
    cfg = load_config(base_dir)
    app = App(cfg, notify_enabled=not args.no_notify)
    try:
        s = app.poll_once()
    finally:
        app.shutdown()
    print(s.describe())
    for e in s.errors:
        print("  !", e)
    return 0 if s.ok else 1


def _already_running(port: int) -> bool:
    """探测面板端口是否已有实例在监听（供热键重复触发时直接开清单）。"""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class InstanceLock:
    """进程级排它锁：保证同一时间只有一个 `radar run` 在巡逻。

    为什么不能只靠端口探测？**端口探测是竞态的**——两个进程可能同时发现"端口还没人
    监听"，于是双双启动。2026-10-09 实测踩到：两个实例在同一秒启动，一起跑了三个多小时，
    结果是双倍轮询、双倍通知、还并发写同一个 SQLite。

    文件锁由操作系统在**进程退出时自动释放**，既原子，又不会留下需要手工清理的
    "死锁文件"（那种方案一崩溃就把自己锁死在外面）。
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        """拿到锁返回 True；已被别的实例占着返回 False。"""
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # ⚠ 这个 open 必须待在 try 里：Windows 上只要别的进程正持着这把锁，
            # **连 open 都会抛 PermissionError**（实测：'rb' 能开，'a+b'/'r+b' 被拒）。
            # 放在 try 外面的话，第二个实例就不是"被礼貌拒绝"，而是带着堆栈直接崩掉。
            fh = open(self.path, "a+b")
        except OSError:
            return False
        try:
            if sys.platform == "win32":
                import msvcrt

                # 锁在内容区之外（第 4096 字节）。Windows 的字节范围锁连**读**都会被挡住，
                # 锁在开头的话，下面写进去的 PID 外部就再也读不到了。
                fh.seek(4096)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        try:  # 把 PID 写进去，纯属方便排查"是谁占着锁"
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode("ascii"))
            fh.flush()
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()  # 关句柄即释放锁
            except OSError:
                pass
            self._fh = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("已有另一个实例持锁")
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _console_python() -> str:
    """pythonw（无控制台）里也要能弹终端窗口：换用同目录的 python.exe。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return str(exe.with_name("python.exe"))
    return str(exe)


def _spawn_list_window(base_dir: Path) -> None:
    """新开终端窗口展示作业清单，回车后关闭（热键/启动时的可见反馈）。"""
    import subprocess

    flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    subprocess.Popen(
        [_console_python(), str(base_dir / "radar.py"), "list", "--pause"],
        creationflags=flags,
        cwd=str(base_dir),
    )


def _cmd_run(base_dir: Path, args) -> int:
    cfg = load_config(base_dir)
    # 双重保险：端口探测能挡住"旧版本/别的启动方式"起的实例，文件锁负责挡住竞态。
    lock = InstanceLock(cfg.data_dir / "radar.lock")
    if _already_running(cfg.dashboard_port) or not lock.acquire():
        _spawn_list_window(base_dir)
        print("AssignmentRadar 已在运行，已弹出终端清单。")
        return 0
    try:
        return _run_locked(base_dir, cfg, args)
    finally:
        lock.release()


def _cmd_loop(base_dir: Path, args) -> int:
    """无头刷新循环：只轮询，不托盘、不面板、不弹窗。

    给一键启动器用：它们已经有自己的窗口和服务，雷达再弹一个托盘/面板只会添乱。
    数据照样每轮写进台账，`radar list`
    因此永远有缓存可读——"打开即见上次结果"靠的就是它。

    与 `radar run` 共用同一把 `data/radar.lock`：谁先起谁跑，另一个自动让位。
    """
    def say(msg: str) -> None:
        # 一键启动器多半用 pythonw 拉起（无控制台），此时 print 会直接抛
        try:
            print(msg)
        except Exception:
            pass

    cfg = load_config(base_dir)
    lock = InstanceLock(cfg.data_dir / "radar.lock")
    if _already_running(cfg.dashboard_port) or not lock.acquire():
        # 已有实例在跑（radar run 或另一个 loop）。循环的目的只是"保证有人在定时
        # 刷新"——已经有人在做了，这次启动就是多余的，安静退出即可。
        log.info("已有 AssignmentRadar 实例在运行，本次 loop 启动取消")
        return 0
    say(f"AssignmentRadar 无头刷新循环已启动：每 {cfg.poll_interval_minutes} 分钟一轮，"
        f"数据写入 {cfg.db_path}")
    say("查看清单：radar list    退出：Ctrl+C")
    app = App(cfg)
    try:
        app.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        lock.release()
    return 0


def _run_locked(base_dir: Path, cfg, args) -> int:
    app = App(cfg)
    url = None
    if not getattr(args, "no_panel", False):
        from .dashboard import start_dashboard

        _, url = start_dashboard(app)
    else:
        print("网页面板已关闭（--no-panel），看清单请用：radar.bat list")
    if not cfg.cookie and not cfg.sso_cookie:
        print("⚠ 尚未配置 cookie：请先运行 radar set-cookie（面板里也会看到红色横幅）")
    # 启动时的可见反馈：默认弹终端清单（网页面板仍可用但不再自动打开）
    if getattr(cfg, "open_list_on_start", True):
        _spawn_list_window(base_dir)
    elif url and cfg.open_dashboard_on_start:
        webbrowser.open(url)
    use_tray = not getattr(args, "no_tray", False)
    if use_tray:
        try:
            from .tray import Tray
        except Exception as exc:
            print(f"托盘不可用（{exc}），进入控制台模式，Ctrl+C 退出。")
            use_tray = False
    if use_tray:
        threading.Thread(target=app.run_forever, name="poller", daemon=True).start()
        try:
            Tray(app, url).run()
        finally:
            app.shutdown()
    else:
        try:
            app.run_forever()
        except KeyboardInterrupt:
            pass
        app.shutdown()
    return 0


def _startup_vbs_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "AssignmentRadar.vbs"


def _cmd_install(base_dir: Path) -> int:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    radar = base_dir / "radar.py"
    vbs = _startup_vbs_path()
    try:
        vbs.parent.mkdir(parents=True, exist_ok=True)
        vbs.write_text(
            f'CreateObject("Wscript.Shell").Run """{pythonw}"" ""{radar}"" run", 0, False\n',
            encoding="utf-8",
        )
    except OSError as exc:
        # 自启只是"省一次双击"，不是必需项——写不进去也要给出等价替代，而不是抛异常
        print(f"✗ 没能写入开机自启项：{exc}")
        print(f"  目标路径：{vbs}")
        print()
        print("  下面三种做法效果一样，任选一种：")
        print("  1) 按 Win+R 输入 shell:startup 回车，把 radar.bat 的快捷方式拖进那个文件夹")
        print("  2) 任务计划程序 → 创建基本任务 → 触发器「登录时」→ 操作「启动程序」填：")
        print(f'       {pythonw}')
        print(f'       参数："{radar}" run')
        print("  3) 不设自启，每次双击 radar.bat（或用桌面 A-Radar 图标）")
        return 1
    print(f"✓ 已注册开机自启：{vbs}")
    print("下次登录 Windows 时将在后台自动启动（无窗口，托盘有图标）。")
    print("现在就想让它起来：radar.bat run")
    return 0


def _cmd_uninstall(base_dir: Path) -> int:
    vbs = _startup_vbs_path()
    if not vbs.exists():
        print("没有找到开机自启项，无需处理。")
        return 0
    try:
        vbs.unlink()
    except OSError as exc:
        print(f"✗ 没能删除：{exc}")
        print(f"  手动删掉这个文件即可：{vbs}")
        return 1
    print(f"✓ 已移除开机自启：{vbs}")
    return 0
