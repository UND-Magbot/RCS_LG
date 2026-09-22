from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

# ── 역할 코드 상수 ──
# 1 = Administrator, 2 = User

ROLE_MAP = {1: "Administrator", 2: "User"}


# ── 요청 스키마 ──

class UserCreate(BaseModel):
    """DB-01 사용자 등록 요청"""
    login_id: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=4, max_length=128)
    username: str = Field(..., min_length=1, max_length=100)
    role: int = Field(default=2, ge=1, le=2)  # 1=Administrator, 2=User


class UserUpdate(BaseModel):
    """DB-02 사용자 수정 요청"""
    password: Optional[str] = Field(None, min_length=4, max_length=128)
    role: Optional[int] = Field(None, ge=1, le=2)
    is_active: Optional[bool] = None


# ── 응답 스키마 ──

class UserResponse(BaseModel):
    """사용자 단건 응답"""
    id: int
    login_id: str
    username: str
    role: int
    role_name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserListResponse(BaseModel):
    """사용자 목록 응답"""
    total: int
    items: list[UserResponse]
