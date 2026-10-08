"""用真实 HTTP 接口把适配后的爬取文件导入正在运行的后端（供前端浏览演示）。

与 verify_*.py 的区别：本脚本不接管数据库、不建临时事务、不写测试用户，
它就是把文件通过 /api/v1/financial/preview + /import 交给真实后端，数据留在库里。

用法：
    python scripts/import_via_api.py --username demo_analyst --password demo_password_2026
    python scripts/import_via_api.py --csv data/processed/accounting_transactions_import.csv --base-url http://127.0.0.1:8000
    python scripts/import_via_api.py --remove     # 撤掉这份文件的批次，给回归脚本一个干净库

`--remove` 存在的原因：verify_crawled_import.py 断言「按期间汇总 == 源文件汇总」，
库里已经有同一份演示数据时汇总会翻倍、断言按设计失败。撤数据后重跑即可，跑完再导回来。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

DEFAULT_CSV = PROJECT_ROOT / "data" / "processed" / "accounting_transactions_import.csv"


def _console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def remove_batch(csv_path: Path) -> int:
    """按 sha256 批次撤掉这份 CSV 导入的数据，并重算受影响的期间。

    只删本批次的行：同期间如果还有别的数据，重算而不是直接删指标，避免把别人的数据一起清掉。
    """
    from sqlalchemy import delete, func, select

    from backend.api_analysis import calculate
    from backend.db import SessionLocal
    from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction

    batch = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    with SessionLocal() as db:
        dates = db.scalars(select(FinancialTransaction.transaction_date)
                           .where(FinancialTransaction.import_batch == batch)).all()
        periods = sorted({value.strftime("%Y-%m") for value in dates})
        if not periods:
            print(f"SKIP: 库里没有 {csv_path.name} 的批次（batch={batch}）")
            return 0
        db.execute(delete(FinancialTransaction).where(FinancialTransaction.import_batch == batch))
        db.flush()
        kept, cleared = [], []
        for period in periods:
            remaining = db.scalar(
                select(func.count()).select_from(FinancialTransaction).where(
                    func.to_char(FinancialTransaction.transaction_date, "YYYY-MM") == period))
            if remaining:
                calculate(db, period, commit=False)
                kept.append(period)
            else:
                db.execute(delete(FinancialAlert).where(FinancialAlert.period == period))
                db.execute(delete(FinancialMetric).where(FinancialMetric.period == period))
                cleared.append(period)
        db.commit()
        print(f"已撤销批次 {batch[:16]}… 共 {len(dates)} 行")
        print(f"  重算期间：{kept or '无'}")
        print(f"  清空期间：{cleared or '无'}")
    return 0


async def run() -> int:
    _console()
    parser = argparse.ArgumentParser(description="通过真实接口导入爬取数据")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="demo_analyst")
    parser.add_argument("--password", default="demo_password_2026")
    parser.add_argument("--remove", action="store_true", help="撤销该文件的批次（给回归脚本准备干净库），不走 HTTP")
    args = parser.parse_args()

    csv_path = Path(args.csv).resolve()
    if not csv_path.exists():
        print(f"FAIL: 文件不存在 {csv_path}", file=sys.stderr)
        return 2
    if args.remove:
        return remove_batch(csv_path)
    content = csv_path.read_bytes()

    # trust_env=False：本机 NO_PROXY 里含 "[::1]" 这种 httpx 无法解析的条目，
    # 开着环境代理会让 AsyncClient 构造直接抛 InvalidURL（见 backend/config.py 的 sanitize_proxy_env）。
    async with httpx.AsyncClient(base_url=args.base_url, timeout=300.0, trust_env=False) as client:
        login = await client.post("/api/v1/auth/login", json={"username": args.username, "password": args.password})
        if login.status_code != 200:
            print(f"FAIL: 登录失败 HTTP {login.status_code} {login.text[:200]}", file=sys.stderr)
            return 3
        token = login.json()["access_token"]
        client.headers["Authorization"] = "Bearer " + token
        print(f"登录成功：{args.username}")

        preview = await client.post("/api/v1/financial/preview",
                                   files={"file": (csv_path.name, content, "text/csv")})
        body = preview.json() if preview.status_code == 200 else {}
        print(f"预览：HTTP {preview.status_code} 行数={body.get('total')} 非法行={body.get('error_count')} valid={body.get('valid')}")
        if preview.status_code != 200 or not body.get("valid"):
            print(f"FAIL: 预览未通过 {json.dumps(body, ensure_ascii=False)[:400]}", file=sys.stderr)
            return 4

        imported = await client.post("/api/v1/financial/import",
                                    files={"file": (csv_path.name, content, "text/csv")})
        if imported.status_code == 409:
            print("SKIP: 该文件批次已导入过（接口按 sha256 判重返回 409），库里已有这份数据，无需重复导入")
            return 0
        if imported.status_code != 200:
            print(f"FAIL: 导入失败 HTTP {imported.status_code} {imported.text[:300]}", file=sys.stderr)
            return 5
        result = imported.json()
        print(f"导入成功：imported={result.get('imported')} batch={result.get('batch')}")
        for item in result.get("calculated") or []:
            print(f"  期间 {item['period']} 预警 {item['alerts']}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
