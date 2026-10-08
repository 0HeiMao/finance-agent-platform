"""数据导出：明细 / 指标 / 预警 → CSV 或 XLSX。

为什么单独一个路由而不是让前端拼：**导出是最容易漏掉权限的出口**——一个 GET 就能把全公司明细
带走，而界面上看不到任何异常。这里的每一条查询都必须先过 `backend/scope.py` 的
`scope_metric / scope_alert / scope_transactions`，和页面上用的是同一套过滤器。

CSV 一律 `utf-8-sig`（Excel 直接双击打开不乱码，与导入模板一致）；XLSX 用 openpyxl 写，
金额当数字写进单元格，方便在 Excel 里直接求和。
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from backend import audit
from backend.auth import current_user
from backend.db import get_db
from backend.files import content_disposition
from backend.limits import export_quota
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction, User
from backend.scope import (
    Scope,
    resolve_requested_scope,
    row_label,
    scope_alert,
    scope_metric,
    scope_of,
    scope_payload,
    scope_slug,
    scope_text,
    scope_transactions,
)

router = APIRouter(prefix="/api/v1/export", tags=["export"])

# 单次导出行数上限：再大就该走异步任务了，同步请求里一次拉几十万行只会把连接拖死。
EXPORT_LIMIT = 20000
PERIOD_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def _resolve(user: User, department: str | None) -> tuple[Scope, str | None]:
    scope, violation = resolve_requested_scope(user, department)
    return scope, violation


def _guard(user: User, department: str | None) -> Scope:
    scope, violation = _resolve(user, department)
    if violation:
        raise HTTPException(403, violation)
    return scope


def _money(value) -> str:
    """金额统一两位小数的字符串（与接口 JSON 一致，避免 Excel 里出现 0.1+0.2 的尾数）。"""
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def _ratio(value) -> str:
    """比率统一四位小数。部门口径的净利率会到百万级，不能用固定宽度写死。"""
    return str(Decimal(value or 0).quantize(Decimal("0.0001")))


_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def _cell(value):
    """XLSX 单元格取值：看起来是数字的字符串写成数字（Excel 里能直接求和），其余原样。

    带前导零的部门号（"097"）与 "2026-10" 这样的期间**不匹配**这个正则，会保持文本，
    不会被 Excel 自作主张转成 97 或日期序列号。
    """
    if isinstance(value, str) and _NUMERIC.match(value.strip()):
        return Decimal(value.strip())
    return value


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows([["" if value is None else value for value in row] for row in rows])
    return buffer.getvalue().encode("utf-8-sig")


def _xlsx_bytes(header: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "导出数据"
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append([_cell(value) for value in row])
    sheet.freeze_panes = "A2"
    for index, name in enumerate(header, start=1):
        width = max(len(str(name)) * 2, 12)
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = min(width, 42)
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _range_tag(period: str | None, period_from: str | None, period_to: str | None) -> str:
    """文件名里的期间标记：别把它写成「全部期间」而实际只导了某段区间。"""
    if period:
        return period
    if period_from and period_to:
        return f"{period_from}~{period_to}"
    if period_from:
        return f"{period_from}起"
    if period_to:
        return f"{period_to}止"
    return "全部期间"


def _disposition(filename: str, suffix: str) -> str:
    """中文文件名走 RFC 5987：实现见 `backend/files.py`（与 Word 报告共用同一套规则）。"""
    return content_disposition(filename, suffix, fallback="export")


def _respond(header: list[str], rows: list[list], filename: str, export_format: str, truncated: bool):
    if export_format == "xlsx":
        payload, media, suffix = _xlsx_bytes(header, rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"
    else:
        payload, media, suffix = _csv_bytes(header, rows), "text/csv", ".csv"
    headers = {
        "Content-Disposition": _disposition(filename, suffix),
        # 前端据此提示「已保存 N 行」，也方便用户核对是否被上限截断。
        "X-Export-Rows": str(len(rows)),
        "Access-Control-Expose-Headers": "X-Export-Rows, X-Export-Truncated, Content-Disposition",
    }
    if truncated:
        headers["X-Export-Truncated"] = "true"
    return StreamingResponse(io.BytesIO(payload), media_type=media, headers=headers)


def _log_export(db: Session, user: User, request: Request, kind: str, rows: int,
                scope: Scope, export_format: str, truncated: bool) -> None:
    """导出也留痕：数据外带是审计最关心的事之一，但只记条数与口径，不记导出的内容。"""
    audit.record(db, user, "data.export", target_type="export", target_id=kind,
                 detail={"kind": kind, "rows": rows, "scope": scope_text(scope),
                         "format": export_format, "truncated": truncated}, request=request)
    db.commit()


def _format(export_format: str) -> str:
    if export_format not in {"csv", "xlsx"}:
        raise HTTPException(422, "format 只支持 csv 或 xlsx")
    return export_format


@router.get("/transactions")
def export_transactions(request: Request, period: str | None = Query(None, pattern=PERIOD_PATTERN),
                        department: str | None = Query(None, max_length=100),
                        account: str | None = Query(None, max_length=200),
                        export_format: str = Query("csv", alias="format"),
                        db: Session = Depends(get_db), user: User = Depends(current_user),
                        _quota: None = Depends(export_quota)):
    """明细导出。period 省略表示导出该账号范围内的全部期间。"""
    export_format = _format(export_format)
    scope = _guard(user, department)
    condition = scope_transactions(select(FinancialTransaction), scope)
    if period:
        year, month = map(int, period.split("-"))
        condition = condition.where(extract("year", FinancialTransaction.transaction_date) == year,
                                    extract("month", FinancialTransaction.transaction_date) == month)
    if account:
        condition = condition.where(FinancialTransaction.account_name.ilike(f"%{account}%"))
    total = db.scalar(select(func.count()).select_from(condition.subquery())) or 0
    rows = db.scalars(condition.order_by(FinancialTransaction.transaction_date.desc(),
                                         FinancialTransaction.id.desc()).limit(EXPORT_LIMIT)).all()
    header = ["交易日期", "科目编码", "科目名称", "部门", "摘要", "收入金额", "成本金额", "费用金额", "现金流入", "现金流出", "导入批次"]
    body = [[x.transaction_date.strftime("%Y-%m-%d"), x.account_code or "", x.account_name or "", x.department or "",
             x.description or "", str(x.revenue_amount or 0), str(x.cost_amount or 0), str(x.expense_amount or 0),
             str(x.cash_in_amount or 0), str(x.cash_out_amount or 0), x.import_batch or ""] for x in rows]
    tag = _range_tag(period, None, None)
    truncated = total > len(rows)
    _log_export(db, user, request, "transactions", len(body), scope, export_format, truncated)
    return _respond(header, body, f"transactions-{tag}-{scope_slug(scope)}", export_format, truncated)


@router.get("/metrics")
def export_metrics(request: Request,
                   period_from: str | None = Query(None, pattern=PERIOD_PATTERN),
                   period_to: str | None = Query(None, pattern=PERIOD_PATTERN),
                   department: str | None = Query(None, max_length=100),
                   export_format: str = Query("csv", alias="format"),
                   db: Session = Depends(get_db), user: User = Depends(current_user),
                   _quota: None = Depends(export_quota)):
    """期间指标导出：导出的是该账号口径的那些行（全公司行或该部门行），不会把两套混在一起。"""
    export_format = _format(export_format)
    scope = _guard(user, department)
    condition = scope_metric(select(FinancialMetric), scope)
    if period_from:
        condition = condition.where(FinancialMetric.period >= period_from)
    if period_to:
        condition = condition.where(FinancialMetric.period <= period_to)
    rows = db.scalars(condition.order_by(FinancialMetric.period)).all()
    header = ["期间", "口径", "收入金额", "成本金额", "费用金额", "毛利金额", "净利润", "毛利率", "净利率"]
    body = [[x.period, row_label(x.department), str(x.revenue_amount or 0), str(x.cost_amount or 0),
             str(x.expense_amount or 0), str(x.gross_profit_amount or 0), str(x.net_profit_amount or 0),
             str(x.gross_profit_ratio or 0), str(x.net_profit_ratio or 0)] for x in rows]
    tag = _range_tag(None, period_from, period_to)
    _log_export(db, user, request, "metrics", len(body), scope, export_format, False)
    return _respond(header, body, f"metrics-{tag}-{scope_slug(scope)}", export_format, False)


@router.get("/alerts")
def export_alerts(request: Request, period: str | None = Query(None, pattern=PERIOD_PATTERN),
                  period_from: str | None = Query(None, pattern=PERIOD_PATTERN),
                  period_to: str | None = Query(None, pattern=PERIOD_PATTERN),
                  level: str | None = Query(None, max_length=20),
                  department: str | None = Query(None, max_length=100),
                  export_format: str = Query("csv", alias="format"),
                  db: Session = Depends(get_db), user: User = Depends(current_user),
                  _quota: None = Depends(export_quota)):
    """预警导出。`needs_human_review` 一起导出，便于线下复核后回填。

    `period` 是单期精确匹配，`period_from`/`period_to` 是区间——看板按「本年」展示时筛的就是区间，
    导出必须能表达同一范围，否则用户导出来的行数和屏幕上看到的对不上。
    """
    export_format = _format(export_format)
    scope = _guard(user, department)
    condition = scope_alert(select(FinancialAlert), scope)
    if period:
        condition = condition.where(FinancialAlert.period == period)
    if period_from:
        condition = condition.where(FinancialAlert.period >= period_from)
    if period_to:
        condition = condition.where(FinancialAlert.period <= period_to)
    if level:
        condition = condition.where(FinancialAlert.level == level)
    total = db.scalar(select(func.count()).select_from(condition.subquery())) or 0
    rows = db.scalars(condition.order_by(FinancialAlert.period.desc(), FinancialAlert.id.desc())
                      .limit(EXPORT_LIMIT)).all()
    header = ["期间", "口径", "预警类型", "描述", "级别", "指标", "当前值", "阈值", "需人工复核", "预警 id"]
    body = [[x.period, row_label(x.department), x.alert_type, x.description, x.level, x.metric_name,
             str(x.current_value or 0), str(x.threshold_value or 0), "是" if x.needs_human_review else "否", x.id]
            for x in rows]
    tag = _range_tag(period, period_from, period_to)
    truncated = total > len(rows)
    _log_export(db, user, request, "alerts", len(body), scope, export_format, truncated)
    return _respond(header, body, f"alerts-{tag}-{scope_slug(scope)}", export_format, truncated)


@router.get("/scope")
def export_scope(user: User = Depends(current_user)):
    """给导出按钮用的口径说明：前端据此在选择非本部门口径时提示。"""
    return {"scope": scope_payload(scope_of(user)), "generated_at": datetime.now().isoformat(timespec="seconds"),
            "formats": ["csv", "xlsx"], "limit": EXPORT_LIMIT}
