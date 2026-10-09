"""⑰ 多轮上下文（会话历史按预算进模型）—— 真机冒烟。

跑法：先起后端（127.0.0.1:8000，且配好 DEEPSEEK_API_KEY），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_agent_memory.py

为什么这些只能在真后端 + 真模型上验：

- **「模型看得见上一轮」这件事只有真模型能证。** 单测把 LLM 换成了假的，只能证明
  「我们把历史放进了 messages」；这里在第一轮埋一个随机口令、第二轮只问口令，
  回答里出现口令才算真送到上游了（假模型会照抄，真模型不会）。
- **预算是否真在拦**：连问 8 轮，第 7 轮的历史对数正好用满 `AGENT_HISTORY_MAX_TURNS` 默认值 6，
  第 8 轮再多一对就超预算——此时对数仍停在 6、`history_truncated` 翻成 true。这是服务端算出来的，
  不是脚本猜的。
- **「被停止的回答照样进上下文」**：停止是服务端标志位，只有真进程能造出
  `status=stopped` 且正文非空的行，然后看它有没有被算进下一轮。

数据都是临时的：用一个临时账号（`agent_memory_*`），跑完删会话、删账号。
用 urllib + 空 ProxyHandler 绕开宿主代理。真模型调用约 10 次，都是短问答。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PASSWORD = "agent-memory-2026"
PREFIX = "agent_memory_"
MARK = "⑰回忆冒烟"
# 与 backend/config.py 的默认值对齐（这里只用来断言"没被超发"，不是脚本自己算的值）
TURNS_WITH_HISTORY = 6
TOKEN_BUDGET = 1500

NONCE = f"Z{int(time.time()) % 100000}-{int(time.perf_counter() * 1000) % 997}"

FIRST_QUESTION = (f"{MARK}：这份对账单的核对口令是 {NONCE}，请记住它。"
                  "另外用一句话回答：2026-07 的费用有没有异常？")
RECALL_QUESTION = "刚才那份对账单的核对口令是什么？只回答口令本身。"
LONG_QUESTION = (f"{MARK}：请把 2026-07 的收入、成本、费用、预警逐条分析，"
                 "每条都给出数据依据和改进建议，写得尽量详细。")
FILLERS = [
    "用一个词回答：2026-07 有没有预警？",
    "用一个词回答：2026-07 的收入比成本高还是低？",
    "用一个词回答：差旅费属于收入、成本还是费用？",
    "用一个词回答：2026-07 的净额是正还是负？",
    "用一个词回答：2026-07 的费用率算不算高？",
    "用一个词回答：2026-07 的收入是正数吗？",
]

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


def deltas(events: list[tuple[str, dict]]) -> str:
    return "".join(item["text"] for name, item in events if name == "delta")


def turn(token: str, question: str, session_id: int | None = None,
         *, stop_after_first_delta: bool = False) -> dict:
    """问一轮并收齐三样东西：start/done 事件、正文、以及（可选）停止请求的结果码。"""
    observed: dict = {}

    def on_event(name: str, payload: dict, so_far: list) -> None:
        if not stop_after_first_delta or name != "delta" or observed:
            return
        status, body = call("POST", f"/ai/agent/sessions/{session_id}/stop", token=token)
        observed["stop_code"] = status
        observed["stop_body"] = body
        observed["at"] = len(so_far)

    events = stream_ask(token, question, session_id=session_id, on_event=on_event)
    start = next((item for name, item in events if name == "start"), {})
    done = next((item for name, item in events if name == "done"), {})
    return {"start": start, "done": done, "events": events, "text": deltas(events),
            "session_id": start.get("session_id") or session_id, **observed}


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def history_of(result: dict) -> tuple[int, int, int, bool]:
    done = result["done"]
    return (done.get("history_turns", -1), done.get("history_messages", -1),
            done.get("history_tokens", -1), done.get("history_truncated"))


def login(username: str, password: str) -> str:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def register_temp(username: str) -> tuple[str, int]:
    """不带部门 = 不受限账号：否则检索不到证据，模型根本不会被调用。"""
    status, payload = call("POST", "/auth/register", body={"username": username, "password": PASSWORD})
    assert status == 200, f"注册 {username} 失败：{status} {payload}"
    token = login(username, PASSWORD)
    me = call("GET", "/auth/me", token=token)[1]
    return token, me["id"]


def delete_user(admin: str, user_id: int | None) -> None:
    if not user_id:
        return
    status, _ = call("DELETE", f"/admin/users/{user_id}", token=admin)
    print(f"  清理临时账号 {user_id} → {status}", flush=True)


def cleanup_leftovers(admin: str) -> None:
    """上一次跑挂掉留下的临时账号连会话一起清掉（同一账号的会话随账号删除而消失）。"""
    status, payload = call("GET", f"/admin/users?query={PREFIX}&limit=50", token=admin)
    if status == 200:
        for row in payload.get("data", []):
            if str(row.get("username", "")).startswith(PREFIX):
                delete_user(admin, row["id"])


def session_detail(token: str, session_id: int) -> dict:
    status, payload = call("GET", f"/ai/agent/sessions/{session_id}", token=token)
    assert status == 200, f"读会话 {session_id} 失败：{status} {payload}"
    return payload


def main() -> int:
    admin = login(*ADMIN)
    cleanup_leftovers(admin)
    username = f"{PREFIX}{int(time.time())}"
    token, user_id = register_temp(username)
    print(f"临时账号 {username}（id={user_id}）\n", flush=True)

    created: list[int] = []
    try:
        # ---------------------------------------------------------- 第一轮：没有历史可带
        first = turn(token, FIRST_QUESTION)
        session_id = first["session_id"]
        created.append(session_id)
        check("第一轮就建了会话，两个事件都带上会话 id",
              bool(session_id) and first["start"].get("session_id") == session_id,
              f"session_id={session_id}")
        check("第一轮的历史是空的：0 轮 / 0 条 / 0 tokens / 没截断",
              history_of(first) == (0, 0, 0, False), f"{history_of(first)}")
        check("第一轮真的调了模型（正文非空、token > 0）",
              bool(first["text"].strip()) and first["done"].get("tokens", {}).get("total_tokens", 0) > 0,
              f"正文 {len(first['text'])} 字 tokens={first['done'].get('tokens')}")

        # ---------------------------------------------------------- 第二轮：口令回忆
        recall = turn(token, RECALL_QUESTION, session_id)
        recall_turns, recall_messages, recall_tokens, recall_truncated = history_of(recall)
        check("第二轮带上了上一轮问答：1 轮 / 2 条 / tokens > 0、没截断",
              (recall_turns, recall_messages, recall_truncated) == (1, 2, False) and recall_tokens > 0,
              f"turns={recall_turns} messages={recall_messages} tokens={recall_tokens}")
        check("★ 模型真的看见了上一轮：第二轮回答里有第一轮埋的口令",
              NONCE in recall["text"], f"口令 {NONCE} 能不能在回答里找到（回答见下）\n"
              f"      {recall['text'][:200]!r}")

        # ---------------------------------------------------------- 连问到底：预算封顶
        history_counts = [history_of(first)[0], history_of(recall)[0]]
        over_budget: list[str] = []
        capped: list[tuple[int, bool]] = []
        for index, question in enumerate(FILLERS, start=3):
            result = turn(token, question, session_id)
            turns, messages, tokens, truncated = history_of(result)
            history_counts.append(turns)
            if turns > TURNS_WITH_HISTORY or messages != turns * 2 or tokens > TOKEN_BUDGET:
                over_budget.append(f"第{index}轮 turns={turns} messages={messages} tokens={tokens}")
            if turns == TURNS_WITH_HISTORY:
                capped.append((index, truncated))
        check("第 7 轮刚攒满 6 对：预算正好放得下，truncated 仍是 false（没有更早的被丢）",
              capped and capped[0] == (len(FILLERS) + 1, False),
              f"逐轮 (轮次, 是否截断) = {capped}")
        check("第 8 轮再多一对就超预算：历史停在 6 轮、truncated 翻成 true",
              capped and capped[-1] == (len(FILLERS) + 2, True),
              f"逐轮 (轮次, 是否截断) = {capped}")
        check("每一轮都没超预算：对数 ≤ 6、条数 = 对数 × 2、估算 tokens ≤ 1500",
              not over_budget, f"{over_budget or '全部在预算内'}；逐轮 {history_counts}")
        check("历史轮数随对话单调不减（第 1 轮 0 → 第 2 轮 1 → … 到第 8 轮封顶 6）",
              history_counts == sorted(history_counts) and history_counts[0] == 0
              and history_counts[-1] == TURNS_WITH_HISTORY,
              f"{history_counts}")

        detail = session_detail(token, session_id)
        answers = [row for row in detail["messages"] if row["role"] == "assistant"]
        check("8 轮问答全部落库（16 条消息 / 8 个回答）",
              len(detail["messages"]) == 16 and len(answers) == 8,
              f"{len(detail['messages'])} 条 / {len(answers)} 个回答")
        check("落库的回答都是终态 ok（没有卡在 streaming 的）",
              all(row["status"] == "ok" for row in answers),
              f"{sorted({row['status'] for row in answers})}")

        # ---------------------------------------------------------- 被停止的回答也算历史
        status, fresh = call("POST", "/ai/agent/sessions", token=token, body={})
        stop_id = fresh["id"] if isinstance(fresh, dict) else None
        created.append(stop_id)
        stopped = turn(token, LONG_QUESTION, stop_id, stop_after_first_delta=True)
        stopped_row = next((row for row in session_detail(token, stop_id)["messages"]
                            if row["role"] == "assistant"), {})
        check("停止请求在第一块 delta 之后就被受理（200）",
              stopped.get("stop_code") == 200 and stopped["done"].get("status") == "stopped",
              f"HTTP {stopped.get('stop_code')} status={stopped['done'].get('status')}")
        check("被停的回答留下了正文（虽然是半截）",
              bool((stopped_row.get("content") or "").strip()),
              f"{len(stopped_row.get('content') or '')} 字")
        after_stop = turn(token, "用一个词回答：刚才这条回答被停在哪一步？", stop_id)
        check("被停止的回答照样进下一轮的上下文（它是半截，但写完了）",
              history_of(after_stop)[0] == 1 and history_of(after_stop)[1] == 2,
              f"{history_of(after_stop)}")
    finally:
        for session_id in created:
            if session_id:
                status, _ = call("DELETE", f"/ai/agent/sessions/{session_id}", token=token)
                print(f"  清理临时会话 {session_id} → {status}", flush=True)
        status, payload = call("GET", "/ai/agent/sessions?limit=200", token=token)
        leftover = [row["id"] for row in payload.get("data", [])] if status == 200 else []
        check("临时会话清理干净", leftover == [], f"{leftover}")
        delete_user(admin, user_id)

    return finish()


def finish() -> int:
    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
