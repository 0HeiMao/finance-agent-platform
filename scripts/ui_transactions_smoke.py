"""⑨ 明细更正页界面冒烟：/financial/transactions 真的能筛选、行内更正、两步冲销。

跑法：后端 8000 与前端 dev server 5173 都起着，然后
    .\\.venv\\Scripts\\python.exe scripts\\ui_transactions_smoke.py

脚本自己用 API 导入三行探针明细（期间 2097-03，刻意避开演示数据），在真浏览器里
点一遍「筛选 → 更正 → 冲销」，最后撤销整批探针数据并确认该期间已经清干净。
复用 scripts/check_pages.py 的 CDP 封装与 scripts/verify_transactions.py 的接口助手。
"""
from __future__ import annotations

import asyncio
import base64
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import httpx  # noqa: E402
from check_pages import OVERFLOW_JS, CdpSession, _auto_login_page, _browser, _debugger_url  # noqa: E402
from verify_transactions import CSV_HEADER, login, revert_batch  # noqa: E402

API = "http://127.0.0.1:8000"
FRONT = "http://127.0.0.1:5173"
UI9_PERIOD = "2097-03"
TAG = "UI9冒烟"
NEW_AMOUNT = "777.77"
REASON = "界面冒烟冲销"
DEBUG_PORT = 9379
PROFILE = PROJECT_ROOT / "output" / "_browser_profile_ui_tx9"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def csv_bytes(suffix: str) -> bytes:
    rows = [
        f"{UI9_PERIOD}-05,6001,{TAG}-{suffix}-A,018,界面A,500.00,0,0,0,0",
        f"{UI9_PERIOD}-06,6002,{TAG}-{suffix}-B,097,界面B,0,0,200.00,0,0",
        f"{UI9_PERIOD}-07,6003,{TAG}-{suffix}-C,,界面C,100.00,0,0,0,0",
    ]
    return ("\ufeff" + "\n".join([CSV_HEADER, *rows]) + "\n").encode("utf-8")


async def sweep(client: httpx.AsyncClient) -> int:
    """清掉上次崩溃留下的探针明细：按批次整批撤销。"""
    listing = await client.get("/api/v1/financial/transactions", params={"query": TAG, "limit": 200})
    if listing.status_code != 200:
        return 0
    batches = sorted({row["import_batch"] for row in listing.json().get("data") or [] if row.get("import_batch")})
    for batch in batches:
        job = await revert_batch(client, batch)
        if job.get("status") != "succeeded":
            check(f"清理残留批次 {batch[:8]}…", False, f"{job}")
    return len(batches)


async def rows_of(client: httpx.AsyncClient, **params) -> dict:
    response = await client.get("/api/v1/financial/transactions", params=params)
    return response.json() if response.status_code == 200 else {"data": [], "total": -1}


FACTS_JS = r"""
(() => {
  const trs = [...document.querySelectorAll('table tbody tr')].filter((tr) => !tr.classList.contains('edit-row'));
  const rows = trs.map((tr) => {
    const tds = [...tr.querySelectorAll('td')].map((td) => td.innerText.trim().replace(/\s+/g, ' '));
    return {
      id: tds[0], account: tds[2], department: tds[3], revenue: tds[5], expense: tds[7],
      status: tds[9], buttons: [...tr.querySelectorAll('.row-actions button')].map((b) => b.innerText.trim()),
    };
  });
  const selects = [...document.querySelectorAll('section.filter-card select')];
  const editRow = document.querySelector('tr.edit-row');
  const notice = document.querySelector('section.state-box.notice');
  const error = document.querySelector('section.state-box.error-state');
  return {
    rows,
    headers: [...document.querySelectorAll('table thead th')].map((th) => th.innerText.trim()),
    periods: selects[0] ? [...selects[0].options].map((o) => o.value) : [],
    departmentOptions: selects[1] ? [...selects[1].options].map((o) => o.innerText.trim()) : [],
    editOpen: !!editRow,
    editLabels: editRow ? [...editRow.querySelectorAll('.edit-grid label span')].map((s) => s.innerText.trim()) : [],
    notice: notice ? notice.innerText.trim().replace(/\s+/g, ' ') : '',
    error: error ? error.innerText.trim().replace(/\s+/g, ' ') : '',
    heading: (document.querySelector('.card-head h2') || {}).innerText || '',
  };
})()
"""

PICK_PERIOD_JS = r"""
(() => {
  const select = document.querySelectorAll('section.filter-card select')[0];
  if (!select) return false;
  select.value = '%s';
  select.dispatchEvent(new Event('change', { bubbles: true }));
  return select.value === '%s';
})()
""" % (UI9_PERIOD, UI9_PERIOD)

