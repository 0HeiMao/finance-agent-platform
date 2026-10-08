"""⑭ 预警外发通道 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_alerts_outbound.py

验的是「预警产生 → 建投递单 → 后台任务真发 HTTP → 失败按退避自动重试 → 人工重试」这条链
在**真后端进程**里跑得通：后台任务用的是服务器进程里的真定时器，不是单测里替换掉的假实现，
所以「退避之后真的又发了一次」只有这里能证。

脚本自己在本机起一个 HTTP sink 当告警网关，检查 webhook 的签名头、事件名与正文快照，
并支持「先失败几次」来观察退避重试。数据走临时批次导入（2099-09），跑完撤销批次、删掉临时通道。
用 urllib + 空 ProxyHandler 绕开宿主代理。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PERIOD = "2099-09"
SECRET_A = "outbound-verify-9f3c"
SECRET_B = "outbound-verify-b7d1"
MARK = "⑭外发冒烟"

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


def call_multipart(path: str, token: str, filename: str, content: bytes):
    boundary = "----finance" + secrets.token_hex(12)
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
              f"Content-Type: text/csv\r\n\r\n").encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(f"{BASE}{path}", method="POST", data=prefix + content + suffix)
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
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


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def login(username: str, password: str) -> str:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def channel_body(name: str, **overrides) -> dict:
    body = {"name": name, "kind": "webhook", "target": SINK.url, "min_level": "low",
            "department": "", "enabled": True, "secret": ""}
    body.update(overrides)
    return body


def deliveries(token: str, channel_id: int, period: str | None = None) -> list[dict]:
    query = f"/admin/alert-deliveries?channel_id={channel_id}&limit=200"
    if period:
        query += f"&period={period}"
    status, payload = call("GET", query, token=token)
    assert status == 200, f"读投递台账失败：{status} {payload}"
    return payload["data"]


def settled(token: str, channel_id: int, period: str, timeout: float = 30.0) -> list[dict]:
    """等这一期的投递单全部离开「待发送」（后台任务的定时器是异步的，只能轮询）。"""
    deadline = time.time() + timeout
    rows: list[dict] = []
    while time.time() < deadline:
        rows = deliveries(token, channel_id, period=period)
        if rows and all(row["status"] in ("sent", "failed") for row in rows):
            return rows
        time.sleep(0.5)
    return rows


def signature_of(secret: str, timestamp: str, raw: str) -> str:
    body = f"{timestamp}.".encode() + raw.encode()
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Sink:
    """本机告警网关：记下每条 POST，支持「失败 N 次」来观察退避重试。"""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.mode = "ok"
        self.failures_left = 0
        self.lock = threading.Lock()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的固定接口名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                with sink.lock:
                    sink.received.append({"path": self.path,
                                          "headers": {key.lower(): value for key, value in self.headers.items()},
                                          "raw": body.decode("utf-8", "replace")})
                    failing = sink.mode == "fail" or (sink.mode == "fail_then_ok" and sink.failures_left > 0)
                    if failing and sink.mode == "fail_then_ok":
                        sink.failures_left -= 1
                if failing:
                    status, payload = 500, b'{"error":"sink busy"}'
                else:
                    status, payload = 200, b'{"ok":true}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # noqa: D102 - 静音，脚本自己打印结论
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/alerts"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> Sink:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def reset(self, mode: str = "ok", failures: int = 0) -> None:
        with self.lock:
            self.received.clear()
            self.mode = mode
            self.failures_left = failures

    def posts(self, event: str | None = None) -> list[dict]:
        with self.lock:
            rows = list(self.received)
        if event:
            rows = [row for row in rows if row["headers"].get("x-finance-event") == event]
        return rows


SINK = Sink()


def main() -> int:
    admin = login(*ADMIN)
    SINK.start()
    channel_a = channel_b = None
    batch = None
    try:
        status, catalog = call("GET", "/admin/alert-channels", token=admin)
        check("GET /admin/alert-channels 200 且带选项与统计",
              status == 200 and set(catalog.get("kinds", {})) == {"webhook", "email"}
              and "low" in catalog.get("min_levels", {}) and "high" in catalog.get("levels", {})
              and set(catalog.get("stats", {})) == {"pending", "sent", "failed"},
              f"{status} kinds={list(catalog.get('kinds', {}))} stats={catalog.get('stats')}")

        status, created = call("POST", "/admin/alert-channels", token=admin,
                               body=channel_body(f"{MARK} A", secret=SECRET_A))
        check("POST 新建 webhook 通道 201（127.0.0.1 这种内网地址允许）",
              status == 201 and created.get("has_secret") is True and created.get("target") == SINK.url,
              f"{status} {str(created)[:110]}")
        channel_a = created.get("id")
        check("通道 secret 只写不读",
              "secret" not in created and SECRET_A not in json.dumps(created, ensure_ascii=False),
              f"has_secret={created.get('has_secret')}")

        status, listing = call("GET", "/admin/alert-channels", token=admin)
        mine = [row for row in listing["data"] if row["id"] == channel_a]
        check("列表里也不回显 secret（连字段都没有）",
              status == 200 and len(mine) == 1 and "secret" not in mine[0], f"{str(mine)[:110]}")

        status, payload = call("POST", "/admin/alert-channels", token=admin,
                               body=channel_body(f"{MARK} A"))
        check("同重名通道 → 409", status == 409, f"{status} {str(payload)[:80]}")

        for label, patch in (("ftp 协议", {"target": "ftp://127.0.0.1/hook"}),
                             ("URL 带用户名口令", {"target": "http://robot:pw@127.0.0.1:9/hook"}),
                             ("非法级别", {"min_level": "urgent"}),
                             ("非法类型", {"kind": "sms"})):
            status, payload = call("POST", "/admin/alert-channels", token=admin,
                                   body=channel_body(f"{MARK} 坏-{label}", **patch))
            check(f"坏配置（{label}）→ 422", status == 422, f"{status} {str(payload)[:70]}")

        status, mail = call("POST", "/admin/alert-channels", token=admin,
                            body=channel_body(f"{MARK} 邮件", kind="email",
                                              target=" fin@example.com , ops@example.com ", min_level="high"))
        check("邮件通道收件人规范化 201",
              status == 201 and mail.get("target") == "fin@example.com, ops@example.com",
              f"{status} {mail.get('target')}")
        status, payload = call("PUT", f"/admin/alert-channels/{mail.get('id')}", token=admin, body={"kind": "webhook"})
        check("改 kind 时按新类型重校 target → 422", status == 422, f"{status} {str(payload)[:80]}")
        status, payload = call("DELETE", f"/admin/alert-channels/{mail.get('id')}", token=admin)
        check("DELETE 邮件通道 200 且 deleted=true",
              status == 200 and payload.get("deleted") is True, f"{status} {str(payload)[:80]}")

        # --- 测试发送：同步发一条，不写台账 -------------------------------------
        SINK.reset("ok")
        status, probe = call("POST", f"/admin/alert-channels/{channel_a}/test", token=admin)
        check("测试发送成功：ok=true、通道状态 ok、有 last_sent_at",
              status == 200 and probe.get("ok") is True and probe["channel"]["last_result"] == "ok"
              and bool(probe["channel"]["last_sent_at"]),
              f"{status} {probe.get('detail')} {probe.get('duration_ms')}ms")
        check("测试通知带 alert.channel_test 事件名",
              bool(SINK.posts("alert.channel_test")), f"{len(SINK.posts('alert.channel_test'))} 条")

        SINK.reset("fail")
        status, probe = call("POST", f"/admin/alert-channels/{channel_a}/test", token=admin)
        check("测试发送失败：接口仍 200，但 ok=false 且说明原因",
              status == 200 and probe.get("ok") is False and "500" in str(probe.get("detail")),
              f"{status} ok={probe.get('ok')} detail={probe.get('detail')}")
        check("测试发送不写投递台账", not deliveries(admin, channel_a), "台账 0 条")

        # --- 真机投递：导入产生预警 → 自动建单 → 后台任务真发 HTTP ---------------
        SINK.reset("ok")
        header = "交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出\n"
        row = f"{PERIOD}-01,6001,{MARK}收入,部门-097,{MARK},100.005,90,20,0,0\n"
        status, imported = call_multipart("/financial/import", admin, "outbound.csv",
                                          (header + row).encode("utf-8-sig"))
        batch = imported.get("batch") if isinstance(imported, dict) else None
        check("导入返回体带 deliveries（⑭ 钩子：commit 之后立刻排程）",
              status == 200 and imported.get("imported") == 1 and imported.get("deliveries", 0) >= 1,
              f"{status} alerts={imported.get('calculated')} deliveries={imported.get('deliveries')}")

        rows = settled(admin, channel_a, PERIOD)
        check("这一期的投递单全部落地（没有卡在待发送）",
              bool(rows) and all(item["status"] in ("sent", "failed") for item in rows),
              f"{len(rows)} 条：{[item['status'] for item in rows]}")
        check("投递成功：sent + attempts=1 + response_status=200",
              bool(rows) and all(item["status"] == "sent" and item["attempts"] == 1
                                 and item["response_status"] == 200 for item in rows),
              f"{[(item['status'], item['attempts'], item['response_status']) for item in rows]}")
        check("台账快照带通道名与期间",
              bool(rows) and all(item["channel_name"] == f"{MARK} A" and item["period"] == PERIOD
                                 for item in rows),
              f"{[(item['channel_name'], item['period']) for item in rows][:3]}")

        posts = SINK.posts("alert.created")
        check("sink 收到真实投递（事件名 alert.created）", bool(posts), f"{len(posts)} 条")
        if posts:
            payload = json.loads(posts[0]["raw"])
            alert = payload.get("alert", {})
            check("正文是预警快照：期间/级别/待处理状态齐全",
                  payload.get("event") == "alert.created" and alert.get("period") == PERIOD
                  and alert.get("level_label") in {"高", "中", "低"}
                  and alert.get("review_status") == "pending" and alert.get("metric_name"),
                  f"event={payload.get('event')} alert={str(alert)[:110]}")
            check("正文里的 delivery_id 就是台账行 id",
                  str(payload.get("delivery_id")) in {str(item["id"]) for item in rows},
                  f"delivery_id={payload.get('delivery_id')} 台账={[item['id'] for item in rows]}")
            bad = []
            for post in posts:
                timestamp = post["headers"].get("x-finance-timestamp", "")
                if post["headers"].get("x-finance-signature") != signature_of(SECRET_A, timestamp, post["raw"]):
                    bad.append("签名不匹配")
                if not timestamp.isdigit():
                    bad.append("时间戳不是秒级整数")
                if not post["headers"].get("x-finance-delivery", "").isdigit():
                    bad.append("缺少投递编号头")
                if not post["headers"].get("content-type", "").startswith("application/json"):
                    bad.append("Content-Type 不对")
            check("每条投递都带 HMAC-SHA256 签名与投递编号头", not bad, f"{bad or '全部通过'}（{len(posts)} 条）")

        status, again = call("POST", "/admin/alerts/notify", token=admin, body={"period": PERIOD})
        check("内容级去重：同一期间再补发不新建（重算会换掉预警行 id，投递单不重复）",
              status == 200 and again.get("created") == 0, f"{status} created={again.get('created')}")

        # --- 退避重试：新通道 + 网关一直 500，看后台任务真的又发了几次 ----------
        status, created_b = call("POST", "/admin/alert-channels", token=admin,
                                 body=channel_body(f"{MARK} B", secret=SECRET_B))
        channel_b = created_b.get("id")
        check("POST 第二条通道 201", status == 201, f"{status} {str(created_b)[:80]}")

        SINK.reset("fail")
        status, queued = call("POST", "/admin/alerts/notify", token=admin, body={"period": PERIOD})
        check("补发为已有待处理预警建单并排队",
              status == 200 and queued.get("created", 0) >= 1 and queued.get("queued", 0) >= 1
              and (queued.get("job") or {}).get("id"),
              f"{status} created={queued.get('created')} queued={queued.get('queued')} job={(queued.get('job') or {}).get('id')}")

        rows_b = settled(admin, channel_b, PERIOD, timeout=45)
        check("网关一直 500 → 预算用尽后判失败（attempts 用满）",
              bool(rows_b) and all(item["status"] == "failed" and item["attempts"] >= 2 for item in rows_b),
              f"{[(item['status'], item['attempts'], item['last_error']) for item in rows_b][:3]}")
        check("失败原因记在台账与通道上",
              bool(rows_b) and "500" in str(rows_b[0]["last_error"]),
              f"{str(rows_b[0]['last_error'])[:90] if rows_b else '无'}")
        attempts_seen = len(SINK.posts("alert.created"))
        check("退避重试真的又发了（sink 收到的次数 > 投递单条数）",
              attempts_seen > len(rows_b), f"sink 收到 {attempts_seen} 次，投递单 {len(rows_b)} 条")

        status, job_list = call("GET", "/admin/jobs?kind=alert.dispatch&limit=50", token=admin)
        jobs_seen = job_list.get("data", []) if isinstance(job_list, dict) else []
        check("外发任务在任务列表里且经历过多次尝试",
              status == 200 and bool(jobs_seen) and max(item["attempts"] for item in jobs_seen) >= 2,
              f"{len(jobs_seen)} 个任务，最多尝试 {max((item['attempts'] for item in jobs_seen), default=0)} 次")

        status, payload = call("GET", "/admin/alert-channels", token=admin)
        item_b = next((row for row in payload["data"] if row["id"] == channel_b), {})
        check("通道上也留下最后一次失败的结果与原因",
              item_b.get("last_result") == "failed" and "500" in str(item_b.get("last_error")),
              f"last_result={item_b.get('last_result')} last_error={str(item_b.get('last_error'))[:60]}")

        # --- 人工重试：通道修好后一次成功 ---------------------------------------
        target = rows_b[0]
        SINK.reset("ok")
        status, retried = call("POST", f"/admin/alert-deliveries/{target['id']}/retry", token=admin)
        check("人工重试接口 200 且重新排队", status == 200 and retried.get("queued") == 1,
              f"{status} queued={retried.get('queued')} job={(retried.get('job') or {}).get('id')}")
        deadline = time.time() + 20
        fresh = target
        while time.time() < deadline:
            fresh = next((item for item in deliveries(admin, channel_b, PERIOD) if item["id"] == target["id"]), target)
            if fresh["status"] == "sent":
                break
            time.sleep(0.4)
        check("重试后当场发成功：sent + attempts 归零到 1 + response_status=200",
              fresh["status"] == "sent" and fresh["attempts"] == 1 and fresh["response_status"] == 200,
              f"{fresh['status']} attempts={fresh['attempts']} status={fresh['response_status']}")

        status, payload = call("GET", "/admin/alert-channels", token=admin)
        item_b = next((row for row in payload["data"] if row["id"] == channel_b), {})
        counts_b = item_b.get("counts", {})
        check("通道计数跟着台账走，成功一次后失败原因被清掉",
              counts_b.get("sent", 0) >= 1 and counts_b.get("failed", 0) >= 1
              and item_b.get("last_result") == "ok" and item_b.get("last_error") is None,
              f"{counts_b} last_result={item_b.get('last_result')} last_error={item_b.get('last_error')}")

        # --- 审计留痕 -----------------------------------------------------------
        for action, label in (("alert_channel.create", "新建通道"), ("alert_channel.test", "测试通道")):
            status, payload = call("GET", f"/admin/audit?action={action}&limit=100", token=admin)
            audit_rows = payload.get("data", []) if isinstance(payload, dict) else []
            check(f"审计留痕 {action}（{label}）",
                  status == 200 and any(MARK in str(row.get("detail")) for row in audit_rows),
                  f"{len(audit_rows)} 条")
        status, payload = call("GET", "/admin/audit?action=alert.notify&limit=100", token=admin)
        notify_rows = payload.get("data", []) if isinstance(payload, dict) else []
        check("审计留痕 alert.notify（补发通知）",
              any(row.get("target_id") == PERIOD for row in notify_rows), f"{len(notify_rows)} 条")
        status, payload = call("GET", "/admin/audit?action=alert_delivery.retry&limit=100", token=admin)
        retry_rows = payload.get("data", []) if isinstance(payload, dict) else []
        check("审计留痕 alert_delivery.retry（人工重试）",
              any(f"{MARK} B" in str(row.get("detail")) for row in retry_rows), f"{len(retry_rows)} 条")
    finally:
        cleanup(admin, batch, channel_a, channel_b)
    return finish()


def cleanup(admin: str, batch: str | None, channel_a: int | None, channel_b: int | None) -> None:
    """撤销临时批次、把还没发完的投递冲掉、删掉临时通道。"""
    if batch:
        status, payload = call("DELETE", f"/admin/batches/{batch}", token=admin)
        print(f"  清理临时批次 {batch[:12]}… → {status} {str(payload)[:70]}", flush=True)
    SINK.reset("ok")
    call("POST", "/admin/alerts/notify", token=admin, body={"period": PERIOD})
    deadline = time.time() + 15
    while time.time() < deadline:
        status, payload = call("GET", f"/admin/alert-deliveries?period={PERIOD}&status=pending&limit=200",
                               token=admin)
        if status == 200 and not payload.get("data"):
            break
        time.sleep(0.5)
    for channel_id in (channel_a, channel_b):
        if channel_id:
            status, _ = call("DELETE", f"/admin/alert-channels/{channel_id}", token=admin)
            print(f"  清理临时通道 {channel_id} → {status}", flush=True)
    leftover = [row for row in _channel_names(admin) if MARK in row]
    check("临时通道清理干净", not leftover, f"残留 {leftover}")
    status, payload = call("GET", f"/admin/alert-deliveries?period={PERIOD}&status=pending&limit=200",
                           token=admin)
    check("这一期没有卡在待发送的投递", status == 200 and not payload.get("data"),
          f"{len(payload.get('data', [])) if isinstance(payload, dict) else '?'} 条")


def _channel_names(admin: str) -> list[str]:
    status, payload = call("GET", "/admin/alert-channels", token=admin)
    if status != 200 or not isinstance(payload, dict):
        return []
    return [row["name"] for row in payload.get("data", [])]


def finish() -> int:
    SINK.stop()
    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
