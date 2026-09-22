"""비상정지(E-STOP) 상태 감시 — LGIT 요청 6번.

로봇 몸통의 빨간 버튼이 눌려 있으면 로봇은 아무 명령도 받지 않는다. 그런데
화면에는 그냥 "이동 중" 으로 보여서, 작업자는 로봇이 왜 안 가는지 알 수 없었다.
그래서 **로봇 태블릿에 전체화면 빨간 배너**를 띄우기로 했다(문구는 템플릿 참조).

## 왜 별도 스레드인가
태블릿은 3초마다 `/robot/{id}/tablet-status` 를 폴링한다. 그 요청 안에서 로봇
REST 를 직접 치면 **LTE 지연이 그대로 화면 멈춤**이 된다. 로봇이 잠깐 응답하지
않으면 태블릿이 통째로 얼어붙는다(2026-08 콘솔에서 같은 사고가 있었고,
`online_ips_cached(block=False)` 로 분리한 이력이 있다).
그래서 읽기는 여기 백그라운드에서 하고, 라우터는 **캐시만 읽는다.**

## 무엇을 읽나
`GET http://<ip>:8090/chassis/status` 의 `emergency_stop_pressed`.
`routers/robot.py` 의 quick-status 가 이미 같은 필드를 본다(같은 출처).

## 모름(None) 과 안 눌림(False) 은 다르다
통신이 안 될 때 "안 눌림" 으로 단정하면 실제로는 눌려 있는데 배너가 사라진다.
반대로 "눌림" 으로 보면 통신 장애마다 거짓 경보가 뜬다.
→ 여기서는 **직전 값을 잠시 유지**하다가, 연속 실패가 이어지면 `None`(모름)으로
  떨군다. 화면에 띄울지 말지는 호출부가 정한다(지금은 None → 배너 없음).

## 부하
로봇 1대 기준으로 3초에 1건, 타임아웃 4초. LTE 에서도 부담이 없는 수준이다.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

POLL_SEC = 3.0              # 로봇 1대 기준 조회 주기 (태블릿 폴링과 같은 박자)
HTTP_TIMEOUT = 4.0          # 이보다 길면 한 바퀴가 늘어져 반응이 느려진다
ROSTER_REFRESH_SEC = 30.0   # 로봇 목록(DB) 다시 읽는 주기
FAIL_TO_UNKNOWN = 4         # 연속 실패가 이 횟수를 넘으면 '모름' 으로 떨군다

_state: dict[str, dict] = {}     # ip → {"pressed": bool|None, "t": ts, "fails": int}
_state_lock = threading.Lock()
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def is_pressed(ip: str) -> Optional[bool]:
    """비상정지가 눌려 있나. True/False/None(모름).

    캐시 조회일 뿐이라 즉시 돌아온다. 로봇을 직접 치지 않는다.
    """
    if not ip:
        return None
    with _state_lock:
        s = _state.get(ip)
    return s.get("pressed") if s else None


def status() -> dict[str, dict]:
    """콘솔·디버그용 전체 상태."""
    now = time.time()
    with _state_lock:
        return {ip: {"pressed": v.get("pressed"),
                     "age_sec": round(now - v.get("t", now), 1)}
                for ip, v in _state.items()}


def _robot_ips() -> list[str]:
    from app.database import SessionLocal
    from app.models.robot import Robot
    db = SessionLocal()
    try:
        rows = db.query(Robot).filter(
            Robot.is_active == True,      # noqa: E712
            Robot.ip_address != None,     # noqa: E711
        ).all()
        return [r.ip_address for r in rows if r.ip_address]
    except Exception:
        logger.warning("[estop] 로봇 목록 조회 실패 — 직전 목록 유지")
        return []
    finally:
        db.close()


def _poll_one(ip: str) -> None:
    try:
        r = requests.get(f"http://{ip}:8090/chassis/status", timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        pressed = bool(r.json().get("emergency_stop_pressed"))
    except Exception:
        with _state_lock:
            s = _state.setdefault(ip, {"pressed": None, "t": 0.0, "fails": 0})
            s["fails"] += 1
            if s["fails"] >= FAIL_TO_UNKNOWN and s["pressed"] is not None:
                # 오래 못 읽었다 — 직전 값을 계속 쓰면 거짓 정보가 된다
                logger.info(f"[estop] {ip} 연속 {s['fails']}회 조회 실패 — '모름' 으로 전환")
                s["pressed"] = None
        return

    with _state_lock:
        s = _state.setdefault(ip, {"pressed": None, "t": 0.0, "fails": 0})
        before = s["pressed"]
        s["pressed"] = pressed
        s["t"] = time.time()
        s["fails"] = 0
    if before != pressed:
        logger.warning(f"[estop] {ip} 비상정지 "
                       f"{'눌림 ★' if pressed else '해제'} (직전: {before})")


def _loop() -> None:
    ips: list[str] = []
    last_roster = 0.0
    while not _stop.is_set():
        try:
            now = time.time()
            if now - last_roster >= ROSTER_REFRESH_SEC or not ips:
                fresh = _robot_ips()
                if fresh:
                    if set(fresh) != set(ips):
                        logger.info(f"[estop] 감시 대상 {len(fresh)}대: {', '.join(fresh)}")
                    ips = fresh
                last_roster = now
            for ip in ips:
                if _stop.is_set():
                    break
                _poll_one(ip)
        except Exception:
            logger.exception("[estop] 감시 오류")
        _stop.wait(POLL_SEC)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    from app.services.thread_utils import safe_thread
    _thread = safe_thread(target=_loop, name="estop-monitor")
    _thread.start()
    logger.info("[estop] 비상정지 감시 시작")


def stop() -> None:
    _stop.set()
