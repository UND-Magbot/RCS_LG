"""주행 중 음성 안내 — LGIT 2026-09-02 요청 2번.

로봇이 움직이는 동안 "로봇이 임무수행중입니다. 안전에 주의하세요" 를 주기적으로 내보낸다.
코너·좁은 길목에서 사람이 로봇을 미리 인지하게 하는 게 목적이다.

## 왜 8090 이 아니라 9000 인가
우리가 평소 쓰는 8090(섀시 REST)에는 오디오 API 가 아예 없다(2026-09-03 전수 확인).
오디오는 **`ws://<robot_ip>:9000/`** 명령 채널에 있다. AutoXing JS-SDK 가 LAN 모드에서
쓰는 그 채널이고, 프레임은 `{deviceId, id, cmd, args, timeoutSec, needConfirm, timestamp}` 다.

## 실측으로 확인된 것 (S300 `crawler_s300_op5` 2.12.21-opi64, 2026-09-03)
- `mode: 2`(섀시) 로 재생된다. 상위기(mode 1) 없이도 소리가 난다
  → L150 처럼 섀시만 있는 기종에도 그대로 넘어갈 가능성이 높다. **mode 2 를 기본으로 둔 이유**
- 로봇 언어가 이미 한국어다 (`GET :9001/custom_settings` → `playLanguage: 5`)
- **`interval`·`num` 필드는 `code:0` 을 돌려주지만 실제로는 반복하지 않는다.**
  그래서 반복은 이쪽에서 주기적으로 다시 쏜다 (`interval: -1, num: 1` 로 1회씩)
- 내장 음성은 `GET :9001/get_audio?audioId=<id>` 로 받아볼 수 있다(MP3). 업로드는 없다
  → 문구를 바꾸려면 우리 서버 mp3 를 `url` 로 물리는 수밖에 없다

## 주행 판별 — `speed` 하나만 본다
9000 이 1초마다 밀어주는 상태의 `speed`(실제 바퀴 속도)로만 판정한다.
`moveState` 는 쓰지 않는다. 두 방향 모두에서 어긋나기 때문이다.
- 원격제어로 밀면 `moveState` 는 `idle` 인데 로봇은 움직인다
- 경로만 생성되고 못 움직이거나 원격 모드로 취소되면 `moveState` 는 `moving` 인데 로봇은 서 있다
  (2026-09-03 실제 발생 — 서 있는 로봇이 "이동중입니다"를 계속 외쳤다)
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Optional

from websocket import WebSocketException, WebSocketTimeoutException, create_connection

logger = logging.getLogger(__name__)

CMD_PORT = 9000              # 명령·상태 채널 (8090 과 별개)
AUDIO_HTTP_PORT = 9001       # 내장 음원 조회용

# 이보다 빠르면 움직이는 것으로 본다. 제자리 회전도 잡으려고 낮게 잡았다.
MOVING_SPEED = 0.02
# 로봇 목록을 다시 읽는 주기
ROSTER_REFRESH_SEC = 30.0
# WS 가 끊겼을 때 재연결 대기
RECONNECT_WAIT_SEC = 5.0

_stop_event = threading.Event()
_supervisor: Optional[threading.Thread] = None
_workers: dict[str, threading.Thread] = {}   # robot_ip → thread
_workers_lock = threading.Lock()


# ── 로봇에 명령 보내기 ────────────────────────────────────────

def _send_cmd(ip: str, sn: str, cmd: str, args: dict, wait: float = 1.5) -> Optional[int]:
    """명령 한 건 전송 후 code 를 받아온다. 상태 스트림 메시지는 흘려보낸다."""
    ws = None
    code = None
    try:
        ws = create_connection(f"ws://{ip}:{CMD_PORT}/", timeout=5)
        ws.send(json.dumps({
            "deviceId": sn,
            "id": str(uuid.uuid4()),
            "cmd": cmd,
            "args": args,
            "timeoutSec": 15,
            "needConfirm": True,
            "timestamp": int(time.time() * 1000),
        }))
        ws.settimeout(1)
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                d = json.loads(ws.recv())
            except Exception:
                continue
            if "code" in d:
                code = d["code"]
                break
    except (WebSocketException, OSError) as e:
        logger.warning(f"[voice] {cmd} 전송 실패 ({ip}): {e}")
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
    return code


def server_ip_for(robot_ip: str) -> Optional[str]:
    """그 로봇에서 우리 서버가 어떤 IP 로 보이는지 알아낸다.

    설정에 서버 주소를 적어두면 현장(다른 대역·LTE)에서 반드시 어긋난다.
    로봇 쪽으로 소켓을 열어보고 우리가 쓴 로컬 주소를 되읽는 방식이라
    개발 PC 든 현장 서버든 알아서 맞는 값이 나온다. 패킷은 보내지 않는다.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((robot_ip, CMD_PORT))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def normalize_base_url(base: str, server_port: int) -> str:
    """설정에 적어둔 서버 공개 주소를 `http://host:port` 로 다듬는다.

    아래 셋 다 받는다 — 현장에서 형식을 틀리게 적어도 동작하게.
      `http://10.115.244.11:8002` / `10.115.244.11:8002` / `10.115.244.11`
    포트를 안 적었으면 `server_port` 를 붙인다. 뒤에 붙은 경로·슬래시는 버린다.
    """
    b = (base or "").strip().rstrip("/")
    if not b:
        return ""
    if "://" not in b:
        b = "http://" + b
    scheme, _, rest = b.partition("://")
    host = rest.split("/", 1)[0]
    if not host:
        return ""
    if ":" not in host:
        host = f"{host}:{server_port}"
    return f"{scheme}://{host}"


