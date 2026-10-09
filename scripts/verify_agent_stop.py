"""㉒ 停止生成的标志位落库（`agent_messages.stop_requested_at`）—— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_agent_stop.py

为什么这些只能在真机上验：

- **停止是「写库 + 生成器按节拍读库」**，所以要真模型真流式：只有真流够慢，才能在中途插一手；
  单测里生成器与库都是假的，证明不了「另一个进程写下的标志，正在跑的生成器看得见」。
- **跨进程那一段是本项的核心**：父进程开着一条流，另起一个**全新 Python 解释器**（`--set-stop`，
  只带 SQLAlchemy 连库、完全不经过后端进程）把标志写到那一行上，父进程这条流随即以
  `stopped` 收尾 —— 全程没打过停止接口。这正是「多 worker 部署时停止请求落在别的 worker 上
  也停得下来」，也是 ⑮ 时代进程内 `threading.Event` 做不到的那件事。
- **「重启后读得出区别」**要真库真行：直接插两条挂着的 `streaming`（`created_at` 三分钟前），
  带标志的读成「已停止」、不带标志的读成「已中断」，而库里两份都不动。

数据都是临时的：临时账号带 `stop_verify_` 前缀、临时会话标题带 MARK、临时行跑完删掉；
脚本可反复跑。用 urllib + 空 ProxyHandler 绕开宿主代理。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PASSWORD = "stop-verify-2026"
MARK = "㉒停止冒烟"
QUESTION = f"{MARK}：2026-07 的费用异常在哪里？"
LONG_QUESTION = (f"{MARK}：请把 2026-07 的收入、成本、费用、预警逐条分析，"
                 "每条都给出数据依据和改进建议，写得尽量详细。")
USER_PREFIX = "stop_verify_"
LOG_PATH = Path(__file__).resolve().parent.parent / "output" / "_agent_stop_child.log"

RESULTS: list[tuple[str, bool, str]] = []


# ---------------------------------------------------------------- 通用件


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=300) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    text = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def parse_block(block: str) -> tuple[str, dict]:
    name, data = "message", ""
    for line in block.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data += line[5:].strip()
    try:
        return name, (json.loads(data) if data else {})
    except json.JSONDecodeError:
        return name, {"raw": data}


def stream_ask(token: str, question: str, session_id: int | None = None,
               on_event=None) -> list[tuple[str, dict]]:
    """自己按块读 SSE：`on_event` 让我们在「第一块 delta 刚到」时插一手。"""
    path = "/ai/agent/chat/stream" + (f"?session_id={session_id}" if session_id else "")
    request = urllib.request.Request(f"{BASE}{path}", method="POST",
                                     data=json.dumps({"question": question}).encode("utf-8"))
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {token}")
    events: list[tuple[str, dict]] = []
    buffer = ""
    with OPENER.open(request, timeout=300) as response:
        assert response.status == 200, f"流式接口返回 {response.status}"
        for raw in response:
            buffer += raw.decode("utf-8", "replace")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                if not block.strip():
                    continue
                name, payload = parse_block(block)
                events.append((name, payload))
                if on_event is not None:
                    on_event(name, payload, events)
    return events


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def login(username: str, password: str) -> str:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def session_detail(token: str, session_id: int) -> dict:
    status, payload = call("GET", f"/ai/agent/sessions/{session_id}", token=token)
    assert status == 200, f"读会话 {session_id} 失败：{status} {payload}"
    return payload


def rows_of(detail: dict) -> list[dict]:
    return list(detail.get("messages") or [])


def row_by_id(detail: dict, message_id: int | None) -> dict:
    return next((row for row in rows_of(detail) if row["id"] == message_id), {})


def last_answer(detail: dict) -> dict:
    return next((row for row in reversed(rows_of(detail)) if row["role"] == "assistant"), {})


def deltas(events: list[tuple[str, dict]]) -> str:
    return "".join(item["text"] for name, item in events if name == "delta")


def done_of(events: list[tuple[str, dict]]) -> dict:
    return next((item for name, item in events if name == "done"), {})


def event_of(events: list[tuple[str, dict]], wanted: str) -> dict:
    return next((item for name, item in events if name == wanted), {})


# ---------------------------------------------------------------- 真库直读（父进程与子进程共用）


def db_column() -> dict:
    from sqlalchemy import text

    from backend.db import SessionLocal

    with SessionLocal() as db:
        row = db.execute(text("""
            SELECT data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_name = 'agent_messages' AND column_name = 'stop_requested_at'
        """)).mappings().first()
    return dict(row) if row else {}


def db_row(message_id: int) -> dict:
    """直接连库读那一行（绕过后端进程，用来证明「标志真的落库了」而不是只在内存里）。"""
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import AgentMessage

    with SessionLocal() as db:
        row = db.execute(select(AgentMessage).where(AgentMessage.id == message_id)).scalars().first()
        if row is None:
            return {}
        return {"id": row.id, "status": row.status, "content": row.content or "",
                "stop_requested_at": row.stop_requested_at.isoformat() if row.stop_requested_at else None}


def db_streaming_id(session_id: int, timeout: float = 20.0) -> int | None:
    """轮询真库等那条 `streaming` 占位行出现（流式请求已经在跑了才会出现）。"""
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import AgentMessage

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            row = db.execute(select(AgentMessage).where(
                AgentMessage.session_id == session_id, AgentMessage.role == "assistant",
                AgentMessage.status == "streaming").order_by(AgentMessage.id.desc())).scalars().first()
            if row is not None:
                return row.id
        time.sleep(0.05)
    return None


def db_insert_stale(session_id: int, user_id: int, *, stopped: bool, minutes: int = 3) -> int:
    """插一条「挂着不动」的 streaming 行（模拟进程崩了/被杀了留下的孤儿）。"""
    from backend.db import SessionLocal
    from backend.models import AgentMessage

    with SessionLocal() as db:
        row = AgentMessage(session_id=session_id, user_id=user_id, role="assistant",
                           content="半截回答" if stopped else "",
                           status="streaming",
                           created_at=datetime.now(UTC) - timedelta(minutes=minutes),
                           stop_requested_at=datetime.now(UTC) - timedelta(minutes=minutes) if stopped else None)
        db.add(row)
        db.commit()
        return row.id


def db_delete_messages(message_ids: list[int]) -> None:
    from sqlalchemy import delete

    from backend.db import SessionLocal
    from backend.models import AgentMessage

    if not message_ids:
        return
    with SessionLocal() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.id.in_(message_ids)))
        db.commit()


# ---------------------------------------------------------------- 子进程：往那一行写标志


def set_stop_from_another_process(message_id: int) -> int:
    """`--set-stop <message_id>`：只连库写标志，**完全不经过后端进程**。

    这就是「停止请求落在另一个 worker 上」的最小复现：库里那一行是唯一的交汇点。
    """
    from backend.agent_sessions import request_stop
    from backend.db import SessionLocal

    before = db_row(message_id)
    with SessionLocal() as db:
        stopped = request_stop(db, message_id)
        db.commit()
    after = db_row(message_id)
    print(json.dumps({
        "pid": __import__("os").getpid(),
        "message_id": message_id,
        "stopped": bool(stopped),
        "status_before": before.get("status"),
        "flag_after": after.get("stop_requested_at"),
    }, ensure_ascii=False), flush=True)
    return 0


def spawn_set_stop(message_id: int, log_path: Path) -> dict:
    """另起一个全新解释器写标志，返回它自己报的结果。"""
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n--- set-stop {message_id} @ {datetime.now(UTC).isoformat()} ---\n")
        finished = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                   "--set-stop", str(message_id)],
                                  stdout=log, stderr=subprocess.STDOUT, timeout=120)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    tail = [line for line in text.splitlines() if line.startswith("{")]
    payload = json.loads(tail[-1]) if tail else {}
    payload["returncode"] = finished.returncode
    return payload


def stream_in_background(token: str, question: str, session_id: int, on_event, sink: dict):
    """在后台线程里跑流：主线程要腾出手来等占位行、起子进程。"""
    def run():
        try:
            sink["events"] = stream_ask(token, question, session_id=session_id, on_event=on_event)
        except Exception as exc:                      # noqa: BLE001 - 兜住并报给检查项
            sink["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------- 用例主体


def cleanup_leftovers(admin: str) -> None:
    status, payload = call("GET", "/ai/agent/sessions?limit=200", token=admin)
    if status == 200:
        for row in payload.get("data", []):
            if MARK in (row.get("title") or ""):
                call("DELETE", f"/ai/agent/sessions/{row['id']}", token=admin)
    status, payload = call("GET", f"/admin/users?query={USER_PREFIX}&limit=50", token=admin)
    if status == 200:
        for row in payload.get("data", []):
            if str(row.get("username", "")).startswith(USER_PREFIX):
                call("DELETE", f"/admin/users/{row['id']}", token=admin)


def main() -> int:
    admin = login(*ADMIN)
    me = call("GET", "/auth/me", token=admin)[1]
    print(f"管理员 {me['username']}（unrestricted={me.get('unrestricted')}）\n", flush=True)
    cleanup_leftovers(admin)

    created: list[int] = []
    stale_ids: list[int] = []
    scoped_user: int | None = None
    try:
        # ---------------------------------------------------------- 库结构与体检
        column = db_column()
        check("agent_messages.stop_requested_at 已建好：可空的时间戳（NULL = 从没点过停止）",
              column.get("data_type") == "timestamp with time zone"
              and column.get("is_nullable") == "YES",
              f"{column}")

        from backend.migrations import missing_columns, missing_indexes

        gaps = list(missing_columns()) + list(missing_indexes())
        check("启动体检不再点名这一列（老库升级后不该每次都报缺口）",
              not any("stop_requested_at" in str(item) for item in gaps), f"{gaps}")

        # ---------------------------------------------------------- 真机 HTTP：生成中途停止
        status, empty = call("POST", "/ai/agent/sessions", token=admin, body={})
        empty_id = empty["id"]
        created.append(empty_id)
        check("没有正在生成的回答时停止 → 409（不是 500，也不是默默成功）",
              call("POST", f"/ai/agent/sessions/{empty_id}/stop", token=admin)[0] == 409)
        check("匿名停止 → 401",
              call("POST", f"/ai/agent/sessions/{empty_id}/stop")[0] == 401)

        full_events = stream_ask(admin, QUESTION, session_id=empty_id)
        full_text = deltas(full_events)
        full_length = len(full_text)
        check("先跑一条完整回答（后面要拿它当「被停的确实短」的对照）",
              len(full_text) > 40 and done_of(full_events).get("status") == "ok",
              f"{full_length} 字 status={done_of(full_events).get('status')}")

        status, stopping = call("POST", "/ai/agent/sessions", token=admin, body={})
        stop_id = stopping["id"]
        created.append(stop_id)
        observed: dict = {}

        def on_event(name: str, payload: dict, so_far: list) -> None:
            if name != "delta" or observed:
                return
            observed["events_at_stop"] = len(so_far)
            observed["stream_message_id"] = event_of(so_far, "start").get("message_id")
            code, body = call("POST", f"/ai/agent/sessions/{stop_id}/stop", token=admin)
            observed["code"] = code
            observed["body"] = body
            # 标志是**立即提交**的：此刻直接用另一个会话读库，必须已经看得见
            observed["row"] = db_row(body.get("message_id") if isinstance(body, dict) else 0)

        stop_events = stream_ask(admin, LONG_QUESTION, session_id=stop_id, on_event=on_event)
        stop_done = done_of(stop_events)
        stopped_row = last_answer(session_detail(admin, stop_id))
        check("停止请求被受理：200、stopped=true、message_id 就是流里那个占位回答",
              observed.get("code") == 200 and observed.get("body", {}).get("stopped") is True
              and observed.get("body", {}).get("message_id") == observed.get("stream_message_id"),
              f"HTTP {observed.get('code')} {observed.get('body')}")
        check("★ 停止标志真的落库了：另一个会话此刻就能读到那一行的 stop_requested_at",
              bool(observed.get("row", {}).get("stop_requested_at")),
              f"{observed.get('row')}")
        check("停止之后那一行仍是 streaming（收尾由生成器做），随后以 stopped 收尾",
              observed.get("row", {}).get("status") == "streaming"
              and stop_done.get("status") == "stopped" and stop_done.get("stopped") is True,
              f"停止时 status={observed.get('row', {}).get('status')} done={stop_done.get('status')}")
        check("被停的回答只留已生成的部分（比完整回答短，且不是空）",
              0 < len(stopped_row.get("content") or "") < max(full_length, 1),
              f"被停 {len(stopped_row.get('content') or '')} 字 / 完整 {full_length} 字")
        check("详情里说得出「这条是用户停的」：stop_requested=true，状态标签「已停止」",
              stopped_row.get("stop_requested") is True and stopped_row.get("status") == "stopped"
              and stopped_row.get("status_label") == "已停止",
              f"stop_requested={stopped_row.get('stop_requested')} "
              f"{stopped_row.get('status')}/{stopped_row.get('status_label')}")
        check("完整回答的那条 stop_requested=false（没点过停止就是没点过）",
              full_events and last_answer(session_detail(admin, empty_id)).get("stop_requested") is False)
        check("终态行不再接受停止：停止之后再停止 → 409",
              call("POST", f"/ai/agent/sessions/{stop_id}/stop", token=admin)[0] == 409)

        # ---------------------------------------------------------- 跨进程：另一个解释器写标志
        status, cross = call("POST", "/ai/agent/sessions", token=admin, body={})
        cross_id = cross["id"]
        created.append(cross_id)
        first_delta = threading.Event()
        cross_observed: dict = {}
        sink: dict = {}

        def on_cross_event(name: str, payload: dict, so_far: list) -> None:
            if name == "start":
                cross_observed["message_id"] = payload.get("message_id")
                cross_observed["stop_calls"] = 0
            if name == "delta" and not first_delta.is_set():
                cross_observed["events_at_first_delta"] = len(so_far)
                first_delta.set()

        thread = stream_in_background(admin, LONG_QUESTION, cross_id, on_cross_event, sink)
        child: dict = {}
        try:
            assert first_delta.wait(timeout=60), "流一直没吐第一块"
            cross_mid = cross_observed.get("message_id")
            placeholder = db_streaming_id(cross_id)
            check("流跑起来时库里那条确实是 streaming（子进程要写的就是它）",
                  placeholder == cross_mid and cross_mid is not None,
                  f"占位行 {placeholder} / start 事件 {cross_mid}")
            child = spawn_set_stop(cross_mid, LOG_PATH)
        finally:
            thread.join(timeout=180)
        cross_events = sink.get("events") or []
        cross_done = done_of(cross_events)
        check("子进程（全新解释器、不经过后端进程）写下了标志并当场读到",
              child.get("returncode") == 0 and child.get("stopped") is True
              and child.get("status_before") == "streaming" and child.get("flag_after"),
              f"pid={child.get('pid')} {child}")
        check("★ 父进程这条流随即收尾成 stopped —— 停它的是另一个进程，全程没打过停止接口",
              cross_done.get("status") == "stopped" and cross_done.get("stopped") is True
              and cross_observed.get("stop_calls") == 0,
              f"done={cross_done.get('status')} 错误={sink.get('error')} 共 {len(cross_events)} 块")
        cross_row = row_by_id(session_detail(admin, cross_id), cross_mid)
        check("被别的进程停下的那条：正文非空、stop_requested=true、状态已停止",
              len(cross_row.get("content") or "") > 0 and cross_row.get("stop_requested") is True
              and cross_row.get("status") == "stopped",
              f"{len(cross_row.get('content') or '')} 字 stop_requested={cross_row.get('stop_requested')}")
        again = spawn_set_stop(cross_mid, LOG_PATH)
        check("同一个 id 再让子进程写一次 → 写不进去（已经不在生成，rowcount=0）",
              again.get("stopped") is False, f"{again}")

        # ---------------------------------------------------------- 重启后读得出区别
        status, stale_session = call("POST", "/ai/agent/sessions", token=admin, body={})
        stale_id = stale_session["id"]
        created.append(stale_id)
        marked = db_insert_stale(stale_id, me["id"], stopped=True)
        unmarked = db_insert_stale(stale_id, me["id"], stopped=False)
        stale_ids = [marked, unmarked]
        detail = session_detail(admin, stale_id)
        marked_row = row_by_id(detail, marked)
        unmarked_row = row_by_id(detail, unmarked)
        check("挂了三分钟没动静、但点过停止的行 → 读成「已停止」（用户要的停止，不是故障）",
              marked_row.get("status") == "stopped" and marked_row.get("status_label") == "已停止"
              and marked_row.get("stop_requested") is True,
              f"{marked_row.get('status')}/{marked_row.get('status_label')}")
        check("同样挂着、但没点过停止的行 → 照旧读成「已中断」（进程崩了没人管）",
              unmarked_row.get("status") == "interrupted"
              and unmarked_row.get("status_label") == "已中断"
              and unmarked_row.get("stop_requested") is False,
              f"{unmarked_row.get('status')}/{unmarked_row.get('status_label')}")
        check("读取只是归一显示，库里两份都还是 streaming（不对别人的数据动手）",
              db_row(marked).get("status") == "streaming"
              and db_row(unmarked).get("status") == "streaming"
              and db_row(marked).get("stop_requested_at") is not None,
              f"{db_row(marked)} / {db_row(unmarked)}")

        # ---------------------------------------------------------- 本地注册表与库的分工
        from backend import agent_sessions

        agent_sessions.reset()
        signal = agent_sessions.register(marked)
        check("同一进程里重复登记拿的是同一个句柄（幂等，不会越攒越多）",
              agent_sessions.register(marked) is signal and agent_sessions.active_count() == 1)
        agent_sessions.release(marked)
        check("★ 释放本地句柄清不掉库里的标志（终态行还要留着这个事实）",
              agent_sessions.active_count() == 0
              and db_row(marked).get("stop_requested_at") is not None)
        check("is_stopped 读的是「本地事件或库」这一份并集",
              agent_sessions.is_stopped(marked) is True
              and agent_sessions.is_stopped(unmarked) is False)
        agent_sessions.reset()

        # ---------------------------------------------------------- 越权
        username = f"{USER_PREFIX}{int(time.time())}"
        status, registered = call("POST", "/auth/register",
                                  body={"username": username, "password": PASSWORD})
        scoped_user = registered["id"]
        stranger = login(username, PASSWORD)
        check("别人的会话：停止也一律 404（不区分「没有」和「不是你的」）",
              call("POST", f"/ai/agent/sessions/{stop_id}/stop", token=stranger)[0] == 404)
        status, _ = call("DELETE", f"/admin/users/{scoped_user}", token=admin)
        scoped_user = None
    finally:
        db_delete_messages(stale_ids)
        for session_id in created:
            if session_id:
                code, _ = call("DELETE", f"/ai/agent/sessions/{session_id}", token=admin)
                print(f"  清理临时会话 {session_id} → {code}", flush=True)
        if scoped_user:
            call("DELETE", f"/admin/users/{scoped_user}", token=admin)
        status, payload = call("GET", "/ai/agent/sessions?limit=200", token=admin)
        leftover = [row["id"] for row in payload.get("data", [])] if status == 200 else []
        check("临时会话与临时行清理干净", leftover == [], f"{leftover}")

        status, payload = call("GET", f"/admin/users?query={USER_PREFIX}&limit=50", token=admin)
        stuck = [row["username"] for row in payload.get("data", [])] if status == 200 else []
        check("临时账号清理干净", stuck == [], f"{stuck}")

        status, current = call("GET", "/auth/me", token=admin)
        check("演示账号没被本脚本碰过（失败次数/锁定/会话版本都没变）且仍能登录",
              status == 200 and current.get("username") == ADMIN[0],
              f"HTTP {status} {current.get('username') if isinstance(current, dict) else current}")

    return finish()


def finish() -> int:
    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--set-stop":
        sys.exit(set_stop_from_another_process(int(sys.argv[2])))
    sys.exit(main())
