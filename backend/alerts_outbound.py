"""预警外发通道（⑭）：把重算产生的预警推到 webhook / 邮件，并留下可查、可重试的投递台账。

为什么要有这一层：预警此前只落在 `financial_alerts` 表里，得有人主动打开界面才看得见。
「成本率超过阈值」这种事要在上班前送到人手上，否则这套预警等于没有。

四件事分开，各自可以单独验证：

1. **排投递单**（`plan()`）：在重算的那个事务里，为「待处理」的预警 × 匹配的通道各建一条
   `pending` 投递单。唯一键是**逻辑键** `(alert_key, channel_id)`，不是预警行 id ——
   重算是「先删旧行再建新行」（见 `api_analysis.calculate`），行 id 每轮都变，
   拿行 id 去重等于每重算一次就把同一批通知再轰一遍。
2. **发**（`dispatch()`，后台任务 `alert.dispatch`）：只发还没成功的，全部处理完先落盘、
   再决定是否抛 `DeliveryPending` 让任务退避重试 —— 顺序反了会把已经发出去的记录一起回滚，
   于是每次重试都重发一遍（收件方看到的就是重复轰炸）。
3. **台账**：每条投递单自带 payload **快照**。重试时那条预警很可能已经被下一次重算删掉了，
   没有快照就只能发一句「预警不存在」，或者更糟——发出被改过的内容。
4. **重试**：复用 `backend/jobs.py` 的退避、预算、人工重试与进程重启接续，不自己写定时器。

安全取舍（写在这里，免得以后有人"顺手加固"把功能弄坏）：

- **不封禁内网地址**。告警目标本来就是内网的机器人 / 网关（企业微信、钉钉、内部告警服务），
  按「私有 IP 一律拒绝」加固等于这个功能永远发不出去（本地演示库上更是完全不可验证）。
  真正的风险控制是另外三条：只允许 http/https、URL 里不许带用户名口令、**不跟随重定向** ——
  重定向会把带签名的通知正文连同签名一起送去任意主机。
- 超时 5 秒、响应体最多读 2 KB：一个卡住的通道不能拖住后台任务，超大响应也不该被读进内存。
- webhook 用 HMAC-SHA256 签名（`X-Finance-Signature: sha256=<hex>`，内容是 `时间戳 + "." + 正文`），
  收件方据此确认这条通知真的来自本平台。`secret` **只写不读**：任何接口都不会把已存的密钥返回。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import biztime, jobs
from backend.config import get_settings
from backend.models import AlertChannel, AlertDelivery, FinancialAlert

logger = logging.getLogger("finance-api.alerts")

# 通道类型与级别（级别顺序 low < medium < high，通道的 min_level 表示"这个级别及以上都发"）
KINDS = {"webhook": "Webhook（HTTP POST）", "email": "邮件（SMTP）"}
LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}
LEVEL_LABELS = {"high": "高", "medium": "中", "low": "低"}
MIN_LEVELS = {"low": "低及以上（全部预警）", "medium": "中及以上", "high": "仅高级预警"}

STATUSES = ("pending", "sent", "failed")
STATUS_LABELS = {"pending": "待发送", "sent": "已送达", "failed": "已失败"}

# 响应体只读这么多：足够记下「HTTP 401: unauthorized」，又不至于把内存吃满
MAX_RESPONSE_BYTES = 2048
# 写进 last_error 的字数上限（与 jobs.mark_failure 的截断口径一致）
MAX_ERROR_TEXT = 480
ALERT_KEY_MAX = 300
WEBHOOK_EVENT = "alert.created"
TEST_EVENT = "alert.channel_test"


class ConfigurationError(RuntimeError):
    """通道配置本身不对（协议不支持、SMTP 没配、认证失败……）。

    这类错误重试多少次都一样，所以 `deliver()` 见到它**直接判失败**，不烧重试预算 ——
    否则管理员要等三轮退避之后才能在台账里看到「SMTP 没配」这句实话。
    """


class DeliveryPending(RuntimeError):
    """本次还有投递单没成功。抛出它 = 让后台任务按退避再试一轮（不是失败，是"还没好"）。"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _now() -> datetime:
    return datetime.now(UTC)


def _number_text(value) -> str:
    """金额/比率转成给人看的字符串。Decimal 走 `:f` 而不是 str()，避免出现科学计数法。"""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:f}"
    return str(value)


