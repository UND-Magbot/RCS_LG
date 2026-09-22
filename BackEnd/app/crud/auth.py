import logging

from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from passlib.hash import bcrypt

from app.database import get_db
from app.models.user import User
from app.schemas.user import ROLE_MAP
from app.schemas.auth import AuthUser

logger = logging.getLogger(__name__)

# JWT 설정
SECRET_KEY = "rcs-secret-key-change-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

security = HTTPBearer(auto_error=False)


def create_access_token(user_id: int, role: int) -> str:
    """JWT 액세스 토큰 생성"""
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    """토큰 검증 → payload 반환"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="토큰이 만료되었습니다.")
    except JWTError:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")


def authenticate_user(db: Session, login_id: str, password: str) -> tuple[User, int]:
    """login_id + 비밀번호로 사용자 인증"""
    user = db.query(User).filter(User.login_id == login_id).first()
    if not user:
        logger.warning(f"[Auth] 로그인 실패 — 존재하지 않는 아이디: '{login_id}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="존재하지 않는 아이디입니다.",
        )

    if not user.is_active:
        logger.warning(f"[Auth] 로그인 실패 — 비활성화된 계정: '{login_id}'")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="비활성화된 계정입니다.",
        )

    if not bcrypt.verify(password, user.password_hash):
        logger.warning(f"[Auth] 로그인 실패 — 비밀번호 오류: '{login_id}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="비밀번호가 올바르지 않습니다.",
        )

    role_code = user.role.role if user.role else 2
    return user, role_code


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> AuthUser:
    """요청 헤더의 Bearer 토큰으로 현재 사용자 조회 (Dependency)"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="인증되지 않았습니다.")

    payload = verify_token(credentials.credentials)
    user_id = int(payload["sub"])

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="유효하지 않은 사용자입니다.")

    role_code = user.role.role if user.role else 2
    return AuthUser(
        id=user.id,
        login_id=user.login_id,
        username=user.username,
        role=role_code,
        role_name=ROLE_MAP.get(role_code, "Unknown"),
    )


def require_admin(
    current_user: AuthUser = Depends(get_current_user),
) -> AuthUser:
    """관리자(role=1) 전용 Dependency"""
    if current_user.role != 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다.",
        )
    return current_user
