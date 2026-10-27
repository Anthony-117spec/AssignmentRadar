"""配置加载与写入（config.toml）。"""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE = '''# AssignmentRadar 配置
# ── cookie 更新方法 ─────────────────────────────────────────────
# 1. 浏览器登录平台 https://moodle.scnu.edu.cn
# 2. F12 打开开发者工具 → 网络(Network) → 随便点一个 moodle 请求
#    → 请求标头 → 复制整行 Cookie 的值（或在 应用程序→Cookie 里逐个复制）
# 3. 运行:  python radar.py set-cookie   然后按提示粘贴
# cookie 失效时程序会弹通知提醒你重复以上步骤。
# 注意：cookie 等同于登录凭证，不要把 config.toml 发给别人。

base_url = "https://moodle.scnu.edu.cn"
cookie = ""

# ── SSO 会话 cookie（自动续期用，强烈建议配置）──────────────────
# 平台网关会频繁轮换 MoodleSession，静态复制的 cookie 几分钟就失效；
# 配置了 SSO cookie 后，工具会在会话失效时自动沿统一身份认证的 OAuth
# 链路重新登录，平时无需再手动复制。
# 获取：浏览器登录统一身份认证后，F12 → 网络(Network) → 打开
#       https://sso.scnu.edu.cn → 点第一个 sso 请求 → 复制 Cookie: 请求头整行
#       （必须包含 JSESSIONID）
# ⚠ 这是全校统一身份的"免密通行证"，比 MoodleSession 更敏感，切勿外传。
sso_cookie = ""

# ── 平台 web service token（推荐，配了它就不再怕会话过期）──────────
# cookie 那条路的致命弱点：MoodleSession 大约 10~12 小时就死一次，死了清单就停在
# 旧数据上。token 没有这个问题——它是平台自己发的长期令牌，走 REST 接口直接拿
# 结构化数据，**完全不碰 cookie**。
# 获取（约 30 秒，不需要账号密码）：
#   浏览器登录平台 → 打开 https://moodle.scnu.edu.cn/user/managetoken.php
#   → 点「创建令牌」→ 令牌名称随便填、有效期留空
#   → 「服务」下拉框选 **学校自建的那一项**（`radar.py set-token` 会直接写出名字）
#   → 保存 → 复制那一串
#   → 运行:  python radar.py set-token
# ⚠ 服务别选 `Moodle mobile web service`：名字最像，但被校方裁剪过，
#   标准函数全被挡（accessexception），选了必定不可用。
# 令牌随时可在同一页面吊销，不影响你的密码和其它系统。
ws_token = ""

# ── 课程范围 ───────────────────────────────────────────────────
# 只关注"我的课程"侧边栏里某个学期的课程，其他学期/培训课全部视为噪音。
# 填分组名，例如 "26-27上学期"。
# 留空 = 不过滤（侧边栏全部课程，含往期课和培训课）。**首次装好建议先留空跑一次**，
# `radar.bat check` 会列出它看到的分组名，照着填一个就不会漏课也不会太吵。
# 注意：填了一个侧边栏里不存在的分组名，结果是 0 门课（不是"全部"）。
semester = ""
# 该日期之前截止的作业视为历史遗留，列表/面板不显示、不发任何通知。
# 留空 = 不过滤。格式 YYYY-MM-DD
ignore_due_before = ""

# 轮询间隔（分钟），120 = 两小时，足够了
poll_interval_minutes = 120
# 相邻两次 HTTP 请求之间的间隔（秒），对学校服务器友好一点
request_delay_seconds = 1.5
# 截止前多少小时提醒一次，可写多个，例如 [24, 2]
deadline_reminder_hours = [24]

dashboard_port = 8731
# 启动（含热键首次拉起）时弹出一个终端作业清单窗口作为可见反馈
open_list_on_start = true
# 改为打开网页面板而非终端清单（面板仍可随时手动访问 127.0.0.1:8731）
open_dashboard_on_start = false
# ── 浏览器静默续期 ─────────────────────────────────────────────
# 平台会话约 10~12 小时过期，而 requests 那条续期链路走不通（网关改过入口）。
# 但真实浏览器能走通：只要私有配置目录里的 SSO 还活着，无头打开平台就能静默重新登录，
# 你不用输任何东西。这里填"每隔几小时主动续一次"（0 = 只在过期后才补救）。
# 定期让浏览器走一遍登录，同时也让 SSO 会话保持活跃。
# ⚠ 配了上面的 ws_token 之后，这一项会被**跳过**：数据已经完全不依赖会话，会话只剩
#    "作业页截图"一个用途；而 SSO 已死时这一步要等浏览器调试端口超时（约 60 秒），
#    拿它拖累每一轮轮询不划算（实测一轮从 30 秒变成 500 秒）。
browser_refresh_hours = 4

# ── 作业附件归档 ───────────────────────────────────────────────
# 老师随作业发布的附件会下载到 <homework_dir>/<课程>/<作业>/ 下，一个科目一个文件夹。
# 相对路径按项目根目录算；留空 = 不归档。
homework_dir = "homework"
# 轮询时顺手把新发现的附件也下载下来（已存在的文件会跳过，所以平时几乎不花请求）
download_attachments_on_poll = true

[notify]
# Windows 系统通知（点击可直达作业页面）
toast = true
# ntfy 手机推送：手机装 ntfy App → 订阅下面的 topic → 消息直达手机
# topic 必须换成只有你知道的随机串（相当于密码）
ntfy_enabled = false
ntfy_server = "https://ntfy.sh"
ntfy_topic = ""
# Server酱（推送到微信）：https://sct.ftqq.com 获取 SendKey
serverchan_enabled = false
serverchan_sendkey = ""

# ── 科目文件夹映射（可选）──────────────────────────────────────
# 如果你已经按科目整理好了文件夹（例如 D:/我的作业/web），
# 在这里把「平台课程名」映射到「homework_dir 下的文件夹名」，
# 附件就会直接进到那个现成的文件夹里，而不是新建一个。
#
# 键可以写**去掉学期后缀**的课名：平台显示「Web 应用开发(26-27上学期)」，
# 这里写 "Web 应用开发" 即可（换学期也不用改）。
# 没列出的课程会在 homework_dir 下按课名自动新建文件夹。
[course_dirs]
# "Web 应用开发" = "web"
# "数据库系统技术" = "db"
'''


