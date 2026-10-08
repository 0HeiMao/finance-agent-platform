"""报告导出：Markdown 月报 + Word（.docx）+ PPTX 演示文稿。

三类报告都从同一份 `FinancialMetric` / `FinancialAlert` 取数，口径说明（`PERIOD_NOTES` 的
PPT/Word 版本 `NOTES`）必须跟着一起走，否则「毛利率 100%」会被读成经营结论。
"""
from __future__ import annotations

import io
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import periods as period_mod
from backend.auth import current_user
from backend.db import get_db
from backend.files import content_disposition
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction, User
from backend.scope import (Scope, metric_totals, resolve_requested_scope, scope_alert, scope_label,
                           scope_metric, scope_of, scope_payload, scope_slug, scope_text,
                           scope_transactions)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


def _report_scope(user: User, requested: str | None) -> tuple[Scope, str | None]:
    """报告的口径解析：返回 (生效口径, 是否由不受限账号主动指定)。越权直接 403。

    报告比页面更容易被外传（下载下来就是一份文件），所以这里和看板用同一套规则，
    不额外放宽：受限账号只能出自己的部门，填别的部门不是「回落到自己的范围」而是报错。
    """
    scope, violation = resolve_requested_scope(user, requested)
    if violation:
        raise HTTPException(403, violation)
    # 显式口径只对「本身不受限、这次又指定了部门」的管理员有意义：返回部门名（供提示条文案用），
    # 其它情况返回 None。以前这里返回的是 bool，提示条于是会写成「正在按部门 True 口径查看」。
    explicit = scope[0] if (scope and len(scope) == 1 and scope_of(user) is None) else None
    return scope, explicit

# 逐期数据里的口径限制必须跟着报告一起走，否则「毛利率 1.0000」会被读成经营结论。
PERIOD_NOTES = (
    "## 口径与限制\n"
    "- 指标由程序按期间确定性汇总，未经人工调整；金额保留两位小数，比率保留四位小数。\n"
    "- 成本金额、成本率恒为 0：当前数据源没有成本科目，全部支出计入费用金额。\n"
    "- 现金流入/流出按 0 处理：数据源没有银行流水时间轴，不从权责发生制金额推现金。\n"
    "- 毛利 = 收入 - 成本，在成本恒为 0 时等于收入，毛利率因此显示为 100.00%，不代表真实盈利水平。\n"
    "- 收支差额 = 收入 - 成本 - 费用，是简化口径，不是会计意义上的利润总额。\n"
)

NOTES = (
    "指标由程序按期间确定性汇总，未经人工调整；金额两位小数，比率四位小数。",
    "成本金额、成本率恒为 0：数据源没有成本科目，全部支出计入费用金额。",
    "现金流入/流出按 0 处理：数据源没有银行流水时间轴，不从权责发生制金额推现金。",
    "毛利在成本恒为 0 时等于收入，毛利率 100.00% 不代表真实盈利水平。",
    "收支差额 = 收入 - 成本 - 费用，是简化口径，不是会计意义上的利润总额。",
)

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# 报告里出现的中文用 Word 里通行的中文字体，否则在只装了雅黑/宋体的机器上会被替换字体、
# 甚至显示成方框（docx 默认是 Calibri，没有中文字形）。
DOCX_FONT = "微软雅黑"


