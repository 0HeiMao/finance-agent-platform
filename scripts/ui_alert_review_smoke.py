"""真浏览器点击预警处理按钮的冒烟脚本。

为什么需要它：`check_pages.py` 只量横向溢出、`pytest` 只打接口，
两者都发现不了「控件在，但点了没反应 / 点了状态不变」这类前端缺陷——
`ScopeSwitcher` 的 onMounted 竞态就是这么被抓到的。这里真的点一遍：
确认异常 → 状态徽标与顶部统计都要变；备注 → 弹出输入框（取消不写库）；退回 → 恢复原状。

用法（注意用正斜杠，反斜杠会触发 unicodeescape 语法错误）：
    .\.venv\Scripts\python.exe scripts/ui_alert_review_smoke.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.check_pages import (  # noqa: E402
    CdpSession,
    _auto_login_page,
    _browser,
    _console,
    _debugger_url,
)

# 结论：读第一条预警的状态徽标、顶部「待处理」计数、以及提示条文本
READ_STATE = """
(() => {
  const chip = document.querySelector('.status-chip');
  const stat = document.querySelector('.stat strong.pending');
  return {
    chip: chip ? chip.textContent.trim() : '',
    pending: stat ? stat.textContent.trim() : '',
    rows: document.querySelectorAll('tbody tr').length,
  };
})()
"""

CLICK_JS = """
(() => {
  const buttons = [...document.querySelectorAll('.ops .link-btn')];
  const target = buttons.find((b) => b.textContent.trim() === %s);
  if (!target) return 'missing';
  target.click();
  return 'clicked';
})()
"""


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    # 本机请求必须绕开系统代理：本机的 HTTP_PROXY 会让 urlopen 拿到非 JSON 的错误页
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request) as response:
        return json.loads(response.read().decode())


async def main_async(args) -> int:
    import websockets

    token = _post_json(f"{args.api_base}/api/v1/auth/login",
                       {"username": args.username, "password": args.password})["access_token"]
    _auto_login_page(token)
    # --user-data-dir 必须有：不指定时 Chromium 会去用真实用户配置，headless 下往往直接起不来
    profile = PROJECT_ROOT / "output" / "_browser_profile_smoke"
    process = subprocess.Popen(
        [_browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions", "--no-sandbox",
         f"--remote-debugging-port={args.port}", f"--user-data-dir={profile}",
         "--window-size=1440,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = 0
    try:
        ws_url = _debugger_url(args.port)
        async with websockets.connect(ws_url, max_size=None) as websocket:
            cdp = CdpSession(websocket)
            target = await cdp.send("Target.createTarget", {"url": "about:blank"})
            attached = await cdp.send("Target.attachToTarget",
                                      {"targetId": target["targetId"], "flatten": True})
            session = attached["sessionId"]
            await cdp.send("Page.enable", session_id=session)
            await cdp.navigate(f"{args.base_url}/_autologin.html?to=%2Fanalysis%2Falerts", session)
            await asyncio.sleep(2.5)

            async def evaluate(expression: str):
                result = await cdp.send("Runtime.evaluate",
                                        {"expression": expression, "returnByValue": True, "awaitPromise": True},
                                        session_id=session)
                return result["result"].get("value")

            async def click(label: str) -> None:
                outcome = await evaluate(CLICK_JS % json.dumps(label))
                assert outcome == "clicked", f"没找到按钮「{label}」：{outcome}"
                await asyncio.sleep(1.5)

            async def state() -> dict:
                return await evaluate(READ_STATE) or {}

            before = await state()
            print(f"初始：{before}")

            await click("确认异常")
            confirmed = await state()
            print(f"点「确认异常」后：{confirmed}")
            # 断言用相对变化：库里有多少条预警取决于演示数据，写死数字等于把脚本绑在数据上
            if not (confirmed.get("chip") == "已确认"
                    and int(confirmed.get("pending", -1)) == int(before.get("pending", -2)) - 1):
                print("FAIL: 状态徽标或待处理计数没跟着变")
                failures += 1

            # 备注走 ElMessageBox：只验证弹窗出现，然后取消（不写库）
            await evaluate(CLICK_JS % json.dumps("备注"))
            await asyncio.sleep(0.8)
            dialog = await evaluate(
                "(() => { const box = document.querySelector('.el-message-box');"
                " return box ? box.querySelector('.el-message-box__title').textContent.trim() : ''; })()")
            print(f"点「备注」后的弹窗标题：{dialog!r}")
            if "备注" not in (dialog or ""):
                print("FAIL: 备注弹窗没出现")
                failures += 1
            await evaluate(
                "(() => { const b = document.querySelector('.el-message-box__btns button');"
                " if (b) b.click(); return true; })()")
            await asyncio.sleep(0.8)

            await click("退回")
            reverted = await state()
            print(f"点「退回」后：{reverted}")
            if not (reverted.get("chip") == "待处理"
                    and reverted.get("pending") == before.get("pending")):
                print("FAIL: 退回没能恢复待处理状态")
                failures += 1

            shot = await cdp.send("Page.captureScreenshot", {"format": "png"}, session_id=session)
            out = PROJECT_ROOT / "output" / "charts" / "ui_alert_review.png"
            out.write_bytes(base64.b64decode(shot["data"]))
            print(f"截图：{out.relative_to(PROJECT_ROOT)}")
            await cdp.send("Target.closeTarget", {"targetId": target["targetId"]})
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        import shutil
        import time

        for _ in range(5):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.5)
        (PROJECT_ROOT / "frontend" / "_autologin.html").unlink(missing_ok=True)
    print("PASS：预警处理按钮真的改变了状态" if not failures else f"FAIL：{failures} 项")
    return 1 if failures else 0


def main() -> int:
    _console()
    parser = argparse.ArgumentParser(description="真浏览器点击预警处理按钮")
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="demo_analyst")
    parser.add_argument("--password", default="demo_password_2026")
    parser.add_argument("--port", type=int, default=9345)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