CLICK_ROW_BUTTON_JS = r"""
((marker, label) => {
  const tr = [...document.querySelectorAll('table tbody tr')]
    .find((node) => !node.classList.contains('edit-row') && node.innerText.includes(marker));
  if (!tr) return false;
  const button = [...tr.querySelectorAll('.row-actions button')].find((node) => node.innerText.trim() === label);
  if (!button) return false;
  button.click();
  return true;
})('%s', '%s')
"""


def click_row_button(marker: str, label: str) -> str:
    """按科目名里的探针标记（如 UI9冒烟-ab12cd-A）找到那一行，点它的按钮。"""
    return CLICK_ROW_BUTTON_JS % (marker, label)


EDIT_AMOUNT_JS = r"""
(() => {
  const row = document.querySelector('tr.edit-row');
  if (!row) return false;
  const label = [...row.querySelectorAll('.edit-grid label')].find((node) => node.innerText.includes('收入金额'));
  const input = label && label.querySelector('input');
  if (!input) return false;
  input.value = '%s';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  return input.value === '%s';
})()
""" % (NEW_AMOUNT, NEW_AMOUNT)

FILL_REASON_JS = r"""
(() => {
  const input = document.querySelector('input.reason');
  if (!input) return false;
  input.value = '%s';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  return input.value === '%s';
})()
""" % (REASON, REASON)

CLICK_TEXT_JS = r"""[...document.querySelectorAll('button')].find((b) => b.innerText.trim() === '%s')?.click() ?? false"""


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


async def drive(ws_url: str, token: str, suffix: str) -> None:
    marker_a = f"{TAG}-{suffix}-A"
    marker_b = f"{TAG}-{suffix}-B"
    import websockets

    async with websockets.connect(ws_url, max_size=None) as websocket:
        cdp = CdpSession(websocket)
        target = await cdp.send("Target.createTarget", {"url": "about:blank"})
        session = (await cdp.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]
        await cdp.send("Page.enable", session_id=session)
        await cdp.send("Emulation.setDeviceMetricsOverride",
                       {"width": 1440, "height": 1200, "deviceScaleFactor": 1, "mobile": False},
                       session_id=session)
        await cdp.navigate(f"{FRONT}/_autologin.html?to=%2Ffinancial%2Ftransactions", session)
        ready = await wait_for(cdp, session, "document.querySelectorAll('table tbody tr').length > 0")
        facts = await evaluate(cdp, session, FACTS_JS) or {}
        check("打开 /financial/transactions 并列出明细", ready, f"{len(facts.get('rows') or [])} 行 | {facts.get('heading')}")
        check("期间下拉来自接口（含探针期间 2097-03）", UI9_PERIOD in (facts.get("periods") or []),
              f"期间 {len(facts.get('periods') or [])} 个")
        check("部门（口径）下拉给出可切换部门",
              any("018" in item for item in (facts.get("departmentOptions") or [])),
              f"首项 {(facts.get('departmentOptions') or [''])[0]}")

        picked = await evaluate(cdp, session, PICK_PERIOD_JS)
        settled = await wait_for(
            cdp, session,
            f"[...document.querySelectorAll('table tbody tr')].filter((tr) => !tr.classList.contains('edit-row')).length === 3"
            f" && document.body.innerText.includes('{TAG}')")
        facts = await evaluate(cdp, session, FACTS_JS) or {}
        rows = facts.get("rows") or []
        check("筛选到探针期间后正好 3 行", picked and settled and len(rows) == 3,
              f"{[(row['account'], row['department']) for row in rows]}")
        check("每行都有「更正」「冲销」按钮",
              all(row["buttons"] == ["更正", "冲销"] for row in rows), f"{[row['buttons'] for row in rows]}")

        clicked = await evaluate(cdp, session, click_row_button(marker_a, "更正"))
        opened = await wait_for(cdp, session, "!!document.querySelector('tr.edit-row')")
        facts = await evaluate(cdp, session, FACTS_JS) or {}
        labels = facts.get("editLabels") or []
        check("点「更正」出现行内编辑区且十个字段都在",
              clicked and opened and len(labels) == 10 and "收入金额" in labels, f"{labels}")

        filled = await evaluate(cdp, session, EDIT_AMOUNT_JS)
        await evaluate(cdp, session, CLICK_TEXT_JS % "保存更正")
        saved = await wait_for(cdp, session, "document.body.innerText.includes('已更正第')", timeout=30)
        facts = await evaluate(cdp, session, FACTS_JS) or {}
        notice = facts.get("notice") or ""
        check("保存后提示已更正并说明重算了哪个期间",
              filled and saved and "已更正第" in notice and UI9_PERIOD in notice, f"{notice[:120]}")
        edited = next((row for row in facts.get("rows") or [] if marker_a in row["account"]), None)
        check("表格里那一行金额已变 777.77 且状态标注已更正",
              bool(edited) and edited["revenue"] == NEW_AMOUNT and "已更正" in edited["status"],
              f"{edited and (edited['revenue'], edited['status'])}")

        await evaluate(cdp, session, click_row_button(marker_b, "冲销"))
        reason_ready = await wait_for(cdp, session, "!!document.querySelector('input.reason')")
        check("点「冲销」先要原因（两步确认，不是一键删）", reason_ready)
        filled_reason = await evaluate(cdp, session, FILL_REASON_JS)
        await evaluate(cdp, session, CLICK_TEXT_JS % "确认冲销")
        reversed_ok = await wait_for(cdp, session, "document.body.innerText.includes('已冲销第')", timeout=30)
        facts = await evaluate(cdp, session, FACTS_JS) or {}
        notice = facts.get("notice") or ""
        rows = facts.get("rows") or []
        check("确认冲销后提示给出冲销行 id",
              filled_reason and reversed_ok and "已冲销第" in notice and "冲销行 #" in notice, f"{notice[:140]}")
        check("列表从 3 行变 4 行（原行保留 + 新增冲销行）", len(rows) == 4, f"{len(rows)} 行")
        statuses = [row["status"] for row in rows]
        check("冲销行与已冲销两种状态都出现", "冲销行" in statuses and "已冲销" in statuses, f"{statuses}")
        reversal = next((row for row in rows if row["status"] == "冲销行"), None)
        check("冲销行的费用金额是负数（-200.00）",
              bool(reversal) and reversal["expense"] == "-200.00", f"{reversal and reversal['expense']}")

        metrics = await evaluate(cdp, session, OVERFLOW_JS) or {}
        check("1440 宽无横向溢出",
              metrics.get("scrollWidth") is not None and metrics["scrollWidth"] <= metrics["clientWidth"],
              f"scrollWidth={metrics.get('scrollWidth')} clientWidth={metrics.get('clientWidth')} "
              f"超宽元素 {len(metrics.get('offenders') or [])}")

        shot = await cdp.send("Page.captureScreenshot", {"format": "png"}, session_id=session)
        out = PROJECT_ROOT / "output" / "charts" / "ui_transactions.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(shot["data"]))
        print(f"  截图 {out.relative_to(PROJECT_ROOT)}", flush=True)

        await cdp.send("Target.closeTarget", {"targetId": target["targetId"]})