def resolve_audio_url(robot_ip: str, url: str, server_port: int = 8002,
                      base_url: str = "") -> str:
    """설정에는 `/static/audio/...` 처럼 상대경로로 저장해두고, 보낼 때 절대주소로 바꾼다.

    ★ `base_url`(설정의 '서버 공개 주소')이 있으면 그걸 그대로 쓴다.
      비어 있으면 예전처럼 로봇 쪽 소켓에서 우리 로컬 IP 를 되읽는다.

      자동 판별은 **로봇과 서버가 같은 LAN 일 때만** 맞다. 현장처럼 라우터가
      둘로 갈리면 서버가 자기 사설 주소(예: 192.168.39.110)를 알려주는데,
      로봇 쪽 LAN 도 같은 192.168.39.x 대역이라 로봇이 그 주소를 **자기 동네에서**
      찾아 음원을 못 받는다. 그때는 서버 라우터의 LTE 주소 + 포워딩 포트
      (예: http://10.115.244.11:8002) 를 설정에 적어야 한다.
    """
    if not url or url.startswith(("http://", "https://")):
        return url
    base = normalize_base_url(base_url, server_port)
    if not base:
        host = server_ip_for(robot_ip)
        if not host:
            return ""
        base = f"http://{host}:{server_port}"
    return base + (url if url.startswith("/") else "/" + url)


def play_once(ip: str, sn: str, *, audio_id: str = "", url: str = "",
              volume: int = 80, mode: int = 2, duration: int = 8,
              server_port: int = 8002, base_url: str = "") -> Optional[int]:
    """1회 재생. interval 은 펌웨어가 무시하므로 반복은 호출부가 책임진다."""
    # ★ 재생은 **매 건 남긴다** (2026-09-15).
    #   종전에는 "주행 시작 — 안내 켬" 전이 때만 로그가 있어서, `interval_sec`
    #   마다 나가는 반복 재생이 흔적 없이 돌았다. "토글을 껐는데 소리가 난다" 를
    #   추적할 때 로그로는 아무것도 확인할 수 없어 원인 규명이 크게 늦어졌다.
    #   한 줄이면 언제·어느 로봇에·무슨 음원이·어떤 볼륨으로 나갔는지 다 남는다.
    logger.info(f"[voice] 재생 {ip} audio_id={audio_id!r} url={url!r} "
                f"volume={volume} mode={mode}")
    abs_url = resolve_audio_url(ip, url, server_port, base_url) if url else ""
    return _send_cmd(ip, sn, "startPlayAudio", {
        "mode": mode,
        "url": abs_url,
        "audioId": "" if abs_url else (audio_id or ""),
        "volume": volume,
        "interval": -1,
        "num": 1,
        "duration": duration,
    })


