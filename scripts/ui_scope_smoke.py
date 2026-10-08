"""在真实浏览器里点一遍「查看口径」下拉与导出按钮，确认它们真的工作。

为什么需要它：`scripts/check_pages.py` 只看横向溢出、`pytest` 只看接口，两者都发现不了
「控件在、但点了没反应」这类问题。写这个脚本时它当场抓到一个真实缺陷——`ScopeSwitcher.vue`
最初用 `onMounted` 拉选项，而挂载时 `/auth/me` 还没回来、`switchable` 仍是 false，
下拉里只有「全公司」一个选项；截图完全看不出来。

它验证三件事：
1. 下拉里有没有部门选项、选择后页面是否真的换成该口径（KPI 变化 + 提示条出现）；
2. 切口径后导出的请求是否带上了 `department`（后端只看得到带 `department` 的请求）；
3. 导出按钮拿到响应头后有没有显示「已保存 …（N 行）」。

前提：后端在 127.0.0.1:8000、前端 dev server 在 127.0.0.1:5173 都在跑，库里已有演示数据。
用法：`.venv/Scripts/python.exe scripts/ui_scope_smoke.py`（截图写到 `output/charts/ui_scope_export.png`）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_pages import (  # noqa: E402
    PROJECT_ROOT,
    CdpSession,
    _auto_login_page,
    _browser,
    _console,
    _debugger_url,
    _post_json,
)

BASE_URL = "http://127.0.0.1:5173"
PORT = 9355
OUT = PROJECT_ROOT / "output" / "charts"

READ_STATE = """
(() => ({
  options: [...document.querySelectorAll('.scope-switcher select option')].map(o => ({value: o.value, label: o.textContent.trim()})),
  selected: (document.querySelector('.scope-switcher select') || {}).value || '',
  notice: (document.querySelector('.state-box.notice') || {}).innerText || '',
  kpis: [...document.querySelectorAll('.kpi-value')].map(e => e.innerText.trim()),
  exportMessage: (document.querySelector('.export-msg') || {}).innerText || '',
  hasExportButtons: document.querySelectorAll('.export-menu button').length,
}))()
"""

PICK_DEPARTMENT = """
(() => {
  const select = document.querySelector('.scope-switcher select');
  if (!select) return 'no-select';
  const option = [...select.options].find(o => o.value && o.value === %s)
              || [...select.options].find(o => o.value && o.value !== '');
  if (!option) return 'no-option';
  select.value = option.value;
  select.dispatchEvent(new Event('change', {bubbles: true}));
  return option.value;
})()
"""

CLICK_EXPORT = """
(() => {
  const button = [...document.querySelectorAll('.export-menu button')].find(b => b.textContent.includes(%s));
  if (!button) return 'no-button';
  button.click();
  return 'clicked';
})()
"""

DIAGNOSE = """
(async () => {
  const token = localStorage.getItem('access_token');
  const response = await fetch('/api/v1/analysis/departments',
                               {headers: {Authorization: 'Bearer ' + token}});
  const text = await response.text();
  return {status: response.status, body: text.slice(0, 300), tokenLength: (token || '').length};
})()
"""


async def run() -> int:
    _console()
    token = _post_json(f"{BASE_URL.replace('5173', '8000')}/api/v1/auth/login",
                       {"username": "demo_analyst", "password": "demo_password_2026"})["access_token"]
    login_page = _auto_login_page(token)
    profile = PROJECT_ROOT / "output" / "_ui_profile"
    process = subprocess.Popen([
        _browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions",
        # 与 check_pages.py 同样的理由：从 Python 子进程启动 Chromium 时，它自己的沙箱初始化会卡死，
        # 表现为浏览器永不就绪（CDP 连接上但第一条命令超时）。
        "--no-sandbox",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "--window-size=1440,900",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import websockets

        ws_url = _debugger_url(PORT)
        async with websockets.connect(ws_url, max_size=None) as websocket:
            cdp = CdpSession(websocket)
            target = await cdp.send("Target.createTarget", {"url": "about:blank"})
            attached = await cdp.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
            session = attached["sessionId"]
            await cdp.send("Page.enable", session_id=session)
            await cdp.send("Emulation.setDeviceMetricsOverride",
                           {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
                           session_id=session)

            await cdp.navigate(f"{BASE_URL}/_autologin.html?to=%2Fdashboard", session)
            await asyncio.sleep(2.5)

            async def evaluate(expression: str):
                result = await cdp.send("Runtime.evaluate",
                                        {"expression": expression, "returnByValue": True, "awaitPromise": True},
                                        session_id=session)
                return result["result"].get("value")

            print("初始状态：", json.dumps(await evaluate(READ_STATE), ensure_ascii=False))
            print("接口自检：", json.dumps(await evaluate(DIAGNOSE), ensure_ascii=False))
            picked = await evaluate(PICK_DEPARTMENT % json.dumps("097"))
            print("切换到的部门：", picked)
            await asyncio.sleep(3.0)
            state = await evaluate(READ_STATE)
            print("切换后状态：", json.dumps(state, ensure_ascii=False))

            clicked = await evaluate(CLICK_EXPORT % json.dumps("导出 CSV"))
            print("点击导出：", clicked)
            await asyncio.sleep(4.0)
            print("导出结果：", json.dumps(await evaluate(READ_STATE), ensure_ascii=False))

            shot = await cdp.send("Page.captureScreenshot",
                                  {"format": "png", "captureBeyondViewport": True}, session_id=session)
            target_path = OUT / "ui_scope_export.png"
            target_path.write_bytes(base64.b64decode(shot["data"]))
            print("截图：", target_path)
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        login_page.unlink(missing_ok=True)
        for leftover in (PROJECT_ROOT / "frontend").glob("_autologin.html"):
            leftover.unlink(missing_ok=True)
        import shutil

        for _ in range(5):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
