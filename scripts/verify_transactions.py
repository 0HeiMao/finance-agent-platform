"""真机验证「明细单行更正 / 冲销」（⑨）：走真实 HTTP 接口，不碰数据库。

为什么要先导入再清理：明细没有创建接口，唯一能造数据的路径就是导入。所以本脚本
往一个**演示数据里不存在的期间**（默认 2096-07）导入三行探针数据（部门 018、部门 097、
部门为空的全公司行各一行），验证完用 `/admin/jobs/batch-revert` 把整批撤掉——
否则每跑一次都会污染演示库，而且 2096 这个期间会永远挂在看板的期间下拉里。

用法：
    python scripts/verify_transactions.py
    python scripts/verify_transactions.py --base-url http://127.0.0.1:8000
    python scripts/verify_transactions.py --keep    # 故意留数据，排查用

覆盖的检查（约 34 项）：
  导入探针 → 列表与筛选（口径/关键字/部门）→ 受限账号只能看/改自己部门 →
  更正与审计（旧值可追）→ 改日期后旧期间被重算、腾空的期间连指标一起清 →
  冲销（原行保留 + 负数行 + 合计归零 + 指标跟着变）→ 冲销/更正的状态冲突 409 →
  参数校验 422 与 404 → 撤销整批后指标/期间一起清干净。
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

# 探针期间刻意选在演示数据之外：2026 那几个月是真实演示数据，不能被冒烟脚本搅动。
PROBE_PERIOD = "2096-07"
MOVED_PERIOD = "2096-06"
TAG = "TX9冒烟"
CSV_HEADER = "交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出"

RESULTS: list[tuple[bool, str, str]] = []


def _console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, str(detail)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""))
    return bool(ok)


def csv_bytes(suffix: str) -> bytes:
    rows = [
        f"{PROBE_PERIOD}-01,6001,{TAG}-{suffix}-A,018,探针A,500.00,0,0,0,0",
        f"{PROBE_PERIOD}-02,6002,{TAG}-{suffix}-B,097,探针B,0,0,200.00,0,0",
        f"{PROBE_PERIOD}-03,6003,{TAG}-{suffix}-C,,探针C,100.00,0,0,0,0",
    ]
    return ("\ufeff" + "\n".join([CSV_HEADER, *rows]) + "\n").encode("utf-8")


async def login(client: httpx.AsyncClient, username: str, password: str) -> bool:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if response.status_code != 200:
        return False
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    return True


async def make_scoped(client: httpx.AsyncClient, suffix: str, departments: list[str]) -> tuple[httpx.AsyncClient, int] | None:
    """注册一个临时账号并限定部门，返回它自己的 client（带 token）与 id。"""
    username = f"tx9_{suffix}"
    password = secrets.token_hex(8)
    # 注册不发 token（只回 id/username），必须再登录一次
    created = await client.post("/api/v1/auth/register", json={"username": username, "password": password})
    if created.status_code != 200:
        check(f"注册临时账号 {username}", False, f"HTTP {created.status_code} {created.text[:120]}")
        return None
    scoped = httpx.AsyncClient(base_url=client.base_url, timeout=120.0, trust_env=False)
    if not await login(scoped, username, password):
        check(f"登录临时账号 {username}", False, "登录失败")
        await scoped.aclose()
        return None
    patched = await scoped.patch("/api/v1/auth/me", json={"departments": departments})
    if patched.status_code != 200:
        check(f"限定临时账号部门 {departments}", False, f"HTTP {patched.status_code} {patched.text[:120]}")
        await scoped.aclose()
        return None
    return scoped, int(created.json()["id"])


async def wait_job(client: httpx.AsyncClient, job_id: int, deadline: float = 120.0) -> dict:
    spent = 0.0
    job: dict = {}
    while spent < deadline:
        response = await client.get(f"/api/v1/admin/jobs/{job_id}")
        if response.status_code == 200:
            job = response.json()
            if job.get("status") not in ("queued", "running"):
                return job
        await asyncio.sleep(0.5)
        spent += 0.5
    return job


async def revert_batch(client: httpx.AsyncClient, batch: str) -> dict:
    response = await client.post("/api/v1/admin/jobs/batch-revert", json={"batch": batch})
    # 202 = 任务已受理（后台线程里跑），不是错误
    if response.status_code not in (200, 202):
        return {"status": f"HTTP {response.status_code}", "error": response.text[:200]}
    return await wait_job(client, int(response.json()["id"]))


async def sweep(client: httpx.AsyncClient) -> int:
    """清掉上一次崩溃留下的探针批次：明细按关键字查得到批次号，再逐批撤销。"""
    response = await client.get("/api/v1/financial/transactions",
                               params={"query": TAG, "limit": 200})
    if response.status_code != 200:
        return 0
    batches = {row["import_batch"] for row in response.json().get("data", [])}
    for batch in batches:
        job = await revert_batch(client, batch)
        print(f"  清理遗留批次 {batch[:12]}… → {job.get('status')}")
    return len(batches)


async def rows_of(client: httpx.AsyncClient, **params) -> dict:
    response = await client.get("/api/v1/financial/transactions", params=params)
    return {"status": response.status_code, "body": response.json() if response.status_code == 200 else {},
            "text": response.text[:200]}


async def summary(client: httpx.AsyncClient, period: str) -> dict:
    response = await client.get("/api/v1/analysis/summary", params={"period": period})
    return response.json() if response.status_code == 200 else {}


async def run() -> int:
    _console()
    parser = argparse.ArgumentParser(description="真机验证明细更正与冲销")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="demo_analyst")
    parser.add_argument("--password", default="demo_password_2026")
    parser.add_argument("--keep", action="store_true", help="跑完保留探针数据（默认撤销整批）")
    args = parser.parse_args()

    suffix = secrets.token_hex(4)
    touched_accounts: list[int] = []
    batch = ""
    async with httpx.AsyncClient(base_url=args.base_url, timeout=180.0, trust_env=False) as client:
        if not await login(client, args.username, args.password):
            print("FAIL: 管理员登录失败", file=sys.stderr)
            return 3
        check("管理员登录成功", True, args.username)

        removed = await sweep(client)
        if removed:
            print(f"  先清掉 {removed} 个遗留探针批次")

        content = csv_bytes(suffix)
        preview = await client.post("/api/v1/financial/preview",
                                    files={"file": (f"tx9-{suffix}.csv", content, "text/csv")})
        body = preview.json() if preview.status_code == 200 else {}
        check("预览探针 CSV 通过", preview.status_code == 200 and body.get("valid") is True and body.get("total") == 3,
              f"HTTP {preview.status_code} total={body.get('total')} errors={body.get('error_count')}")

        imported = await client.post("/api/v1/financial/import",
                                     files={"file": (f"tx9-{suffix}.csv", content, "text/csv")})
        result = imported.json() if imported.status_code == 200 else {}
        batch = str(result.get("batch") or "")
        periods = [item.get("period") for item in (result.get("calculated") or [])]
        check("导入探针数据（三行：018 / 097 / 全公司）",
              imported.status_code == 200 and result.get("imported") == 3 and PROBE_PERIOD in periods,
              f"HTTP {imported.status_code} imported={result.get('imported')} batch={batch[:12]}… periods={periods}")

        listing = await rows_of(client, period=PROBE_PERIOD, batch=batch)
        data = listing["body"].get("data") or []
        by_dept = {row["department"]: row for row in data}
        check("列表按期间 + 批次能查到这三行", listing["body"].get("total") == 3 and len(data) == 3,
              f"total={listing['body'].get('total')}")
        check("列表带期间下拉选项与口径载荷",
              PROBE_PERIOD in (listing["body"].get("periods") or []) and listing["body"].get("scope", {}).get("unrestricted") is True,
              f"periods={len(listing['body'].get('periods') or [])} 个 scope={listing['body'].get('scope', {}).get('label')}")
        check("按关键字只查到探针 A",
              (await rows_of(client, query=f"{TAG}-{suffix}-A"))["body"].get("total") == 1)
        check("按部门 018 只查到部门那一行",
              (await rows_of(client, period=PROBE_PERIOD, department="018", batch=batch))["body"].get("total") == 1)

        row_a = by_dept.get("018") or {}
        row_b = by_dept.get("097") or {}
        row_c = by_dept.get("") or {}
        check("三行的部门解析正确（018 / 097 / 空 = 全公司）",
              bool(row_a) and bool(row_b) and bool(row_c),
              f"018={row_a.get('id')} 097={row_b.get('id')} 空={row_c.get('id')}")
        check("全公司行的部门落成空串（口径语义，不是丢字段）", row_c.get("department") == "", repr(row_c.get("department")))

        scoped = await make_scoped(client, suffix, ["018"])
        if scoped is None:
            return 4
        scoped_client, scoped_id = scoped
        touched_accounts.append(scoped_id)
        scoped_list = await rows_of(scoped_client, period=PROBE_PERIOD)
        scoped_rows = scoped_list["body"].get("data") or []
        check("受限账号只看得到自己部门的明细（并集口径）",
              scoped_list["body"].get("total") == 1 and scoped_rows and scoped_rows[0]["department"] == "018",
              f"total={scoped_list['body'].get('total')} 部门={[row['department'] for row in scoped_rows]}")
        blocked_list = await rows_of(scoped_client, period=PROBE_PERIOD, department="097")
        check("受限账号切到别的部门看明细被拒（403）", blocked_list["status"] == 403,
              f"HTTP {blocked_list['status']} {blocked_list['text'][:80]}")
        own_patch = await scoped_client.patch(f"/api/v1/financial/transactions/{row_a['id']}",
                                             json={"description": "受限账号改自己部门"})
        check("受限账号能更正自己部门的行", own_patch.status_code == 200 and own_patch.json()["data"]["corrections"] == 1,
              f"HTTP {own_patch.status_code} corrections={own_patch.json().get('data', {}).get('corrections') if own_patch.status_code == 200 else '-'}")
        other_patch = await scoped_client.patch(f"/api/v1/financial/transactions/{row_b['id']}",
                                               json={"description": "不该成功"})
        check("受限账号改别的部门的行被拒（403）", other_patch.status_code == 403,
              f"HTTP {other_patch.status_code} {other_patch.text[:80]}")
        company_patch = await scoped_client.patch(f"/api/v1/financial/transactions/{row_c['id']}",
                                                 json={"description": "全公司行不该动"})
        check("受限账号改全公司行被拒（403）", company_patch.status_code == 403,
              f"HTTP {company_patch.status_code} {company_patch.text[:80]}")

        fixed = await client.patch(f"/api/v1/financial/transactions/{row_a['id']}",
                                   json={"revenue_amount": "250.50", "description": "更正后"})
        fixed_body = fixed.json() if fixed.status_code == 200 else {}
        check("管理员更正金额成功", fixed.status_code == 200 and fixed_body.get("data", {}).get("revenue_amount") == 250.5,
              f"HTTP {fixed.status_code} revenue={fixed_body.get('data', {}).get('revenue_amount')}")
        check("更正后立刻重算这一期", fixed_body.get("recalculated") == [PROBE_PERIOD] and fixed_body.get("cleared") == [],
              f"recalculated={fixed_body.get('recalculated')} cleared={fixed_body.get('cleared')}")
        check("更正次数累加、更正与冲销是两个状态",
              fixed_body.get("data", {}).get("corrections") == 2 and fixed_body.get("data", {}).get("reversed") is False,
              f"corrections={fixed_body.get('data', {}).get('corrections')} reversed={fixed_body.get('data', {}).get('reversed')}")

        board = await summary(client, PROBE_PERIOD)
        check("期间合计跟着更正走（收入 250.50 + 100 = 350.50）",
              round(float(board.get("revenue_amount") or 0), 2) == 350.50,
              f"revenue={board.get('revenue_amount')} 费用={board.get('expense_amount')}")

        audit = (await client.get("/api/v1/admin/audit", params={"action": "transaction.update", "limit": 20})).json()
        entry = next((item for item in audit.get("data", []) if item.get("target_id") == str(row_a["id"])), None)
        detail = (entry or {}).get("detail") or {}
        check("审计里有旧值与新值（before/after）",
              bool(entry) and detail.get("before", {}).get("revenue_amount") == "500.00"
              and detail.get("after", {}).get("revenue_amount") == "250.50",
              f"before={detail.get('before', {}).get('revenue_amount')} after={detail.get('after', {}).get('revenue_amount')}")

        same = await client.patch(f"/api/v1/financial/transactions/{row_a['id']}", json={"revenue_amount": "250.50"})
        check("改动的值与原值一致 → 422", same.status_code == 422 and "一致" in same.text, f"HTTP {same.status_code} {same.text[:60]}")
        empty = await client.patch(f"/api/v1/financial/transactions/{row_a['id']}", json={})
        check("没有要更正的字段 → 422", empty.status_code == 422 and "更正" in empty.text, f"HTTP {empty.status_code} {empty.text[:60]}")
        unknown = await client.patch(f"/api/v1/financial/transactions/{row_a['id']}", json={"nope": 1})
        # 未知字段被 pydantic 的 extra="forbid" 拦在进入业务代码之前，报文是校验错误数组
        check("不认识的字段 → 422", unknown.status_code == 422 and ("不认识" in unknown.text or "extra_forbidden" in unknown.text),
              f"HTTP {unknown.status_code} {unknown.text[:80]}")
        bad_amount = await client.patch(f"/api/v1/financial/transactions/{row_a['id']}", json={"revenue_amount": "abc"})
        check("非法金额 → 422", bad_amount.status_code == 422, f"HTTP {bad_amount.status_code} {bad_amount.text[:60]}")
        tz_date = await client.patch(f"/api/v1/financial/transactions/{row_a['id']}",
                                     json={"transaction_date": f"{PROBE_PERIOD}-01T00:00:00+08:00"})
        check("带时区的日期 → 422（与导入同一条规则）", tz_date.status_code == 422 and "时区" in tz_date.text,
              f"HTTP {tz_date.status_code} {tz_date.text[:60]}")
        missing = await client.get("/api/v1/financial/transactions/999999999")
        check("不存在的行读 → 404", missing.status_code == 404, f"HTTP {missing.status_code}")
        missing_patch = await client.patch("/api/v1/financial/transactions/999999999", json={"description": "x"})
        check("不存在的行更正 → 404", missing_patch.status_code == 404, f"HTTP {missing_patch.status_code}")

        moved = await client.patch(f"/api/v1/financial/transactions/{row_a['id']}",
                                   json={"transaction_date": f"{MOVED_PERIOD}-28"})
        moved_body = moved.json() if moved.status_code == 200 else {}
        # 两个期间都要重算：新期间多了这一行，旧期间的合计少了一行，只算一个都会留下假数
        check("改日期把这一行挪到新期间并重算新旧两个期间",
              moved_body.get("data", {}).get("period") == MOVED_PERIOD
              and sorted(moved_body.get("recalculated") or []) == sorted([MOVED_PERIOD, PROBE_PERIOD])
              and moved_body.get("cleared") == [],
              f"period={moved_body.get('data', {}).get('period')} recalculated={moved_body.get('recalculated')}")
        moved_board = await summary(client, MOVED_PERIOD)
        check("新期间合计只含这一行（250.50）",
              round(float(moved_board.get("revenue_amount") or 0), 2) == 250.50
              and moved_board.get("has_data") is True,
              f"revenue={moved_board.get('revenue_amount')} has_data={moved_board.get('has_data')}")

        back = await client.patch(f"/api/v1/financial/transactions/{row_a['id']}",
                                  json={"transaction_date": f"{PROBE_PERIOD}-01"})
        back_body = back.json() if back.status_code == 200 else {}
        check("挪回去以后腾空的旧期间被清掉（cleared 点名）",
              back_body.get("recalculated") == [PROBE_PERIOD] and back_body.get("cleared") == [MOVED_PERIOD],
              f"recalculated={back_body.get('recalculated')} cleared={back_body.get('cleared')}")
        empty_board = await summary(client, MOVED_PERIOD)
        check("腾空的期间不再有合计（has_data=False）", empty_board.get("has_data") is False,
              f"has_data={empty_board.get('has_data')} revenue={empty_board.get('revenue_amount')}")

        before_reverse_board = await summary(client, PROBE_PERIOD)
        # 冲销之前：收入 350.50、费用 200 → net = 150.50（summary 不单列费用，用净利反推）
        check("冲销前这一期净利 150.50（收入 350.50 - 费用 200）",
              round(float(before_reverse_board.get("net_profit_amount") or 0), 2) == 150.50,
              f"net={before_reverse_board.get('net_profit_amount')} revenue={before_reverse_board.get('revenue_amount')}")

        reversed_row = await client.post(f"/api/v1/financial/transactions/{row_b['id']}/reverse",
                                        json={"reason": "探针冲销"})
        reverse_body = reversed_row.json() if reversed_row.status_code == 200 else {}
        original = reverse_body.get("original") or {}
        reversal = reverse_body.get("reversal") or {}
        check("冲销：原行保留并标记 reversed",
              reversed_row.status_code == 200 and original.get("id") == row_b["id"] and original.get("reversed") is True
              and bool(original.get("reversed_at")),
              f"HTTP {reversed_row.status_code} reversed={original.get('reversed')} at={original.get('reversed_at')}")
        check("冲销：新增一条金额取负的冲销行",
              reversal.get("is_reversal") is True and float(reversal.get("expense_amount") or 0) == -200.0
              and reversal.get("reversal_of_id") == row_b["id"],
              f"expense={reversal.get('expense_amount')} reversal_of_id={reversal.get('reversal_of_id')}")
        check("冲销行的批次沿用原批次（撤销批次才收得回两行）",
              reversal.get("import_batch") == row_b.get("import_batch") and reversal.get("import_batch") == batch,
              f"冲销行批次={str(reversal.get('import_batch'))[:12]}…")
        after_rows = await rows_of(client, period=PROBE_PERIOD, batch=batch)
        expense_sum = round(sum(float(item["expense_amount"] or 0) for item in after_rows["body"].get("data") or []), 2)
        after_board = await summary(client, PROBE_PERIOD)
        check("冲销后这一期费用归零（-200 与原 200 相加）且净利回到 350.50",
              expense_sum == 0.0 and round(float(after_board.get("net_profit_amount") or 0), 2) == 350.50,
              f"行内费用合计={expense_sum} net={after_board.get('net_profit_amount')}")
        check("列表里冲销行与原行都在（一共 4 行）",
              after_rows["body"].get("total") == 4,
              f"total={after_rows['body'].get('total')}")
        twice = await client.post(f"/api/v1/financial/transactions/{row_b['id']}/reverse", json={})
        check("重复冲销 → 409", twice.status_code == 409, f"HTTP {twice.status_code} {twice.text[:80]}")
        reverse_reversal = await client.post(f"/api/v1/financial/transactions/{reversal['id']}/reverse", json={})
        check("冲销冲销行 → 409", reverse_reversal.status_code == 409, f"HTTP {reverse_reversal.status_code} {reverse_reversal.text[:80]}")
        fix_reversed = await client.patch(f"/api/v1/financial/transactions/{row_b['id']}", json={"description": "改已冲销的行"})
        check("更正已冲销的行 → 409", fix_reversed.status_code == 409, f"HTTP {fix_reversed.status_code} {fix_reversed.text[:80]}")
        reverse_audit = (await client.get("/api/v1/admin/audit", params={"action": "transaction.reverse", "limit": 20})).json()
        entry = next((item for item in reverse_audit.get("data", []) if item.get("target_id") == str(row_b["id"])), None)
        check("审计里冲销带冲销行 id 与原因",
              bool(entry) and (entry.get("detail") or {}).get("reversal_id") == reversal.get("id")
              and (entry.get("detail") or {}).get("reason") == "探针冲销",
              f"detail={((entry or {}).get('detail') or {})}")

        if scoped:
            await scoped_client.aclose()

        if args.keep:
            print(f"  保留探针数据（--keep）：batch={batch}")
        else:
            job = await revert_batch(client, batch)
            check("撤销整批探针数据（后台任务成功）", job.get("status") == "succeeded",
                  f"status={job.get('status')} error={job.get('error')} 重算={job.get('result')}")
            left = await rows_of(client, batch=batch)
            check("撤销后该批次 0 行", left["body"].get("total") in (0, None),
                  f"total={left['body'].get('total')}")
            gone = await summary(client, PROBE_PERIOD)
            check("撤销后探针期间没有合计（指标与落库一起清）", gone.get("has_data") is False,
                  f"has_data={gone.get('has_data')} revenue={gone.get('revenue_amount')}")
            moved_gone = await summary(client, MOVED_PERIOD)
            check("被挪动过的期间也清干净了", moved_gone.get("has_data") is False,
                  f"has_data={moved_gone.get('has_data')}")

        for account_id in touched_accounts:
            response = await client.delete(f"/api/v1/admin/users/{account_id}")
            check(f"删除临时账号 #{account_id}", response.status_code == 200, f"HTTP {response.status_code}")

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print(f"\n共 {len(RESULTS)} 项，PASS {passed}，FAIL {len(RESULTS) - passed}")
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name}  {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
