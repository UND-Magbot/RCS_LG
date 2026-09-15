import logging
import os

from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI

# 로깅 설정 — 콘솔 + 파일
#
# ★ 파일 기록은 2026-09-09 추가. 그전에는 basicConfig 에 handlers 가 없어
#   **콘솔로만** 나갔고, 현장 서버는 런처가 별도 창을 띄우는 구조라
#   창을 닫으면 로그가 통째로 사라졌다. 멈칫·정지 원인을 사후에 규명할 근거가
#   남지 않아, 현장에서 문제가 나도 [safety]/[route] 로그를 볼 수 없었다.
#   (log_config.json 에 파일 핸들러 설정이 있었지만 참조하는 코드가 없었다)
#
#   10 MB 가 차면 backend.log.1 … .3 으로 밀리고 그 이상은 지워진다.
#   최대 사용량은 약 40 MB 로 묶인다.
_LOG_DIR = Path(__file__).resolve().parent.parent / "_logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", datefmt="%H:%M:%S")
_console = logging.StreamHandler()
_console.setFormatter(_fmt)
_file = RotatingFileHandler(
    _LOG_DIR / "backend.log", maxBytes=10 * 1024 * 1024, backupCount=3,
    encoding="utf-8")
_file.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.routers import user, robot, auth, map, alarm_log, activity_log, backup, log, jack_test, task, settings, dispatch
from app.services.scheduler import init_scheduler, shutdown_scheduler
from app.services import dispatch_service
# 데드락 감지/양보 기능 비활성화 — 좁은 통로 없는 사이트.
# 다시 켜려면 아래 import 와 lifespan 의 start/stop 주석을 해제하세요.
# from app.services import deadlock_monitor

# 모델 import (테이블 메타데이터 등록용)
import app.models  # noqa: F401


def _apply_saved_speeds():
    """서버 시작 시 DB에 저장된 속도를 로봇에 적용"""
    import requests
    from app.database import SessionLocal
    from app.models.robot import Robot
    db = SessionLocal()
    try:
        robots = db.query(Robot).filter(Robot.is_active == True, Robot.ip_address != None, Robot.max_speed != None).all()
        for r in robots:
            try:
                requests.post(
                    f"http://{r.ip_address}:8090/robot-params",
                    json={"/wheel_control/max_forward_velocity": r.max_speed},
                    timeout=3,
                )
                logging.getLogger(__name__).info(f"[startup] 속도 적용: {r.name} → {r.max_speed} m/s")
            except Exception:
                logging.getLogger(__name__).warning(f"[startup] 속도 적용 실패: {r.name} ({r.ip_address})")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(application: FastAPI):
    log = logging.getLogger(__name__)
    log.info("[startup] init_db...")
    init_db()
    log.info("[startup] init_db done")
    from app.services.thread_utils import safe_thread
    safe_thread(target=_apply_saved_speeds, name="apply-saved-speeds").start()
    log.info("[startup] init_scheduler...")
    init_scheduler()
    log.info("[startup] init_scheduler done")
    log.info("[startup] dispatch recovery...")
    try:
        dispatch_service.recover_on_startup()
    except Exception as e:
        log.warning(f"[startup] dispatch recovery 실패: {e}")
    # 로봇 부팅(재부팅) 후 자동 복구 — 온라인 감지 시 current-map 설정 + 충전소 기준 위치재조정
    log.info("[startup] boot_recovery.start...")
    try:
        from app.services import boot_recovery
        boot_recovery.start()
    except Exception as e:
        log.warning(f"[startup] boot_recovery 시작 실패: {e}")
    # 주행 중 음성 안내 (LGIT 요청) — 설정에서 켜야 실제로 소리가 난다
    log.info("[startup] robot_voice.start...")
    try:
        from app.services import robot_voice
        robot_voice.start()
    except Exception as e:
        log.warning(f"[startup] robot_voice 시작 실패: {e}")
    # 비상정지(E-STOP) 감시 (LGIT 요청 6번) — 로봇 태블릿 배너용 상태 캐시
    log.info("[startup] estop_monitor.start...")
    try:
        from app.services import estop_monitor
        estop_monitor.start()
    except Exception as e:
        log.warning(f"[startup] estop_monitor 시작 실패: {e}")
    # 전방 장애물 안전거리 Yellow/Red (LGIT 요청 4번) — 설정에서 켜야 동작한다
    log.info("[startup] safety_zone.start...")
    try:
        from app.services import safety_zone
        safety_zone.start()
    except Exception as e:
        log.warning(f"[startup] safety_zone 시작 실패: {e}")
    # 데드락 자동 감지/양보 기능 비활성화 — 사이트에 좁은 통로 없어 양보 불필요
    # 다시 켜려면 아래 두 줄 주석 해제
    # log.info("[startup] deadlock_monitor.start...")
    # deadlock_monitor.start()
    yield
    try:
        from app.services import boot_recovery
        boot_recovery.stop()
    except Exception:
        pass
    try:
        from app.services import robot_voice
        robot_voice.stop()
    except Exception:
        pass
    try:
        from app.services import safety_zone
        safety_zone.stop()
    except Exception:
        pass
    # deadlock_monitor.stop()
    shutdown_scheduler()