def _collect(db: Session, period: str, scope: Scope = None,
             requested: str | None = None) -> dict:
    """把一期数据整理成报告所需的全部素材（Markdown 与 PPTX 共用）。

    `department` 是查看者的数据范围：None = 全公司口径（读 department IS NULL 的汇总行），
    有值 = 该部门口径。报告与看板必须给同一个数，所以取数和看板走同一套 scope 过滤。
    """
    # 多部门账号同期有多行：金额相加、比率按合计重算（metric_totals 与看板/汇总同一份实现）。
    rows = db.scalars(scope_metric(
        select(FinancialMetric).where(FinancialMetric.period == period), scope)).all()
    totals = metric_totals(rows, period_order=[period])
    metric = totals[0] if totals else None
    alerts = db.scalars(
        scope_alert(select(FinancialAlert).where(FinancialAlert.period == period), scope)
        .order_by(FinancialAlert.id)).all()
    top_departments: list[tuple[str, Decimal]] = []
    if metric:
        # 期间用前闭后开区间（`period_mod.period_range`）而不是 `to_char(...) = period`：函数套在列上
        # 就用不到 `(department, transaction_date)` 索引，而且每一行都要算一次 to_char。
        # 注意传的必须是 date 对象，字符串会报
        # `operator does not exist: timestamp without time zone >= character varying`。
        statement = scope_transactions(
            select(FinancialTransaction.department, FinancialTransaction.expense_amount)
            .where(period_mod.period_range(FinancialTransaction.transaction_date, period)), scope)
        rows = db.execute(statement).all()
        grouped: dict[str, Decimal] = {}
        for row_department, expense in rows:
            key = (row_department or "未标注部门").strip() or "未标注部门"
            grouped[key] = grouped.get(key, Decimal("0")) + (expense or Decimal("0"))
        top_departments = sorted(grouped.items(), key=lambda item: item[1], reverse=True)[:5]
    return {"metric": metric, "alerts": alerts, "top_departments": top_departments,
            "scope": scope_payload(scope, requested=requested)}


def _percent(value) -> str:
    """比率列在库里存的是 0.8100 这类小数，报告统一按百分数呈现。"""
    return f"{Decimal(value or 0) * 100:.2f}%"


def _metric_rows(metric) -> list[tuple[str, str]]:
    """核心指标行。报告正文、PPT 正文与前端大纲共用同一份字符串，避免三处格式漂移。"""
    return [
        ("收入", f"{metric.revenue_amount:.2f} 元"),
        ("成本", f"{metric.cost_amount:.2f} 元"),
        ("费用", f"{metric.expense_amount:.2f} 元"),
        ("净利润", f"{metric.net_profit_amount:.2f} 元"),
        ("毛利率", _percent(metric.gross_profit_ratio)),
        ("净利率", _percent(metric.net_profit_ratio)),
    ]


@router.get("/monthly", response_class=PlainTextResponse)
def monthly_report(period: str, department: str | None = Query(None, max_length=100),
                   db: Session = Depends(get_db), user: User = Depends(current_user)):
    scope, explicit = _report_scope(user, department)
    data = _collect(db, period, scope, explicit)
    metric = data["metric"]
    if not metric:
        return "# 财务分析报告\n\n数据不足，无法生成报告。\n"
    alerts = data["alerts"]
    # 带上 alert_type（利润异常/成本率异常）：只给 level + description 时，
    # 报告里的预警和看板/预警页显示的名字对不上，读者无法引用同一口径。
    risk = "\n".join(f"- [{x.level}] {x.alert_type}：{x.description}" for x in alerts) or "- 该期间未触发预警。"
    metrics = "\n".join(f"- {label}：{value}" for label, value in _metric_rows(metric))
    # 受限账号的报告必须在标题下就写明范围，否则「部门 097 的费用」会被当成公司费用引用出去。
    scope_line = "" if scope is None else f"> 数据范围：仅统计「{scope_label(scope)}」的明细，不是全公司口径。\n\n"
    return (
        f"# {period} 财务分析报告\n\n"
        f"{scope_line}"
        "## 核心指标\n"
        f"{metrics}\n\n"
        "## 风险预警\n"
        f"{risk}\n\n"
        f"{PERIOD_NOTES}"
    )


def _outline(data: dict, period: str) -> list[dict]:
    """PPTX 的逐页大纲。前端先看大纲再决定下不下载，也便于测试断言。"""
    metric = data["metric"]
    scope = data.get("scope") or {}
    subtitle = "智能财务分析平台 · 指标由程序确定性汇总，报告只做呈现"
    if not scope.get("unrestricted", True):
        subtitle += f" · 数据范围：{scope.get('label', '')}"
    slides = [{
        "title": f"{period} 财务分析报告",
        "kind": "cover",
        "subtitle": subtitle,
        "bullets": [],
    }]
    if not metric:
        slides[0]["bullets"] = ["数据不足，无法生成报告。"]
        return slides
    slides.append({
        "title": "核心指标",
        "kind": "table",
        # 带上真实的指标行：前端大纲要能逐字对照 PPT，否则这一页在前端看不出内容
        "table": [[label, value] for label, value in _metric_rows(metric)],
        "bullets": [],
    })
    slides.append({
        "title": "风险预警",
        "kind": "bullets",
        "bullets": [f"[{x.level}] {x.alert_type}：{x.description}" for x in data["alerts"]]
        or ["该期间未触发预警。"],
    })
    top = data["top_departments"]
    slides.append({
        "title": "费用构成（按部门 Top5）",
        "kind": "chart",
        "bullets": [f"{name}：{value:.2f} 元" for name, value in top] or ["本期没有可归集的费用数据。"],
    })
    slides.append({"title": "口径与限制", "kind": "bullets", "bullets": list(NOTES)})
    return slides


