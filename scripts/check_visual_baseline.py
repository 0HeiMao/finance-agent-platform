"""截图基线视觉回归：把「首屏看起来应该是什么样」冻结进仓库，改坏了就在本机红。

为什么需要它：`check_pages.py` 只查「无横向溢出」与「页面渲染出内容」，这两项都是**几何与存在性**检查 ——
把某个 KPI 卡片的数字颜色改白、把图表容器高度写死成 0、把表格的表头文字删掉，它全都照样绿。
`ui_*_smoke.py` 点的是交互路径，也只断言点下去之后有没有反应。视觉上「没坏但变丑了」这一段没人守，
所以这里退一步：**把首屏的像素冻结下来**，以后每次跑到就比对，不一样的地方不但报比例，还把差异图写出来。

判断口径（都在 `compare_images` 里，可单测）：
- 逐像素比，**单个通道差值 > `--tolerance`（默认 8）** 才算「这个像素不一样」——抗锯齿与字体微调
  本来就会让几个像素差 1~3，容差太小会把每次运行都变成假红；
- 不一样的像素占比超过 `--threshold`（默认 0.2%）判失败；
- **基线尺寸变了直接判失败**（版面高度也是视觉结果：多出一行、图表被压扁都会改高度）；
- 基线文件本身也被校验：`manifest.json` 里记的宽高与 sha256 要和磁盘上的图对得上，少一张、多一张、
  被人手工改过都会报出来。

页面上会自己变的内容（时间戳之类）由前端标注 `data-visual-mask` 属性，截图时注入的样式把它的字变透明
（占位还在，所以版面变了仍然会红）：这是**在源头消掉噪声**，而不是把阈值调高到噪声之上 —— 抬高阈值
会把真正的视觉改动一起放过。当前只有 `/dashboard` 的「数据更新于 …」用了它。

为什么它**不进 CI**（见 README「截图基线视觉回归」）：像素依赖渲染字体与抗锯齿，换台机器/换个 Edge 版本
就会整片差一点；这类基线只在「同一台机器上前后对比」时才有意义。所以它是本地门禁，配套的是
「有意的界面改动 → `--update-baseline` → 把新基线和代码一起提交」这条约定。

流程：真实登录接口拿 token → 临时自动登录页（`frontend/_autologin.html`，用完即删）或在容器里直接用
CDP 注入 token → 用 Edge 无头 + CDP 逐页逐档设视口、注入确定性样式（停动画/过渡/光标闪烁）、
等页面渲染出内容且资源安静下来、再等图表动画结束 → 截图 → 与基线比对。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\check_visual_baseline.py                  # 比对（要前后端在跑）
    .\\.venv\\Scripts\\python.exe scripts\\check_visual_baseline.py --update-baseline # 有意的界面改动后重刷基线
    .\\.venv\\Scripts\\python.exe scripts\\check_visual_baseline.py --pages /settings --sizes 375x812
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = PROJECT_ROOT / "docs" / "visual-baseline"
MANIFEST_PATH = BASELINE_DIR / "manifest.json"
DIFF_DIR = PROJECT_ROOT / "output" / "charts"

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

# 基线集合：第一条屏幕（视口内）就是最容易碎的地方，而整页图动辄 1.7 MB × 17 页 —— 进版本库不值当，
# 想看整页效果用 `--full-page` 跑一次，但别把整页基线提交进去。
# （要加页面就跑 `--update-baseline --pages /x --sizes 1440x900`，manifest 是数据源，脚本照着它跑。）
DEFAULT_BASELINES = (
    ("/dashboard", "1440x900"),
    ("/dashboard", "375x812"),
    ("/analysis/revenue", "1440x900"),
    ("/analysis/alerts", "1440x900"),
    ("/financial/transactions", "1440x900"),
    ("/agent", "1440x900"),
    ("/admin/users", "1440x900"),
    ("/settings", "375x812"),
)

# 确定性样式：动画与过渡会让「同一份代码」每次截出不同的图；光标闪烁同理。
# ECharts 的动画是 JS 驱动的（CSS 关不掉），所以还要靠 _wait_quiet 里那段等待。
VISUAL_CSS = """
*, *::before, *::after {
  animation: none !important;
  transition: none !important;
  caret-color: transparent !important;
}
html { scroll-behavior: auto !important; }
[data-visual-mask] { color: transparent !important; }
"""

READY_JS = """
(() => {
  const app = document.getElementById('app');
  const resources = performance.getEntriesByType('resource').length;
  return {
    appChildren: app ? app.children.length : 0,
    textLen: (document.body.innerText || '').trim().length,
    resources: resources,
    canvases: document.querySelectorAll('canvas').length,
  };
})()
"""
MIN_TEXT_LEN = 200


@dataclass(frozen=True)
class DiffResult:
    """一次比对的结论。ratio 是「不一样的像素占比」，size_mismatch 时其它字段无意义。"""

    ratio: float
    pixels: int
    total: int
    max_delta: int
    size_mismatch: bool


def compare_images(left: Image.Image, right: Image.Image, tolerance: int = 8) -> DiffResult:
    """逐像素比对：单通道差值 > tolerance 才算不一样的像素。纯函数，可单测。"""
    if left.size != right.size:
        return DiffResult(ratio=1.0, pixels=left.size[0] * left.size[1],
                          total=left.size[0] * left.size[1], max_delta=255, size_mismatch=True)
    import numpy as np

    a = np.asarray(left.convert("RGB"), dtype=np.int16)
    b = np.asarray(right.convert("RGB"), dtype=np.int16)
    delta = np.abs(a - b).max(axis=2)
    changed = delta > tolerance
    total = int(delta.size)
    pixels = int(changed.sum())
    return DiffResult(ratio=pixels / total if total else 0.0, pixels=pixels, total=total,
                      max_delta=int(delta.max()) if total else 0, size_mismatch=False)


def diff_image(left: Image.Image, right: Image.Image, tolerance: int = 8) -> Image.Image:
    """差异图：变了的地方涂红，没变的地方压成灰底 —— 给人看的，不进版本库。"""
    import numpy as np

    a = np.asarray(left.convert("RGB"), dtype=np.int16)
    b = np.asarray(right.convert("RGB"), dtype=np.int16)
    changed = np.abs(a - b).max(axis=2) > tolerance
    grey = (b.mean(axis=2) * 0.4 + 150).astype("uint8")
    canvas = np.stack([grey, grey, grey], axis=2)
    canvas[changed] = (220, 30, 30)
    return Image.fromarray(canvas, mode="RGB")


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def baseline_file(page: str, size: str) -> str:
    return f"{page.strip('/').replace('/', '_') or 'root'}_{size}.png"


def manifest_problems(manifest: dict, baseline_dir: Path = BASELINE_DIR) -> list[str]:
    """基线目录与 manifest 对不对得上：少图、多图、尺寸不符、被人手工改过都算问题。纯函数，可单测。"""
    problems: list[str] = []
    entries = manifest.get("images") or []
    declared = {entry["file"] for entry in entries}
    for entry in entries:
        path = baseline_dir / entry["file"]
        if not path.exists():
            problems.append(f"基线图缺失：{entry['file']}")
            continue
        with Image.open(path) as image:
            if list(image.size) != [entry["width"], entry["height"]]:
                problems.append(f"基线图尺寸与 manifest 不符：{entry['file']} "
                                f"（磁盘 {image.size[0]}x{image.size[1]}，manifest "
                                f"{entry['width']}x{entry['height']}）")
        if entry.get("sha256") and file_digest(path) != entry["sha256"]:
            problems.append(f"基线图被人手工改过（sha256 不符）：{entry['file']}")
    for path in sorted(baseline_dir.glob("*.png")):
        if path.name not in declared:
            problems.append(f"基线目录里有 manifest 没登记的图：{path.name}")
    return problems


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
    """临时自动登录页（同 check_pages.py）：必须放在 Vite 根目录，dev server 启动后往 public/ 加文件不生效。"""
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
    """极简 CDP 客户端（同 check_pages.py）：只封装 send / 等 load 事件。"""

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
        self._loaded = False
        await self.send("Page.navigate", {"url": url}, session_id=session_id)
        deadline = time.time() + timeout
        while not self._loaded:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                message = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=remaining))
            except TimeoutError:
                return
            if message.get("method") == "Page.loadEventFired":
                self._loaded = True


async def _wait_quiet(cdp: CdpSession, session: str, timeout: float, settle: float) -> tuple[bool, dict]:
    """等页面真的渲染出内容、且连续两次采样都没有新资源进来，再等 settle 秒让图表动画走完。

    固定 sleep 会在 dev server 冷启动（现编译 chunk）时截到空白图 —— 而**空白图也是一种「基线」**，
    比对时会稳定通过，于是这项检查整段失效。所以先要求真有内容，再等安静。
    """
    deadline = time.time() + timeout
    info: dict = {}
    last_resources = -1
    quiet_since: float | None = None
    while True:
        evaluated = await cdp.send("Runtime.evaluate",
                                   {"expression": READY_JS, "returnByValue": True},
                                   session_id=session)
        info = evaluated["result"].get("value") or {}
        rendered = ((info.get("appChildren") or 0) > 0
                    and (info.get("textLen") or 0) >= MIN_TEXT_LEN)
        resources = int(info.get("resources") or 0)
        if rendered and resources == last_resources:
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= 0.8:
                break
        else:
            quiet_since = None
        last_resources = resources
        if time.time() >= deadline:
            break
        await asyncio.sleep(0.4)
    rendered = (info.get("appChildren") or 0) > 0 and (info.get("textLen") or 0) >= MIN_TEXT_LEN
    if rendered:
        await asyncio.sleep(settle)
    return rendered, info


async def _capture(cdp: CdpSession, session: str, route: str, size: str, base_url: str,
                   ready_timeout: float, settle: float, full_page: bool) -> bytes:
    width, _, height = size.partition("x")
    await cdp.send("Emulation.setDeviceMetricsOverride",
                   {"width": int(width), "height": int(height),
                    "deviceScaleFactor": 1, "mobile": int(width) < 768},
                   session_id=session)
    await cdp.navigate(f"{base_url}{route}", session)
    await cdp.send("Runtime.evaluate",
                   {"expression": f"(() => {{ const s = document.createElement('style');"
                                  f" s.textContent = {json.dumps(VISUAL_CSS)};"
                                  f" document.head.appendChild(s); }})()"},
                   session_id=session)
    rendered, info = await _wait_quiet(cdp, session, ready_timeout, settle)
    if not rendered:
        raise RuntimeError(f"{route} @{size} 没有渲染出内容（innerText {info.get('textLen')} 字、"
                           f"#app 子节点 {info.get('appChildren')} 个）——空白图不能当基线")
    shot = await cdp.send("Page.captureScreenshot",
                          {"format": "png", "captureBeyondViewport": full_page},
                          session_id=session)
    return base64.b64decode(shot["data"])


async def _drive(ws_url: str, plan: list[tuple[str, str]], args, base_url: str) -> int:
    import websockets

    failures = 0
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    async with websockets.connect(ws_url, max_size=None) as websocket:
        cdp = CdpSession(websocket)
        target = await cdp.send("Target.createTarget", {"url": "about:blank"})
        attached = await cdp.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        session = attached["sessionId"]
        await cdp.send("Page.enable", session_id=session)

        if args.inject_token:
            await cdp.navigate(f"{base_url}/", session)
            await cdp.send("Runtime.evaluate",
                           {"expression": f"localStorage.setItem('access_token', {json.dumps(args.token)});"},
                           session_id=session)
        else:
            await cdp.navigate(f"{base_url}/_autologin.html?to=%2Fdashboard", session)

        captured: list[dict] = []
        for route, size in plan:
            path = BASELINE_DIR / baseline_file(route, size)
            try:
                png = await _capture(cdp, session, route, size, base_url,
                                     args.ready_timeout, args.settle, args.full_page)
            except RuntimeError as exc:
                failures += 1
                print(f"FAIL {route} @{size}：{exc}")
                continue
            with Image.open(io.BytesIO(png)) as image:
                image.load()
                width, height = image.size
                live = image.convert("RGB")
            if args.update_baseline:
                path.write_bytes(png)
                captured.append({"page": route, "size": size, "file": path.name,
                                 "width": width, "height": height, "bytes": len(png),
                                 "sha256": hashlib.sha256(png).hexdigest()})
                print(f"已写入基线 {path.name}（{width}x{height}，{len(png) // 1024} KB）")
                continue
            if not path.exists():
                failures += 1
                print(f"FAIL {route} @{size}：没有基线图（先跑 --update-baseline 生成并提交）")
                continue
            with Image.open(path) as base_file:
                base = base_file.convert("RGB")
            result = compare_images(base, live, args.tolerance)
            if result.size_mismatch:
                failures += 1
                print(f"FAIL {route} @{size}：尺寸变了 —— 基线 {base.size[0]}x{base.size[1]}，"
                      f"现在 {width}x{height}（版面高度也是视觉结果）")
                continue
            verdict = "一致" if result.ratio <= args.threshold else "有差异"
            line = (f"{verdict} {route} @{size}：不一样 {result.pixels}/{result.total} 像素"
                    f"（{result.ratio * 100:.3f}%，容差 {args.tolerance}，阈值 {args.threshold * 100:.2f}%，"
                    f"最大通道差 {result.max_delta}）")
            if result.ratio > args.threshold:
                failures += 1
                DIFF_DIR.mkdir(parents=True, exist_ok=True)
                diff_path = DIFF_DIR / f"visual_diff_{baseline_file(route, size)}"
                diff_image(base, live, args.tolerance).save(diff_path)
                line += f" → 差异图 {diff_path.relative_to(PROJECT_ROOT)}"
            print(line)
        if args.update_baseline and captured:
            manifest = {
                "note": "由 scripts/check_visual_baseline.py --update-baseline 生成：这是首屏视觉基线，"
                        "有意的界面改动要连图和代码一起提交",
                "full_page": bool(args.full_page),
                "tolerance": args.tolerance,
                "threshold": args.threshold,
                "images": sorted(captured, key=lambda item: (item["page"], item["size"])),
            }
            MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8", newline="\n")
            print(f"已更新 {MANIFEST_PATH.relative_to(PROJECT_ROOT)}：{len(captured)} 张基线")
        await cdp.send("Target.closeTarget", {"targetId": target["targetId"]})
    return 1 if failures else 0


def _plan_from_manifest() -> list[tuple[str, str]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return [(entry["page"], entry["size"]) for entry in manifest.get("images") or []]


def main() -> int:
    _console()
    parser = argparse.ArgumentParser(description="截图基线视觉回归（首屏像素比对）")
    parser.add_argument("--base-url", default="http://127.0.0.1:5173", help="前端 dev server")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="demo_analyst")
    parser.add_argument("--password", default="demo_password_2026")
    parser.add_argument("--pages", nargs="*", default=None, help="默认取 manifest 里登记过的页面")
    parser.add_argument("--sizes", nargs="*", default=None, help="默认取 manifest 里登记过的档位")
    parser.add_argument("--update-baseline", action="store_true",
                        help="重新生成基线（有意的界面改动才这么做，然后把新基线和代码一起提交）")
    parser.add_argument("--threshold", type=float, default=0.002,
                        help="允许的不一样像素占比（默认 0.2%%）")
    parser.add_argument("--tolerance", type=int, default=8,
                        help="单通道差值超过多少才算不一样的像素（默认 8）")
    parser.add_argument("--settle", type=float, default=1.5,
                        help="内容安静下来后再等多久让图表动画结束（默认 1.5s）")
    parser.add_argument("--ready-timeout", type=float, default=25.0)
    parser.add_argument("--full-page", action="store_true",
                        help="整页截图（默认只截首屏：整页图动辄 1.7 MB，不适合进版本库）")
    parser.add_argument("--inject-token", action="store_true",
                        help="不开临时登录页，直接用 CDP 往同源 localStorage 写 token（容器里验构建产物时用）")
    parser.add_argument("--browser", default=None)
    parser.add_argument("--port", type=int, default=9334)
    args = parser.parse_args()

    if args.pages or args.sizes:
        pages = args.pages or sorted({page for page, _ in DEFAULT_BASELINES})
        sizes = args.sizes or sorted({size for _, size in DEFAULT_BASELINES})
        plan = [(page, size) for page in pages for size in sizes]
    elif MANIFEST_PATH.exists() and not args.update_baseline:
        plan = _plan_from_manifest()
    else:
        plan = list(DEFAULT_BASELINES)
    if not plan:
        print("没有要比对的页面（manifest 是空的？）")
        return 1

    if not args.update_baseline and MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        problems = manifest_problems(manifest)
        if problems:
            for problem in problems:
                print(f"FAIL {problem}")
            return 1

    token = _post_json(f"{args.api_base}/api/v1/auth/login",
                       {"username": args.username, "password": args.password})["access_token"]
    args.token = token
    page = None if args.inject_token else _auto_login_page(token)
    if page:
        print(f"登录成功：{args.username}；临时登录页 {page.relative_to(PROJECT_ROOT)}")
    else:
        print(f"登录成功：{args.username}；用 CDP 直接注入 token（不开临时登录页）")

    browser = args.browser or _browser()
    profile = PROJECT_ROOT / "output" / "_visual_profile"
    process = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions",
         # 从 Python 子进程启动时 Chromium 自己的沙箱初始化会卡死（本机沙箱禁止嵌套沙箱 IPC）。
         "--no-sandbox", f"--remote-debugging-port={args.port}", f"--user-data-dir={profile}",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = _debugger_url(args.port)
        return asyncio.run(_drive(ws_url, plan, args, args.base_url))
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
        if page:
            page.unlink(missing_ok=True)
        print("已关闭无头浏览器并删除临时登录页")


if __name__ == "__main__":
    raise SystemExit(main())