def stop_play(ip: str, sn: str, mode: int = 2) -> Optional[int]:
    return _send_cmd(ip, sn, "stopPlayAudio", {"mode": mode})


def set_volume(ip: str, sn: str, volume: int, mode: int = 2) -> Optional[int]:
    """로봇 볼륨 0~100. SDK 의 setVolume 이 내부적으로 보내는 것과 같은 프레임."""
    return _send_cmd(ip, sn, "setVoice", {"type": max(0, min(100, int(volume))), "mode": mode})


def list_builtin_audio(ip: str, timeout: float = 4.0) -> dict:
    """로봇에 심긴 음성 설정(언어 등) 조회. 실패해도 예외를 올리지 않는다."""
    import requests
    try:
        r = requests.get(f"http://{ip}:{AUDIO_HTTP_PORT}/custom_settings", timeout=timeout)
        return r.json().get("data", {}) if r.status_code == 200 else {}
    except Exception:
        return {}


# ── 로봇 한 대를 지켜보는 워커 ────────────────────────────────

def _worker(ip: str, sn: str) -> None:
    """9000 상태 스트림을 붙잡고 주행 여부에 따라 음성을 켜고 끈다."""
    from app.routers.settings import get_voice_settings

    playing = False
    last_sent = 0.0
    stopped_since = 0.0
    play_count = 0
    fail_streak = 0
    disabled_sent = False     # 꺼진 상태에서 정지를 이미 보냈나 (매 프레임 전송 방지)
    logger.info(f"[voice] 감시 시작 — {ip} ({sn})")
    # ★ 시작 시 남아 있을 수 있는 재생을 한 번 정리한다.
    #   직전 프로세스가 재생 중에 죽었으면 로봇은 계속 울고 있는데
    #   새 워커는 playing=False 로 시작해 그걸 모른다.
    try:
        stop_play(ip, sn)
    except Exception:
        pass

    while not _stop_event.is_set():
        ws = None
        try:
            ws = create_connection(f"ws://{ip}:{CMD_PORT}/", timeout=6)
            ws.settimeout(3)
            if fail_streak:
                logger.info(f"[voice] {ip} 재접속 성공 (실패 {fail_streak}회 뒤)")
            fail_streak = 0
            while not _stop_event.is_set():
                try:
                    raw = ws.recv()
                except WebSocketTimeoutException:
                    continue          # 잠깐 안 온 것뿐 — 계속 기다린다
                except Exception as e:
                    # 연결이 끊긴 경우다. 여기서 continue 하면 죽은 소켓을 붙들고
                    # 영원히 맴돌게 되므로 반드시 바깥 루프로 나가 재연결해야 한다.
                    logger.info(f"[voice] {ip} 스트림 끊김 → 재연결 ({type(e).__name__})")
                    break
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                st = msg.get("state")
                if not st:
                    continue

                cfg = get_voice_settings()
                if not cfg.get("enabled", False):
                    # ★ 2026-09-15 — `playing` 을 보고 끄면 안 된다.
                    #   `playing` 은 이 워커 스레드의 **로컬 변수**라, 백엔드가
                    #   리로드·재시작되면 False 로 초기화된다. 그때 로봇이 아직
                    #   울리고 있으면 서버는 "재생 안 함"으로 믿어 **정지를 영영
                    #   안 보냈다** — 토글을 꺼도 소리가 계속 나던 원인.
                    #   stopPlayAudio 는 멱등이라 그냥 보내도 부작용이 없다.
                    #   매 프레임 보내지 않도록 최초 1회만 보낸다.
                    if playing or not disabled_sent:
                        stop_play(ip, sn, int(cfg.get("mode", 2)))
                        if playing:
                            logger.info(f"[voice] {ip} 설정 꺼짐 — 안내 끔")
                        playing = False
                        disabled_sent = True
                    continue
                disabled_sent = False      # 다시 켜졌다 — 다음 끄기 때 또 보낸다

                # 판정은 speed(실제 바퀴 속도) 하나만 본다.
                # moveState 는 "명령을 들고 있는 상태"라서, 경로만 생성되고 못 움직이거나
                # 원격 모드로 취소된 뒤에도 moving 으로 남아 있는 경우가 있다.
                # 그러면 서 있는 로봇이 "이동중입니다"를 계속 외친다(2026-09-03 실제 발생).
                # 안내의 목적은 "실제로 움직이니 비켜라"이므로 물리적 속도가 유일하게 옳은 기준이다.
                speed = float(st.get("speed", 0) or 0)
                moving = abs(speed) > MOVING_SPEED

                if moving:
                    stopped_since = 0.0
                    now = time.time()
                    if not playing or now - last_sent >= float(cfg.get("interval_sec", 5)):
                        play_once(
                            ip, sn,
                            audio_id=cfg.get("audio_id", ""),
                            url=cfg.get("url", ""),
                            volume=int(cfg.get("volume", 80)),
                            mode=int(cfg.get("mode", 2)),
                            server_port=int(cfg.get("server_port", 8002)),
                            base_url=str(cfg.get("server_url", "")),
                        )
                        last_sent = now
                        play_count = play_count + 1 if playing else 1
                        if not playing:
                            playing = True
                            logger.info(f"[voice] {ip} 주행 시작 — 안내 켬")
                elif playing:
                    # 감속·잠깐 멈춤에 소리가 끊기지 않도록 정지가 이어질 때만 끈다
                    if stopped_since == 0.0:
                        stopped_since = time.time()
                    elif time.time() - stopped_since >= float(cfg.get("stop_delay_sec", 3)):
                        stop_play(ip, sn, int(cfg.get("mode", 2)))
                        logger.info(f"[voice] {ip} 주행 종료 — 안내 끔 ({play_count}회)")
                        playing = False
                        stopped_since = 0.0
                        play_count = 0
        except (WebSocketException, OSError) as e:
            if fail_streak == 0:
                logger.info(f"[voice] {ip} 접속 실패 — {RECONNECT_WAIT_SEC:.0f}초마다 재시도 ({e})")
            fail_streak += 1
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
        if not _stop_event.is_set():
            _stop_event.wait(RECONNECT_WAIT_SEC)

    if playing:
        stop_play(ip, sn)
    logger.info(f"[voice] 감시 종료 — {ip}")