async def run() -> int:
    suffix = secrets.token_hex(3)
    async with httpx.AsyncClient(base_url=API, timeout=180.0, trust_env=False) as client:
        if not await login(client, "demo_analyst", "demo_password_2026"):
            check("登录演示管理员", False)
            return 1
        check("登录演示管理员", True)

        removed = await sweep(client)
        check("清掉上次崩溃留下的探针批次", True, f"撤销 {removed} 个批次")

        imported = await client.post("/api/v1/financial/import",
                                     files={"file": (f"ui-tx9-{suffix}.csv", csv_bytes(suffix), "text/csv")})
        body = imported.json() if imported.status_code == 200 else {}
        batch = str(body.get("batch") or "")
        check("导入三行探针明细",
              imported.status_code == 200 and body.get("imported") == 3 and bool(batch),
              f"HTTP {imported.status_code} batch={batch[:12]}…")

        browser = _browser()
        page = _auto_login_page(await _token(client))
        process = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--no-sandbox",
             f"--remote-debugging-port={DEBUG_PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await drive(_debugger_url(DEBUG_PORT), await _token(client), suffix)
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
            for _ in range(5):
                shutil.rmtree(PROFILE, ignore_errors=True)
                if not PROFILE.exists():
                    break
                time.sleep(0.5)
            page.unlink(missing_ok=True)

        job = await revert_batch(client, batch)
        check("撤销整批探针明细（后台任务成功）", job.get("status") == "succeeded", f"{job}")
        listing = await rows_of(client, period=UI9_PERIOD)
        check("撤销后探针期间 0 行", listing.get("total") == 0, f"total={listing.get('total')}")
        summary = await client.get("/api/v1/analysis/summary", params={"period": UI9_PERIOD})
        payload = summary.json() if summary.status_code == 200 else {}
        check("撤销后探针期间没有合计（指标与落库一起清）",
              payload.get("has_data") is False and payload.get("source") == "empty",
              f"has_data={payload.get('has_data')} source={payload.get('source')}")

    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}", flush=True)
    return 1 if failed else 0


async def _token(client: httpx.AsyncClient) -> str:
    return client.headers["Authorization"].removeprefix("Bearer ")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(run()))
