"""审计日志：把「谁改了什么」写进 `audit_logs`。

为什么要有它：撤销导入批次、删账号、删知识库、改预警规则这些动作在此之前**只改数据不留痕**，
出了问题只能靠翻 uvicorn 日志，而日志重启即丢。

三条设计约定：

1. **不记敏感内容**。只用白名单式的 `detail`（条数、批次号、部门名、旧值/新值这种），
   密码、令牌、上传文件内容一律不进；`_scrub` 还会把所有含 password/token/secret 的键和
  超过 200 字的字符串丢掉，避免以后有人顺手把整份请求体塞进来。
2. **默认不自己 commit**。写在业务事务里，业务成功提交时一起落库，业务回滚时留痕也回滚；
   只有登录失败这种「业务本身就是失败」的场合才 `commit=True` 单独提交。
3. **失败不拦业务**。`record` 内部不抛异常（列缺失、JSON 序列化失败都只记 warning），
   审计不能成为导入/撤销失败的原因。
4. **只增不减，唯一的删除是「按保留期清理」**（`purge`），并且那次清理自己也要留一条痕
   （动作 `audit.purge`，detail 里有截止时间与删除条数）。管理员没有任何「清空审计」的入口：
   能随手清掉的审计等于没有审计，保留期是策略而不是后门。
"""
from datetime import datetime, timedelta, timezone
import json
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.models import AuditLog, User

logger = logging.getLogger("finance.audit")

# 动作白名单：值写库，标签进响应。新增动作时同时更新这里，前端下拉直接读接口返回值。
ACTIONS: dict[str, str] = {
    "user.register": "注册账号",
    "user.login": "登录成功",
    "user.login_failed": "登录失败",
    "user.login_blocked": "登录被限流",
    "user.login_locked": "账号锁定",
    "user.unlock": "解除账号锁定",
    "user.register_rejected": "注册被拒（弱口令）",
    "user.department_self": "修改本人部门",
    "user.department_change": "修改他人部门",
    "user.delete": "删除账号",
    "user.logout": "退出登录",
    "user.force_logout": "强制下线",
    "data.import": "导入财务数据",
    "data.export": "导出数据",
    "transaction.update": "更正明细",
    "transaction.reverse": "冲销明细",
    "metrics.calculate": "重算指标",
    "rollups.rebuild": "重算季/年落库",
    "batch.revert": "撤销导入批次",
    "knowledge.add": "写入知识库",
    "knowledge.delete": "删除知识库来源",
    "alert.review": "处理预警",
    "alert_rule.update": "修改预警规则",
    "alert_rule.create": "新增分档规则",
    "alert_rule.delete": "删除分档规则",
    "audit.export": "导出审计日志",
    "audit.purge": "清理过期审计",
}

# 这些键一旦出现就整条丢掉（大小写不敏感的前缀匹配）
_SENSITIVE = ("password", "token", "secret", "authorization", "api_key", "apikey", "hash")
_MAX_TEXT = 200


def _scrub(detail: dict | None) -> dict:
    """把 detail 洗成「能安全存进 JSONB 的朴素结构」。

    规则：键名命中敏感词就整条丢掉（任意嵌套深度都丢）；字符串截断到 200 字；
    dict 递归保留结构（`before`/`after` 这种前后对比必须留下来才有意义）；
    list 只留前 50 项、每项按标量处理；其余类型转字符串。
    """
    if not detail:
        return {}
    clean: dict = {}
    for key, value in detail.items():
        low = str(key).lower()
        if any(word in low for word in _SENSITIVE):
            continue
        clean[str(key)] = _scrub_value(value)
    return clean


def _scrub_value(value):
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return _scrub(value)
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item) for item in value][:50]
    return str(value)[:_MAX_TEXT]


def client_ip(request) -> str | None:
    """取客户端 IP：优先 X-Forwarded-For 的第一跳（反向代理后面才有意义），否则直接用连接地址。"""
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for") if hasattr(request, "headers") else None
    if forwarded:
        return forwarded.split(",")[0].strip()[:60]
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


def record(db: Session, actor: User | str | None, action: str, *, target_type: str = "",
           target_id: str | int | None = None, detail: dict | None = None,
           request=None, commit: bool = False) -> AuditLog | None:
    """写一条审计。`actor` 传 User 或用户名字符串；失败只记日志，不影响调用方。"""
    try:
        if action not in ACTIONS:
            logger.warning("未登记的审计动作：%s", action)
        username = actor.username if isinstance(actor, User) else (actor or "anonymous")
        department = actor.department if isinstance(actor, User) else None
        row = AuditLog(
            actor=username[:100],
            actor_department=(department or None),
            action=action[:60],
            target_type=(target_type or "")[:40],
            target_id=(str(target_id)[:120] if target_id is not None else None),
            detail=_scrub(detail),
            ip=client_ip(request),
        )
        db.add(row)
        if commit:
            db.commit()
        return row
    except Exception:  # noqa: BLE001 —— 审计失败不允许把业务带崩
        logger.warning("审计写入失败（已忽略）：action=%s", action, exc_info=True)
        return None


