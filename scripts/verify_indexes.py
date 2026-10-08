"""① 明细读路径索引 —— 真机体检。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_indexes.py

验四件事：

1. **结构闸门**：`migrations.missing_indexes()` 在真库上是空的，三条索引都能在 `pg_indexes` 里查到，
   且复合索引的列顺序是 `(department, transaction_date)` —— 顺序反了第二列就用不上。
2. **读路径还在**：真登录受限账号口径，按「部门 + 期间」拉明细仍然 200 且有数据（补索引不该改变结果），
   管理概览也照样 200。
3. **计划形状**：把顺序扫关掉（`enable_seqscan = off`）看规划器有没有可用的索引 ——
   「部门 + 期间」要能选上 `ix_financial_transactions_department_date`，冲销来源与预警状态各自选上自己的索引；
   反过来，**旧的 `to_char(transaction_date,'YYYY-MM') = '...'` 形状选不上它**，
   这就是把期间过滤从「函数套在列上」改成区间比较的原因（留证据，别靠记忆）。
4. **真掏钱**：临时造一张 80 万行的同构表，先测无索引的 `Seq Scan` 耗时，建完复合索引再测一次，
   打印倍数。演示库只有 8909 行，规划器在小表上本来就爱顺序扫，不造大表证明不了什么。
   接着用**结果集完全相同**的一对写法（整月区间 vs `to_char(...)='YYYY-MM'`）比一遍：
   行数必须一模一样（证明改谓词没改语义），但区间版能靠 `Index Cond` 定位、
   `to_char` 版只能拿部门定位、日期退化成逐行 `Filter`。几毫秒的对比容易被噪声盖住，
   每个形状连测 3 次取最快的一次。
   临时表随连接关闭自动消失（`CREATE TEMP TABLE`），不动任何业务数据。

脚本只读业务数据（唯一写入是那张临时表），可反复跑。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from backend.db import engine  # noqa: E402
from backend.migrations import INDEXES, missing_indexes  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
COMPOSITE = "ix_financial_transactions_department_date"
REVERSAL = "ix_financial_transactions_reversal_of_id"
REVIEW = "ix_financial_alerts_review_status"
SCRATCH_ROWS = 800_000

RESULTS: list[tuple[str, bool, str]] = []


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=120) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    text_body = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(text_body)
    except json.JSONDecodeError:
        return status, text_body


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def login(username: str, password: str) -> str:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def plan_of(connection, sql: str, *, seqscan: bool = True) -> tuple[str, float]:
    """跑一次 `EXPLAIN (ANALYZE)`，返回 (节点 / 索引名 / 过滤条件拼成的串, 执行毫秒)。

    把 `Filter` 与 `Index Cond` 也带上：这正是要区分的东西 ——
    「索引定位（Index Cond）」和「取回来逐行算（Filter）」的差别就是可搜索性的差别。
    """
    if not seqscan:
        connection.execute(text("SET LOCAL enable_seqscan = off"))
    raw = connection.execute(text(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}")).scalar()
    # psycopg 会把 json 列直接解析成 list（不是 str），两种都兜住。
    parsed = raw if isinstance(raw, list) else json.loads(raw)
    plan = parsed[0]
    keys = ("Node Type", "Index Name", "Index Cond", "Recheck Cond", "Filter")
    parts: list[str] = []
    stack = [plan["Plan"]]
    while stack:
        node = stack.pop()
        parts.extend(f"{key}={node[key]}" for key in keys if node.get(key) is not None)
        stack.extend(node.get("Plans") or [])
    return " / ".join(parts), float(plan["Execution Time"])


def best_of(connection, sql: str, rounds: int = 3) -> tuple[str, float]:
    """同一形状连续测 `rounds` 次取最快的一次（单次几毫秒的对比很容易被噪声盖过去）。"""
    nodes, best = "", float("inf")
    for _ in range(rounds):
        nodes, ms = plan_of(connection, sql)
        best = min(best, ms)
    return nodes, best


def sanity() -> None:
    """结构闸门 + 真库目录（不碰数据，直接在库上查）。"""
    check("`missing_indexes()` 在真库上为空（启动时不会再报缺索引）", missing_indexes() == [],
          f"缺失：{missing_indexes()}")
    with engine.connect() as connection:
        definitions = dict(connection.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")).all())
    composite = definitions.get(COMPOSITE, "")
    check("复合索引在真库里", bool(composite), composite or f"{COMPOSITE} 不存在")
    check("复合索引列顺序是 (department, transaction_date)",
          bool(composite) and composite.index("department") < composite.index("transaction_date"),
          composite)
    check("冲销来源索引在真库里", REVERSAL in definitions, definitions.get(REVERSAL, ""))
    check("预警状态索引在真库里", REVIEW in definitions, definitions.get(REVIEW, ""))
    check("手工补索引清单与真库对得上",
          {name for _table, name, _columns, _why in INDEXES} <= set(definitions),
          f"清单：{[name for _t, name, _c, _w in INDEXES]}")


def live_api() -> None:
    """真机读路径：补索引不该改变结果，只该让它更快。"""
    token = login(*ADMIN)
    status, body = call("GET", "/financial/transactions?limit=5", token)
    periods = (body or {}).get("periods") or [] if isinstance(body, dict) else []
    check("明细接口仍然可用（返回期间清单）", status == 200 and bool(periods),
          f"status={status} periods={periods[:3]}")
    if not periods:
        return
    period = periods[0]
    status, body = call("GET", f"/financial/transactions?period={period}&limit=5", token)
    total = (body or {}).get("total") if isinstance(body, dict) else None
    check("按期间筛明细仍有数据（区间比较与旧口径同结果）",
          status == 200 and isinstance(total, int) and total > 0,
          f"period={period} status={status} total={total}")
    check("列表返回的行带着部门字段（部门口径过滤依赖它）",
          status == 200 and all("department" in row for row in (body or {}).get("data") or []),
          f"{(body or {}).get('data', [{}])[0].get('department') if (body or {}).get('data') else '无行'}")
    status, overview = call("GET", "/admin/overview", token)
    check("管理概览仍然 200（结构变化没打断管理页）", status == 200 and isinstance(overview, dict),
          f"status={status}")


def plan_shapes() -> None:
    """计划形状：对得上谓词的索引用得上，函数套在列上的用不上。"""
    with engine.connect() as connection:
        departments = [row[0] for row in connection.execute(text(
            "SELECT department FROM financial_transactions WHERE department IS NOT NULL"
            " GROUP BY department ORDER BY count(*) DESC LIMIT 1")).all()]
        department = departments[0] if departments else "001"

        nodes, ms = plan_of(
            connection,
            "SELECT count(*) FROM financial_transactions"
            f" WHERE department = '{department}'"
            " AND transaction_date >= DATE '2026-01-01' AND transaction_date < DATE '2027-01-01'",
            seqscan=False)
        check("「部门 + 期间」能用上复合索引",
              COMPOSITE in nodes, f"{ms:.2f} ms | {nodes}")

        nodes, ms = plan_of(
            connection,
            "SELECT count(*) FROM financial_transactions"
            " WHERE to_char(transaction_date, 'YYYY-MM') = '2026-10'",
            seqscan=False)
        # 它确实会扫单列的日期索引（整条索引都得过一遍），但用不上复合索引的第二列做定位：
        # 日期上套了函数，索引就没有「范围」可以用。
        check("旧的 to_char 形状用不上复合索引（函数套在列上没有范围可定位）",
              COMPOSITE not in nodes, f"{ms:.2f} ms | {nodes}")

        nodes, ms = plan_of(
            connection,
            "SELECT count(*) FROM financial_transactions WHERE reversal_of_id IS NOT NULL",
            seqscan=False)
        check("冲销来源查询能用上冲销索引", REVERSAL in nodes, f"{ms:.2f} ms | {nodes}")

        nodes, ms = plan_of(
            connection,
            "SELECT count(*) FROM financial_alerts WHERE review_status = 'pending'",
            seqscan=False)
        check("预警按状态筛能用上状态索引", REVIEW in nodes, f"{ms:.2f} ms | {nodes}")


def scratch_benchmark() -> None:
    """80 万行同构临时表：无索引 vs 有复合索引，打印真实倍数。"""
    with engine.connect() as connection:
        connection.execute(text(
            "CREATE TEMP TABLE scratch_index_probe ("
            " id serial PRIMARY KEY, department text, transaction_date timestamp,"
            " revenue_amount numeric(18, 2))"))
        connection.execute(text(
            "INSERT INTO scratch_index_probe (department, transaction_date, revenue_amount)"
            " SELECT 'D' || lpad((g % 50)::text, 2, '0'),"
            "        timestamp '2026-01-01' + ((g % 365) * INTERVAL '1 day'),"
            "        (g % 1000)::numeric"
            f" FROM generate_series(1, {SCRATCH_ROWS}) AS g"))
        connection.execute(text("ANALYZE scratch_index_probe"))
        rows = connection.execute(text("SELECT count(*) FROM scratch_index_probe")).scalar()
        check(f"临时表造出 {SCRATCH_ROWS} 行同构数据", rows == SCRATCH_ROWS, f"实际 {rows} 行")

        day_query = ("SELECT count(*) FROM scratch_index_probe WHERE department = 'D07'"
                     " AND transaction_date >= TIMESTAMP '2026-03-01'"
                     " AND transaction_date < TIMESTAMP '2026-03-02'")
        nodes, before_ms = best_of(connection, day_query)
        check("没索引时是顺序扫（全表）", "Seq Scan" in nodes, f"{before_ms:.2f} ms | {nodes}")

        connection.execute(text(
            f"CREATE INDEX scratch_index_probe_dept_date ON scratch_index_probe (department, transaction_date)"))
        connection.execute(text("ANALYZE scratch_index_probe"))
        nodes, after_ms = best_of(connection, day_query)
        check("建完复合索引就用上它", "scratch_index_probe_dept_date" in nodes, f"{after_ms:.2f} ms | {nodes}")
        ratio = before_ms / after_ms if after_ms else 0.0
        check("同一条查询真的更快（倍数 > 2）", ratio > 2.0,
              f"{before_ms:.2f} ms → {after_ms:.2f} ms（{ratio:.1f}×，{SCRATCH_ROWS} 行）")

        # 结果集完全相同的一对写法：只有「日期怎么表达」不同。行数必须一模一样 ——
        # 这是「改谓词是为了让索引用得上，不是为了改语义」的证据。
        month_range = ("SELECT count(*) FROM scratch_index_probe WHERE department = 'D07'"
                       " AND transaction_date >= TIMESTAMP '2026-03-01'"
                       " AND transaction_date < TIMESTAMP '2026-04-01'")
        month_to_char = ("SELECT count(*) FROM scratch_index_probe WHERE department = 'D07'"
                         " AND to_char(transaction_date, 'YYYY-MM') = '2026-03'")
        rows_range = connection.execute(text(month_range)).scalar()
        rows_to_char = connection.execute(text(month_to_char)).scalar()
        check("两种写法结果集完全相同（改谓词没有改语义）",
              rows_range == rows_to_char and bool(rows_range),
              f"区间 {rows_range} 行 / to_char {rows_to_char} 行")

        range_nodes, range_ms = best_of(connection, month_range)
        check("区间比较的 Index Cond 里带着日期（第二列真的用上了）",
              "Index Cond" in range_nodes and "transaction_date" in range_nodes,
              f"{range_ms:.2f} ms | {range_nodes}")

        char_nodes, char_ms = best_of(connection, month_to_char)
        check("to_char 形状只能靠部门定位，日期退化成逐行 Filter",
              "Index Cond" in char_nodes and "to_char" in char_nodes,
              f"{char_ms:.2f} ms | {char_nodes}")

        char_ratio = char_ms / range_ms if range_ms else 0.0
        check("同一批行两种写法：区间比较更快（倍数 > 1.5）", char_ratio > 1.5,
              f"{char_ms:.2f} ms → {range_ms:.2f} ms（{char_ratio:.1f}×，同 {rows_range} 行）")

        existed = connection.execute(text(
            "SELECT to_regclass('scratch_index_probe') IS NOT NULL")).scalar()
        connection.execute(text("DROP TABLE scratch_index_probe"))
        dropped = connection.execute(text(
            "SELECT to_regclass('scratch_index_probe') IS NULL")).scalar()
        check("临时表已清掉（不留生产库垃圾）", bool(existed) and bool(dropped),
              f"建过={existed} 删掉后查不到={dropped}")


def main() -> int:
    print(f"真库：{engine.url.render_as_string(hide_password=True)}", flush=True)
    sanity()
    live_api()
    plan_shapes()
    scratch_benchmark()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 脚本要一句话说清哪儿炸了
        print(f"\n异常终止：{type(exc).__name__}: {exc}", flush=True)
        print("检查后端是否在 127.0.0.1:8000、数据库是否可用", flush=True)
        sys.exit(1)
