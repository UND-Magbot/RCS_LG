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
#
# ★ 2026-09-29 — 현장 로그가 **14:41 에 멈춰 그 뒤 한 줄도 안 남았다.**
#   backend.log 를 **세 곳이 동시에 열고** 있었다.
#     ① uvicorn --reload 감시(부모) 프로세스 — log_config.json 의 file 핸들러
#     ② 서버(자식) 프로세스 — 같은 log_config.json 의 file 핸들러
#     ③ 서버 프로세스 — 아래 이 파일의 RotatingFileHandler
#   Windows 는 남이 열어 둔 파일의 이름을 못 바꾼다. 10 MB 가 차서 순환(이름 바꾸기)
#   하려는 순간 실패하고, 그 뒤로는 기록이 계속 실패한다. 현장 파일이 정확히
#   10,485,744 바이트에서 멈춰 있었다.
#   → log_config.json 에서 파일 핸들러를 뺐고(콘솔만), **이 파일에서 한 번만** 연다.
#     그래도 이름 바꾸기가 실패하면 '복사 후 비우기' 로 순환한다(_SafeRotatingFileHandler).
#   시각 형식은 `월-일 시:분:초` — 진단 도구(drive_log·field_session)가 날짜를 읽는다.
_LOG_DIR = Path(__file__).resolve().parent.parent / "_logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)


class _SafeRotatingFileHandler(RotatingFileHandler):
    """이름 바꾸기가 막혀도(Windows 파일 잠금) 순환이 멈추지 않는 핸들러."""

    def doRollover(self):
        try:
            super().doRollover()
            return
        except OSError:
            pass
        # 대체 순환 — 뒤에서부터 복사로 밀고, 현재 파일은 비운다.
        #   ※ 표준 doRollover 는 이름을 바꾸기 **전에** 스트림을 닫는다. 그래서 여기
        #     왔을 때 스트림이 이미 닫혀 있을 수 있다 — 'w' 로 다시 열어 비운다.
        import shutil
        try:
            if self.stream:
                self.stream.flush()
                self.stream.close()
                self.stream = None
            base = self.baseFilename
            for i in range(self.backupCount - 1, 0, -1):
                src, dst = f"{base}.{i}", f"{base}.{i + 1}"
                if os.path.exists(src):
                    shutil.copyfile(src, dst)
            if os.path.exists(base):
                shutil.copyfile(base, f"{base}.1")
            with open(base, "w", encoding=self.encoding or "utf-8"):
                pass                                # 비우기
            self.stream = self._open()
        except Exception:
            # 순환 자체가 안 되면 계속 이어 쓴다 — 기록이 끊기는 것보다 낫다
            if self.stream is None:
                self.stream = self._open()


_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", datefmt="%m-%d %H:%M:%S")
_file = _SafeRotatingFileHandler(
    _LOG_DIR / "backend.log", maxBytes=10 * 1024 * 1024, backupCount=5,
    encoding="utf-8", delay=True)
_file.setFormatter(_fmt)
_file.setLevel(logging.INFO)

_root = logging.getLogger()
if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
           for h in _root.handlers):
    _console = logging.StreamHandler()          # --log-config 없이 띄운 경우
    _console.setFormatter(_fmt)
    _root.addHandler(_console)
_root.setLevel(logging.INFO)
# 루트와, 전파가 꺼진 uvicorn 로거들에 **이 핸들러 하나만** 붙인다.
for _name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
    _lg = logging.getLogger(_name)
    # 설정 파일에 남아 있는 옛 파일 핸들러가 있으면 떼어낸다(구버전 log_config.json 대비)
    for _h in list(_lg.handlers):
        if isinstance(_h, logging.FileHandler) and _h is not _file:
            _lg.removeHandler(_h)
            try:
                _h.close()
            except Exception:
                pass
    if _name == "" or not _lg.propagate:
        if _file not in _lg.handlers:
            _lg.addHandler(_file)
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
