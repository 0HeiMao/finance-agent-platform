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
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import audit, biztime, periods, rollups, transactions
from backend.auth import current_user
from backend.db import get_db
from backend.files import content_disposition
from backend.limits import export_quota
from backend.models import FinancialAlert, FinancialMetric, FinancialRollup, FinancialTransaction, User
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
# 预警行本身就是月口径（`financial_alerts.period` 存 "2026-10"），预警导出的期间只认月。
PERIOD_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
# 明细导出的期间认月/季/年：与 `GET /financial/transactions` 同语义（同一套 `periods.period_range`），
# 所以不能只收 "YYYY-MM"——明细页的期间就是季度/年度落库之后能选的。
PERIOD_RANGE_PATTERN = r"^\d{4}(?:-Q[1-4]|-0[1-9]|-1[0-2])?$"
# 指标导出的 grain 与「同粒度期间怎么写」的样例（报错信息里直接给用户看）。
GRAIN_EXAMPLES = {periods.MONTH: "2026-07", periods.QUARTER: "2026-Q3", periods.YEAR: "2026"}


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


def _grain(value: str | None) -> str:
    """指标导出的粒度：月行在 `financial_metrics`，季/年合计在 `financial_rollups`（⑧ 单开的表）。"""
    grain = (value or periods.MONTH).strip().lower()
    if grain not in periods.GRAINS:
        raise HTTPException(422, "grain 只支持 month、quarter 或 year")
    return grain


def _grain_period(value: str | None, grain: str, field: str) -> str | None:
    """区间端点必须与 grain 同粒度。

    这条校验不能省：月行与季/年行在两张表里，把 "2026-07" 拿去比 `financial_rollups.period`
    只会一行都匹配不上（字符串比较），用户看到的是「导出是空的」而不是「参数写错了」。
    """
    if not value:
        return None
    if periods.grain_of(value) != grain:
        raise HTTPException(422, f"{field} 要和 grain={grain} 同粒度，例如 {GRAIN_EXAMPLES[grain]}")
    return value


def _reversal_state(row: FinancialTransaction) -> str:
    """冲销状态三态：普通行（空）/ 被冲销的原行（已冲销）/ 冲销行。

    三种状态互斥：`transactions.reverse()` 不允许冲销一条冲销行，所以「冲销行又被冲销」不存在。
    """
    if row.reversal_of_id is not None:
        return "冲销行"
    if row.reversed_at is not None:
        return "已冲销"
    return ""


@router.get("/transactions")
def export_transactions(request: Request, period: str | None = Query(None, pattern=PERIOD_RANGE_PATTERN),
                        department: str | None = Query(None, max_length=100),
                        account: str | None = Query(None, max_length=200),
                        batch: str | None = Query(None, max_length=64),
                        query: str | None = Query(None, max_length=100),
                        export_format: str = Query("csv", alias="format"),
                        db: Session = Depends(get_db), user: User = Depends(current_user),
                        _quota: None = Depends(export_quota)):
    """明细导出。period 省略表示导出该账号范围内的全部期间。

    表尾带冲销与更正标记：界面上看得到「已冲销 / 冲销行 / 更正 N 次」，导出也必须看得到——
    否则冲销产生的负数行在表里毫无解释（同一批里一条 +69.5 和一条 -69.5 并排，谁知道是冲销）。

    `batch`/`query` 与明细页面的同名筛选是同一套（`transactions.apply_filters`）：页面上筛完再导出，
    导出的必须就是屏幕上那一批，否则「我明明只导出这个批次」是个查不出来的错。
    """
    export_format = _format(export_format)
    scope = _guard(user, department)
    condition = transactions.apply_filters(
        scope_transactions(select(FinancialTransaction), scope),
        period=period, batch=batch, query=query)
    if account:
        condition = condition.where(FinancialTransaction.account_name.ilike(f"%{account}%"))
    total = db.scalar(select(func.count()).select_from(condition.subquery())) or 0
    rows = db.scalars(condition.order_by(FinancialTransaction.transaction_date.desc(),
                                         FinancialTransaction.id.desc()).limit(EXPORT_LIMIT)).all()
    header = ["明细 id", "交易日期", "科目编码", "科目名称", "部门", "摘要", "收入金额", "成本金额",
              "费用金额", "现金流入", "现金流出", "导入批次", "冲销状态", "原始行 id", "冲销时间",
              "冲销操作人", "更正次数"]
    body = [[x.id, x.transaction_date.strftime("%Y-%m-%d"), x.account_code or "", x.account_name or "",
             x.department or "", x.description or "", str(x.revenue_amount or 0), str(x.cost_amount or 0),
             str(x.expense_amount or 0), str(x.cash_in_amount or 0), str(x.cash_out_amount or 0),
             x.import_batch or "", _reversal_state(x), x.reversal_of_id or "",
             x.reversed_at.strftime("%Y-%m-%d %H:%M:%S") if x.reversed_at else "",
             x.reversed_by or "", x.corrections or 0] for x in rows]
    tag = _range_tag(period, None, None)
    truncated = total > len(rows)
    _log_export(db, user, request, "transactions", len(body), scope, export_format, truncated)
    return _respond(header, body, f"transactions-{tag}-{scope_slug(scope)}", export_format, truncated)


