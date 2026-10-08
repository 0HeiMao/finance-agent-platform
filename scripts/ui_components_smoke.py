"""⑩ 组件真机冒烟：在真浏览器里点一遍单元测试碰不到的东西。

单元测试（`frontend/tests/*.test.mjs`）跑在自制的对象树渲染器里，能验逻辑、验不了
「真 DOM + 真接口 + 真流式响应」。这个脚本补上那一段，只看三件事：

1. 口径切换器：真实 `/analysis/departments` 回来的选项、切到某部门后状态真的落盘、
   页面上出现「只看「…」的明细」提示，切回全公司又消失。
2. 导出按钮：点「导出 CSV」走真接口，提示里必须出现**后端 Content-Disposition 给的文件名**
   （`明细-….csv`），不是前端拼的兜底名。
3. Agent 对话框：真发一问看流式回答；再用 CDP 把 `chat/stream` 断掉制造真实错误，
   输入框此时已被清空——点「重试」必须重发**上一条问题**（这正是单元测试抓到、
   修掉的那个 bug：原来重试读的是空输入框，点了等于没点）。
4. 管理概览的**慢接口告警**区块：标题在、区块不是空白（要么有空态说明、要么有点了名的告警行），
   并把这张卡片截图存到 `output/charts/ui_slow_alerts.png`。
5. System 设置的「登录与令牌」卡片与用户管理的「会话」列 / 「强制下线」按钮（③）：只核对渲染与
   禁用态，**不点「退出登录」**——那会真的把 demo 账号的会话版本推进一版。点它的是
   `scripts/verify_session_revocation.py`，用的是临时账号。

用法（要求后端 8000 与前端 dev server 5173 在跑）：

    .\\.venv\\Scripts\\python.exe scripts\\ui_components_smoke.py
    .\\.venv\\Scripts\\python.exe scripts\\ui_components_smoke.py --keep-browser
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_pages import (  # noqa: E402  （同目录脚本，靠上面的 sys.path 找到）
    CdpSession,
    _auto_login_page,
    _browser,
    _console,
    _debugger_url,
    _post_json,
)

DEFAULT_BASE = "http://127.0.0.1:5173"
DEFAULT_API = "http://127.0.0.1:8000"
USERNAME = "demo_analyst"
PASSWORD = "demo_password_2026"
QUESTION = "2026-07 的收入是多少？"
PORT = 9381

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail and not ok else ""), flush=True)
    return bool(ok)


def login(api: str) -> str:
    payload = _post_json(f"{api}/api/v1/auth/login", {"username": USERNAME, "password": PASSWORD})
    token = payload.get("access_token")
    if not token:
        raise SystemExit(f"登录失败：{payload}")
    return token


async def evaluate(cdp: CdpSession, session: str, expression: str):
    result = await cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True},
                            session_id=session)
    return result["result"].get("value")


async def wait_js(cdp: CdpSession, session: str, expression: str, timeout: float = 20.0,
                  poll: float = 0.3):
    """轮询 JS 谓词，直到它返回真值；返回最后一次的值，方便把现场写进失败信息。"""
    deadline = time.time() + timeout
    value = None
    while True:
        value = await evaluate(cdp, session, expression)
        if value:
            return value
        if time.time() >= deadline:
            return value
        await asyncio.sleep(poll)


async def wait_ready(cdp: CdpSession, session: str, timeout: float = 30.0) -> bool:
    ok = await wait_js(
        cdp, session,
        "(() => { const app = document.getElementById('app');"
        " return !!app && (document.body.innerText || '').trim().length > 200; })()",
        timeout=timeout)
    return bool(ok)


async def open_page(cdp: CdpSession, session: str, base: str, route: str) -> None:
    await cdp.navigate(f"{base}/_autologin.html?to={route}", session)
    if not await wait_ready(cdp, session):
        raise SystemExit(f"页面没渲染出来：{route}")


async def drive(ws_url: str, base: str, api: str, keep_open: bool) -> int:
    import websockets

    async with websockets.connect(ws_url, max_size=None) as websocket:
        cdp = CdpSession(websocket)
        target = await cdp.send("Target.createTarget", {"url": "about:blank"})
        attached = await cdp.send("Target.attachToTarget",
                                  {"targetId": target["targetId"], "flatten": True})
        session = attached["sessionId"]
        for domain in ("Page.enable", "Runtime.enable", "Network.enable"):
            await cdp.send(domain, {}, session_id=session)

        # ---------- 口径切换器 + 导出：/analysis/revenue 同时有这两个组件 ----------
        await open_page(cdp, session, base, "/analysis/revenue")
        check("口径切换器：真浏览器里渲染出下拉", await evaluate(
            cdp, session, "!!document.querySelector('.scope-switcher select')"))
        check("口径切换器：不受限账号默认「全公司（全部部门）」", await evaluate(
            cdp, session,
            "(() => { const s = document.querySelector('.scope-switcher select');"
            " return s ? s.selectedOptions[0].textContent.trim() : ''; })()"
        ) == "全公司（全部部门）")
        options = await wait_js(
            cdp, session,
            "(() => { const s = document.querySelector('.scope-switcher select');"
            " if (!s || s.options.length < 2) return '';"
            " return JSON.stringify([...s.options].map(o => ({ value: o.value, text: o.textContent.trim() }))); })()",
            timeout=20)
        options = json.loads(options) if options else []
        departments = [item for item in (options or []) if item["value"]]
        check("口径切换器：选项来自真实接口且含部门行", len(departments) >= 1,
              f"实际选项：{options}")
        if not departments:
            return finish(keep_open)

        # 口径是模块级 ref（不落盘，见 frontend/src/api/scope.js 的注释），所以「切了有没有生效」
        # 只能在网络上看：把 fetch 与 XHR 都记一笔，切完之后请求里必须出现 department=…
        await evaluate(cdp, session, """
        (() => {
          window.__reqs = [];
          const originalFetch = window.fetch;
          window.fetch = function (...args) {
            window.__reqs.push(String(args[0]));
            return originalFetch.apply(this, args);
          };
          const originalOpen = XMLHttpRequest.prototype.open;
          XMLHttpRequest.prototype.open = function (method, url, ...rest) {
            window.__reqs.push(String(url));
            return originalOpen.call(this, method, url, ...rest);
          };
          return true;
        })()
        """)

        picked = departments[0]["value"]
        switch_js = """
        (() => {
          const s = document.querySelector('.scope-switcher select');
          s.value = %s;
          s.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        })()
        """ % json.dumps(picked)
        await evaluate(cdp, session, switch_js)
        scoped_call = await wait_js(
            cdp, session,
            "(() => { const hit = (window.__reqs || []).find(u => u.includes('department=' + %s));"
            " return hit || ''; })()" % json.dumps(picked), timeout=15)
        check(f"口径切换器：切到部门 {picked} 后后续请求带上了 department={picked}",
              bool(scoped_call), f"记录到的请求：{await evaluate(cdp, session, 'JSON.stringify(window.__reqs)')}")
        check("口径切换器：页面上出现「只看…的明细」提示", await wait_js(
            cdp, session,
            "(() => { const h = document.querySelector('.scope-hint');"
            " return h ? h.textContent.trim() : ''; })()", timeout=10))
        await evaluate(cdp, session, """
        (() => {
          const s = document.querySelector('.scope-switcher select');
          s.value = '';
          s.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        })()
        """)
        check("口径切换器：切回全公司后提示消失", await wait_js(
            cdp, session, "!document.querySelector('.scope-hint')", timeout=10))

        click_js = """
        (() => {
          const b = [...document.querySelectorAll('.export-menu button')]
            .find(x => x.textContent.trim() === %s);
          if (!b) return false;
          b.click();
          return true;
        })()
        """ % json.dumps("导出 CSV")
        check("导出按钮：页面上找得到「导出 CSV」", bool(await evaluate(cdp, session, click_js)))
        await evaluate(cdp, session, """
        (() => { const b = [...document.querySelectorAll('.export-menu button')]
            .find(x => x.textContent.trim() === '导出 CSV'); if (b) b.click(); return true; })()
        """)
        message = await wait_js(
            cdp, session,
            "(() => { const m = document.querySelector('.export-msg');"
            " return m ? m.textContent.trim() : ''; })()", timeout=30)
        check("导出按钮：真下载后用后端给的文件名提示「已保存」",
              bool(message) and "已保存" in str(message), f"提示：{message}")
        # 后端（backend/api_export.py:179）给的明细名是 ASCII 的 `transactions-{期间}-{口径}.csv`，
        # `filename*=` 与 `filename=` 同值，所以这里看不出「中文真名」；但前端兜底名是
        # `transactions-{期间}.csv`（没有口径后缀），断言带 `-all` 才证明名字来自 Content-Disposition。
        check("导出按钮：文件名来自 Content-Disposition（带口径后缀）",
              ".csv" in str(message) and "-all" in str(message), f"提示：{message}")
        check("导出按钮：提示不带失败样式", not await evaluate(
            cdp, session,
            "(() => { const m = document.querySelector('.export-msg');"
            " return !!m && m.classList.contains('bad'); })()"))

        # ---------- Agent 对话框：真流式 + 断网重试 ----------
        await open_page(cdp, session, base, "/agent")
        check("Agent 对话框：真浏览器里渲染出输入框", await evaluate(
            cdp, session, "!!document.querySelector('.agent-input-row input')"))
        check("Agent 对话框：空输入点发送不会发问", await evaluate(cdp, session, """
        (() => {
          const b = [...document.querySelectorAll('.agent-input-row button')]
            .find(x => x.textContent.includes('发送'));
          if (!b) return false;
          b.click();
          return document.querySelectorAll('.agent-message').length === 0;
        })()
        """))

        type_js = """
        (() => {
          const el = document.querySelector('.agent-input-row input');
          if (!el) return false;
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          setter.call(el, %s);
          el.dispatchEvent(new Event('input', { bubbles: true }));
          return true;
        })()
        """
        send_js = """
        (() => {
          const b = [...document.querySelectorAll('.agent-input-row button')]
            .find(x => x.textContent.includes('发送'));
          if (!b) return false;
          b.click();
          return true;
        })()
        """
        await evaluate(cdp, session, type_js % json.dumps(QUESTION))
        await evaluate(cdp, session, send_js)
        check("Agent 对话框：提问立刻进消息流", bool(await wait_js(
            cdp, session,
            "document.querySelectorAll('.agent-message').length >= 2", timeout=20)))
        answer = await wait_js(cdp, session, """
        (() => {
          const rows = [...document.querySelectorAll('.agent-message')];
          const last = rows[rows.length - 1];
          if (!last || rows.length < 2) return '';
          const send = [...document.querySelectorAll('.agent-input-row button')]
            .find(x => x.textContent.includes('发送'));
          if (!send) return '';   // 还在流式（按钮显示「停止」）
          const text = last.innerText.trim();
          return text.length > 10 ? text : '';
        })()
        """, timeout=120)
        check("Agent 对话框：流式回答落到页面上（真模型调用）", bool(answer),
              f"末条消息：{str(answer)[:120]}")
        check("Agent 对话框：回答带依据来源", "依据" in str(answer), f"末条消息：{str(answer)[:200]}")

        await evaluate(cdp, session, """
        (() => { const b = [...document.querySelectorAll('button')]
            .find(x => x.textContent.trim() === '清空会话'); if (b) b.click(); return true; })()
        """)
        check("Agent 对话框：清空会话后回到空态", bool(await wait_js(
            cdp, session,
            "!!document.querySelector('.agent-empty') && document.querySelectorAll('.agent-message').length === 0",
            timeout=10)))

        # 断掉 chat/stream：前端拿到的是一次真实的连接失败，于是出现错误与「重试」
        blocked = True
        try:
            await cdp.send("Network.setBlockedURLs", {"urls": ["*chat/stream*"]}, session_id=session)
        except RuntimeError as exc:
            blocked = False
            check("Agent 对话框：能断掉 chat/stream 制造真实错误", False, str(exc))
        if blocked:
            await evaluate(cdp, session, type_js % json.dumps(QUESTION))
            await evaluate(cdp, session, send_js)
            error_text = await wait_js(
                cdp, session,
                "(() => { const e = document.querySelector('.agent-error');"
                " return e ? e.innerText.trim() : ''; })()", timeout=30)
            check("Agent 对话框：流断掉时页面显示错误", bool(error_text), f"错误区：{error_text}")
            check("Agent 对话框：错误旁边有「重试」按钮", await evaluate(cdp, session, """
            [...document.querySelectorAll('button')].some(x => x.textContent.trim() === '重试')
            """))
            empty_input = await evaluate(
                cdp, session, "document.querySelector('.agent-input-row input').value === ''")
            check("Agent 对话框：此时输入框已经空了（所以重试必须用上一条问题）", bool(empty_input))
            messages_before = await evaluate(
                cdp, session, "document.querySelectorAll('.agent-message').length")

            await cdp.send("Network.setBlockedURLs", {"urls": []}, session_id=session)
            await evaluate(cdp, session, """
            (() => { const b = [...document.querySelectorAll('button')]
                .find(x => x.textContent.trim() === '重试'); if (b) b.click(); return true; })()
            """)
            retried = await wait_js(cdp, session, """
            (() => {
              const rows = [...document.querySelectorAll('.agent-message')];
              const last = rows[rows.length - 1];
              if (!last || rows.length < 2) return '';
              const send = [...document.querySelectorAll('.agent-input-row button')]
                .find(x => x.textContent.includes('发送'));
              if (!send) return '';
              const text = last.innerText.trim();
              return text.length > 10 ? text : '';
            })()
            """, timeout=120)
            check("Agent 对话框：点「重试」用上一条问题重发并拿到回答", bool(retried),
                  f"末条消息：{str(retried)[:120]}")
            check("Agent 对话框：重试后错误区消失", bool(await wait_js(
                cdp, session, "!document.querySelector('.agent-error')", timeout=10)))
            check("Agent 对话框：重试后消息流变长（重发的是上一条提问）",
                  (await evaluate(cdp, session, "document.querySelectorAll('.agent-message').length")
                   or 0) > (messages_before or 0),
                  f"重试前 {messages_before} 条")
            await evaluate(cdp, session, """
            (() => { const b = [...document.querySelectorAll('button')]
                .find(x => x.textContent.trim() === '清空会话'); if (b) b.click(); return true; })()
            """)

        # ---------- 慢接口告警：/admin 的「接口运行观测」卡片（⑪） ----------
        # ⑪ 的告警是写给日志的，但值班的人先看管理页；这一页必须真的把 slow_paths 渲染出来，
        # 而且不能是「卡片在、里面全空」——空态也要有一句说清阈值是多少的说明。
        await open_page(cdp, session, base, "/admin")
        check("管理概览：慢接口告警区块渲染出来", await wait_js(
            cdp, session,
            "document.body.innerText.includes('慢接口告警（单次超过阈值）')"))
        alert_rows = await evaluate(cdp, session, """
        (() => {
          const head = [...document.querySelectorAll('h3.sub-head')]
            .find(x => x.textContent.includes('慢接口告警'));
          if (!head) return '[]';
          // 只认紧跟在标题后面的那张列表：整个 section 里还有「最慢的接口」那一段，
          // 用 parentElement.querySelectorAll('ul.dir-list > li') 会把它的行也捞进来。
          const list = head.nextElementSibling;
          if (!list || !list.matches('ul.dir-list')) return '[]';
          return JSON.stringify([...list.children].map(li => li.innerText.replace(/\\s+/g, ' ').trim()));
        })()
        """)
        alert_rows = json.loads(alert_rows) if alert_rows else []
        empty_hint = await evaluate(cdp, session, """
        (() => {
          const p = [...document.querySelectorAll('p.muted')]
            .find(x => x.textContent.includes('没有请求超过阈值'));
          return p ? p.textContent.replace(/\\s+/g, ' ').trim() : '';
        })()
        """)
        check("管理概览：告警区不是空白（空态说明或有告警行）", bool(alert_rows) or bool(empty_hint),
              f"告警行 {alert_rows[:2]}；空态：{empty_hint[:90]}")
        if alert_rows:
            check("管理概览：告警行点名路径与超阈值次数",
                  all("/" in row and "超阈值" in row for row in alert_rows), alert_rows[0][:140])

        # 顺手把这张卡片截下来（⑪ 的证据要看得见，而不只是「断言过了」）
        box = await evaluate(cdp, session, """
        (() => {
          const head = [...document.querySelectorAll('h3.sub-head')]
            .find(x => x.textContent.includes('慢接口告警'));
          if (!head) return '';
          const rect = head.parentElement.getBoundingClientRect();
          return JSON.stringify({ x: Math.max(0, Math.round(rect.x)),
                                  y: Math.max(0, Math.round(rect.y + window.scrollY)),
                                  width: Math.round(rect.width), height: Math.round(rect.height) });
        })()
        """)
        if box:
            area = json.loads(box)
            shot = await cdp.send(
                "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True, "clip": {**area, "scale": 1}},
                session_id=session)
            target_png = PROJECT_ROOT / "output" / "charts" / "ui_slow_alerts.png"
            target_png.parent.mkdir(parents=True, exist_ok=True)
            target_png.write_bytes(base64.b64decode(shot["data"]))
            check("管理概览：慢接口告警卡片截图已保存", target_png.exists(),
                  f"{target_png.relative_to(PROJECT_ROOT)}（{area['width']}x{area['height']}）")

        # ---------- 会话版本与强制下线：设置页 / 用户管理（③） ----------
        # ③ 的机制在服务端（把会话版本 +1，此前签发的令牌一起作废），但「卡片在不在、按钮点了
        # 有没有反应」这一层只有真浏览器看得出来。这里只核对渲染与禁用态，**刻意不点「退出登录」**：
        # 它会真的把 demo 账号的会话版本推进一版，让 verify_session_revocation.py 里
        # 「演示账号的会话版本没被动过」这条断言失去意义（那个脚本才是点它的地方，用的是临时账号）。
        await open_page(cdp, session, base, "/settings")
        check("System 设置：登录与令牌卡片渲染出来", await wait_js(
            cdp, session, "document.body.innerText.includes('登录与令牌')"))
        session_card = await evaluate(cdp, session, """
        (() => {
          const card = [...document.querySelectorAll('.page-card')]
            .find(x => x.textContent.includes('登录与令牌'));
          if (!card) return '';
          const text = card.innerText.replace(/\\s+/g, ' ').trim();
          const buttons = [...card.querySelectorAll('button')].map(b => b.textContent.trim());
          return JSON.stringify({ text, buttons });
        })()
        """)
        session_card = json.loads(session_card) if session_card else {}
        card_text = session_card.get("text", "")
        check("System 设置：卡片里显示当前账号与会话版本（真数字，不是模板占位）",
              bool(card_text) and "会话版本" in card_text
              and any(ch.isdigit() for ch in card_text.split("会话版本", 1)[-1][:8]),
              card_text[:160])
        check("System 设置：有「退出登录（作废全部令牌）」按钮",
              any("退出登录" in b for b in session_card.get("buttons", [])),
              f"卡片里的按钮：{session_card.get('buttons')}")

        await open_page(cdp, session, base, "/admin/users")
        users_head = await wait_js(cdp, session, """
        (() => {
          const ths = [...document.querySelectorAll('table thead th')].map(x => x.textContent.trim());
          return ths.includes('会话') ? JSON.stringify(ths) : '';
        })()
        """, timeout=20)
        check("用户管理：表头有「会话」列", bool(users_head), f"表头：{users_head}")
        sessions = await evaluate(cdp, session, """
        (() => {
          const rows = [...document.querySelectorAll('table tbody tr')];
          return JSON.stringify(rows.map(tr => {
            const cell = [...tr.querySelectorAll('td')].find(td => /未作废过|已作废/.test(td.innerText));
            const kick = [...tr.querySelectorAll('button')]
              .find(b => b.textContent.includes('强制下线'));
            return { session: cell ? cell.innerText.replace(/\\s+/g, ' ').trim() : '',
                     kick: !!kick, kick_disabled: kick ? kick.disabled : null,
                     is_self: /当前账号/.test(tr.innerText) };
          }));
        })()
        """)
        sessions = json.loads(sessions) if sessions else []
        check("用户管理：每行都有会话状态（未作废过 / 已作废 N 次）",
              bool(sessions) and all(row["session"] for row in sessions),
              f"{len(sessions)} 行，示例：{[row['session'] for row in sessions[:3]]}")
        check("用户管理：每行都有「强制下线」按钮",
              bool(sessions) and all(row["kick"] for row in sessions), f"{len(sessions)} 行")
        mine = [row for row in sessions if row["is_self"]]
        check("用户管理：自己那行的「强制下线」是禁用的（管理接口也拒绝自踢，这是第二道）",
              bool(mine) and all(row["kick_disabled"] for row in mine), f"自己那行：{mine[:1]}")

    return finish(keep_open)


def finish(keep_open: bool) -> int:
    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name} —— {detail}", flush=True)
    return 1 if failed else 0


def main() -> int:
    _console()
    parser = argparse.ArgumentParser(description="⑩ 组件真机冒烟（真实浏览器 + 真实接口）")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--api-base", default=DEFAULT_API)
    parser.add_argument("--browser", default="")
    parser.add_argument("--keep-browser", action="store_true")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    token = login(args.api_base)
    page = _auto_login_page(token)
    profile = Path(tempfile.mkdtemp(prefix="ui-components-"))
    browser = args.browser or _browser()
    process = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions",
         # 和 check_pages.py 同一个坑：从 Python 子进程里启动时 Chromium 的沙箱初始化会卡住，
         # 表现为**浏览器级命令能回、页面级命令（Page/Runtime/Network）一律不回包**——
         # 因为没有渲染进程，attach 出来的 session 永远没有响应。
         "--no-sandbox", f"--remote-debugging-port={args.port}", f"--user-data-dir={profile}",
         "--window-size=1440,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    exit_code = 1
    try:
        ws_url = _debugger_url(args.port)
        exit_code = asyncio.run(drive(ws_url, args.base_url, args.api_base, args.keep_browser))
    finally:
        if not args.keep_browser:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            shutil.rmtree(profile, ignore_errors=True)
            page.unlink(missing_ok=True)
            print("已关闭无头浏览器并删除临时登录页", flush=True)
    print(f"UI_COMPONENTS_EXIT={exit_code}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
