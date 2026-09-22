import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import SessionLocal
from app.models.activity_log import ActivityLog
from app.schemas.activity_log import ActivityLogResponse, CATEGORY_MAP

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _today_range() -> tuple[datetime, datetime]:
    """오늘 자정(KST) ~ 내일 자정(KST)을 naive datetime 튜플로 반환"""
    now_kst = datetime.now(KST)
    start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    end = start + timedelta(days=1)
    return start, end


def _to_response(log: ActivityLog) -> ActivityLogResponse:
    return ActivityLogResponse(
        id=log.id,
        category=log.category,
        category_name=CATEGORY_MAP.get(log.category, "알 수 없음"),
        action=log.action,
        message=log.message,
        detail=log.detail,
        robot_id=log.robot_id,
        robot_name=log.robot_name,
        source=log.source,
        created_at=log.created_at,
    )


def create_activity_log(db: Session, data) -> ActivityLogResponse:
    log = ActivityLog(
        category=data.category,
        action=data.action,
        message=data.message,
        detail=data.detail,
        robot_id=data.robot_id,
        robot_name=data.robot_name,
        source=data.source,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return _to_response(log)


def log_activity(
    category: str,
    action: str,
    message: str,
    detail: str | None = None,
    robot_id: int | None = None,
    robot_name: str | None = None,
    source: str | None = None,
):
    """백그라운드 스레드 안전 — 자체 DB 세션 생성/반환"""
    db = SessionLocal()
    try:
        log = ActivityLog(
            category=category,
            action=action,
            message=message,
            detail=detail,
            robot_id=robot_id,
            robot_name=robot_name,
            source=source,
        )
        db.add(log)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[ActivityLog] 저장 실패: {e}")
    finally:
        db.close()


def get_activity_logs(
    db: Session,
    skip: int = 0,
    limit: int = 50,
    category: str | None = None,
    action: str | None = None,
    robot_id: int | None = None,
    robot_name: str | None = None,
    message: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[ActivityLogResponse], int]:
    query = db.query(ActivityLog)

    if date_from or date_to:
        if date_from:
            query = query.filter(ActivityLog.created_at >= date_from)
        if date_to:
            query = query.filter(ActivityLog.created_at <= date_to)
    else:
        today_start, today_end = _today_range()
        query = query.filter(ActivityLog.created_at >= today_start, ActivityLog.created_at < today_end)

    if category:
        query = query.filter(ActivityLog.category == category)
    if action:
        query = query.filter(ActivityLog.action == action)
    if robot_id is not None:
        query = query.filter(ActivityLog.robot_id == robot_id)
    if robot_name:
        query = query.filter(ActivityLog.robot_name.ilike(f"%{robot_name}%"))
    if message:
        query = query.filter(ActivityLog.message.ilike(f"%{message}%"))

    total = query.count()
    items = query.order_by(desc(ActivityLog.created_at)).offset(skip).limit(limit).all()
    return [_to_response(log) for log in items], total


def get_distinct_categories(db: Session) -> list[dict]:
    rows = db.query(ActivityLog.category).distinct().all()
    return [
        {"code": r[0], "name": CATEGORY_MAP.get(r[0], r[0])}
        for r in sorted(rows, key=lambda x: x[0])
    ]
