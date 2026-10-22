"""本地作业面板：标准库 http.server，只绑定 127.0.0.1。"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

log = logging.getLogger(__name__)

HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AssignmentRadar · 作业面板</title>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --ink:#1c2333; --muted:#7a8194; --line:#e5e8ef;
          --red:#d64545; --yellow:#c07f1f; --green:#3a9e63; --blue:#3a6ea5; --gray:#98a0b3; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; background:var(--bg); color:var(--ink); }
  header { padding:20px 28px 8px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .meta { color:var(--muted); font-size:13px; }
  #banner { display:none; margin:12px 28px 0; padding:10px 14px; border-radius:8px;
            background:#fdecec; color:var(--red); font-size:14px; border:1px solid #f3c4c4; }
  main { padding:8px 28px 40px; max-width:980px; }
  .course { background:var(--card); border:1px solid var(--line); border-radius:10px; margin:14px 0; overflow:hidden; }
  .course h2 { font-size:15px; margin:0; padding:12px 16px; border-bottom:1px solid var(--line); background:#fafbfd; }
  .course h2 .count { color:var(--muted); font-weight:normal; font-size:12px; margin-left:8px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  td { padding:9px 16px; border-top:1px solid var(--line); }
  tr:first-child td { border-top:none; }
  .name a { color:var(--ink); text-decoration:none; }
  .name a:hover { color:var(--blue); text-decoration:underline; }
  .due { white-space:nowrap; color:var(--muted); font-size:13px; width:150px; }
  .badge { display:inline-block; font-size:12px; padding:2px 8px; border-radius:99px; white-space:nowrap; }
  .b-red { background:#fdecec; color:var(--red); }
  .b-yellow { background:#fdf3e2; color:var(--yellow); }
  .b-green { background:#e8f5ee; color:var(--green); }
  .b-gray { background:#eef0f4; color:var(--gray); }
  .b-blue { background:#e8f0fa; color:var(--blue); }
  .empty { color:var(--muted); text-align:center; padding:30px 0; }
  code { background:#eef0f4; padding:1px 5px; border-radius:4px; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>&#128225; AssignmentRadar</h1>
  <div class="meta" id="meta">加载中…</div>
</header>
<div id="banner"></div>
<main id="main"></main>
<script>
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function fmtHours(h) {
  if (h < 48) return Math.max(1, Math.round(h)) + " 小时";
  return Math.round(h / 24) + " 天";
}
function fmtDur(s) {
  const v = parseFloat(s);
  if (!isFinite(v) || v < 0) return "";
  if (v < 60) return v.toFixed(1) + " 秒";
  const total = Math.round(v);
  return Math.floor(total / 60) + " 分 " + (total % 60) + " 秒";
}
function fmtDue(ts) {
  if (!ts) return "—";
  const d = new Date(ts), p = n => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth()+1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}
function badge(a) {
  if (a.submitted === true) return '<span class="badge b-blue">已提交</span>';
  if (!a.due_ts) return '<span class="badge b-gray">未知截止</span>';
  const hours = (new Date(a.due_ts) - Date.now()) / 3600000;
  if (hours < 0) return '<span class="badge b-gray">已截止</span>';
  if (hours <= 24) return '<span class="badge b-red">剩 ' + fmtHours(hours) + '</span>';
  if (hours <= 72) return '<span class="badge b-yellow">剩 ' + fmtHours(hours) + '</span>';
  return '<span class="badge b-green">剩 ' + fmtHours(hours) + '</span>';
}
const sortKey = a => (a.due_ts ? new Date(a.due_ts).getTime() : 9e15) + (a.submitted === true ? 1e16 : 0);
async function refresh() {
  try {
    const st = await (await fetch("/api/state")).json();
    const banner = document.getElementById("banner");
    if (st.session_ok === false) {
      banner.style.display = "block";
      banner.innerHTML = "&#9888; 平台登录已过期，提醒已暂停。请在浏览器重新登录后运行 <code>radar set-cookie</code>";
    } else { banner.style.display = "none"; }
    const dur = fmtDur(st.last_duration);
    const pathName = st.last_path === "token" ? "令牌通路"
                   : (st.last_path === "html" ? "页面通路" : "");
    const durText = dur ? "（用时 " + dur + (pathName ? " · " + pathName : "") + "）" : "";
    document.getElementById("meta").textContent =
      "最后检查：" + (st.last_poll ? st.last_poll.replace("T", " ") : "尚未运行") + durText +
      " · 每轮间隔约 " + st.interval_minutes + " 分钟 · 本页每 60 秒自动刷新" +
      (st.ignored_count ? " · 已忽略历史作业 " + st.ignored_count + " 个" : "");
    const main = document.getElementById("main");
    if (!st.courses.length) {
      main.innerHTML = '<div class="empty">还没有数据。第一轮轮询完成后这里会列出全部作业。</div>';
      return;
    }
    let html = "";
    for (const c of st.courses) {
      const items = [...c.assignments].sort((x, y) => sortKey(x) - sortKey(y));
      if (!items.length) continue;
      html += '<div class="course"><h2>' + esc(c.name) +
              '<span class="count">' + items.length + ' 个作业</span></h2><table>';
      for (const a of items) {
        html += '<tr><td class="name"><a href="' + esc(a.url) + '" target="_blank">' + esc(a.name) +
                '</a></td><td class="due">' + fmtDue(a.due_ts) +
                '</td><td style="width:110px;text-align:right">' + badge(a) + '</td></tr>';
      }
      html += '</table></div>';
    }
    main.innerHTML = html || '<div class="empty">课程里暂时没有发现作业。</div>';
  } catch (e) {
    const banner = document.getElementById("banner");
    banner.style.display = "block";
    banner.innerHTML = "&#9888; 面板后端连接不上（radar run 可能已退出）。" +
      "请重新启动：运行 <code>radar run</code> 或按 Ctrl+Alt+A，然后刷新本页。";
    document.getElementById("meta").textContent = "获取状态失败：" + e;
  }
}
refresh();
setInterval(refresh, 60000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    app = None  # 由 start_dashboard 注入

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            try:
                data = self.app.api_state()
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self._respond(200, body, "application/json; charset=utf-8")
            except Exception:
                log.exception("api_state 失败")
                self._respond(500, b'{"ok": false}', "application/json")
        elif path == "/":
            self._respond(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._respond(404, b"not found", "text/plain; charset=utf-8")

    def _respond(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默访问日志
        pass


def start_dashboard(app) -> tuple[ThreadingHTTPServer, str]:
    handler = type("Handler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", app.cfg.dashboard_port), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    log.info("作业面板已启动：%s", url)
    return server, url
