"""⑲ 导出：冲销/更正标记 + 指标导出月/季/年粒度 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_exports.py

为什么必须有这个脚本：单元/接口用例证明的是「代码里写了这些列与这些参数」，证明不了
「**跑在 127.0.0.1:8000 上的那份代码**真的按季/年去读了落库表，而且读出来的数字与 /admin
概览上那张卡是同一份」。这里做四件用例做不到的事：

1. **真改一笔再导出**：导入一个 2097 年小批次（4 行），真的调更正接口改掉一行、真的调冲销接口
   冲掉一行，然后导出明细，逐行核对三种状态（空 / 已冲销 / 冲销行）、`原始行 id`、`更正次数`
   与金额——「导出的负数行终于有了解释」这句话只有在真库上跑一遍才算数。
2. **季/年粒度读的是哪张表**：`grain=quarter|year` 必须命中 `financial_rollups`，且与
   `GET /admin/rollups` 返回的同一行**逐值一致**；`complete=False` 的期间要如实写「否」——
   这正是 ⑧ 警告过的「年度合计看起来像全年」的解药。（演示库里 2097-Q4 只落了 1 个月，
   正好是个天然的 `complete=False` 样本。）
3. **口径守卫在新表上也成立**：临时注册一个只绑 097 的账号，它的季/年导出必须只有
   「部门 097」的行，既没有「全公司」也没有别人的部门——⑨/⑧ 的 scope 过滤不能在换表时漏掉。
4. **导出的痕迹**：每次导出都要在审计里留一条 `data.export`（kind/rows/scope/format/truncated），
   而且 `truncated` 不再是写死的 False。

顺带验到的一件事：导出有配额（默认 30 次/分钟/账号），这个脚本打 30 多次导出必然撞上 429，
所以 `request()` 默认按 `Retry-After` 等一轮再重试——脚本末尾还用临时账号**主动**打了一串，
把「429 + Retry-After + 窗口滑过之后放行」这条链路也钉在真机上（跑一次要等 1 分钟左右）。

测试数据只用 2097 年（演示数据是 2026 年）、批次 `verify-exports-*` 与 `verify_exports_*` 账号，
脚本开头结尾都清干净（收尾还走一次真的 `POST /admin/jobs/batch-revert` 撤销批次）。
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from backend.db import engine  # noqa: E402

HOST = "http://127.0.0.1:8000"
BASE = f"{HOST}/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")

# 测试数据（2097 年，与演示数据的 2026 年错开）：三行 097 + 一行 018（用来验口径）。
PERIOD = "2097-12"
QUARTER = "2097-Q4"
YEAR = "2097"
DEPT = "097"
OTHER_DEPT = "018"
MARK = "⑲导出冒烟"
PURGE_LIKE = "2097%"
TEMP_PREFIX = "verify_exports_"
TEMP_PASSWORD = "Zq7-harbor-lantern-2097"     # 过 ④ 的强度规则（无弱词、无键盘序、够长）

TX_HEADER = ["明细 id", "交易日期", "科目编码", "科目名称", "部门", "摘要", "收入金额", "成本金额",
             "费用金额", "现金流入", "现金流出", "导入批次", "冲销状态", "原始行 id", "冲销时间",
             "冲销操作人", "更正次数"]
METRIC_HEADER = ["期间", "粒度", "口径", "包含月份", "是否完整", "收入金额", "成本金额", "费用金额",
                 "毛利金额", "净利润", "毛利率", "净利率"]
ALERT_HEADER = ["期间", "口径", "预警类型", "描述", "级别", "指标", "当前值", "阈值",
                "需人工复核", "预警 id"]
IMPORT_HEADER = "交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出"
EXPORT_LIMIT = 20000

RESULTS: list[tuple[str, bool, str]] = []
QUOTA_HITS: list[dict] = []        # 被导出配额拦下的记录（见 request() 的重试）


# ------------------------------------------------------------------ 小工具

def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def scalar(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def execute(sql: str, **params) -> int:
    with engine.begin() as conn:
        return conn.execute(text(sql), params).rowcount or 0


def request(method: str, path: str, token: str | None = None, body: dict | None = None,
            headers: dict | None = None, retry: bool = True):
    """返回 (status, 原始字节, 响应头)。字节不解析 —— 导出是 CSV/XLSX，得看原始内容。

    导出有配额（`EXPORT_MAX_PER_MINUTE`，默认 30 次/分钟/账号）。这个脚本要打 30 多次导出，
    真机上必然撞上 429 —— 所以**默认按 Retry-After 等一轮再重试**（这也顺手证明了配额回的是
    429 + Retry-After、且窗口真的会滑动）。要观察 429 本身就把 `retry=False`。
    """
    for attempt in range(3):
        status, raw, head = _once(method, path, token, body, headers)
        if status != 429 or not retry or attempt == 2:
            return status, raw, head
        retry_after = head_value(head, "Retry-After") or "30"
        QUOTA_HITS.append({"path": path, "retry_after": retry_after})
        print(f"  · 被导出配额拦了一下（{path}）：按 Retry-After={retry_after}s 等一轮再试", flush=True)
        time.sleep(max(1, int(retry_after)))
    return status, raw, head


def _once(method: str, path: str, token: str | None, body: dict | None, headers: dict | None):
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with OPENER.open(req, timeout=300) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def call(method: str, path: str, token: str | None = None, body: dict | None = None,
         headers: dict | None = None):
    status, raw, head = request(method, path, token, body, headers)
    try:
        return status, json.loads(raw.decode("utf-8", "replace")), head
    except json.JSONDecodeError:
        return status, raw.decode("utf-8", "replace"), head


def head_value(head: dict, name: str) -> str:
    """HTTP 头大小写不敏感：uvicorn 原样透传我们设的名字，但比对时不该在意大小写。"""
    wanted = name.lower()
    for key, value in (head or {}).items():
        if key.lower() == wanted:
            return value
    return ""


def upload(path: str, filename: str, content: bytes, token: str):
    boundary = "----verifyexports"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").encode()
    body = head + content + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(req, timeout=300) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    text_body = payload.decode("utf-8", "replace")
    try:
        return {**json.loads(text_body), "status": status}
    except json.JSONDecodeError:
        return {"status": status, "body": text_body}


def csv_rows(raw: bytes) -> tuple[list[str], list[dict]]:
    """CSV → (表头, 每行字典)。导出是 utf-8-sig（Excel 要 BOM），所以按 BOM 解码。"""
    text_body = raw.decode("utf-8-sig")
    reader = list(csv.reader(io.StringIO(text_body)))
    header = reader[0] if reader else []
    return header, [dict(zip(header, row, strict=True)) for row in reader[1:] if row]


def xlsx_rows(raw: bytes) -> tuple[list[str], list[dict]]:
    from openpyxl import load_workbook  # noqa: PLC0415 - 只有这条检查需要它

    sheet = load_workbook(io.BytesIO(raw), read_only=True).active
    values = list(sheet.values)
    header = [str(item) for item in values[0]]
    return header, [dict(zip(header, row, strict=True)) for row in values[1:]]


def money(row: dict, column: str) -> Decimal:
    return Decimal(str(row.get(column) or 0))


def login(username: str, password: str) -> str:
    status, payload, _ = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def poll_job(job_id: int, token: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    seen: list[str] = []
    while time.time() < deadline:
        _, payload, _ = call("GET", f"/admin/jobs/{job_id}", token)
        status = payload.get("status") if isinstance(payload, dict) else None
        if status and (not seen or seen[-1] != status):
            seen.append(status)
        if status in ("succeeded", "failed", "cancelled"):
            return {"job": payload, "seen": seen}
        time.sleep(0.4)
    return {"job": payload if isinstance(payload, dict) else {}, "seen": seen}


def purge() -> None:
    """清掉 2097 的测试数据与临时账号（幂等，跑前跑后各来一次）。"""
    execute("DELETE FROM financial_transactions WHERE transaction_date >= '2097-01-01' "
            "AND transaction_date < '2098-01-01'")
    execute("DELETE FROM financial_metrics WHERE period LIKE :like", like=PURGE_LIKE)
    execute("DELETE FROM financial_alerts WHERE period LIKE :like", like=PURGE_LIKE)
    execute("DELETE FROM financial_rollups WHERE period LIKE :like", like=PURGE_LIKE)
    execute("DELETE FROM users WHERE username LIKE :prefix", prefix=f"{TEMP_PREFIX}%")


def baseline() -> dict:
    return {
        "transactions": int(scalar("SELECT count(*) FROM financial_transactions") or 0),
        "metrics": int(scalar("SELECT count(*) FROM financial_metrics") or 0),
        "alerts": int(scalar("SELECT count(*) FROM financial_alerts") or 0),
        "rollups": int(scalar("SELECT count(*) FROM financial_rollups") or 0),
        "users": int(scalar("SELECT count(*) FROM users") or 0),
    }


def leftovers() -> dict:
    return {
        "transactions": int(scalar("SELECT count(*) FROM financial_transactions "
                                   "WHERE transaction_date >= '2097-01-01' AND transaction_date < '2098-01-01'") or 0),
        "metrics": int(scalar("SELECT count(*) FROM financial_metrics WHERE period LIKE :like",
                              like=PURGE_LIKE) or 0),
        "alerts": int(scalar("SELECT count(*) FROM financial_alerts WHERE period LIKE :like",
                             like=PURGE_LIKE) or 0),
        "rollups": int(scalar("SELECT count(*) FROM financial_rollups WHERE period LIKE :like",
                              like=PURGE_LIKE) or 0),
        "temp_users": int(scalar("SELECT count(*) FROM users WHERE username LIKE :prefix",
                                 prefix=f"{TEMP_PREFIX}%") or 0),
    }


def openapi_params(suffix: str) -> set[str]:
    """/openapi.json 挂在根上（不在 /api/v1 下），所以单独走一次：参数表就是「跑的是哪份代码」的证据。"""
    req = urllib.request.Request(f"{HOST}/openapi.json", method="GET")
    try:
        with OPENER.open(req, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    if status != 200:
        return set()
    spec = json.loads(raw.decode("utf-8", "replace"))
    for path, item in (spec.get("paths") or {}).items():
        if path.endswith(suffix):
            names = set()
            for method in item.values():
                for param in method.get("parameters") or []:
                    names.add(param.get("name"))
            return names
    return set()


# ------------------------------------------------------------------ 主流程

def main() -> int:
    purge()
    start = baseline()
    print(f"起始基线：{start}", flush=True)

    # ---------------------------------------------------------- 闸门：跑的是新代码
    metric_params = openapi_params("/export/metrics")
    tx_params = openapi_params("/export/transactions")
    check("服务在跑的是 ⑲ 的代码：/export/metrics 有 grain，/export/transactions 有 batch/query",
          {"grain", "period_from", "period_to"} <= metric_params
          and {"batch", "query", "department", "period"} <= tx_params,
          f"metrics 参数 {sorted(metric_params)}；transactions 参数 {sorted(tx_params)}")

    check("起始干净：没有 2097 的残留数据与临时账号",
          all(value == 0 for value in leftovers().values()), f"{leftovers()}")

    token = login(*ADMIN)
    me_status, me, _ = call("GET", "/auth/me", token)
    check("演示管理员是不受限账号（全公司口径）", me_status == 200 and me.get("unrestricted") is True,
          f"status={me_status} unrestricted={me.get('unrestricted')}")

    # ---------------------------------------------------------- 导入 + 更正 + 冲销
    batch = f"verify-exports-{uuid.uuid4().hex[:12]}"
    rows_spec = [
        (f"{PERIOD}-05", "6101", f"{MARK}甲", DEPT, f"{MARK}甲说明", "100"),
        (f"{PERIOD}-06", "6102", f"{MARK}乙", DEPT, f"{MARK}乙说明", "300"),
        (f"{PERIOD}-07", "6103", f"{MARK}丙", DEPT, f"{MARK}丙说明", "500"),
        (f"{PERIOD}-08", "6104", f"{MARK}丁", OTHER_DEPT, f"{MARK}丁说明", "700"),
    ]
    csv_body = IMPORT_HEADER + "\n" + "\n".join(
        f"{day},{code},{name},{dept},{note},{revenue},0,0,0,0"
        for day, code, name, dept, note, revenue in rows_spec
    ) + "\n"
    imported = upload("/financial/import", "verify-exports.csv", csv_body.encode("utf-8-sig"), token)
    check("导入 2097-12 的四行测试明细（三行 097、一行 018）",
          imported.get("status") == 200 and imported.get("imported") == 4,
          f"status={imported.get('status')} imported={imported.get('imported')} "
          f"batch={imported.get('batch')}")
    batch = imported.get("batch") or batch

    _, listed, _ = call("GET", f"/financial/transactions?period={QUARTER}&batch={batch}&limit=50", token)
    by_code = {row["account_code"]: row for row in listed.get("data", [])}
    row_a, row_b, row_c, row_d = (by_code.get("6101"), by_code.get("6102"),
                                  by_code.get("6103"), by_code.get("6104"))
    check("明细列表用季度期间也能取到这批（与导出同语义的 period_range）",
          listed.get("total") == 4 and all((row_a, row_b, row_c, row_d)),
          f"total={listed.get('total')} 科目 {sorted(by_code)}")
    if not all((row_a, row_b, row_c, row_d)):
        raise RuntimeError(f"导入后取不到四行测试明细（{listed.get('total')} 行）：后面没法继续")

    status, corrected, _ = call("PATCH", f"/financial/transactions/{row_a['id']}", token,
                                {"revenue_amount": "150", "description": f"{MARK}甲（已更正）"})
    check("更正一行：corrections 加一、重算报到 2097-12",
          status == 200 and (corrected.get("data") or {}).get("corrections") == 1
          and PERIOD in (corrected.get("recalculated") or []),
          f"status={status} corrections={(corrected.get('data') or {}).get('corrections')} "
          f"recalculated={corrected.get('recalculated')}")


    status, reversed_payload, _ = call("POST", f"/financial/transactions/{row_b['id']}/reverse", token,
                                       {"reason": "⑲冒烟：凭证重复"})
    reversal = reversed_payload.get("reversal") or {}
    check("冲销一行：新增负数行、原行被标记（不删原行）",
          status == 200 and reversal.get("reversal_of_id") == row_b["id"]
          and money(reversal, "revenue_amount") == Decimal("-300")
          and (reversed_payload.get("original") or {}).get("reversal_of_id") is None,
          f"status={status} 冲销行 id={reversal.get('id')} 原始行 id={reversal.get('reversal_of_id')} "
          f"金额={reversal.get('revenue_amount')}")

    status, again, _ = call("POST", f"/financial/transactions/{row_b['id']}/reverse", token, {})
    check("已冲销的行不能再冲销（409）——导出的三态语义没被改动松掉",
          status == 409 and "已经冲销" in str(again), f"status={status} 响应={again}")

    # ---------------------------------------------------------- 明细导出：标记与区间
    status, raw, head = request(
        "GET", f"/export/transactions?period={YEAR}&format=csv&batch={batch}", token)
    header, rows = csv_rows(raw) if status == 200 else ([], [])
    index = {row["科目编码"]: row for row in rows}
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    check("明细导出：17 列表头（多了 id 与冲销/更正标记）、5 行（含冲销行）、带 BOM",
          status == 200 and header == TX_HEADER and len(rows) == 5 and has_bom
          and head_value(head, "X-Export-Rows") == "5"
          and not head_value(head, "X-Export-Truncated"),
          f"status={status} 列数={len(header)} 行数={len(rows)} "
          f"X-Export-Rows={head_value(head, 'X-Export-Rows')} BOM={has_bom}")

    reversal_row = next((row for row in rows if row["冲销状态"] == "冲销行"), {})
    check("明细导出：三种冲销状态各自写在行上（普通空 / 已冲销 / 冲销行）",
          index.get("6101", {}).get("冲销状态") == ""
          and index.get("6102", {}).get("冲销状态") == "已冲销"
          and reversal_row.get("原始行 id") == str(row_b["id"])
          and index.get("6103", {}).get("冲销状态") == "",
          f"6101={index.get('6101', {}).get('冲销状态')!r} 6102={index.get('6102', {}).get('冲销状态')!r} "
          f"冲销行 原始行 id={reversal_row.get('原始行 id')!r}")
    check("明细导出：更正次数与更正后的金额是导出的那个数，冲销行金额取负",
          index.get("6101", {}).get("更正次数") == "1"
          and money(index.get("6101", {}), "收入金额") == Decimal("150")
          and money(index.get("6102", {}), "收入金额") == Decimal("300")
          and money(reversal_row, "收入金额") == Decimal("-300")
          and money(index.get("6104", {}), "收入金额") == Decimal("700"),
          f"6101={index.get('6101', {}).get('收入金额')}/更正 {index.get('6101', {}).get('更正次数')} "
          f"6102={index.get('6102', {}).get('收入金额')} 冲销行={reversal_row.get('收入金额')}")
    reversed_row = index.get("6102", {})
    check("明细导出：被冲销的那一行写着「谁、什么时候」冲的（业务时区的那一刻，不是行的日期）",
          reversed_row.get("冲销操作人") == ADMIN[0]
          and re_fullmatch(reversed_row.get("冲销时间") or ""),
          f"操作人={reversed_row.get('冲销操作人')!r} 时间={reversed_row.get('冲销时间')!r}")
    check("明细导出：冲销行自带解释（摘要写「冲销：…」、原始行 id 指回去），不需要外部对照表",
          (reversal_row.get("摘要") or "").startswith("冲销：")
          and reversal_row.get("原始行 id") == str(row_b["id"])
          and not reversal_row.get("冲销时间"),
          f"摘要={reversal_row.get('摘要')!r} 原始行 id={reversal_row.get('原始行 id')!r} "
          f"冲销时间={reversal_row.get('冲销时间')!r}")

    quarter_counts = {}
    for period in (YEAR, QUARTER, PERIOD, "2097-11"):
        status, raw_q, _ = request(
            "GET", f"/export/transactions?period={quote(period)}&format=csv&batch={batch}", token)
        _, body_q = csv_rows(raw_q) if status == 200 else ([], [])
        quarter_counts[period] = (status, len(body_q))
    expected_counts = {YEAR: (200, 5), QUARTER: (200, 5), PERIOD: (200, 5), "2097-11": (200, 0)}
    check("明细导出的期间认月/季/年（2097 / 2097-Q4 / 2097-12 都是这 5 行，2097-11 是 0 行）",
          quarter_counts == expected_counts, f"{quarter_counts}")


    bad_periods = {}
    for period in ("2097-13", "2097-Q5", "2097-4", "全部期间"):
        status, _, _ = request(
            "GET", f"/export/transactions?period={quote(period)}&format=csv", token)
        bad_periods[period] = status
    check("明细导出：写坏的期间一律 422（不会安静地当成空结果）",
          set(bad_periods.values()) == {422}, f"{bad_periods}")

    filter_expectations = {"6113": 0, f"{MARK}丙": 1, "6104": 1}
    filtered = {}
    for keyword in filter_expectations:
        status, raw_f, _ = request(
            "GET", f"/export/transactions?batch={batch}&query={quote(keyword)}&format=csv", token)
        _, body_f = csv_rows(raw_f) if status == 200 else ([], [])
        filtered[keyword] = len(body_f)
    check("明细导出与页面同一套筛选：关键字命中一行，批次与关键字是「且」",
          filtered == filter_expectations, f"{filtered}")


    status, raw_x, head_x = request(
        "GET", f"/export/transactions?period={QUARTER}&format=xlsx&batch={batch}", token)
    x_header, x_rows = xlsx_rows(raw_x) if status == 200 else ([], [])
    check("明细导出的 Excel 与 CSV 是同一张表：表头一致、行数一致、文件名带期间",
          status == 200 and x_header == TX_HEADER and len(x_rows) == 5
          and "transactions-2097-Q4-" in head_value(head_x, "content-disposition"),
          f"status={status} 列数={len(x_header)} 行数={len(x_rows)} "
          f"文件名={head_value(head_x, 'content-disposition')}")


    # ---------------------------------------------------------- 指标导出：月/季/年
    status, raw_m, head_m = request(
        "GET", f"/export/metrics?grain=month&period_from={PERIOD}&period_to={PERIOD}"
               f"&department={DEPT}&format=csv", token)
    m_header, m_rows = csv_rows(raw_m) if status == 200 else ([], [])
    month_row = m_rows[0] if m_rows else {}
    check("指标导出（月口径）：粒度/包含月份/是否完整三列，收入是更正+冲销之后的净额",
          status == 200 and m_header == METRIC_HEADER and len(m_rows) == 1
          and month_row.get("粒度") == "月" and month_row.get("包含月份") == PERIOD
          and month_row.get("是否完整") == "是" and month_row.get("口径") == f"部门 {DEPT}"
          and money(month_row, "收入金额") == Decimal("650.00"),
          f"status={status} 行={month_row.get('期间')}/{month_row.get('口径')} "
          f"收入={month_row.get('收入金额')}（097 应为 150+300-300+500=650）")

    status, raw_q, head_q = request(
        "GET", f"/export/metrics?grain=quarter&period_from={QUARTER}&period_to={QUARTER}&format=csv",
        token)
    q_header, q_rows = csv_rows(raw_q) if status == 200 else ([], [])
    quarter_row = q_rows[0] if q_rows else {}
    check("指标导出（季口径）：读的是 financial_rollups，只落了 1 个月的季如实写「否」",
          status == 200 and q_header == METRIC_HEADER and len(q_rows) == 1
          and quarter_row.get("期间") == QUARTER and quarter_row.get("粒度") == "季"
          and quarter_row.get("包含月份") == PERIOD and quarter_row.get("是否完整") == "否"
          and quarter_row.get("口径") == "全公司" and money(quarter_row, "收入金额") == Decimal("1350.00")
          and "metrics-quarter-" in head_value(head_q, "content-disposition"),
          f"status={status} 期间={quarter_row.get('期间')} 包含月份={quarter_row.get('包含月份')} "
          f"完整={quarter_row.get('是否完整')} 收入={quarter_row.get('收入金额')} "
          f"文件名={head_value(head_q, 'content-disposition')}")


    _, rollup_payload, _ = call("GET", "/admin/rollups?grain=quarter", token)
    stored = next((row for row in rollup_payload.get("data", [])
                   if row.get("period") == QUARTER and not row.get("department")), {})
    check("季/年导出的数字与 /admin/rollups（屏幕上那张卡）逐值一致——同一份落库，不是另算一套",
          bool(stored) and money(stored, "revenue_amount") == money(quarter_row, "收入金额")
          and bool(stored.get("complete")) is False
          and list(stored.get("months") or []) == [PERIOD],
          f"落库 revenue={stored.get('revenue_amount')} complete={stored.get('complete')} "
          f"months={stored.get('months')}；导出 {quarter_row.get('收入金额')}")

    status, raw_y, _ = request(
        "GET", f"/export/metrics?grain=year&period_from={YEAR}&period_to={YEAR}&format=csv", token)
    y_header, y_rows = csv_rows(raw_y) if status == 200 else ([], [])
    year_row = y_rows[0] if y_rows else {}
    check("指标导出（年口径）：2097 年度只有这一个月，同样标「否」",
          status == 200 and y_header == METRIC_HEADER and len(y_rows) == 1
          and year_row.get("期间") == YEAR and year_row.get("粒度") == "年"
          and year_row.get("是否完整") == "否" and money(year_row, "收入金额") == Decimal("1350.00"),
          f"status={status} 期间={year_row.get('期间')} 完整={year_row.get('是否完整')} "
          f"收入={year_row.get('收入金额')}")

    status, raw_d, _ = request(
        "GET", "/export/metrics?grain=quarter&period_from=2026-Q2&period_to=2026-Q4&format=csv", token)
    d_header, d_rows = csv_rows(raw_d) if status == 200 else ([], [])
    q3 = next((row for row in d_rows if row["期间"] == "2026-Q3" and row["口径"] == "全公司"), {})
    check("演示数据的季导出：不受限账号只读全公司行（2026-Q2~Q4 共 3 行，部门行不混进来），"
          "2026-Q3 全公司收入与库值一致",
          status == 200 and d_header == METRIC_HEADER and len(d_rows) == 3
          and {row["口径"] for row in d_rows} == {"全公司"}
          and money(q3, "收入金额") == Decimal("12191391796.83"),
          f"status={status} 行数={len(d_rows)} 口径={sorted({row['口径'] for row in d_rows})} "
          f"2026-Q3 全公司收入={q3.get('收入金额')}")

    status, raw_2026, _ = request(
        "GET", "/export/metrics?grain=year&period_from=2026&period_to=2026&format=csv", token)
    _, rows_2026 = csv_rows(raw_2026) if status == 200 else ([], [])
    year_2026 = next((row for row in rows_2026 if row["口径"] == "全公司"), {})
    check("演示数据的年导出：2026 只有全公司 1 行，标「否」并列出真的落了库的 7 个月（不假装是全年）",
          status == 200 and len(rows_2026) == 1 and year_2026.get("是否完整") == "否"
          and len((year_2026.get("包含月份") or "").split("、")) == 7
          and "2026-04" in (year_2026.get("包含月份") or "")
          and "2026-10" in (year_2026.get("包含月份") or ""),
          f"status={status} 行数={len(rows_2026)} 2026 全公司 完整={year_2026.get('是否完整')} "
          f"月份={year_2026.get('包含月份')}")


    grain_errors = {}
    for path in ("/export/metrics?grain=week&format=csv",
                 "/export/metrics?grain=quarter&period_from=2088-04&format=csv",
                 "/export/metrics?grain=year&period_from=2088-Q2&format=csv",
                 "/export/metrics?grain=quarter&period_from=2088-Q4&period_to=2088-Q2&format=csv"):
        status, payload, _ = call("GET", path, token)
        grain_errors[path] = (status, str(payload))
    check("指标导出：粒度写错/端点与粒度不同、区间倒过来，全是 422 且提示里给样例",
          all(item[0] == 422 for item in grain_errors.values())
          and any("month、quarter 或 year" in item[1] for item in grain_errors.values())
          and any("同粒度" in item[1] and "2026-Q3" in item[1] for item in grain_errors.values())
          and any("不能晚于" in item[1] for item in grain_errors.values()),
          f"{ {key: item[0] for key, item in grain_errors.items()} }")

    # ---------------------------------------------------------- 口径守卫（新表也要守）
    temp_username = f"{TEMP_PREFIX}{uuid.uuid4().hex[:8]}"
    status, registered, _ = call("POST", "/auth/register",
                                 body={"username": temp_username, "password": TEMP_PASSWORD})
    temp_id = registered.get("id") if isinstance(registered, dict) else None
    status, updated, _ = call("PATCH", f"/admin/users/{temp_id}", token, {"departments": [DEPT]})
    temp_token = login(temp_username, TEMP_PASSWORD) if temp_id else None
    _, temp_me, _ = call("GET", "/auth/me", temp_token) if temp_token else (0, {}, {})
    check("临时账号：注册后由管理员绑定 097，就成了受限账号",
          temp_id is not None and updated.get("unrestricted") is False
          and temp_me.get("unrestricted") is False and temp_me.get("departments") == [DEPT],
          f"temp id={temp_id} 绑定后 unrestricted={updated.get('unrestricted')} "
          f"me.departments={temp_me.get('departments')}")

    status, raw_t, _ = request(
        "GET", f"/export/transactions?period={YEAR}&format=csv&batch={batch}", temp_token)
    _, t_rows = csv_rows(raw_t) if status == 200 else ([], [])
    check("受限账号导明细：只看得到 097 的 4 行（含冲销行），018 那行不在导出里",
          status == 200 and len(t_rows) == 4
          and {row["部门"] for row in t_rows} == {DEPT}
          and all(row["科目编码"] != "6104" for row in t_rows),
          f"status={status} 行数={len(t_rows)} 部门={sorted({row['部门'] for row in t_rows})}")

    status, raw_tq, _ = request(
        "GET", "/export/metrics?grain=quarter&format=csv", temp_token)
    tq_header, tq_rows = csv_rows(raw_tq) if status == 200 else ([], [])
    temp_quarters = [row for row in tq_rows if row["期间"] == QUARTER]
    check("受限账号导季/年：新表上也只出它自己的部门行——没有「全公司」也没有别人的部门",
          status == 200 and tq_header == METRIC_HEADER and bool(tq_rows)
          and {row["口径"] for row in tq_rows} == {f"部门 {DEPT}"}
          and len(temp_quarters) == 1 and money(temp_quarters[0], "收入金额") == Decimal("650.00"),
          f"status={status} 行数={len(tq_rows)} 口径={sorted({row['口径'] for row in tq_rows})} "
          f"2097-Q4 收入={temp_quarters[0].get('收入金额') if temp_quarters else None}")

    status, payload, _ = call("GET", f"/export/transactions?period={YEAR}&department={OTHER_DEPT}",
                              temp_token)
    check("受限账号要别人的部门：403（不是静默返回空表）",
          status == 403, f"status={status} 响应={payload}")

    # ---------------------------------------------------------- 导出配额：拦得住脚本，也放得出来
    # 用这个刚建的临时账号打一串导出：它的配额窗口是新的，所以第 31 次一定被拦（默认 30 次/分钟）。
    burst_statuses = []
    first_retry_after = ""
    for _ in range(45):
        b_status, _, b_head = request("GET", "/export/alerts?period=2026-09&format=csv", temp_token,
                                      retry=False)
        burst_statuses.append(b_status)
        if b_status == 429:
            first_retry_after = head_value(b_head, "Retry-After")
            break
    recovered_status, _, _ = request("GET", "/export/alerts?period=2026-09&format=csv", temp_token)
    check("导出配额拦脚本时如实回 429 + Retry-After，等过窗口后同一个请求就成功",
          "429" in [str(item) for item in burst_statuses] and first_retry_after.isdigit()
          and 1 <= int(first_retry_after) <= 60
          and recovered_status == 200
          and any(item["path"].startswith("/export/alerts") for item in QUOTA_HITS),
          f"连打 {len(burst_statuses)} 次的状态={burst_statuses[-5:]} Retry-After={first_retry_after!r} "
          f"等待后重试={recovered_status}（本脚本共被拦 {len(QUOTA_HITS)} 次）")

    # ---------------------------------------------------------- 预警仍是月口径 + 配额
    status, payload, _ = call("GET", "/export/alerts?period=2026-Q3&format=csv", token)
    check("预警导出保持月口径：季度期间 422（预警行本身就是月，不该被当成空结果）",
          status == 422, f"status={status} 响应={payload}")
    status, raw_a, head_a = request("GET", "/export/alerts?period=2026-09&format=csv", token)
    a_header, a_rows = csv_rows(raw_a) if status == 200 else ([], [])
    check("预警导出照常：表头 10 列、行数与 X-Export-Rows 对得上",
          status == 200 and a_header == ALERT_HEADER
          and head_value(head_a, "X-Export-Rows") == str(len(a_rows)),
          f"status={status} 列数={len(a_header)} 行数={len(a_rows)} "
          f"X-Export-Rows={head_value(head_a, 'X-Export-Rows')}")
    _, scope_payload, _ = call("GET", "/export/scope", token)
    check(f"/export/scope 如实报告上限 {EXPORT_LIMIT}（截断与否由它加行数判定）",
          scope_payload.get("limit") == EXPORT_LIMIT and set(scope_payload.get("formats") or []) == {"csv", "xlsx"},
          f"limit={scope_payload.get('limit')} formats={scope_payload.get('formats')}")

    # ---------------------------------------------------------- 审计留痕
    _, audit_payload, _ = call("GET", "/admin/audit?action=data.export&limit=50", token)
    exports = [row for row in audit_payload.get("data", []) if row.get("action") == "data.export"]
    tx_export = [row for row in exports
                 if (row.get("detail") or {}).get("kind") == "transactions"
                 and (row.get("detail") or {}).get("rows") == 5]
    check("每次导出都在审计里留痕：kind/rows/scope/format/truncated，且 truncated 不再是写死的 False",
          len(exports) >= 6 and bool(tx_export)
          and all({"kind", "rows", "scope", "format", "truncated"} <= set(row.get("detail") or {})
                  for row in exports)
          and all((row.get("detail") or {}).get("truncated") is False for row in exports),
          f"这批共 {len(exports)} 条 data.export（其中明细 5 行的 {len(tx_export)} 条）")

    # ---------------------------------------------------------- 收尾：撤销批次 + 清干净
    status, submitted, _ = call("POST", "/admin/jobs/batch-revert", token, {"batch": batch})
    job_id = submitted.get("id") if isinstance(submitted, dict) else None
    outcome = poll_job(job_id, token) if job_id else {"job": {}, "seen": []}
    job = outcome["job"]
    check("撤销这个批次走的是真后台任务：202 → succeeded，且如实报告删了几行",
          status == 202 and job.get("status") == "succeeded"
          and (job.get("result") or {}).get("deleted_rows") == 5,
          f"status={status} job={job_id} 状态流转={outcome['seen']} "
          f"result={job.get('result')}")

    if temp_id:
        call("DELETE", f"/admin/users/{temp_id}", token)
    purge()
    left = leftovers()
    end = baseline()
    check("收尾干净：2097 的明细/指标/预警/落库与临时账号都清掉了",
          all(value == 0 for value in left.values()), f"{left}")
    check("演示数据的基线行数一行没动（明细/指标/预警/落库/用户）",
          end == start, f"起始 {start} → 结束 {end}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


def re_fullmatch(value: str) -> bool:
    import re  # noqa: PLC0415 - 只给这一条检查用

    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value or ""))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 脚本要一句话说清哪儿炸了
        print(f"\n异常终止：{type(exc).__name__}: {exc}", flush=True)
        print("检查后端是否在 127.0.0.1:8000、数据库容器是否在跑", flush=True)
        sys.exit(1)