@router.get("/pptx/outline")
def pptx_outline(period: str, department: str | None = Query(None, max_length=100),
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    scope, explicit = _report_scope(user, department)
    data = _collect(db, period, scope, explicit)
    metric = data["metric"]
    return {
        "period": period,
        "has_data": bool(metric),
        "filename": f"财务分析报告-{period}.pptx",
        "slides": _outline(data, period),
        "scope": data["scope"],
        "notes": list(NOTES),
    }


def _build_ppt(data: dict, period: str) -> bytes:
    """用 python-pptx 原生图表生成演示文稿（不依赖 matplotlib/PIL）。"""
    try:
        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches, Pt
    except ImportError as exc:  # pragma: no cover - 只有漏装依赖才会走到
        raise HTTPException(503, "未安装 python-pptx，请执行 pip install python-pptx") from exc

    metric = data["metric"]
    presentation = Presentation()
    presentation.slide_width = Inches(13.333)  # 16:9
    presentation.slide_height = Inches(7.5)

    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    cover.shapes.title.text = f"{period} 财务分析报告"
    cover.placeholders[1].text = "智能财务分析平台 · 指标由程序确定性汇总，报告只做呈现"

    if not metric:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "数据不足"
        slide.placeholders[1].text = "该期间没有已入库的指标，请先导入财务数据并重算。"
        buffer = io.BytesIO()
        presentation.save(buffer)
        return buffer.getvalue()

    kpi_slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    kpi_slide.shapes.title.text = "核心指标"
    body = kpi_slide.placeholders[1]
    body.text_frame.text = "\n".join(f"{label}：{value}" for label, value in _metric_rows(metric))
    for paragraph in body.text_frame.paragraphs:
        paragraph.font.size = Pt(20)

    alert_slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    alert_slide.shapes.title.text = "风险预警"
    alerts = data["alerts"]
    lines = [f"[{x.level}] {x.alert_type}：{x.description}" for x in alerts] or ["该期间未触发预警。"]
    alert_slide.placeholders[1].text_frame.text = "\n".join(lines)
    for paragraph in alert_slide.placeholders[1].text_frame.paragraphs:
        paragraph.font.size = Pt(18)

    top = data["top_departments"]
    if top:
        chart_slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        chart_slide.shapes.title.text = "费用构成（按部门 Top5）"
        chart_data = CategoryChartData()
        chart_data.categories = [name for name, _ in top]
        chart_data.add_series("费用金额", tuple(float(value) for _, value in top))
        chart_slide.shapes.add_chart(
            XL_CHART_TYPE.BAR_CLUSTERED, Inches(0.8), Inches(1.7), Inches(11.7), Inches(5.2),
            chart_data)
    else:
        empty_slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        empty_slide.shapes.title.text = "费用构成（按部门 Top5）"
        empty_slide.placeholders[1].text_frame.text = "本期没有可归集的费用数据。"

    notes_slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    notes_slide.shapes.title.text = "口径与限制"
    notes_slide.placeholders[1].text_frame.text = "\n".join(NOTES)
    for paragraph in notes_slide.placeholders[1].text_frame.paragraphs:
        paragraph.font.size = Pt(14)

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _docx_set_fonts(document) -> None:
    """把正文与各级标题的东亚字体设成 `DOCX_FONT`。

    python-docx 的样式默认只有西文（Calibri），中文段落会走字体替换；显式给 `w:eastAsia`
    之后 Word/WPS 在任何机器上都按中文字体渲染，不会再出现方框或错位排版。
    """
    from docx.oxml.ns import qn

    for name in ("Normal", "Title", "Heading 1", "Heading 2"):
        try:
            style = document.styles[name]
        except KeyError:  # pragma: no cover - 模板变化才会走到
            continue
        style.font.name = DOCX_FONT
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), DOCX_FONT)


