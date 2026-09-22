from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.models.alarm_log import AlarmLog
from app.schemas.alarm_log import (
    AlarmLogCreate,
    AlarmLogResponse,
    ERROR_TYPE_MAP,
    SEVERITY_MAP,
)

KST = timezone(timedelta(hours=9))


def _today_range() -> tuple[datetime, datetime]:
    """오늘 자정(KST) ~ 내일 자정(KST)을 naive datetime 튜플로 반환"""
    now_kst = datetime.now(KST)
    start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    end = start + timedelta(days=1)
    return start, end


def _to_response(log: AlarmLog) -> AlarmLogResponse:
    return AlarmLogResponse(
        id=log.id,
        error_code=log.error_code,
        error_type=log.error_type,
        error_type_name=ERROR_TYPE_MAP.get(log.error_type, "알 수 없음"),
        severity=log.severity,
        severity_name=SEVERITY_MAP.get(log.severity, "알 수 없음"),
        message=log.message,
        description=log.description,
        source=log.source,
        robot_sn=log.robot_sn,
        is_read=log.is_read,
        created_at=log.created_at,
    )


def create_alarm_log(db: Session, data: AlarmLogCreate) -> AlarmLogResponse:
    log = AlarmLog(
        error_code=data.error_code,
        error_type=data.error_type,
        severity=data.severity,
        message=data.message,
        description=data.description,
        source=data.source,
        robot_sn=data.robot_sn,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return _to_response(log)


def get_alarm_logs(
    db: Session,
    skip: int = 0,
    limit: int = 50,
    error_type: str | None = None,
    severity: str | None = None,
    robot_sn: str | None = None,
    is_read: bool | None = None,
    error_code: str | None = None,
    message: str | None = None,
    hours: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[AlarmLogResponse], int]:
    query = db.query(AlarmLog).filter(AlarmLog.is_active == True)

    if date_from or date_to:
        if date_from:
            query = query.filter(AlarmLog.created_at >= date_from)
        if date_to:
            query = query.filter(AlarmLog.created_at <= date_to)
    elif hours is not None and hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        query = query.filter(AlarmLog.created_at >= cutoff)
    else:
        today_start, today_end = _today_range()
        query = query.filter(AlarmLog.created_at >= today_start, AlarmLog.created_at < today_end)
    if error_type:
        query = query.filter(AlarmLog.error_type == error_type)
    if severity:
        query = query.filter(AlarmLog.severity == severity)
    if robot_sn:
        query = query.filter(AlarmLog.robot_sn == robot_sn)
    if is_read is not None:
        query = query.filter(AlarmLog.is_read == is_read)
    if error_code:
        query = query.filter(AlarmLog.error_code == error_code)
    if message:
        query = query.filter(AlarmLog.message.ilike(f"%{message}%"))

    total = query.count()
    items = query.order_by(desc(AlarmLog.created_at)).offset(skip).limit(limit).all()
    return [_to_response(log) for log in items], total


def get_unread_count(db: Session) -> int:
    today_start, today_end = _today_range()
    return db.query(AlarmLog).filter(
        AlarmLog.is_active == True,
        AlarmLog.is_read == False,
        AlarmLog.created_at >= today_start,
        AlarmLog.created_at < today_end,
    ).count()


def mark_as_read(db: Session, ids: list[int]) -> int:
    count = db.query(AlarmLog).filter(
        AlarmLog.id.in_(ids),
        AlarmLog.is_active == True,
    ).update({"is_read": True}, synchronize_session="fetch")
    db.commit()
    return count


def mark_all_as_read(db: Session) -> int:
    count = db.query(AlarmLog).filter(
        AlarmLog.is_active == True,
        AlarmLog.is_read == False,
    ).update({"is_read": True}, synchronize_session="fetch")
    db.commit()
    return count


def get_distinct_robot_sns(db: Session) -> list[str]:
    rows = db.query(AlarmLog.robot_sn).filter(
        AlarmLog.is_active == True,
        AlarmLog.robot_sn.isnot(None),
    ).distinct().all()
    return sorted([r[0] for r in rows])


def get_distinct_error_codes(db: Session) -> list[str]:
    rows = db.query(AlarmLog.error_code).filter(
        AlarmLog.is_active == True,
    ).distinct().all()
    return sorted([r[0] for r in rows])
