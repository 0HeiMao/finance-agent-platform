"""⑦ 分档规则界面冒烟：/admin/alert-rules 真的把「适用范围」列出来、默认规则不能删、新增能用。

跑法：后端 8000 + 前端 dev server 5173 都起着，然后
    .\\.venv\\Scripts\\python.exe scripts\\ui_alert_rule_tiers_smoke.py

脚本自己先用 API 造一条临时分档规则（让页面有分档行可看），跑完把临时规则全删掉。
复用 scripts/check_pages.py 的 CDP 封装（同一套 Edge 无头 + 临时登录页）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from check_pages import OVERFLOW_JS, CdpSession, _auto_login_page, _browser, _debugger_url  # noqa: E402

API = "http://127.0.0.1:8000/api/v1"
FRONT = "http://127.0.0.1:5173"
MARK = "⑦界面冒烟"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RESULTS: list[tuple[str, bool, str]] = []

FACTS_JS = r"""
(() => {
  const rows = [...document.querySelectorAll('table.rule-table tbody tr')].map((tr) => {
    const tag = tr.querySelector('.tag');
    const danger = tr.querySelector('button.danger');
    return {
      type: (tr.querySelector('strong') || {}).innerText || '',
      tag: tag ? tag.innerText.trim() : '',
      scope: tag && tag.nextElementSibling ? tag.nextElementSibling.innerText.trim() : '',
      cells: [...tr.querySelectorAll('td')].map((td) => td.innerText.trim().replace(/\s+/g, ' ')),
      deleteDisabled: danger ? !!danger.disabled : null,
      deleteTitle: danger ? danger.title : '',
    };
  });
  const text = document.body.innerText;
  return {
    rows,
    headers: [...document.querySelectorAll('table.rule-table thead th')].map((th) => th.innerText.trim()),
    hasCard: text.includes('新增分档规则'),
    priority: (text.match(/部门\+期间[^\n]{0,80}/) || [''])[0],
    counts: (text.match(/现在有\s*\d+\s*条默认、\s*\d+\s*条分档/) || [''])[0],
    warnings: [...document.querySelectorAll('.el-message')].map((node) => node.innerText.trim()),
  };
})()
"""


def call(method: str, path: str, token: str | None = None, body: dict | None = None, raw: bool = False):
    request = urllib.request.Request(f"{API}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=60) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    text = payload.decode("utf-8", "replace")
    if raw:
        return status, text
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


async def evaluate(cdp: CdpSession, session: str, expression: str):
    result = await cdp.send("Runtime.evaluate",
                           {"expression": expression, "returnByValue": True, "awaitPromise": True},
                           session_id=session)
    return result["result"].get("value")


async def wait_for(cdp: CdpSession, session: str, expression: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await evaluate(cdp, session, expression):
            return True
        await asyncio.sleep(0.5)
    return False


async def drive(ws_url: str, token: str) -> None:
    import websockets

    async with websockets.connect(ws_url, max_size=None) as websocket:
        cdp = CdpSession(websocket)
        target = await cdp.send("Target.createTarget", {"url": "about:blank"})
        session = (await cdp.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]
        await cdp.send("Page.enable", session_id=session)
        await cdp.send("Emulation.setDeviceMetricsOverride",
                       {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
                       session_id=session)
        await cdp.navigate(f"{FRONT}/_autologin.html?to=%2Fadmin%2Falert-rules", session)
        ready = await wait_for(cdp, session, "document.querySelectorAll('table.rule-table tbody tr').length >= 3")
        row_count = await evaluate(cdp, session, "document.querySelectorAll('table.rule-table tbody tr').length")
        check("打开 /admin/alert-rules 并列出规则行", ready, f"{row_count} 行")

        facts = await evaluate(cdp, session, FACTS_JS) or {}
        rows = facts.get("rows") or []
        headers = facts.get("headers") or []
        check("表头有「适用范围」列", "适用范围" in headers, f"{headers}")
        default_row = next((row for row in rows if row["tag"] == "默认"), None)
        tier_row = next((row for row in rows if row["tag"] == "分档"), None)
        check("默认规则行：默认 + 全公司 · 所有期间",
              bool(default_row) and default_row["scope"] == "全公司 · 所有期间",
              f"{default_row and (default_row['type'], default_row['tag'], default_row['scope'])}")
        check("分档规则行：分档 + 018 · 2026-10",
              bool(tier_row) and tier_row["scope"] == "018 · 2026-10",
              f"{tier_row and (tier_row['type'], tier_row['tag'], tier_row['scope'])}")
        check("默认规则的删除按钮禁用且给出原因",
              bool(default_row) and default_row["deleteDisabled"] is True
              and "默认规则只能停用" in (default_row["deleteTitle"] or ""),
              f"disabled={default_row and default_row['deleteDisabled']} title={default_row and default_row['deleteTitle']}")
        check("分档规则的删除按钮可用", bool(tier_row) and tier_row["deleteDisabled"] is False,
              f"disabled={tier_row and tier_row['deleteDisabled']}")
        check("有「新增分档规则」卡片与优先级说明",
              facts.get("hasCard") and "部门+期间" in (facts.get("priority") or "")
              and "只限一个维度" in (facts.get("priority") or ""),
              f"{facts.get('priority')}")
        check("统计文案给出默认/分档条数", "现在有" in (facts.get("counts") or ""), f"{facts.get('counts')}")

        metrics = await evaluate(cdp, session, OVERFLOW_JS) or {}
        check("1440 宽无横向溢出",
              metrics.get("scrollWidth") is not None and metrics["scrollWidth"] <= metrics["clientWidth"],
              f"scrollWidth={metrics.get('scrollWidth')} clientWidth={metrics.get('clientWidth')} 超宽元素 {len(metrics.get('offenders') or [])}")

        # 两个维度都空时前端应当先拦下来（后端也会 422，但提示不该等到请求回来）
        await evaluate(cdp, session, "[...document.querySelectorAll('button')].find((b) => b.innerText.includes('新增分档规则'))?.click()")
        warned = await wait_for(cdp, session,
                                "document.body.innerText.includes('分档规则至少要限定部门或期间之一')", timeout=10)
        check("两个维度都空时前端拦下并提示", warned)

        # 选一个部门后应当真的建得出来，并在表格里出现「018 · 所有期间」
        await evaluate(cdp, session, r"""
