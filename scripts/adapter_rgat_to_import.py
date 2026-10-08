"""把爬取到的 Receiver General 会计交易源文件适配成平台导入模板格式。

源文件列名是「英文-法文」双语拼接，且字段语义是政府会计：
- Credit/Debit-Code：CR=贷方，DR=借方
- 金额一律为正数，借贷方向由 CR/DR 决定

适配规则（全部确定性，不臆造数字）：
- 交易日期  <- Accounting-Effective-Date
- 收入金额  <- CR(贷方) 金额
- 费用金额  <- DR(借方) 金额
- 现金流入/流出 不填：源数据没有银行流水时间轴，留 0 而不是用发生额冒充现金流
- 科目名称  <- 总账科目 > 明细科目 > 财务编码块，逐级回退
- 科目编码  <- 总账科目代码原样保留
- 摘要      <- 财务编码块 + 部门 + 凭证类型 + 控制号，保证每行可回溯到源记录

用法：
    python scripts/adapter_rgat_to_import.py --limit 500
    python scripts/adapter_rgat_to_import.py --source data/raw/xxx.csv --out data/processed/yyy.csv --limit 0
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SOURCE = PROJECT_ROOT / "data" / "raw" / "receiver_general_accounting_transactions_sample_source.csv"
DEFAULT_OUT = PROJECT_ROOT / "data" / "processed" / "accounting_transactions_import.csv"

IMPORT_COLUMNS = [
    "交易日期", "科目编码", "科目名称", "部门", "摘要",
    "收入金额", "成本金额", "费用金额", "现金流入", "现金流出",
]

# 源列前缀 -> 内部字段，避免硬编码含重音符号的完整双语列名。
FIELD_PREFIXES = {
    "date": "Accounting-Effective-Date",
    "department": "DepartmentNumber",
    "credit_debit": "Credit/Debit-Code",
    "amount": "Journal-Voucher-Item-Amount",
    "gl_account": "General-Ledger-Account-Code",
    "subledger": "Subleger-Account-Identifier",
    "coding_block": "Financial-Coding-Block",
    "voucher_type": "Journal-Voucher-Type-Code",
    "control_number": "Accounting-Control-Number",
    "fiscal_year": "Fiscal-Year",
}


class AdapterError(RuntimeError):
    """源文件结构不符合预期。"""


def _console() -> None:
    """Windows 控制台默认 GBK，中文日志会乱码甚至抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_source(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise AdapterError("源文件不是有效的 UTF-8 或 GB18030 编码")

    reader = csv.DictReader(io.StringIO(text))
    columns = reader.fieldnames or []
    mapping: dict[str, str] = {}
    for key, prefix in FIELD_PREFIXES.items():
        match = next((c for c in columns if c.startswith(prefix)), None)
        if match is None:
            raise AdapterError(f"源文件缺少字段：{prefix}*")
        mapping[key] = match

    rows = []
    for record in reader:
        rows.append({key: (record.get(col) or "").strip() for key, col in mapping.items()})
    if not rows:
        raise AdapterError("源文件没有数据行")
    return columns, rows


def account_name(row: dict[str, str]) -> str:
    """总账科目优先，其次明细科目，最后回退到财务编码块。"""
    for key in ("gl_account", "subledger", "coding_block"):
        value = row.get(key, "")
        if value:
            return f"科目-{value}"
    return "科目-未分类"


def to_import_row(row: dict[str, str]) -> dict[str, str]:
    if row["credit_debit"] not in {"CR", "DR"}:
        raise AdapterError(f"未知借贷方向：{row['credit_debit']!r}")
    amount = row["amount"]
    if not amount:
        raise AdapterError("金额为空")
    float(amount)  # 提前暴露非数字金额，交给导入层做 Decimal 精确校验

    revenue = amount if row["credit_debit"] == "CR" else "0"
    expense = amount if row["credit_debit"] == "DR" else "0"
    summary_parts = [
        f"编码块:{row.get('coding_block') or '-'}",
        f"部门:{row.get('department') or '-'}",
        f"凭证:{row.get('voucher_type') or '-'}",
        f"控制号:{row.get('control_number') or '-'}",
    ]
    return {
        "交易日期": row["date"],
        "科目编码": row.get("gl_account", ""),
        "科目名称": account_name(row),
        "部门": row.get("department", ""),
        "摘要": " ".join(summary_parts),
        "收入金额": revenue,
        "成本金额": "0",
        "费用金额": expense,
        "现金流入": "0",
        "现金流出": "0",
    }


def adapt(rows: list[dict[str, str]], limit: int | None) -> tuple[list[dict[str, str]], dict]:
    selected = rows if not limit else rows[:limit]
    adapted = [to_import_row(row) for row in selected]
    revenues = sum(float(r["收入金额"]) for r in adapted)
    expenses = sum(float(r["费用金额"]) for r in adapted)
    source_total = sum(float(r["amount"]) for r in selected)
    return adapted, {
        "rows": len(adapted),
        "revenue_total": round(revenues, 2),
        "expense_total": round(expenses, 2),
        "source_total": round(source_total, 2),
        "credit_rows": sum(1 for r in selected if r["credit_debit"] == "CR"),
        "debit_rows": sum(1 for r in selected if r["credit_debit"] == "DR"),
        "date_min": min((r["date"] for r in selected), default=""),
        "date_max": max((r["date"] for r in selected), default=""),
        "departments": len({r["department"] for r in selected}),
        "accounts": len({account_name(r) for r in selected}),
    }


def write_import_file(rows: list[dict[str, str]], target: Path) -> None:
    """按导入模板格式落盘；utf-8-sig 便于 Excel 直接打开中文表头。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=IMPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    _console()
    parser = argparse.ArgumentParser(description="RGAT 源文件 -> 平台导入模板")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=500, help="取前 N 行；0 表示全量")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.exists():
        print(f"FAIL: 源文件不存在 {source}", file=sys.stderr)
        return 2

    try:
        columns, rows = read_source(source)
        adapted, stats = adapt(rows, args.limit or None)
    except (AdapterError, ValueError) as exc:
        print(f"FAIL: 适配失败：{exc}", file=sys.stderr)
        return 3

    target = Path(args.out).resolve()
    write_import_file(adapted, target)

    balanced = round(stats["revenue_total"] + stats["expense_total"], 2) == stats["source_total"]
    print(f"源文件    : {source.name}（{len(columns)} 列，{len(rows)} 行）")
    print(f"适配行数  : {stats['rows']}（CR {stats['credit_rows']} / DR {stats['debit_rows']}）")
    print(f"日期范围  : {stats['date_min']} ~ {stats['date_max']}")
    print(f"部门数量  : {stats['departments']}　科目数量：{stats['accounts']}")
    print(f"收入合计  : {stats['revenue_total']:,.2f}")
    print(f"费用合计  : {stats['expense_total']:,.2f}")
    print(f"源金额合计: {stats['source_total']:,.2f}")
    print(f"落盘路径  : {target.relative_to(PROJECT_ROOT)}")
    if not balanced:
        print("FAIL: 适配后收入+费用合计与源金额合计不一致（数据在适配中丢失）", file=sys.stderr)
        return 4
    print("PASS: 适配完成，且收入+费用合计与源金额合计分毫对平")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