app = FastAPI(
    title="RCS API",
    description="Robot Control System — 사용자 / 로봇 관리 API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS (프론트 연결용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# 라우터 등록
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(robot.router)
app.include_router(map.router)
app.include_router(alarm_log.router)
app.include_router(activity_log.router)
app.include_router(backup.router)
app.include_router(log.router)
app.include_router(jack_test.router)
app.include_router(task.router)
app.include_router(settings.router)
app.include_router(dispatch.router)


# 정적 파일 서빙 (맵 이미지 등)
_static_dir = Path(__file__).resolve().parent.parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/ping")
def ping():
    return {"message": "pong"}


@app.get("/health")
def health_check():
    """헬스체크 — DB 연결 확인"""
    from app.database import engine
    from sqlalchemy import text as sa_text
    import time

    result = {"status": "ok", "timestamp": time.time(), "checks": {}}

    try:
        with engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        result["checks"]["database"] = "ok"
    except Exception as e:
        result["checks"]["database"] = f"error: {e}"
        result["status"] = "degraded"

    return result


# ══════════════════════════════════════════════════════════════
# 관제 화면(Next.js) 정적 서빙 — ★ 반드시 이 파일 맨 끝에 둘 것
# ══════════════════════════════════════════════════════════════
# frontend 를 `BUILD_STATIC=1 npm run build` 로 뽑으면 순수 HTML/JS(`out/`)가 나온다.
# 그 결과를 여기로 복사해 두면 백엔드가 관제 화면까지 같이 서빙한다.
#   → 서버 PC 에 Node.js 불필요 / 포트 8002 하나 / API 주소가 상대경로라 IP 바뀌어도 재빌드 없음
# 콘솔·로봇태블릿 HTML 은 이미 백엔드가 서빙하고 있어 방식이 통일된다.
#
# ⚠️ "/" 는 catch-all 이라 라우터보다 먼저 mount 하면 /api/* 요청까지 삼킨다.
#    그래서 모든 라우터·엔드포인트 등록이 끝난 이 위치에 둔다.
#
# 폴더가 없으면 그냥 넘어간다 → 개발 PC 에서는 지금처럼 npm run dev(3000) 로 쓰면 된다.
_web_dir = Path(os.getenv("WEB_DIR") or (Path(__file__).resolve().parent.parent / "web"))
if _web_dir.is_dir():
    # html=True : 디렉터리 요청 → index.html, 없는 경로 → 404.html
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
    logging.getLogger(__name__).info(f"[web] 관제 화면 정적 서빙 ON — {_web_dir}")
else:
    logging.getLogger(__name__).info(
        f"[web] 정적 빌드 없음({_web_dir}) — 관제 화면은 npm run dev 로 별도 실행"
    )