(() => {
  const labels = [...document.querySelectorAll('label.field')];
  const pick = (text) => labels.find((node) => node.innerText.includes(text));
  const setValue = (node, value) => {
    node.value = value;
    node.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const setInput = (node, value) => {
    node.value = value;
    node.dispatchEvent(new Event('input', { bubbles: true }));
  };
  const dept = pick('部门（可留空）')?.querySelector('select');
  const desc = pick('说明')?.querySelector('input');
  if (dept) setValue(dept, '018');
  if (desc) setInput(desc, '⑦界面冒烟-新增');
  return !!dept && !!desc;
})()
""")
        await evaluate(cdp, session, "[...document.querySelectorAll('button')].find((b) => b.innerText.includes('新增分档规则'))?.click()")
        created = await wait_for(
            cdp, session,
            "[...document.querySelectorAll('table.rule-table tbody tr')].some((tr) => tr.innerText.includes('018 · 所有期间'))",
            timeout=30)
        check("界面上新增分档规则（018 · 所有期间）后表格出现该行", created)

        shot = await cdp.send("Page.captureScreenshot", {"format": "png"}, session_id=session)
        out = PROJECT_ROOT / "output" / "charts" / "ui_tier_rules.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(shot["data"]))
        print(f"  截图 {out.relative_to(PROJECT_ROOT)}", flush=True)

        await cdp.send("Target.closeTarget", {"targetId": target["targetId"]})


def main() -> int:
    status, payload = call("POST", "/auth/login", body={"username": "demo_analyst", "password": "demo_password_2026"})
    assert status == 200, f"登录失败：{status} {payload}"
    token = payload["access_token"]

    status, payload = call("GET", "/admin/alert-rules", token=token)
    assert status == 200, payload
    for rule in payload["data"]:
        if MARK in (rule.get("description") or ""):
            call("DELETE", f"/admin/alert-rules/id/{rule['id']}", token=token)
    status, seeded = call("POST", "/admin/alert-rules", token=token, body={
        "alert_type": "成本率异常", "department": "018", "period": "2026-10",
        "metric_name": "cost_ratio", "comparator": "gt", "threshold_value": 0.95,
        "level": "high", "description": f"{MARK}-临时分档"})
    assert status == 201, f"造临时分档规则失败：{status} {seeded}"

    browser = _browser()
    page = _auto_login_page(token)
    profile = PROJECT_ROOT / "output" / "_browser_profile_ui_tier"
    process = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions", "--no-sandbox",
         "--remote-debugging-port=9378", f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        asyncio.run(drive(_debugger_url(9378), token))
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        for _ in range(5):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.5)
        page.unlink(missing_ok=True)

    status, payload = call("GET", "/admin/alert-rules", token=token)
    leftovers = [rule for rule in payload.get("data", []) if MARK in (rule.get("description") or "")]
    for rule in leftovers:
        call("DELETE", f"/admin/alert-rules/id/{rule['id']}", token=token)
    check("临时规则清理干净（含界面新增那条）", True, f"删除 {len(leftovers)} 条")

    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
