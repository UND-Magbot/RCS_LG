from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
import pymysql
from urllib.parse import quote_plus

# MariaDB 접속 정보 (환경변수 우선, 없으면 기본값)
import os
from pathlib import Path as _Path


def _load_config_env() -> None:
    """`BackEnd/config.env` 가 있으면 읽어 환경변수로 넣는다.

    서버 PC 에서 실행 스크립트를 건드리지 않고 이 파일 하나만 고치면 되게 하려는 것.
    **이미 설정된 환경변수가 우선**하므로, run_local.ps1 로 띄우는 개발 방식은 그대로 동작한다.
    (외부 라이브러리 없이 처리 — 서버 PC 설치 패키지를 늘리지 않기 위해)
    """
    path = _Path(__file__).resolve().parent.parent / "config.env"
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:   # 기존 환경변수를 덮지 않는다
                os.environ[key] = value
    except Exception as e:
        print(f"[DB] config.env 읽기 실패(무시하고 기본값 사용): {e}")


_load_config_env()

DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "1234")
DB_HOST = os.getenv("DB_HOST", "192.168.0.21")

DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "rcs_basic_db")

DATABASE_URL = f"mysql+pymysql://{quote_plus(DB_USER)}:{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"

engine = create_engine(
    DATABASE_URL, echo=False, pool_pre_ping=True,
    pool_size=10, max_overflow=20, pool_recycle=3600,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def create_database_if_not_exists():
    """rcs_db 데이터베이스가 없으면 자동 생성"""
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            charset="utf8mb4",
            connect_timeout=5,
        )
    except Exception as e:
        print(f"[DB] 데이터베이스 서버 연결 실패 ({DB_HOST}:{DB_PORT}): {e}")
        raise

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    except Exception as e:
        print(f"[DB] 데이터베이스 생성 실패 ({DB_NAME}): {e}")
        raise
    finally:
        conn.close()


def init_db():
    """DB 생성 + 테이블 생성"""
    try:
        # create_database_if_not_exists는 hang 이슈로 스킵 (DB는 미리 만들어두기)
        print("[DB] Base.metadata.create_all...")
        Base.metadata.create_all(bind=engine)
        print("[DB] init_db done")
    except Exception as e:
        print(f"[DB] 데이터베이스 초기화 실패: {e}")
        raise


def get_db():
    """FastAPI Dependency — 요청마다 세션 생성/반환"""
    from fastapi import HTTPException
    db = SessionLocal()
    try:
        yield db
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"[DB] 세션 처리 중 오류 발생, 롤백 수행: {e}")
        raise
    finally:
        db.close()
