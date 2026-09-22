from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.activity_log import ActivityLogListResponse
from app.crud.activity_log import get_activity_logs, get_distinct_categories

router = APIRouter(prefix="/api/activity-logs", tags=["운영 활동 로그"])


@router.get("", response_model=ActivityLogListResponse)
def api_get_activity_logs(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    category: str | None = Query(None),
    action: str | None = Query(None),
    robot_id: int | None = Query(None),
    robot_name: str | None = Query(None),
    message: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    db: Session = Depends(get_db),
):
    """운영 활동 로그 목록 조회 (기본: 당일 KST)"""
    items, total = get_activity_logs(
        db, skip=skip, limit=limit,
        category=category, action=action,
        robot_id=robot_id, robot_name=robot_name,
        message=message, date_from=date_from, date_to=date_to,
    )
    return ActivityLogListResponse(total=total, items=items)


@router.get("/categories")
def api_get_categories(db: Session = Depends(get_db)):
    """등록된 카테고리 목록 조회"""
    return get_distinct_categories(db)
