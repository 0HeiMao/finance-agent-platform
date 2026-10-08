"""② 并发重算互斥（期间锁）—— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_recalc_lock.py

为什么必须有这个脚本：单元/接口用例只能证明「接口发出了 `pg_advisory_xact_lock`」，
证明不了「两个真连接真的会互相挡住」——那要靠真库。这里用**进程外的一条真连接**持住
某个月份的期间锁，然后真发 HTTP 请求（重算 / 导入 / 更正）：接口必须被挡住（比无竞争的
基线明显变长），说明它抢的正是同一把锁；释放后请求立刻完成，而且该期间的指标仍然等于
明细之和——即没有丢更新。

顺带验「不同期间不该互相排队」：锁必须是**按期间**的，不是一把全局大锁。
注意一个刻意的取舍：同一个**年**下的不同月份会共享年锁（月重算要刷新年度落库行），
所以「互不阻塞」要用不同年份来验（2097 与 2098）。

测试期间用 2097 年（演示数据是 2026 年），脚本开头与结尾都把 2097 的
明细/指标/预警/落库行删干净，不污染演示数据，可以反复跑。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from backend import locks  # noqa: E402
from backend.db import engine  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PERIOD = "2097-12"          # 测试用月份（含它的季 2097-Q4 与年 2097）
OTHER_YEAR = "2098-05"      # 另一年的月份：不该被 2097 的锁挡住
CSV_HEADER = "交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出"
HOLD_CALC = 2.0             # 持锁时长：要明显大于接口本身的耗时，才看得出「在等锁」
HOLD_SHORT = 1.5

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
    raw = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def upload(path: str, filename: str, content: bytes, token: str):
    """带鉴权的 multipart 上传（导入接口要的是 form-data 里的 `file`）。"""
    boundary = "----verifyrecalclock"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").encode("utf-8")
    body = head + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(f"{BASE}{path}", data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=180) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    raw = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def scalar(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def execute(sql: str, **params) -> int:
    with engine.begin() as conn:
        return conn.execute(text(sql), params).rowcount or 0


def login(username: str, password: str) -> str:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def advisory_locks() -> int:
    """当前库里的事务级 advisory 锁条数。用来验「释放干净」——残留的锁会让后续请求莫名排队。"""
    return int(scalar("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") or 0)


def purge() -> None:
    """清掉 2097 年的测试明细与派生数据（幂等，跑前跑后各来一次）。"""
    execute("DELETE FROM financial_transactions WHERE transaction_date >= '2097-01-01' "
            "AND transaction_date < '2098-01-01'")
    execute("DELETE FROM financial_metrics WHERE period LIKE '2097%'")
    execute("DELETE FROM financial_alerts WHERE period LIKE '2097%'")
    execute("DELETE FROM financial_rollups WHERE period LIKE '2097%'")


def seed_details() -> None:
    """直接往 2097-12 插 3 行明细（走 SQL 最快，且脚本要验的是锁不是导入校验）。"""
    for day, revenue in ((5, "100.00"), (6, "200.00"), (7, "300.00")):
        execute(
            "INSERT INTO financial_transactions (transaction_date, account_code, account_name, "
            "department, description, revenue_amount, cost_amount, expense_amount, cash_in_amount, "
            "cash_out_amount, import_batch, corrections, created_at) VALUES "
            f"('2097-12-{day:02d}', '6001', '锁测试科目', '部门-097', '期间锁冒烟', {revenue}, 0, 0, 0, 0, "
            "'verify-lock', 0, now())")


def period_revenue_details(period: str = PERIOD):
    """该期明细的收入合计（直接读明细，作为「指标算对了没有」的参照）。"""
    year, month = period.split("-")
    lo = f"{period}-01"
    hi = (f"{int(year) + 1}-01-01" if month == "12" else f"{year}-{int(month) + 1:02d}-01")
    return scalar("SELECT coalesce(sum(revenue_amount), 0) FROM financial_transactions "
                  "WHERE transaction_date >= :lo AND transaction_date < :hi", lo=lo, hi=hi)


def period_revenue_metric(period: str = PERIOD):
    return scalar("SELECT revenue_amount FROM financial_metrics WHERE period = :p "
                  "AND department IS NULL", p=period)


def money(value) -> str:
    return "0.00" if value is None else f"{value:.2f}"


class Holder:
    """在一条**独立真连接**上持有一组期间锁，直到 release()。

    这就是「另一个正在重算同一期的请求」：它和 uvicorn 进程里的请求抢同一把事务级
    advisory 锁（锁是数据库级的，跨进程有效），事务一结束锁自动释放。
    """

    def __init__(self, periods):
        self.conn = engine.connect()
        self.locked = locks.lock_periods(self.conn, periods)

    def release(self, commit: bool = True) -> None:
        try:
            if commit:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


def hold_in_thread(periods, seconds: float):
    """后台线程持锁 seconds 秒；返回 (ready_event, thread)。"""
    ready = threading.Event()
    errors: list[str] = []

    def run():
        try:
            holder = Holder(periods)
        except Exception as exc:  # noqa: BLE001 - 拿不到锁就直接失败
            errors.append(f"{type(exc).__name__}: {exc}")
            ready.set()
            return
        ready.set()
        time.sleep(seconds)
        holder.release()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5), "抱锁线程没起来"
    assert not errors, errors
    return thread


def timed_calculate(token: str) -> tuple[int, object, int]:
    started = time.monotonic()
    status, payload = call("POST", f"/analysis/calculate?period={PERIOD}", token)
    return status, payload, round((time.monotonic() - started) * 1000)


def main() -> int:
    token = login(*ADMIN)

    # ---------------------------------------------------------- 结构闸门（真模块）
    keys = {name: locks.lock_key(name) for name in
            ("2026", "2026-Q2", "2026-04", "2026-05", "2026-06", "2026-12", "2027", "2098-05")}
    check("锁 key 稳定且不同期间不撞号，都在 bigint 范围内",
          len(set(keys.values())) == len(keys) and locks.lock_key("2097-12") == locks.lock_key("2097-12")
          and all(-(2 ** 63) <= value < 2 ** 63 for value in keys.values()),
          f"命名空间 {locks.NAMESPACE}，{len(keys)} 个期间算出 {len(set(keys.values()))} 个不同 key")
    check("锁的期间全集含所在的季与年，且按名字排序",
          locks.period_lock_set([PERIOD]) == ["2097", PERIOD, "2097-Q4"]
          and locks.period_lock_set([PERIOD, "2097-11"]) == ["2097", "2097-11", PERIOD, "2097-Q4"],
          f"{PERIOD} → {locks.period_lock_set([PERIOD])}")

    purge()
    time.sleep(0.3)
    check("起始状态干净：2097 年没有测试数据、库里没有残留 advisory 锁",
          int(scalar("SELECT count(*) FROM financial_transactions WHERE transaction_date >= '2097-01-01' "
                     "AND transaction_date < '2098-01-01'") or 0) == 0 and advisory_locks() == 0,
          f"advisory 锁 {advisory_locks()} 条")

    # ---------------------------------------------------------- 两个真连接互相挡（不经过 HTTP）
    holder = Holder([PERIOD])
    try:
        message = ""
        blocked = False
        with engine.connect() as other:
            other.execute(text("SET LOCAL lock_timeout = '400ms'"))
            try:
                locks.lock_periods(other, [PERIOD])
            except Exception as exc:  # noqa: BLE001 - 期望的就是超时
                blocked = True
                message = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:150]
        check("同一期间的事务锁真的会互相挡住", blocked and "lock timeout" in message.lower(),
              message or "没被挡住：锁根本没生效")

        # 另一年的月份不该排队：锁是按期间的，不是一把全局大锁
        started = time.monotonic()
        with engine.connect() as free:
            free.execute(text("SET LOCAL lock_timeout = '400ms'"))
            taken = locks.lock_periods(free, [OTHER_YEAR])
            free.rollback()
        elapsed_ms = round((time.monotonic() - started) * 1000)
        check("另一年的月份不被挡住（锁按期间，不是全局大锁）",
              taken == locks.period_lock_set([OTHER_YEAR]) and elapsed_ms < 400,
              f"{taken}，耗时 {elapsed_ms} ms")
        # 同一年、不同季的两个月仍然共享年锁（月重算要刷新年度落库行，属于刻意取舍）；
        # 不同年份之间则一把锁都不共用。这里用 2097-08（Q3）而不是 2097-11：11 月与 12 月
        # 同在 Q4，交集里除了年还会多出季，说不清「共享的是年锁」还是「共享的是季锁」。
        same_year = set(locks.period_lock_set([PERIOD])) & set(locks.period_lock_set(["2097-08"]))
        other_year = set(locks.period_lock_set([PERIOD])) & set(locks.period_lock_set(["2098-08"]))
        check("同一年、不同季的两个月仍共享年锁；不同年份没有任何共同锁",
              same_year == {"2097"} and other_year == set(),
              f"{locks.period_lock_set([PERIOD])} ∩ {locks.period_lock_set(['2097-08'])} = "
              f"{sorted(same_year)}；与 2098 年交集 {sorted(other_year)}")
    finally:
        holder.release()

    with engine.connect() as after_commit:
        after_commit.execute(text("SET LOCAL lock_timeout = '400ms'"))
        taken_after_commit = locks.lock_periods(after_commit, [PERIOD])
        after_commit.rollback()
    check("持锁方提交后，锁自动释放（事务级锁）", taken_after_commit == locks.period_lock_set([PERIOD]),
          f"{taken_after_commit}")

    holder = Holder([PERIOD])
    holder.release(commit=False)
    with engine.connect() as after_rollback:
        after_rollback.execute(text("SET LOCAL lock_timeout = '400ms'"))
        taken_after_rollback = locks.lock_periods(after_rollback, [PERIOD])
        after_rollback.rollback()
    check("持锁方回滚后，锁同样释放（重算失败不会把这一期永久锁死）",
          taken_after_rollback == locks.period_lock_set([PERIOD]), f"{taken_after_rollback}")

    # ---------------------------------------------------------- 端到端：真接口抢同一把锁
    seed_details()
    status, payload, baseline_ms = timed_calculate(token)
    check("无竞争时重算接口正常", status == 200 and money(period_revenue_metric()) == "600.00",
          f"status={status}，耗时 {baseline_ms} ms，指标收入 {money(period_revenue_metric())}")

    thread = hold_in_thread([PERIOD], HOLD_CALC)
    status, payload, contended_ms = timed_calculate(token)
    thread.join(6)
    wait_ms = contended_ms - baseline_ms
    check("有人持锁时，重算接口被挡住（说明它抢的是同一把锁）",
          status == 200 and wait_ms >= 1000,
          f"有竞争 {contended_ms} ms vs 无竞争 {baseline_ms} ms，多等 {wait_ms} ms（持锁 {int(HOLD_CALC * 1000)} ms）")
    check("挡住之后仍然算对：期间指标等于明细之和",
          money(period_revenue_metric()) == money(period_revenue_details())
          and money(period_revenue_details()) == "600.00",
          f"指标 {money(period_revenue_metric())} vs 明细 {money(period_revenue_details())}")
    check("请求结束后没有残留的 advisory 锁", advisory_locks() == 0, f"残留 {advisory_locks()} 条")

    # ---------------------------------------------------------- 导入路径也抢期间锁
    csv = ("\n".join([CSV_HEADER, f"2097-12-09,6001,导入锁测试,部门-097,期间锁冒烟,400,0,0,0,0"]) + "\n"
           ).encode("utf-8-sig")
    thread = hold_in_thread([PERIOD], HOLD_SHORT)
    started = time.monotonic()
    status, imported = upload("/financial/import", "verify-lock.csv", csv, token)
    import_ms = round((time.monotonic() - started) * 1000)
    thread.join(6)
    check("有人持锁时，导入接口也被挡住（导入会改这一期的行集合）",
          status == 200 and import_ms >= 1000,
          f"status={status}，耗时 {import_ms} ms（持锁 {int(HOLD_SHORT * 1000)} ms）")
    check("导入成功后指标跟着变成 1000.00（明细 600 + 新导入 400）",
          money(period_revenue_metric()) == "1000.00" and money(period_revenue_details()) == "1000.00",
          f"指标 {money(period_revenue_metric())} vs 明细 {money(period_revenue_details())}")

    # ---------------------------------------------------------- 更正路径：改行之前就先持锁
    row_id = scalar("SELECT id FROM financial_transactions WHERE transaction_date >= '2097-12-01' "
                    "AND transaction_date < '2098-01-01' ORDER BY id LIMIT 1")
    thread = hold_in_thread([PERIOD], HOLD_SHORT)
    started = time.monotonic()
    status, patched = call("PATCH", f"/financial/transactions/{row_id}", token,
                           {"revenue_amount": "111.00"})
    patch_ms = round((time.monotonic() - started) * 1000)
    thread.join(6)
    check("有人持锁时，单行更正也被挡住（改行之前就先持锁）",
          status == 200 and patch_ms >= 1000,
          f"status={status}，耗时 {patch_ms} ms（持锁 {int(HOLD_SHORT * 1000)} ms）")

    # ---------------------------------------------------------- 并发重算：不丢更新
    results: list[tuple[int, int]] = []

    def concurrent_calculate() -> None:
        status, _payload, elapsed = timed_calculate(token)
        results.append((status, elapsed))

    threads = [threading.Thread(target=concurrent_calculate, daemon=True) for _ in range(2)]
    started = time.monotonic()
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join(60)
    total_ms = round((time.monotonic() - started) * 1000)
    check("两个并发重算同一期都成功（锁把它们排成队，没人拿到 500）",
          len(results) == 2 and all(status == 200 for status, _ms in results),
          f"结果 {results}，总耗时 {total_ms} ms")
    check("并发重算之后指标仍然等于明细之和（没有丢更新）",
          money(period_revenue_metric()) == money(period_revenue_details()),
          f"指标 {money(period_revenue_metric())} vs 明细 {money(period_revenue_details())}")

    # ---------------------------------------------------------- 收尾：清干净
    purge()
    left_details = int(scalar("SELECT count(*) FROM financial_transactions "
                              "WHERE transaction_date >= '2097-01-01' "
                              "AND transaction_date < '2098-01-01'") or 0)
    left_metrics = int(scalar("SELECT count(*) FROM financial_metrics "
                              "WHERE period LIKE '2097%'") or 0)
    left_locks = advisory_locks()
    check("跑完把 2097 的测试数据清干净，且没有残留 advisory 锁",
          left_details == 0 and left_metrics == 0 and left_locks == 0,
          f"明细 {left_details} 行，指标 {left_metrics} 行，advisory 锁 {left_locks} 条")

    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 脚本要一句话说清哪儿炸了
        print(f"\n异常终止：{type(exc).__name__}: {exc}", flush=True)
        print("检查后端是否在 127.0.0.1:8000、数据库容器是否在跑", flush=True)
        sys.exit(1)