@dataclass
class NotifyConfig:
    toast: bool = True
    ntfy_enabled: bool = False
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    serverchan_enabled: bool = False
    serverchan_sendkey: str = ""


@dataclass
class Config:
    base_dir: Path
    base_url: str = "https://moodle.scnu.edu.cn"
    cookie: str = ""
    sso_cookie: str = ""
    ws_token: str = ""
    semester: str = ""
    ignore_due_before: str = ""
    poll_interval_minutes: int = 120
    request_delay_seconds: float = 1.5
    deadline_reminder_hours: list[int] = field(default_factory=lambda: [24])
    dashboard_port: int = 8731
    open_list_on_start: bool = True
    open_dashboard_on_start: bool = False
    browser_refresh_hours: int = 4
    homework_dir: str = "homework"
    download_attachments_on_poll: bool = True
    # 平台课程名 → 本地科目文件夹名（见 TEMPLATE 里的 [course_dirs]）
    course_dirs: dict[str, str] = field(default_factory=dict)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def config_path(self) -> Path:
        return self.base_dir / "config.toml"

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def homework_path(self) -> Path | None:
        """作业附件归档目录；未配置则返回 None（表示不归档）。"""
        if not self.homework_dir.strip():
            return None
        p = Path(self.homework_dir)
        return p if p.is_absolute() else (self.base_dir / p)


