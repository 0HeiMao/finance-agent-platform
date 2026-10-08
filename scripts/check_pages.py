"""对前端页面做视觉验收：横向溢出检查 + 整页截图，覆盖 AGENTS.md 要求的 375 / 768 / 1440 三档宽度。

为什么需要它：本机沙箱里 `npm run build` 会因为 esbuild 清理临时文件被拒而失败
（`[vite:esbuild-transpile] remove …: Access is denied.`），所以验收走
「dev server 转译 + 无头浏览器」这条等价路径。

流程：真实登录接口拿 token → 写一个临时自动登录页（读 token 写 localStorage 后跳到目标路由）
→ 用 Edge 无头模式 + CDP 逐页逐档设置视口、读 `scrollWidth/clientWidth` 并列出超宽元素、整页截图。
临时登录页用完即删。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\check_pages.py
    .\\.venv\\Scripts\\python.exe scripts\\check_pages.py --sizes 375x812 --pages /reports
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
DEFAULT_PAGES = ("/dashboard", "/analysis/revenue", "/analysis/cost", "/analysis/alerts", "/reports",
                 "/reports/pptx", "/settings", "/financial/import", "/agent",
                 "/admin", "/admin/users", "/admin/batches", "/admin/knowledge", "/admin/departments",
                 "/admin/alert-rules", "/admin/audit")
# 表格在窄屏本来就要横向滚动，它自己带 .table-wrap 容器，不算页面溢出。
IGNORED_OFFENDER_CLASS = "table-wrap"

OVERFLOW_JS = """
(() => {
  const de = document.documentElement;
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 && rect.right > de.clientWidth + 1) {
      const cls = typeof el.className === 'string' ? el.className : '';
      if (cls.includes(%s) || (el.closest && el.closest('.%s'))) continue;
      offenders.push({tag: el.tagName, cls: cls.slice(0, 60), right: Math.round(rect.right), width: Math.round(rect.width)});
    }
  }
  return {scrollWidth: de.scrollWidth, clientWidth: de.clientWidth, offenders: offenders.slice(0, 12)};
})()
""" % (json.dumps(IGNORED_OFFENDER_CLASS), IGNORED_OFFENDER_CLASS)


def _console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _browser() -> str:
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise SystemExit("未找到 Edge/Chrome 可执行文件，请用 --browser 指定")


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _auto_login_page(token: str) -> Path:
    """临时自动登录页：注入 token 后立刻跳到目标路由。用完由调用方删除。

    必须放在 Vite 根目录（frontend/）而不是 public/：dev server 启动后再往 public/ 里加文件，
    sirv 的目录列表不会刷新，请求会落到 SPA fallback（返回 index.html），自动登录就不生效。
    """
    target = PROJECT_ROOT / "frontend" / "_autologin.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>check</title><script>\n"
        f"localStorage.setItem('access_token', {json.dumps(token)});\n"
        "const to = new URLSearchParams(location.search).get('to') || '/dashboard';\n"
        "location.replace(to);\n"
        "</script>\n",
        encoding="utf-8")
    return target


def _debugger_url(port: int, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))["webSocketDebuggerUrl"]
        except Exception as exc:  # 浏览器还没起来
            last = exc
            time.sleep(0.5)
    raise SystemExit(f"无法连接无头浏览器调试端口 {port}：{last}")


class CdpSession:
    """极简 CDP 客户端：够用就好，只封装 send / 等待特定事件。"""

    def __init__(self, websocket) -> None:
        self._ws = websocket
        self._next_id = 0
        self._loaded = False

    async def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        self._next_id += 1
        message = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        await self._ws.send(json.dumps(message))
        while True:
            reply = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=60))
            if reply.get("id") == message["id"]:
                if "error" in reply:
                    raise RuntimeError(f"{method} 失败：{reply['error']}")
                return reply.get("result") or {}
            if reply.get("method") == "Page.loadEventFired":
                self._loaded = True

    async def navigate(self, url: str, session_id: str, timeout: float = 15.0) -> None:
        """导航并等 load 事件。注意 load 事件可能被上一个 send 顺手消费掉，所以要复用 _loaded 标志。"""
        self._loaded = False
        await self.send("Page.navigate", {"url": url}, session_id=session_id)
        deadline = time.time() + timeout
        while not self._loaded:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                message = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=remaining))
            except asyncio.TimeoutError:
                return
            if message.get("method") == "Page.loadEventFired":
                self._loaded = True


async def _drive(ws_url: str, args, pages, sizes, out_dir: Path, base_url: str,
                 viewport_only: bool = False) -> int:
    import websockets

    failures = 0
    async with websockets.connect(ws_url, max_size=None) as websocket:
        cdp = CdpSession(websocket)
        target = await cdp.send("Target.createTarget", {"url": "about:blank"})
        attached = await cdp.send("Target.attachToTarget",
                                  {"targetId": target["targetId"], "flatten": True})
        session = attached["sessionId"]
        await cdp.send("Page.enable", session_id=session)

        # 先过一遍自动登录页，把 token 写进 localStorage；同源之后所有页面都能直接用。
        await cdp.navigate(f"{base_url}/_autologin.html?to=%2Fdashboard", session)

        for route in pages:
            for size in sizes:
                width, _, height = size.partition("x")
                await cdp.send("Emulation.setDeviceMetricsOverride",
                               {"width": int(width), "height": int(height),
                                "deviceScaleFactor": 1, "mobile": int(width) < 768},
                               session_id=session)
                await cdp.navigate(f"{base_url}{route}", session)
                await asyncio.sleep(1.2)  # 等图表与接口回填

                evaluated = await cdp.send("Runtime.evaluate",
                                           {"expression": OVERFLOW_JS, "returnByValue": True},
                                           session_id=session)
                metrics = evaluated["result"].get("value") or {}
                scroll, client = metrics.get("scrollWidth"), metrics.get("clientWidth")
                offenders = metrics.get("offenders") or []
                verdict = "无横向溢出" if scroll is not None and scroll <= client else "横向溢出"
                if offenders:
                    verdict += f"，超宽元素 {len(offenders)} 个"
                    failures += 1
                print(f"{route} @{size}: {verdict}（scrollWidth={scroll} clientWidth={client}）")
                for item in offenders:
                    print(f"    {item['tag']}.{item['cls']} right={item['right']} width={item['width']}")

                shot = await cdp.send("Page.captureScreenshot",
                                      {"format": "png",
                                       "captureBeyondViewport": not viewport_only},
                                      session_id=session)
                out = out_dir / f"check_{route.strip('/').replace('/', '_') or 'root'}_{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
        await cdp.send("Target.closeTarget", {"targetId": target["targetId"]})
    return 1 if failures else 0


def main() -> int:
    _console()
    parser = argparse.ArgumentParser(description="无头浏览器横向溢出检查 + 整页截图")
    parser.add_argument("--base-url", default="http://127.0.0.1:5173", help="前端 dev server")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="demo_analyst")
    parser.add_argument("--password", default="demo_password_2026")
    parser.add_argument("--pages", nargs="*", default=list(DEFAULT_PAGES))
    parser.add_argument("--sizes", nargs="*", default=["1440x900", "768x1000", "375x812"])
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "output" / "charts"))
    parser.add_argument("--browser", default=None)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--viewport-only", action="store_true",
                        help="只截视口（长页面整页截图会超过 8192px，图片工具打不开）")
    args = parser.parse_args()

    browser = args.browser or _browser()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = PROJECT_ROOT / "output" / "_browser_profile"

    token = _post_json(f"{args.api_base}/api/v1/auth/login",
                       {"username": args.username, "password": args.password})["access_token"]
    page = _auto_login_page(token)
    print(f"登录成功：{args.username}；临时登录页 {page.relative_to(PROJECT_ROOT)}")

    process = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions",
         # 从 Python 子进程里启动时 Chromium 自己的沙箱初始化会卡死（本机沙箱禁止嵌套沙箱的 IPC），
         # 表现为浏览器永不就绪；直接贴到 pwsh 里跑反而正常。
         "--no-sandbox", f"--remote-debugging-port={args.port}", f"--user-data-dir={profile}",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = _debugger_url(args.port)
        return asyncio.run(_drive(ws_url, args, args.pages, args.sizes, out_dir, args.base_url,
                                  args.viewport_only))
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        import shutil

        for _ in range(5):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.5)
        page.unlink(missing_ok=True)
        print("已关闭无头浏览器并删除临时登录页")


if __name__ == "__main__":
    raise SystemExit(main())