@router.get("/metrics")
def export_metrics(request: Request,
                   grain: str | None = Query(None, max_length=20),
                   period_from: str | None = Query(None, max_length=20),
                   period_to: str | None = Query(None, max_length=20),
                   department: str | None = Query(None, max_length=100),
                   export_format: str = Query("csv", alias="format"),
                   db: Session = Depends(get_db), user: User = Depends(current_user),
                   _quota: None = Depends(export_quota)):
    """期间指标导出：导出的是该账号口径的那些行（全公司行或该部门行），不会把两套混在一起。

    月口径与季/年口径是两张表（`financial_metrics` / `financial_rollups`），所以 `grain` 在这里换表，
    而不是假装一张表里什么粒度都有。季/年行额外带上「包含月份」与「是否完整」：
    `financial_rollups.complete=False` 的期间只有部分月落了库，年度合计看起来像全年，必须写清楚。
    """
    export_format = _format(export_format)
    grain = _grain(grain)
    period_from = _grain_period(period_from, grain, "period_from")
    period_to = _grain_period(period_to, grain, "period_to")
    if period_from and period_to and periods.sort_key(period_from) > periods.sort_key(period_to):
        raise HTTPException(422, "period_from 不能晚于 period_to")
    scope = _guard(user, department)
    if grain == periods.MONTH:
        condition = scope_metric(select(FinancialMetric), scope)
        if period_from:
            condition = condition.where(FinancialMetric.period >= period_from)
        if period_to:
            condition = condition.where(FinancialMetric.period <= period_to)
        statement = condition.order_by(FinancialMetric.period)
    else:
        condition = rollups.scope_rollup(select(FinancialRollup), scope).where(FinancialRollup.grain == grain)
        if period_from:
            condition = condition.where(FinancialRollup.period >= period_from)
        if period_to:
            condition = condition.where(FinancialRollup.period <= period_to)
        statement = condition.order_by(FinancialRollup.period, FinancialRollup.department)
    total = db.scalar(select(func.count()).select_from(condition.subquery())) or 0
    rows = db.scalars(statement.limit(EXPORT_LIMIT)).all()
    header = ["期间", "粒度", "口径", "包含月份", "是否完整", "收入金额", "成本金额", "费用金额",
              "毛利金额", "净利润", "毛利率", "净利率"]
    if grain == periods.MONTH:
        body = [[x.period, periods.GRAIN_LABELS[periods.MONTH], row_label(x.department), x.period, "是",
                 str(x.revenue_amount or 0), str(x.cost_amount or 0), str(x.expense_amount or 0),
                 str(x.gross_profit_amount or 0), str(x.net_profit_amount or 0),
                 str(x.gross_profit_ratio or 0), str(x.net_profit_ratio or 0)] for x in rows]
    else:
        body = [[x.period, periods.GRAIN_LABELS[x.grain], row_label(x.department),
                 "、".join(list(x.months or [])), "是" if x.complete else "否",
                 str(x.revenue_amount or 0), str(x.cost_amount or 0), str(x.expense_amount or 0),
                 str(x.gross_profit_amount or 0), str(x.net_profit_amount or 0),
                 str(x.gross_profit_ratio or 0), str(x.net_profit_ratio or 0)] for x in rows]
    tag = _range_tag(None, period_from, period_to)
    truncated = total > len(rows)
    _log_export(db, user, request, "metrics", len(body), scope, export_format, truncated)
    return _respond(header, body, f"metrics-{grain}-{tag}-{scope_slug(scope)}", export_format, truncated)


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
    # `generated_at` 原来是 naive 的本机时刻（前端只能当浏览器本地时间猜），现在带业务时区偏移（⑯）。
    return {"scope": scope_payload(scope_of(user)), "generated_at": biztime.now().isoformat(timespec="seconds"),
            "timezone": biztime.label(),
            "formats": ["csv", "xlsx"], "limit": EXPORT_LIMIT}