def alert_key(period: str, department: str | None, alert_type: str,
              metric_name: str) -> str:
    """投递去重用的逻辑键：同一条预警（同期间、同口径、同类型、同指标）只发一次。"""
    key = f"{period or ''}|{(department or '').strip()}|{alert_type or ''}|{metric_name or ''}"
    return key[:ALERT_KEY_MAX]


def snapshot(alert: FinancialAlert) -> dict:
    """预警内容快照。字段名与 `api_analysis._alert_json` 保持一致，便于前端复用同一套渲染。"""
    return {
        "period": alert.period,
        "department": alert.department or "",
        "alert_type": alert.alert_type,
        "metric_name": alert.metric_name,
        "level": alert.level,
        "level_label": LEVEL_LABELS.get(alert.level, alert.level),
        "description": alert.description or "",
        "current_value": _number_text(alert.current_value),
        "threshold_value": _number_text(alert.threshold_value),
        "review_status": alert.review_status,
    }


def envelope(payload: dict, *, event: str = WEBHOOK_EVENT, delivery_id: int | None = None,
             timestamp: str | None = None) -> dict:
    """发出去的完整报文：事件名 + 投递单号 + 时间 + 预警内容。"""
    return {
        "event": event,
        "delivery_id": delivery_id,
        "sent_at": timestamp or _now().isoformat(timespec="seconds"),
        "alert": payload,
    }


def matches(channel: AlertChannel, alert: FinancialAlert) -> bool:
    """这条预警该不该发给这个通道：看级别阈值与部门范围（通道没限定部门 = 全部，含全公司口径）。"""
    if not channel.enabled:
        return False
    alert_level = LEVEL_ORDER.get((alert.level or "").strip().lower(), 0)
    min_level = LEVEL_ORDER.get((channel.min_level or "low").strip().lower(), 0)
    if alert_level < min_level:
        return False
    scope = (channel.department or "").strip()
    return not scope or (alert.department or "").strip() == scope


def plan(db: Session, *, period: str | None = None) -> int:
    """为「待处理」的预警排投递单，返回新建条数（**不提交**：跟着重算的那个事务一起落盘）。

    已经有过投递单的 `(alert_key, channel_id)` 一律跳过，不管上次是成功还是失败：
    失败的那条要人工/任务去重试它自己，而不是每重算一次就再排一张新单子。
    """
    channels = list(db.scalars(
        select(AlertChannel).where(AlertChannel.enabled.is_(True)).order_by(AlertChannel.id)
    ).all())
    if not channels:
        return 0
    statement = select(FinancialAlert).where(FinancialAlert.review_status == "pending")
    if period:
        statement = statement.where(FinancialAlert.period == period)
    alerts = list(db.scalars(statement.order_by(FinancialAlert.id)).all())
    if not alerts:
        return 0

    keys = {alert_key(a.period, a.department, a.alert_type, a.metric_name) for a in alerts}
    existing = {
        (row[0], row[1])
        for row in db.execute(
            select(AlertDelivery.alert_key, AlertDelivery.channel_id).where(
                AlertDelivery.alert_key.in_(keys))
        ).all()
    }
    created = 0
    for alert in alerts:
        key = alert_key(alert.period, alert.department, alert.alert_type, alert.metric_name)
        payload = snapshot(alert)
        for channel in channels:
            if not matches(channel, alert):
                continue
            if (key, channel.id) in existing:
                continue
            db.add(AlertDelivery(
                alert_key=key, alert_id=alert.id, channel_id=channel.id,
                channel_name=channel.name, channel_kind=channel.kind,
                period=alert.period, department=alert.department,
                alert_type=alert.alert_type, metric_name=alert.metric_name, level=alert.level,
                payload=payload, status="pending", attempts=0,
            ))
            existing.add((key, channel.id))
            created += 1
    if created:
        # SessionLocal 是 autoflush=False：调用方可能紧接着就读台账，先落盘免得读不到。
        db.flush()
    return created