def load_config(base_dir: Path) -> Config:
    path = base_dir / "config.toml"
    if not path.exists():
        path.write_text(TEMPLATE, encoding="utf-8")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    notify_raw = raw.get("notify", {})
    cfg = Config(
        base_dir=base_dir,
        base_url=str(raw.get("base_url", "https://moodle.scnu.edu.cn")).rstrip("/"),
        cookie=str(raw.get("cookie", "")).strip(),
        sso_cookie=str(raw.get("sso_cookie", "")).strip(),
        ws_token=str(raw.get("ws_token", "")).strip(),
        semester=str(raw.get("semester", "")).strip(),
        ignore_due_before=str(raw.get("ignore_due_before", "")).strip(),
        poll_interval_minutes=max(5, int(raw.get("poll_interval_minutes", 120))),
        request_delay_seconds=max(0.5, float(raw.get("request_delay_seconds", 1.5))),
        deadline_reminder_hours=[int(h) for h in raw.get("deadline_reminder_hours", [24])],
        dashboard_port=int(raw.get("dashboard_port", 8731)),
        open_list_on_start=bool(raw.get("open_list_on_start", True)),
        open_dashboard_on_start=bool(raw.get("open_dashboard_on_start", False)),
        browser_refresh_hours=max(0, int(raw.get("browser_refresh_hours", 4))),
        homework_dir=str(raw.get("homework_dir", "homework")).strip(),
        download_attachments_on_poll=bool(raw.get("download_attachments_on_poll", True)),
        course_dirs={
            str(k): str(v)
            for k, v in (raw.get("course_dirs") or {}).items()
            if str(v).strip()
        },
        notify=NotifyConfig(
            toast=bool(notify_raw.get("toast", True)),
            ntfy_enabled=bool(notify_raw.get("ntfy_enabled", False)),
            ntfy_server=str(notify_raw.get("ntfy_server", "https://ntfy.sh")).rstrip("/"),
            ntfy_topic=str(notify_raw.get("ntfy_topic", "")).strip(),
            serverchan_enabled=bool(notify_raw.get("serverchan_enabled", False)),
            serverchan_sendkey=str(notify_raw.get("serverchan_sendkey", "")).strip(),
        ),
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def parse_cookie_header(raw: str) -> dict[str, str]:
    """解析浏览器复制的 Cookie 请求头，形如 "a=1; b=2"。"""
    cookies: dict[str, str] = {}
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def _set_toml_field(text: str, field: str, value: str) -> str:
    """替换 config.toml 里某个顶层字符串字段，保留注释与其余配置。

    两个容易踩的点，都在这里处理掉：

    1. **新字段必须插在第一个表头之前。** TOML 里表头之后的键属于那张表；直接追加到
       文件末尾，键就变成 `<最后一张表>.<字段>`，`load_config` 再也读不到它 ——
       表现为"明明写进去了，却像没生效"。config.toml 末尾有 `[notify]` / `[course_dirs]`，
       所以这不是理论问题。
    2. **只在顶层区域里替换。** 否则 `field="toast"` 之类会误改 `[notify]` 表里的同名字段。
    """
    replacement = f"{field} = " + json.dumps(value, ensure_ascii=False)
    m = re.search(r"(?m)^\[", text)
    head, tail = (text[: m.start()], text[m.start():]) if m else (text, "")
    if re.search(rf"(?m)^{field}\s*=.*$", head):
        head = re.sub(rf"(?m)^{field}\s*=.*$", lambda _m: replacement, head, count=1)
        return head + tail
    if not head.strip():
        return replacement + "\n\n" + tail.lstrip("\n")
    return head.rstrip("\n") + "\n" + replacement + "\n\n" + tail.lstrip("\n")


def _atomic_write(path: Path, text: str) -> None:
    """写 .tmp 再 os.replace——Windows 上这一步是原子的。

    直接 write_text 的话，写一半崩了（或被另一个进程截断）会把 config.toml
    变成半份 TOML，而它是**唯一**的配置源：坏了连报错都报不清楚。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_toml_field(base_dir: Path, field: str, value: str, *, attempts: int = 3) -> None:
    """原子写回一个顶层字段，并防住「别人用旧文本覆盖你的写入」。

    背景（2026-10-16 修复）：常驻托盘的轮询线程会在后台续期 cookie
    （poller._persist_cookies）。旧实现是 `read_text()` → `write_text()` 两步，
    中间隔着整段网络往返——你在终端跑 `radar.bat set-token` 的同一瞬间，它手里那份
    **旧文本**写完就回去了，你刚粘的 token 凭空消失，而下一次轮询只是「静默
    回退 HTML 通路」，界面上一个字都不提。

    两道防线：
      ① 写完前复核 mtime，发现中间被人写过就重读重放（最多 attempts 次）；
      ② 走 _atomic_write，不留半成品。
    """
    path = base_dir / "config.toml"
    for _ in range(attempts):
        existed = path.exists()
        mtime = path.stat().st_mtime if existed else None
        text = path.read_text(encoding="utf-8") if existed else TEMPLATE
        new_text = _set_toml_field(text, field, value)
        if existed and path.stat().st_mtime != mtime:
            continue  # 读的这段时间里有人写过了，重来一遍（保留对方的改动）
        _atomic_write(path, new_text)
        return
    raise RuntimeError(
        "config.toml 正在被其它进程反复写入，%s 未能落盘（已重试 %d 次）。"
        "通常是常驻托盘正在续期 cookie——稍等几秒再试一次。" % (field, attempts)
    )


def set_ws_token(base_dir: Path, token: str) -> None:
    """把平台 web service token 写回 config.toml。

    token 不是 cookie，不做解析，只做最小校验——一个带空白的 token 复制粘贴时
    几乎肯定是把别的东西一起带进来了，与其写进去等运行时报"invalidtoken"，
    不如当场拒绝。
    """
    token = (token or "").strip()
    if not token:
        raise ValueError("token 为空")
    if any(c.isspace() for c in token):
        raise ValueError("token 里不该有空白字符，请只粘贴那一串")
    _write_toml_field(base_dir, "ws_token", token)


def set_cookie(base_dir: Path, raw_cookie: str, *, field: str = "cookie") -> int:
    """把 cookie 写回 config.toml，返回解析到的 cookie 个数。"""
    cookies = parse_cookie_header(raw_cookie)
    if not cookies:
        raise ValueError("没有解析到任何 cookie，请确认粘贴的是浏览器里 Cookie 请求头的内容")
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    _write_toml_field(base_dir, field, cookie_str)
    return len(cookies)