def _docx_table(document, header: list[str], rows: list[list[str]]):
    table = document.add_table(rows=1, cols=len(header))
    table.style = "Table Grid"
    for index, name in enumerate(header):
        cell = table.rows[0].cells[index]
        cell.text = name
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True
    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = "" if value is None else str(value)
    return table


def _build_docx(data: dict, period: str) -> bytes:
    """用 python-docx 生成一份可编辑的 Word 报告。

    Word 里没有「原生图表」这种约束（pptx 有），但这里同样只用表格：报告要被复制进别的
    文档、被逐字引用，表格里的数字比一张图更好核对；图交给 PPTX。
    """
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - 只有漏装依赖才会走到
        raise HTTPException(503, "未安装 python-docx，请执行 pip install python-docx") from exc

    metric = data["metric"]
    scope = data.get("scope") or {}
    document = Document()
    _docx_set_fonts(document)
    document.add_heading(f"{period} 财务分析报告", level=0)
    subtitle = "智能财务分析平台 · 指标由程序确定性汇总，报告只做呈现"
    if not scope.get("unrestricted", True):
        subtitle += f" · 数据范围：{scope.get('label', '')}"
    document.add_paragraph(subtitle)

    if not metric:
        document.add_heading("数据不足", level=1)
        document.add_paragraph("该期间没有已入库的指标，请先导入财务数据并重算。")
    else:
        document.add_heading("一、核心指标", level=1)
        _docx_table(document, ["指标", "金额 / 比率"], [list(row) for row in _metric_rows(metric)])

        document.add_heading("二、风险预警", level=1)
        alerts = data["alerts"]
        if alerts:
            _docx_table(document, ["级别", "预警类型", "描述"],
                        [[x.level, x.alert_type, x.description] for x in alerts])
        else:
            document.add_paragraph("该期间未触发预警。")

        document.add_heading("三、费用构成（按部门 Top5）", level=1)
        top = data["top_departments"]
        if top:
            _docx_table(document, ["部门", "费用金额"], [[name, f"{value:.2f} 元"] for name, value in top])
        else:
            document.add_paragraph("本期没有可归集的费用数据。")

    document.add_heading("四、口径与限制", level=1)
    for note in NOTES:
        document.add_paragraph(note, style="List Bullet")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@router.get("/docx")
def docx_report(period: str, department: str | None = Query(None, max_length=100),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    scope, explicit = _report_scope(user, department)
    data = _collect(db, period, scope, explicit)
    payload = _build_docx(data, period)
    tag = f" {scope_text(scope)}" if scope else ""
    # 后缀交给 content_disposition 拼：名字里一旦带 ".docx"，ASCII 兜底就永远「合格」了
    # （".docx" 自带字母），中文名会被剥成 ` 2097-05.docx` 这种谁也认不出的样子。
    filename = f"财务分析报告 {period}{tag}"
    # ASCII 兜底名里不能带中文部门名（HTTP 头是 latin-1），部门号本身多为数字，过滤后仍可用。
    safe_department = "".join(ch for ch in scope_text(scope) if ch.isascii() and ch.isalnum()) if scope else ""
    fallback = f"financial-report-{period}" + (f"-{safe_department}" if safe_department else "")
    return Response(
        content=payload,
        media_type=DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": content_disposition(filename, ".docx", fallback=fallback)},
    )


@router.get("/pptx")
def pptx_report(period: str, department: str | None = Query(None, max_length=100),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    scope, explicit = _report_scope(user, department)
    data = _collect(db, period, scope, explicit)
    payload = _build_ppt(data, period)
    # 指定口径时文件名带上部门，避免「部门 097 报告」被当成公司报告发出去；
    # 多部门账号用 scope_slug（018-097），中文/特殊字符不会进文件名。
    tag = f"-{scope_slug(scope)}" if scope else ""
    filename = f"financial-report-{period}{tag}.pptx"
    return Response(
        content=payload,
        media_type=PPTX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