def schedule(db: Session, *, periods=None, actor: str | None = None) -> dict:
    """把待发送的投递单交给后台任务。**在调用方 commit 之后**调用（见 api_analysis 等三处）。

    不挂 `BackgroundTasks`：这个函数会被三个不同的重算入口调用，其中两个（更正/冲销）在
    `transactions.py` 里提交、根本拿不到 `BackgroundTasks` 对象。用进程内定时器排一个
    0 秒任务，语义一样是「响应之后立刻发」，而且不必改动那三个入口的签名。

    幂等：只挑 `pending` 的投递单，同一批 id 的 payload 相同，`jobs.submit` 会命中去重
    （双击「补发」不会排出第二条一模一样的活儿）。
    """
    settings = get_settings()
    statement = select(AlertDelivery.id).where(AlertDelivery.status == "pending")
    if periods:
        statement = statement.where(AlertDelivery.period.in_(list(periods)))
    ids = sorted(int(row) for row in db.scalars(statement).all())
    if not ids:
        return {"deliveries": 0, "job": None, "deduplicated": False}
    job, created = jobs.submit(
        db, "alert.dispatch", {"delivery_ids": ids}, actor=actor,
        max_attempts=max(1, int(settings.alert_max_attempts)),
    )
    if created:
        jobs.SCHEDULER(0.0, job.id)
    return {"deliveries": len(ids), "job": jobs.serialize(job), "deduplicated": not created}


