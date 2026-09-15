from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ── 에러 타입 / 심각도 상수 ──

ERROR_TYPE_MAP = {
    "auth": "인증 오류",
    "task": "작업 오류",
    "robot": "로봇 오류",
    "map": "맵 오류",
    "net": "통신 오류",
    "data": "데이터 오류",
}

SEVERITY_MAP = {
    "info": "정보",
    "warning": "경고",
    "error": "오류",
}


# ── 요청 스키마 ──

class AlarmLogCreate(BaseModel):
    error_code: str = Field(..., max_length=20)
    error_type: str = Field(..., max_length=20)
    severity: str = Field(default="error", max_length=20)
    message: str = Field(..., max_length=500)
    description: Optional[str] = None
    source: Optional[str] = Field(None, max_length=100)
    robot_sn: Optional[str] = Field(None, max_length=100)


class AlarmLogMarkRead(BaseModel):
    ids: list[int] = Field(..., min_length=1)


# ── 응답 스키마 ──

class AlarmLogResponse(BaseModel):
    id: int
    error_code: str
    error_type: str
    error_type_name: str
    severity: str
    severity_name: str
    message: str
    description: Optional[str]
    source: Optional[str]
    robot_sn: Optional[str]
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AlarmLogListResponse(BaseModel):
    total: int
    items: list[AlarmLogResponse]


class UnreadCountResponse(BaseModel):
    count: int
