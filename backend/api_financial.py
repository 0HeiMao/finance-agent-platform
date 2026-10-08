from uuid import uuid4
from pathlib import Path
import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
import io
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from hashlib import sha256
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from backend import audit
from backend.auth import current_user
from backend.config import get_settings
from backend.db import get_db
from backend.models import FinancialTransaction, User
from backend.api_analysis import calculate

router = APIRouter(prefix="/api/v1/financial", tags=["financial"])

AMOUNT_COLUMNS = ["收入金额", "成本金额", "费用金额", "现金流入", "现金流出"]


class LegacyExcelError(ValueError):
    """旧版 .xls 需要 xlrd；缺依赖时要给一句人能看懂的话，而不是 500。"""


def read_frame(source, suffix: str) -> pd.DataFrame:
    """按后缀读成 DataFrame。显式指定引擎，避免 pandas 在 .xls 上抛裸 ImportError。"""
    if suffix == ".csv":
        return pd.read_csv(source, dtype=str, keep_default_na=False)
    if suffix == ".xls":
        try:
            return pd.read_excel(source, dtype=str, keep_default_na=False, engine="xlrd")
        except ImportError as exc:
            raise LegacyExcelError(
                "旧版 .xls 需要服务端安装 xlrd（pip install \"xlrd>=2.0.1\"）；"
                "也可以先在 Excel 里「另存为 .xlsx」再导入"
            ) from exc
    return pd.read_excel(source, dtype=str, keep_default_na=False, engine="openpyxl")

def validate_row(row):
    """校验并规范化一行数据，返回 (交易日期的 datetime, {金额列: Decimal})。

    错误信息面向使用者：excel 里常见的千分位（"1,234"）会被去掉，
    无法解析时给出「哪一列、原值是什么」，而不是抛 decimal 的内部异常文本。
    """
    raw_date = str(row.get("交易日期", "") or "").strip()
    try:
        date = pd.to_datetime(raw_date, errors="raise")
    except Exception as exc:
        raise ValueError(f"交易日期无法解析：{raw_date or '（空）'}") from exc
    if pd.isna(date) or not str(row.get("科目名称", "")).strip():
        raise ValueError("日期和科目名称不能为空")
    if date.tzinfo is not None: raise ValueError("交易日期不能包含时区")
    amounts = {}
    for name in AMOUNT_COLUMNS:
        raw = str(row.get(name, "") or "").strip().replace(",", "").replace("，", "") or "0"
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name}不是有效金额：{raw}") from exc
        if not value.is_finite(): raise ValueError(f"{name}不是有效金额：{raw}")
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if abs(value) >= Decimal("10000000000000000"): raise ValueError(f"{name}超出范围")
        amounts[name] = value
    return date.to_pydatetime(), amounts

@router.post("/import")
async def import_excel(request: Request, file: UploadFile = File(...),
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    if Path(file.filename or "").suffix.lower() not in {".xlsx", ".xls", ".csv"}:
        raise HTTPException(400, "仅支持 Excel 或 CSV 文件")
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件不能超过10MB")
    batch = sha256(content).hexdigest()
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(batch[:15], 16)})
    if db.scalar(select(FinancialTransaction.id).where(FinancialTransaction.import_batch == batch).limit(1)):
        db.rollback()
        raise HTTPException(409, "此文件已导入，请勿重复提交")
    # data/raw 按仓库约定只读，导入上传件另放 data/uploads。
    # 这份上传件只是解析用的临时副本：内容哈希已经成了批次号、明细也落库了，
    # 留在 data/uploads 只会让目录只增不减（成功、失败、重复导入都会各留一份）。
    path = Path(get_settings().upload_dir) / f"{uuid4()}{Path(file.filename).suffix.lower()}"
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    try:
        frame = read_frame(path, path.suffix.lower())
        required = {"交易日期", "科目名称"}
        missing = required - set(frame.columns)
        if missing: raise ValueError(f"缺少字段：{', '.join(missing)}")
        errors = []; rows = []
        for index, row in frame.iterrows():
            try:
                date, amounts = validate_row(row)
                def amount(name): return amounts[name]
                rows.append(FinancialTransaction(transaction_date=date, account_name=str(row["科目名称"]), account_code=str(row.get("科目编码", "")), department=str(row.get("部门", "")), description=str(row.get("摘要", "")), revenue_amount=amount("收入金额"), cost_amount=amount("成本金额"), expense_amount=amount("费用金额"), cash_in_amount=amount("现金流入"), cash_out_amount=amount("现金流出"), import_batch=batch))
            except Exception as exc: errors.append({"row": int(index) + 2, "error": str(exc)})
        if errors:
            db.rollback()
            return {"success": False, "imported": 0, "errors": errors, "calculated": [], "batch": batch}
        if not rows: raise ValueError('文件没有有效数据行')
        db.add_all(rows)
        periods = sorted({row.transaction_date.strftime('%Y-%m') for row in rows})
        calculated = []
        for period in periods:
            metric, period_alerts = calculate(db, period, commit=False)
            calculated.append({"period": period, "alerts": len(period_alerts)})
        # 导入是数据进入系统的唯一入口，必须留痕（记文件名/行数/期间，不记文件内容）
        audit.record(db, user, "data.import", target_type="batch", target_id=batch,
                     detail={"filename": file.filename, "rows": len(rows), "periods": periods},
                     request=request)
        db.commit()
        return {"success": True, "batch": batch, "imported": len(rows), "errors": errors, "calculated": calculated}
    except LegacyExcelError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(422, f"导入失败：{exc}") from exc
    finally:
        # 无论成功、校验失败还是解析失败，临时副本都删掉；批次号与审计日志已足够追溯。
        path.unlink(missing_ok=True)

@router.get("/template")
def download_template(_: User = Depends(current_user)):
    content = "交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出\n2026-09-01,6001,示例收入,销售部,示例交易,10000,0,0,10000,0\n"
    return StreamingResponse(io.BytesIO(content.encode('utf-8-sig')), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=financial_import_template.csv"})

@router.post("/preview")
async def preview_excel(file: UploadFile = File(...), _: User = Depends(current_user)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xls", ".csv"}: raise HTTPException(400, "仅支持 Excel 或 CSV 文件")
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024: raise HTTPException(413, "文件不能超过10MB")
    try:
        frame = read_frame(io.BytesIO(content), suffix)
    except LegacyExcelError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, "无法解析文件，请检查格式和编码") from exc
    try:
        required = {"交易日期", "科目名称"}; missing = sorted(required - set(frame.columns))
        errors=[]
        for index,row in frame.iterrows():
            try:
                validate_row(row)
            except Exception as exc: errors.append({"row": int(index)+2, "error": str(exc)})
        return {"filename": file.filename, "total": len(frame), "columns": list(frame.columns), "missing": missing, "errors": errors[:100], "error_count": len(errors), "valid": not missing and not errors and len(frame) > 0, "preview": frame.head(10).fillna("").to_dict(orient="records")}
    except Exception as exc:
        raise HTTPException(422, "无法解析文件，请检查格式和编码") from exc
