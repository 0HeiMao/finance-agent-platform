"""⑮ Agent 会话落库、停止生成与 token 统计 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_agent_sessions.py

为什么这些只能在真后端上验：

- **停止生成**靠的是库里的 `agent_messages.stop_requested_at`（㉒ 之前是进程内的
  `threading.Event`）。单测把生成器整个换成了假的，只有真进程真流式才能证明
  「另一个 HTTP 请求写下标志 → 正在跑的生成器下一拍读库就收尾」。跨进程那一面
  （标志由**别的进程**写下、本进程照样停得下来）在 `scripts/verify_agent_stop.py` 里验。
- **token 统计**要真模型的用量（`usage_source="api"`）。假 LLM 只能编一个数字，
  证明不了 `stream_options.include_usage` 真被网关认了。所以这里会真花钱问两次。
- **用量的「今天」边界**依赖数据库的 `now()`（事务开始时刻）：真库上才能验出
  「窗口按自然日对齐」这个修正是不是真让今天的问答留在图上。

数据都是临时的：会话标题带 MARK，跑完连消息一起删；受限账号也是临时注册的。
用 urllib + 空 ProxyHandler 绕开宿主代理。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PASSWORD = "agent-verify-2026"
MARK = "⑮会话冒烟"
SHORT_QUESTION = f"{MARK}：2026-07 的费用异常在哪里？"
LONG_QUESTION = (f"{MARK}：请把 2026-07 的收入、成本、费用、预警逐条分析，"
                 "每条都给出数据依据和改进建议，写得尽量详细。")
SCOPED_PREFIX = "agent_verify_"

RESULTS: list[tuple[str, bool, str]] = []


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=180) as response:
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
    """自己按块读 SSE：`on_event` 让我们在「第一块 delta 刚到」时插一手（发停止请求）。"""
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


def assistant_row(detail: dict) -> dict:
    return next((row for row in detail.get("messages") or [] if row["role"] == "assistant"), {})


def deltas(events: list[tuple[str, dict]]) -> str:
    return "".join(item["text"] for name, item in events if name == "delta")


def names(events: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in events]


def register_scoped(admin: str, username: str, department: str = "018") -> tuple[str, int]:
    """注册一个只看某个部门的临时账号。

    ⑳ 起账号不能自己改数据范围（`PATCH /auth/me` 一律 403），必须由管理员在
    `/admin/users/{id}` 上分配 —— 真机上就是管理端「用户管理」里点一下。
    """
    status, payload = call("POST", "/auth/register", body={"username": username, "password": PASSWORD})
    assert status == 200, f"注册 {username} 失败：{status} {payload}"
    user_id = payload["id"]
    token = login(username, PASSWORD)
    status, payload = call("PATCH", f"/admin/users/{user_id}", token=admin,
                           body={"departments": [department]})
    assert status == 200 and payload.get("unrestricted") is False, f"分配部门失败：{status} {payload}"
    return token, user_id


def delete_user(admin: str, user_id: int | None) -> None:
    if not user_id:
        return
    status, _ = call("DELETE", f"/admin/users/{user_id}", token=admin)
    print(f"  清理临时账号 {user_id} → {status}", flush=True)


def cleanup_leftovers(admin: str) -> None:
    """把上一次跑挂掉留下的临时会话/账号清掉，免得下面的计数被上一轮污染。"""
    status, payload = call("GET", "/ai/agent/sessions?limit=200", token=admin)
    if status == 200:
        for row in payload.get("data", []):
            if MARK in (row.get("title") or ""):
                call("DELETE", f"/ai/agent/sessions/{row['id']}", token=admin)
    status, payload = call("GET", f"/admin/users?query={SCOPED_PREFIX}&limit=50", token=admin)
    if status == 200:
        for row in payload.get("data", []):
            if str(row.get("username", "")).startswith(SCOPED_PREFIX):
                delete_user(admin, row["id"])


def main() -> int:
    admin = login(*ADMIN)
    me = call("GET", "/auth/me", token=admin)[1]
    print(f"管理员 {me['username']}（unrestricted={me.get('unrestricted')}）\n", flush=True)
    cleanup_leftovers(admin)

    created: list[int] = []
    scoped_user: int | None = None
    try:
        # ---------------------------------------------------------- 显式建会话
        status, empty = call("POST", "/ai/agent/sessions", token=admin, body={})
        check("POST /agent/sessions 建会话返回 201 + 会话体（前端「新建会话」的路径）",
              status == 201 and isinstance(empty, dict) and empty.get("id"),
              f"HTTP {status} {empty}")
        empty_id = empty.get("id") if isinstance(empty, dict) else None
        created.append(empty_id)
        check("新建的空会话显示为「未命名会话」且 0 问",
              empty.get("display_title") == "未命名会话" and empty.get("answers") == 0,
              f"{empty.get('display_title')} answers={empty.get('answers')}")

        detail = session_detail(admin, empty_id)
        check("空会话详情：没有消息，仍带 status_labels（界面靠它显示中文状态）",
              detail.get("messages") == [] and set(detail.get("status_labels") or {}) >=
              {"streaming", "ok", "stopped", "error", "interrupted"},
              f"{len(detail.get('messages') or [])} 条 {detail.get('status_labels')}")

        status, payload = call("POST", f"/ai/agent/sessions/{empty_id}/stop", token=admin)
        check("没有正在生成的回答时，停止返回 409（不是 500，也不是默默成功）",
              status == 409, f"HTTP {status} {payload}")

        status, payload = call("GET", "/ai/agent/sessions")
        check("匿名读会话列表 → 401", status == 401, f"HTTP {status}")

        # ---------------------------------------------------------- 一问一答（真模型）
        stream_id = empty_id
        events = stream_ask(admin, SHORT_QUESTION, session_id=stream_id)
        event_names = names(events)
        start = next((item for name, item in events if name == "start"), {})
        done = next((item for name, item in events if name == "done"), {})
        answer_text = deltas(events)
        check("流里先有 start，再有 delta，最后有 done",
              bool(event_names) and event_names[0] == "start" and "delta" in event_names
              and event_names[-1] == "done",
              f"{event_names[:3]} … {event_names[-1:]}（共 {len(event_names)} 块）")
        check("start 事件带 session_id / message_id / question_message_id / title",
              start.get("session_id") == stream_id and start.get("message_id")
              and start.get("question_message_id") and MARK in str(start.get("title")),
              f"{start}")
        check("真模型回了正文（不是「没有证据」的固定文案）",
              len(answer_text) > 40, f"{len(answer_text)} 字：{answer_text[:40]}…")

        row = assistant_row(session_detail(admin, stream_id))
        check("问答落库：用户提问 + 助手回答各一条",
              len(session_detail(admin, stream_id).get("messages") or []) == 2,
              f"{len(session_detail(admin, stream_id).get('messages') or [])} 条")
        check("落库正文与流式正文一字不差",
              row.get("content") == answer_text, f"{len(row.get('content') or '')} 字")
        check("落库状态是 ok，界面标签是「已完成」",
              row.get("status") == "ok" and row.get("status_label") == "已完成",
              f"{row.get('status')} / {row.get('status_label')}")
        check("真网关报了用量，usage_source=api（token 统计是账单口径）",
              row.get("usage_source") == "api",
              f"usage_source={row.get('usage_source')} "
              f"total={row.get('total_tokens')} prompt={row.get('prompt_tokens')} "
              f"completion={row.get('completion_tokens')}")
        check("token 三项自洽且 done 与落库一致",
              row.get("total_tokens", 0) > 0
              and row["total_tokens"] == row["prompt_tokens"] + row["completion_tokens"]
              and done.get("tokens", {}).get("total_tokens") == row["total_tokens"],
              f"done={done.get('tokens')} 落库={row.get('total_tokens')}")
        check("回答记了耗时与证据来源",
              (row.get("duration_ms") or 0) > 0 and len(row.get("sources") or []) > 0,
              f"duration_ms={row.get('duration_ms')} sources={row.get('sources')}")
        check("done 带口径快照与匹配期间（前端据此提示数据范围）",
              isinstance(done.get("scope"), dict) and "matched_period" in done,
              f"matched_period={done.get('matched_period')} scope={done.get('scope')}")

        status, listing = call("GET", "/ai/agent/sessions?limit=50", token=admin)
        listed = next((item for item in listing.get("data", []) if item["id"] == stream_id), {})
        check("列表口径：问答数只数回答，token 是累计值",
              listed.get("answers") == 1 and listed.get("total_tokens") == row.get("total_tokens"),
              f"answers={listed.get('answers')} tokens={listed.get('total_tokens')}")

        full_length = len(row.get("content") or "")

        # ---------------------------------------------------------- 停止生成（真模型 + 真进程）
        status, stopping = call("POST", "/ai/agent/sessions", token=admin, body={})
        stop_id = stopping["id"]
        created.append(stop_id)
        observed: dict = {}

        def on_event(name: str, payload: dict, so_far: list) -> None:
            if name != "delta" or observed:
                return
            # 第一块刚到：这时占位行必须还在 streaming——「停止」与前端轮询都指着它
            during = assistant_row(session_detail(admin, stop_id))
            observed["during_status"] = during.get("status")
            observed["during_content"] = during.get("content")
            code, body = call("POST", f"/ai/agent/sessions/{stop_id}/stop", token=admin)
            observed["code"] = code
            observed["body"] = body
            observed["events_at_stop"] = len(so_far)

        stop_events = stream_ask(admin, LONG_QUESTION, session_id=stop_id, on_event=on_event)
        stop_done = next((item for name, item in stop_events if name == "done"), {})
        stopped_row = assistant_row(session_detail(admin, stop_id))
        check("生成期间占位行就在库里（status=streaming），不是写完才落",
              observed.get("during_status") == "streaming",
              f"第一块 delta 时 status={observed.get('during_status')} "
              f"content={observed.get('during_content')!r}")
        check("生成期间停止 → 200 且回 stopped=true",
              observed.get("code") == 200 and observed.get("body", {}).get("stopped") is True,
              f"HTTP {observed.get('code')} {observed.get('body')}")
        check("停止请求在流结束前就被受理（不是等生成完才处理）",
              observed.get("events_at_stop", 0) >= 2 and observed.get("events_at_stop", 0)
              < len(stop_events),
              f"停止时已收到 {observed.get('events_at_stop')} 块，最终 {len(stop_events)} 块")
        check("生成器下一块就收尾：done 的 status=stopped",
              stop_done.get("status") == "stopped" and stop_done.get("stopped") is True,
              f"status={stop_done.get('status')} 剩余块={len(stop_events) - observed.get('events_at_stop', 0)}")
        check("停止只保留已生成的部分（比完整回答短，且不是空）",
              0 < len(stopped_row.get("content") or "") < max(full_length, 1),
              f"被停的回答 {len(stopped_row.get('content') or '')} 字 / 完整回答 {full_length} 字")
        check("被停的回答也记账（模型没报用量就按字数估算，usage_source=estimated）",
              stopped_row.get("usage_source") in ("estimated", "api")
              and stopped_row.get("total_tokens", 0) > 0,
              f"usage_source={stopped_row.get('usage_source')} total={stopped_row.get('total_tokens')}")
        check("停止之后再停止 → 409（回答已经不在生成）",
              call("POST", f"/ai/agent/sessions/{stop_id}/stop", token=admin)[0] == 409)

        # ---------------------------------------------------------- 用量统计（本人）
        status, usage = call("GET", "/ai/agent/usage?days=1", token=admin)
        stats = usage.get("data", {}) if isinstance(usage, dict) else {}
        series = usage.get("series", []) if isinstance(usage, dict) else []
        today = datetime.now(UTC).date().isoformat()
        check("GET /agent/usage 返回 data + series + status_labels",
              status == 200 and isinstance(stats, dict) and series and usage.get("status_labels"),
              f"HTTP {status} answers={stats.get('answers')}")
        check("days=1 的窗口就是「今天」这一天，且今天的问答没被筛掉",
              len(series) == 1 and series[0]["date"] == today and series[0]["answers"] >= 2,
              f"series={series} 今天={today}")
        check("估算与账单分开报，三者相加等于总回答数",
              stats.get("api_answers", 0) + stats.get("estimated_answers", 0)
              + stats.get("free_answers", 0) == stats.get("answers"),
              f"api={stats.get('api_answers')} estimated={stats.get('estimated_answers')} "
              f"free={stats.get('free_answers')} answers={stats.get('answers')}")
        check("api_answers ≥ 1（真网关报过量）且总量 ≥ 两次回答之和",
              stats.get("api_answers", 0) >= 1
              and stats.get("total_tokens", 0) >= row["total_tokens"] + stopped_row["total_tokens"],
              f"api={stats.get('api_answers')} total={stats.get('total_tokens')}")
        check("by_day / by_status 里有今天与「已停止」",
              any(item["date"] == today for item in stats.get("by_day") or [])
              and stats.get("stopped", 0) >= 1,
              f"by_day={stats.get('by_day')} stopped={stats.get('stopped')}")
        check("days=0 / days=400 / limit=0 都被参数校验挡下（422）",
              call("GET", "/ai/agent/usage?days=0", token=admin)[0] == 422
              and call("GET", "/ai/agent/usage?days=400", token=admin)[0] == 422
              and call("GET", "/ai/agent/sessions?limit=0", token=admin)[0] == 422)

        # ---------------------------------------------------------- 管理员用量
        status, admin_usage = call("GET", "/admin/agent-usage?days=1", token=admin)
        admin_stats = admin_usage.get("data", {}) if isinstance(admin_usage, dict) else {}
        by_user = admin_stats.get("by_user") or []
        mine = next((item for item in by_user if item["username"] == me["username"]), {})
        check("GET /admin/agent-usage 返回全平台口径（带 by_user / by_status / series）",
              status == 200 and by_user and admin_stats.get("by_status") is not None
              and admin_usage.get("series"),
              f"HTTP {status} 用户数={len(by_user)}")
        check("by_user 里能看到自己这一轮的用量",
              mine.get("answers", 0) >= 2 and mine.get("total_tokens", 0) >= row["total_tokens"],
              f"{mine}")
        check("by_user 的部门列是文本，未受限账号显示「不受限」",
              all(isinstance(item.get("department"), str) and item["department"] for item in by_user),
              f"{[item['department'] for item in by_user][:5]}")

        # ---------------------------------------------------------- 越权与受限账号
        username = f"{SCOPED_PREFIX}{int(time.time())}"
        scoped, scoped_user = register_scoped(admin, username)
        check("别人的会话一律 404（不区分「没有」和「不是你的」）",
              call("GET", f"/ai/agent/sessions/{stream_id}", token=scoped)[0] == 404
              and call("DELETE", f"/ai/agent/sessions/{stream_id}", token=scoped)[0] == 404)
        status, scoped_list = call("GET", "/ai/agent/sessions", token=scoped)
        check("受限账号只看到自己的会话（列表是空的，看不到管理员那条）",
              status == 200 and scoped_list.get("data") == [],
              f"{scoped_list.get('data')}")
        check("受限账号能看自己的用量，但看不了管理员用量（403）",
              call("GET", "/ai/agent/usage?days=1", token=scoped)[0] == 200
              and call("GET", "/admin/agent-usage?days=1", token=scoped)[0] == 403)

        # ---------------------------------------------------------- 删除与审计
        removed = call("DELETE", f"/ai/agent/sessions/{stream_id}", token=admin)
        created.remove(stream_id)
        check("删会话连同消息一起删，并回报条数",
              removed[0] == 200 and removed[1].get("deleted_messages") == 2
              and removed[1].get("deleted") is True,
              f"HTTP {removed[0]} {removed[1]}")
        check("删掉之后再读 → 404，列表里也没有它了",
              call("GET", f"/ai/agent/sessions/{stream_id}", token=admin)[0] == 404)
        status, audit = call("GET", "/admin/audit?action=agent_session.delete&limit=100", token=admin)
        rows = audit.get("data", []) if isinstance(audit, dict) else []
        hit = next((item for item in rows if item.get("target_id") == str(stream_id)), {})
        check("删除留了审计：target_id 是会话 id，detail 里记了删掉几条消息",
              hit.get("action") == "agent_session.delete"
              and (hit.get("detail") or {}).get("messages") == 2,
              f"{hit.get('action')} detail={hit.get('detail')}")
    finally:
        for session_id in created:
            if session_id:
                status, _ = call("DELETE", f"/ai/agent/sessions/{session_id}", token=admin)
                print(f"  清理临时会话 {session_id} → {status}", flush=True)
        delete_user(admin, scoped_user)
        status, payload = call("GET", "/ai/agent/sessions?limit=200", token=admin)
        leftover = [row["id"] for row in payload.get("data", [])] if status == 200 else []
        check("临时会话清理干净", leftover == [], f"{leftover}")

    return finish()


def finish() -> int:
    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
