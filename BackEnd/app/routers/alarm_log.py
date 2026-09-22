from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.alarm_log import (
    AlarmLogCreate,
    AlarmLogResponse,
    AlarmLogListResponse,
    AlarmLogMarkRead,
    UnreadCountResponse,
)
from app.crud.alarm_log import (
    create_alarm_log,
    get_alarm_logs,
    get_unread_count,
    mark_as_read,
    mark_all_as_read,
    get_distinct_robot_sns,
    get_distinct_error_codes,
)

router = APIRouter(prefix="/api/alarm-logs", tags=["알람 로그"])


@router.post("", response_model=AlarmLogResponse, status_code=201)
def api_create_alarm_log(data: AlarmLogCreate, db: Session = Depends(get_db)):
    """알람 로그 생성"""
    return create_alarm_log(db, data)


@router.get("", response_model=AlarmLogListResponse)
def api_get_alarm_logs(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    error_type: str | None = Query(None),
    severity: str | None = Query(None),
    robot_sn: str | None = Query(None),
    is_read: bool | None = Query(None),
    error_code: str | None = Query(None),
    message: str | None = Query(None),
    hours: int | None = Query(None, ge=0),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    db: Session = Depends(get_db),
):
    """알람 로그 목록 조회 (기본: 당일 KST, hours=N이면 최근 N시간, hours=0이면 전체)"""
    items, total = get_alarm_logs(
        db, skip=skip, limit=limit,
        error_type=error_type, severity=severity,
        robot_sn=robot_sn, is_read=is_read,
        error_code=error_code, message=message,
        hours=hours, date_from=date_from, date_to=date_to,
    )
    return AlarmLogListResponse(total=total, items=items)


@router.get("/distinct-robot-sns")
def api_get_distinct_robot_sns(db: Session = Depends(get_db)):
    """등록된 로봇 SN 목록 조회"""
    return get_distinct_robot_sns(db)


@router.get("/distinct-error-codes")
def api_get_distinct_error_codes(db: Session = Depends(get_db)):
    """등록된 에러 코드 목록 조회"""
    return get_distinct_error_codes(db)


@router.get("/unread-count", response_model=UnreadCountResponse)
def api_get_unread_count(db: Session = Depends(get_db)):
    """읽지 않은 알림 수 조회 (최근 24시간)"""
    return UnreadCountResponse(count=get_unread_count(db))


@router.patch("/mark-read")
def api_mark_as_read(data: AlarmLogMarkRead, db: Session = Depends(get_db)):
    """지정 ID 읽음 처리"""
    updated = mark_as_read(db, data.ids)
    return {"updated": updated}


@router.patch("/mark-all-read")
def api_mark_all_as_read(db: Session = Depends(get_db)):
    """전체 읽음 처리"""
    updated = mark_all_as_read(db)
    return {"updated": updated}