def serialize(row: AuditLog) -> dict:
    at = row.created_at
    return {
        "id": row.id,
        "at": (at.astimezone(timezone.utc).isoformat() if isinstance(at, datetime) else None),
        "actor": row.actor,
        "department": row.actor_department or "",
        "action": row.action,
        "action_label": ACTIONS.get(row.action, row.action),
        "target_type": row.target_type or "",
        "target_id": row.target_id or "",
        "detail": row.detail or {},
        "ip": row.ip or "",
    }


def query(db: Session, *, action: str | None = None, actor: str | None = None,
          target_type: str | None = None, ip: str | None = None,
          since: datetime | None = None, until: datetime | None = None,
          limit: int = 50, offset: int = 0) -> tuple[list[AuditLog], int]:
    """按动作/操作人/对象类型/IP/时间范围过滤，返回 (当页行, 命中总数)。

    时间范围与筛选一律在分页之前生效（`total` 也按过滤后的条件数），否则「翻到第 2 页
    数字突然变了」这类问题会让人不敢信这个页面。
    """
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if actor:
        conditions.append(AuditLog.actor == actor)
    if target_type:
        conditions.append(AuditLog.target_type == target_type)
    if ip:
        # 前缀匹配：IPv6/代理链会带上端口或后续跳，写全就没法查了
        conditions.append(AuditLog.ip.like(f"{ip}%"))
    if since is not None:
        conditions.append(AuditLog.created_at >= since)
    if until is not None:
        conditions.append(AuditLog.created_at <= until)
    total = db.scalar(select(func.count()).select_from(AuditLog).where(*conditions)) or 0
    rows = db.scalars(
        select(AuditLog).where(*conditions).order_by(AuditLog.id.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), int(total)


def retention_cutoff(days: int, *, now: datetime | None = None) -> datetime:
    """保留期边界：比它更早的记录才算过期（`days` 天以前的那个时刻）。"""
    return (now or datetime.now(timezone.utc)) - timedelta(days=max(int(days), 1))


def count_older_than(db: Session, cutoff: datetime) -> int:
    return int(db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.created_at < cutoff)
    ) or 0)


def purge(db: Session, *, cutoff: datetime, dry_run: bool = False) -> int:
    """删掉 `created_at < cutoff` 的审计记录，返回删除条数；`dry_run` 只数不删。

    这是全项目**唯一**会删审计的代码路径，调用方（`POST /admin/audit/purge`）必须自己
    再写一条 `audit.purge` 留痕——清理动作本身也是要能查得到的动作。
    """
    if dry_run:
        return count_older_than(db, cutoff)
    return int(db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff)).rowcount or 0)


def action_options() -> list[dict]:
    return [{"value": value, "label": label} for value, label in ACTIONS.items()]


# 导出 CSV 的列（与页面上的列一致，外加部门与 IP；详情压成一行 JSON 便于筛选）
CSV_HEADER = ("时间(UTC)", "操作人", "部门", "动作", "动作标签", "对象类型", "对象ID", "详情", "IP")


def csv_record(row: AuditLog) -> list[str]:
    """把一行审计拍成 CSV 的字符串列表。

    时间用 ISO-8601 UTC（接口给的都是 UTC，页面上由前端换算成本机时间）：导出的表格是要拿去
    对数、进审计底稿的，含糊的本地时间比时区偏移更容易出错。详情里的非 ASCII 不转义
    （`ensure_ascii=False`），配合导出时的 UTF-8 BOM，Excel 打开中文才不会是乱码。
    """
    item = serialize(row)
    detail = item["detail"]
    text = json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else ""
    return [item["at"] or "", item["actor"], item["department"], item["action"],
            item["action_label"], item["target_type"], item["target_id"], text, item["ip"]]


def known_actors(db: Session, limit: int = 50) -> list[str]:
    """最近出现过的操作人，供前端下拉用（不额外查 users 表，删掉的账号也要能回看）。"""
    rows = db.execute(
        select(AuditLog.actor, func.max(AuditLog.id).label("last"))
        .group_by(AuditLog.actor).order_by(func.max(AuditLog.id).desc()).limit(limit)
    ).all()
    return [row[0] for row in rows]


def known_ips(db: Session, limit: int = 50) -> list[str]:
    """最近出现过的来源 IP，供前端下拉用。

    和 `known_actors` 同理走审计表本身（不查 users），按最近一次出现排序；空 IP（脚本、
    测试客户端）不进下拉——它们没有筛选价值，用户要的是「哪个来源干的」。
    """
    rows = db.execute(
        select(AuditLog.ip, func.max(AuditLog.id).label("last"))
        .where(AuditLog.ip.is_not(None), AuditLog.ip != "")
        .group_by(AuditLog.ip).order_by(func.max(AuditLog.id).desc()).limit(limit)
    ).all()
    return [row[0] for row in rows]