def schedule_safely(db: Session, *, periods=None, actor: str | None = None) -> dict:
    """`schedule()` 的「失败也不影响业务」版本，给重算入口用。

    调用它的时候业务数据**已经提交**了（导入成功、更正生效、批次撤销完成），此时因为
    排不出通知就把整个请求报成失败，是把用户能干成的事说成不能干。失败只记一条 WARNING，
    投递单仍然是 `pending`，管理员在预警外发页点一次「补发」就能接上。
    """
    try:
        return schedule(db, periods=periods, actor=actor)
    except Exception:  # noqa: BLE001 —— 通知排程失败不能反噬已经成功的业务写入
        logger.warning("预警外发排程失败（已忽略：业务数据已提交，投递单仍为待发送）",
                       exc_info=True)
        return {"deliveries": 0, "job": None, "deduplicated": False}


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    """带「不跟随重定向」的 opener。

    urllib 默认自动跟 301/302，而 3xx 会把**整份带签名的正文**转发到 Location 指向的主机；
    这里让 `redirect_request` 返回 None，重定向就变成一次普通的 HTTPError 失败。
    顺带禁掉环境里的 HTTP_PROXY：内网告警网关不该被一个代理变量绕出去
    （本地演示环境里还有一层代理，会把 127.0.0.1 的请求也劫走）。
    """

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
            return None

    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def check_webhook_target(target: str) -> str:
    """校验 webhook 地址，返回规范化后的地址；不合法就抛 `ConfigurationError`。"""
    url = (target or "").strip()
    if not url:
        raise ConfigurationError("webhook 地址不能为空")
    scheme = urllib.request.urlsplit(url).scheme.lower()
    if scheme not in {"http", "https"}:
        raise ConfigurationError(f"只支持 http/https 的 webhook 地址，收到的是 {scheme or '空'} 协议")
    parts = urllib.request.urlsplit(url)
    if parts.username or parts.password:
        raise ConfigurationError("webhook 地址里不能带用户名口令（会把凭据写进日志与台账）")
    if not parts.hostname:
        raise ConfigurationError("webhook 地址缺少主机名")
    return url


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 签名：`sha256=<hex>`，签名内容 = `时间戳 + "." + 正文`。

    带上时间戳是为了让收件方能拒绝重放（同一个签名只在时间窗内有效）。
    """
    mac = hmac.new((secret or "").encode(), f"{timestamp}.".encode() + body,
                   hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def send_webhook(channel: AlertChannel, payload: dict, *, delivery_id: int | None = None,
                 settings=None, event: str = WEBHOOK_EVENT) -> tuple[bool, int | None, str]:
    """发一条 webhook。返回 `(是否成功, HTTP 状态码, 简短说明)`，不抛网络异常。"""
    settings = settings or get_settings()
    url = check_webhook_target(channel.target)
    moment = _now()
    timestamp = str(int(moment.timestamp()))
    body = json.dumps(
        envelope(payload, event=event, delivery_id=delivery_id,
                 timestamp=moment.isoformat(timespec="seconds")),
        ensure_ascii=False, sort_keys=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "finance-alert/1.0",
        "X-Finance-Event": event,
        "X-Finance-Timestamp": timestamp,
    }
    if delivery_id is not None:
        headers["X-Finance-Delivery"] = str(delivery_id)
    if channel.secret:
        headers["X-Finance-Signature"] = sign(channel.secret, timestamp, body)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    timeout = max(0.5, float(settings.alert_webhook_timeout_seconds))
    try:
        with _no_redirect_opener().open(request, timeout=timeout) as response:
            detail = response.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
            return True, int(response.status), detail.strip()[:MAX_ERROR_TEXT]
    except urllib.error.HTTPError as exc:
        body_text = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace").strip()
        return False, int(exc.code), f"HTTP {exc.code}：{body_text[:200] or exc.reason}"
    except urllib.error.URLError as exc:
        return False, None, f"连接失败：{exc.reason}"
    except TimeoutError:
        return False, None, f"超时（{timeout:g} 秒）"


def _mail_text(payload: dict) -> str:
    alert = payload or {}
    lines = [
        f"级别：{alert.get('level_label') or alert.get('level') or ''}",
        f"期间：{alert.get('period', '')}",
        f"部门：{alert.get('department') or '全公司'}",
        f"类型：{alert.get('alert_type', '')}",
        f"指标：{alert.get('metric_name', '')}",
        f"当前值：{alert.get('current_value', '')}",
        f"阈值：{alert.get('threshold_value', '')}",
        "",
        str(alert.get("description") or ""),
        "",
        "（本邮件由智能财务分析平台的预警外发通道发出）",
    ]
    return "\n".join(lines)


def send_email(channel: AlertChannel, payload: dict, *, settings=None,
               event: str = WEBHOOK_EVENT) -> tuple[bool, int | None, str]:
    """发一封预警邮件。SMTP 没配好 = 配置错误（重试无意义），直接抛 `ConfigurationError`。"""
    settings = settings or get_settings()
    host = (settings.alert_smtp_host or "").strip()
    sender = (settings.alert_mail_from or settings.alert_smtp_user or "").strip()
    if not host:
        raise ConfigurationError("没有配置 SMTP（ALERT_SMTP_HOST），邮件通道发不出去")
    if not sender:
        raise ConfigurationError("没有配置发件人（ALERT_MAIL_FROM），邮件通道发不出去")
    recipients = [part.strip() for part in (channel.target or "").split(",") if part.strip()]
    if not recipients:
        raise ConfigurationError("邮件通道没有收件人")
    alert = payload or {}
    subject = "".join([
        f"[财务预警][{alert.get('level_label') or alert.get('level') or '-'}] ",
        f"{alert.get('period', '')} ",
        f"{alert.get('department') or '全公司'} ",
        f"{alert.get('alert_type', '')}",
    ]).strip()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(_mail_text(alert))
    timeout = max(0.5, float(settings.alert_webhook_timeout_seconds))
    try:
        with smtplib.SMTP(host, int(settings.alert_smtp_port), timeout=timeout) as client:
            if settings.alert_smtp_starttls:
                client.starttls()
            if settings.alert_smtp_user:
                client.login(settings.alert_smtp_user, settings.alert_smtp_password or "")
            client.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise ConfigurationError(f"SMTP 认证失败：{exc.smtp_code} {exc.smtp_error}") from exc
    except smtplib.SMTPException as exc:
        return False, None, f"{type(exc).__name__}：{exc}"
    except OSError as exc:
        return False, None, f"连接 SMTP 失败：{exc}"
    return True, None, "已投递到 SMTP 服务器"


def deliver(db: Session, delivery: AlertDelivery, *, settings=None) -> str:
    """发一条投递单，更新它的状态与通道的最近结果，返回新状态（**不提交**，由调用方决定）。

    - 通道被删/被停用：直接判失败，并在 `last_error` 里说清楚（人工重试也只会有同样结果）。
    - 通道配置错（`ConfigurationError`）：直接判失败，不烧重试预算。
    - 其它失败（网络、5xx）：`attempts` 没到预算就留在 `pending` 等任务退避重试。
    """
    settings = settings or get_settings()
    now = _now()
    delivery.attempts = int(delivery.attempts or 0) + 1
    delivery.updated_at = now
    channel = db.get(AlertChannel, delivery.channel_id) if delivery.channel_id else None
    if channel is None:
        delivery.status = "failed"
        delivery.last_error = "通道已被删除，无法投递"
        return delivery.status
    if not channel.enabled:
        delivery.status = "failed"
        delivery.last_error = "通道已停用"
        return delivery.status

    ok = False
    status_code: int | None = None
    detail = ""
    permanent = False
    try:
        if channel.kind == "email":
            ok, status_code, detail = send_email(channel, delivery.payload or {}, settings=settings)
        else:
            ok, status_code, detail = send_webhook(
                channel, delivery.payload or {}, delivery_id=delivery.id, settings=settings)
    except ConfigurationError as exc:
        permanent = True
        detail = str(exc)
    except Exception as exc:  # noqa: BLE001 —— 任何意外都记进台账，不能让后台线程静默炸掉
        detail = f"{type(exc).__name__}：{exc}"

    budget = max(1, int(settings.alert_max_attempts))
    if ok:
        delivery.status = "sent"
        delivery.response_status = status_code
        delivery.last_error = None
        delivery.sent_at = now
        channel.last_result = "ok"
        channel.last_error = None
        channel.last_sent_at = now
    else:
        delivery.status = "failed" if (permanent or delivery.attempts >= budget) else "pending"
        delivery.response_status = status_code
        delivery.last_error = (detail or "投递失败")[:MAX_ERROR_TEXT]
        channel.last_result = "failed"
        channel.last_error = delivery.last_error
    return delivery.status


def dispatch(db: Session, payload: dict, context: dict) -> dict:
    """后台任务 `alert.dispatch` 的处理函数：把这一批投递单发到成功或判失败为止。

    **先 commit 再抛异常**（还有 pending 时）：发送结果必须落盘，否则 `run_job` 的
    `session.rollback()` 会把「已经发出去的两条」一起抹掉，下一轮重试再发一遍。
    """
    ids = [int(value) for value in (payload.get("delivery_ids") or [])]
    settings = get_settings()
    sent = failed = pending = 0
    problems: list[str] = []
    if ids:
        rows = db.scalars(
            select(AlertDelivery).where(AlertDelivery.id.in_(ids)).order_by(AlertDelivery.id)
        ).all()
        for row in rows:
            # 幂等：上一次尝试已经成功的（或已经判失败的）不再发第二遍
            if row.status != "pending":
                continue
            state = deliver(db, row, settings=settings)
            if state == "sent":
                sent += 1
            elif state == "failed":
                failed += 1
                problems.append(f"#{row.id} {row.last_error}")
            else:
                pending += 1
                problems.append(f"#{row.id} {row.last_error}")
        db.commit()
    result = {"deliveries": len(ids), "sent": sent, "failed": failed, "pending": pending,
              "errors": problems[:5]}
    if sent or failed or pending:
        logger.info("预警外发：共 %s 条，成功 %s 条，失败 %s 条，待重试 %s 条",
                    len(ids), sent, failed, pending)
    if pending:
        raise DeliveryPending(
            f"还有 {pending} 条投递没成功" + (f"：{problems[0]}" if problems else ""))
    return result


def test_channel(db: Session, channel: AlertChannel, *, settings=None) -> dict:
    """同步发一条测试通知（通道连通性自检）。**不写投递台账**：它不是预警，只是探活。"""
    settings = settings or get_settings()
    payload = {
        # 业务时区（⑯）：这条测试通知里的 period 是给人看的「这个月」，用 UTC 月份的话，
        # UTC+8 的每月 1 号早上 8 点前会写成上个月。
        "period": biztime.month_of(),
        "department": "",
        "alert_type": "通道连通性测试",
        "metric_name": "channel_test",
        "level": "high",
        "level_label": "高",
        "description": f"这是通道「{channel.name}」的测试通知，不会写入投递台账。",
        "current_value": "",
        "threshold_value": "",
        "review_status": "pending",
    }
    started = _now()
    ok = False
    status_code: int | None = None
    detail = ""
    try:
        if channel.kind == "email":
            ok, status_code, detail = send_email(channel, payload, settings=settings,
                                                 event=TEST_EVENT)
        else:
            ok, status_code, detail = send_webhook(channel, payload, settings=settings,
                                                   event=TEST_EVENT)
    except ConfigurationError as exc:
        detail = str(exc)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}：{exc}"
    duration_ms = round((_now() - started).total_seconds() * 1000, 1)
    channel.last_result = "ok" if ok else "failed"
    channel.last_error = None if ok else (detail or "测试失败")[:MAX_ERROR_TEXT]
    if ok:
        channel.last_sent_at = _now()
    db.commit()
    return {"ok": ok, "status": status_code, "detail": "" if ok else channel.last_error,
            "duration_ms": duration_ms}


def channel_json(channel: AlertChannel, counts: dict | None = None) -> dict:
    """通道的接口表示。**永远不含 `secret`** —— 只告诉前端"配没配签名密钥"。"""
    counts = counts or {}
    return {
        "id": channel.id,
        "name": channel.name,
        "kind": channel.kind,
        "kind_label": KINDS.get(channel.kind, channel.kind),
        "target": channel.target,
        "min_level": channel.min_level,
        "min_level_label": MIN_LEVELS.get(channel.min_level, channel.min_level),
        "department": channel.department or "",
        "enabled": bool(channel.enabled),
        "has_secret": bool(channel.secret),
        "last_result": channel.last_result,
        "last_error": channel.last_error,
        "last_sent_at": _iso(channel.last_sent_at),
        "created_at": _iso(channel.created_at),
        "updated_at": _iso(channel.updated_at),
        "counts": {state: int(counts.get(state, 0)) for state in STATUSES},
    }


def serialize(delivery: AlertDelivery) -> dict:
    return {
        "id": delivery.id,
        "alert_key": delivery.alert_key,
        "alert_id": delivery.alert_id,
        "channel_id": delivery.channel_id,
        "channel_name": delivery.channel_name,
        "channel_kind": delivery.channel_kind,
        "channel_kind_label": KINDS.get(delivery.channel_kind, delivery.channel_kind),
        "period": delivery.period,
        "department": delivery.department or "",
        "alert_type": delivery.alert_type,
        "metric_name": delivery.metric_name,
        "level": delivery.level,
        "level_label": LEVEL_LABELS.get(delivery.level, delivery.level),
        "status": delivery.status,
        "status_label": STATUS_LABELS.get(delivery.status, delivery.status),
        "attempts": int(delivery.attempts or 0),
        "response_status": delivery.response_status,
        "last_error": delivery.last_error,
        "sent_at": _iso(delivery.sent_at),
        "created_at": _iso(delivery.created_at),
        "updated_at": _iso(delivery.updated_at),
    }


def delivery_counts(db: Session) -> dict[int, dict]:
    """每个通道各状态的投递条数（列表页一次查完，别在循环里发 N 条 SQL）。"""
    rows = db.execute(
        select(AlertDelivery.channel_id, AlertDelivery.status, func.count())
        .group_by(AlertDelivery.channel_id, AlertDelivery.status)
    ).all()
    counts: dict[int, dict] = {}
    for channel_id, status, total in rows:
        if channel_id is None:
            continue
        counts.setdefault(int(channel_id), {})[status] = int(total)
    return counts


def stats(db: Session) -> dict:
    rows = db.execute(
        select(AlertDelivery.status, func.count()).group_by(AlertDelivery.status)).all()
    totals = dict.fromkeys(STATUSES, 0)
    for status, total in rows:
        totals[status] = int(total)
    return totals


def query(db: Session, *, status: str | None = None, channel_id: int | None = None,
          period: str | None = None, level: str | None = None,
          limit: int = 50, offset: int = 0) -> tuple[list[AlertDelivery], int]:
    filters = []
    if status:
        filters.append(AlertDelivery.status == status)
    if channel_id is not None:
        filters.append(AlertDelivery.channel_id == channel_id)
    if period:
        filters.append(AlertDelivery.period == period)
    if level:
        filters.append(AlertDelivery.level == level)
    condition = select(AlertDelivery)
    counter = select(func.count()).select_from(AlertDelivery)
    for item in filters:
        condition = condition.where(item)
        counter = counter.where(item)
    rows = db.scalars(condition.order_by(AlertDelivery.id.desc()).limit(limit).offset(offset)).all()
    return list(rows), int(db.scalar(counter) or 0)


def retry_delivery(db: Session, delivery: AlertDelivery) -> AlertDelivery:
    """人工重试一条失败的投递单：放回队列、把尝试次数清到预算之下，等 `schedule` 再排任务。

    为什么把 `attempts` 归零：投递单的 `attempts` 只是「这一轮自动重试用了几次」，
    人工点「重试」就是人判断过「通道那边应该修好了」——继续按上一轮的计数立刻再判失败，
    等于这个按钮点不动（与 `jobs.prepare_retry` 保留编号的口径不同，理由也写在这里：
    任务的重试编号出现在界面上代表"整体进度"，投递单的次数只用来决定还要不要再试）。
    """
    delivery.status = "pending"
    delivery.attempts = 0
    delivery.last_error = None
    # 必须立刻 flush：`SessionLocal` 是 autoflush=False，紧接着的 `schedule()` 用 SELECT
    # 找「待发送」的行，不 flush 的话它只看得到数据库里那个还是 failed 的旧状态，
    # 于是「点了重试却什么都没排出去」。
    db.flush()
    return delivery