# ── 로봇 목록을 보고 워커를 붙였다 뗐다 하는 관리자 ───────────

def _supervisor_loop() -> None:
    from app.database import SessionLocal
    from app.models.robot import Robot

    while not _stop_event.is_set():
        try:
            db = SessionLocal()
            try:
                robots = db.query(Robot).filter(
                    Robot.is_active == True,           # noqa: E712
                    Robot.ip_address != None,          # noqa: E711
                ).all()
                roster = {r.ip_address: (r.serial_number or r.name or "") for r in robots}
            finally:
                db.close()

            with _workers_lock:
                for ip, sn in roster.items():
                    t = _workers.get(ip)
                    if t is None or not t.is_alive():
                        t = threading.Thread(target=_worker, args=(ip, sn),
                                             name=f"voice-{ip}", daemon=True)
                        _workers[ip] = t
                        t.start()
                # 목록에서 빠진 로봇의 죽은 스레드는 정리만 한다
                for ip in [k for k, v in _workers.items() if k not in roster and not v.is_alive()]:
                    _workers.pop(ip, None)
        except Exception as e:
            logger.warning(f"[voice] 로봇 목록 갱신 실패: {e}")
        _stop_event.wait(ROSTER_REFRESH_SEC)


def start() -> None:
    global _supervisor
    if _supervisor and _supervisor.is_alive():
        return
    _stop_event.clear()
    _supervisor = threading.Thread(target=_supervisor_loop, name="voice-supervisor", daemon=True)
    _supervisor.start()
    logger.info("[voice] 주행 음성 안내 서비스 시작")


def stop() -> None:
    _stop_event.set()
    logger.info("[voice] 주행 음성 안내 서비스 정지 요청")
