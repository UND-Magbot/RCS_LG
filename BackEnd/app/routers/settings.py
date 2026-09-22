"""시스템 전역 설정 — 시스템 이름 등.

저장소: BackEnd/static/system_settings.json (도커 볼륨 마운트로 영속)
- DB 마이그레이션 불필요
- RCS 웹과 태블릿이 같은 값을 보도록 단일 진실 원천 역할
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

_SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent / "static" / "system_settings.json"
_DEFAULT_SYSTEM_NAME = "UND RCS"
_lock = threading.Lock()


def _read_settings() -> dict:
    if not _SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[settings] system_settings.json 읽기 실패: {e}")
        return {}


def _write_settings(data: dict) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class SystemNameResponse(BaseModel):
    system_name: str


class SystemNameUpdate(BaseModel):
    system_name: str = Field(..., max_length=200)


@router.get("/system-name", response_model=SystemNameResponse)
def get_system_name():
    with _lock:
        data = _read_settings()
    return SystemNameResponse(system_name=data.get("system_name") or _DEFAULT_SYSTEM_NAME)


@router.patch("/system-name", response_model=SystemNameResponse)
def update_system_name(payload: SystemNameUpdate):
    name = payload.system_name.strip()
    # 빈 문자열이면 기본값으로 복원 — 키 제거
    with _lock:
        data = _read_settings()
        if name:
            data["system_name"] = name
        else:
            data.pop("system_name", None)
        try:
            _write_settings(data)
        except Exception as e:
            logger.exception(f"[settings] system_settings.json 저장 실패: {e}")
            raise HTTPException(500, f"설정 저장 실패: {e}")
    effective = name or _DEFAULT_SYSTEM_NAME
    logger.info(f"[settings] system_name 변경: {effective}")
    return SystemNameResponse(system_name=effective)


# ── 주행 중 음성 안내 (LGIT 요청) ─────────────────────────────
# 로봇 오디오는 8090 이 아니라 9000 채널에 있다. 자세한 배경은 services/robot_voice.py 참조.

# LG 제공 음원 01 = "이동중입니다". url 은 상대경로로 두고 보낼 때 서버 IP 를 붙인다
# (설정에 IP 를 박아두면 현장에서 반드시 어긋난다 — robot_voice.resolve_audio_url 참조)
# 내장 음성을 쓰려면 url 을 비우고 audio_id 를 준다. 3512069 = "로봇이 임무수행중입니다. 안전에 주의하세요"
_LG_AUDIO_DIR = _SETTINGS_PATH.parent / "audio" / "lg"
_DEFAULT_VOICE = {
    "enabled": False,        # 기본은 꺼둔다 — 켜는 건 콘솔에서
    "volume": 80,            # 0~100
    "interval_sec": 5.0,     # 재생 주기. 펌웨어 자체 반복이 안 먹어 서버가 이 주기로 다시 쏜다
    "stop_delay_sec": 3.0,   # 이만큼 멈춰 있어야 음성을 끈다 (감속 중 끊김 방지)
    "audio_id": "3512069",   # url 이 비어 있을 때만 쓰인다
    "url": "/static/audio/lg/01_moving.mp3",
    "mode": 2,               # 1=상위기 / 2=섀시. 섀시가 기종 호환에 유리
    "server_port": 8002,     # 로봇이 음원을 받으러 올 우리 서버 포트
    # ★ 로봇이 우리 서버를 찾아올 주소. **비워두면 자동 판별**(같은 LAN 일 때만 맞다).
    #   현장처럼 로봇과 서버가 라우터로 갈리면 자동 판별이 서버의 사설 주소를 내놓는데,
    #   양쪽 LAN 이 같은 192.168.39.x 대역이라 로봇이 그 주소를 자기 동네에서 찾는다.
    #   그때 서버 라우터의 LTE 주소 + 포워딩 포트를 여기 적는다.
    #   예) http://10.115.244.11:8002   (포트를 빼면 server_port 를 붙여 쓴다)
    "server_url": "",
}


def get_voice_settings() -> dict:
    """저장된 값에 기본값을 덮어씌워 항상 완전한 dict 를 준다. 감시 워커가 매 틱 호출한다."""
    with _lock:
        saved = (_read_settings() or {}).get("voice") or {}
    return {**_DEFAULT_VOICE, **saved}


class VoiceSettings(BaseModel):
    enabled: bool
    volume: int = Field(..., ge=0, le=100)
    interval_sec: float = Field(..., ge=1.0, le=60.0)
    stop_delay_sec: float = Field(..., ge=0.0, le=30.0)
    audio_id: str = Field("", max_length=32)
    url: str = Field("", max_length=500)
    mode: int = Field(2, ge=1, le=2)
    server_port: int = Field(8002, ge=1, le=65535)
    server_url: str = Field("", max_length=200)


class AudioChoice(BaseModel):
    value: str      # url 로 쓸 상대경로. 내장 음성이면 "builtin:<audioId>"
    label: str


@router.get("/voice/audio-list", response_model=list[AudioChoice])
def list_audio_choices():
    """콘솔 드롭다운용 음원 목록 — LG 제공 mp3 + 로봇 내장 음성 몇 개."""
    out: list[AudioChoice] = []
    try:
        index_path = _LG_AUDIO_DIR / "_목록.json"
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            for no in sorted(index):
                item = index[no]
                out.append(AudioChoice(
                    value=f"/static/audio/lg/{item['file']}",
                    label=f"{no}. {item['text']}",
                ))
    except Exception as e:
        logger.warning(f"[settings] LG 음원 목록 읽기 실패: {e}")
    # 서버가 안 떠 있어도 나오는 폴백 — 로봇 안에 들어 있는 음성
    out.append(AudioChoice(value="builtin:3512069", label="[내장] 로봇이 임무수행중입니다"))
    out.append(AudioChoice(value="builtin:3512061", label="[내장] 로봇이 회전중이니 조심해주세요"))
    return out


class VoiceSettingsUpdate(BaseModel):
    enabled: bool | None = None
    volume: int | None = Field(None, ge=0, le=100)
    interval_sec: float | None = Field(None, ge=1.0, le=60.0)
    stop_delay_sec: float | None = Field(None, ge=0.0, le=30.0)
    audio_id: str | None = Field(None, max_length=32)
    url: str | None = Field(None, max_length=500)
    mode: int | None = Field(None, ge=1, le=2)
    server_port: int | None = Field(None, ge=1, le=65535)
    # 빈 문자열로 되돌릴 수 있어야 하므로 exclude_none 대상이 아닌 str 로 받는다
    server_url: str | None = Field(None, max_length=200)
    # 콘솔 드롭다운 전용 — "builtin:<audioId>" 또는 음원 상대경로 한 값으로 온다
    audio_choice: str | None = Field(None, max_length=500)


@router.get("/voice", response_model=VoiceSettings)
def get_voice():
    return VoiceSettings(**get_voice_settings())


@router.patch("/voice", response_model=VoiceSettings)
def update_voice(payload: VoiceSettingsUpdate):
    """보낸 항목만 바꾼다. 볼륨은 저장과 동시에 온라인 로봇에 바로 적용한다."""
    changes = payload.model_dump(exclude_none=True)
    # 드롭다운 한 값을 audio_id / url 짝으로 풀어준다. 둘 중 하나만 유효해야 헷갈리지 않는다
    choice = changes.pop("audio_choice", None)
    if choice:
        if choice.startswith("builtin:"):
            changes["audio_id"] = choice.split(":", 1)[1]
            changes["url"] = ""
        else:
            changes["url"] = choice
    with _lock:
        data = _read_settings()
        merged = {**_DEFAULT_VOICE, **(data.get("voice") or {}), **changes}
        data["voice"] = merged
        try:
            _write_settings(data)
        except Exception as e:
            logger.exception(f"[settings] voice 저장 실패: {e}")
            raise HTTPException(500, f"설정 저장 실패: {e}")
    logger.info(f"[settings] voice 변경: {changes}")

    # 볼륨은 다음 재생 때가 아니라 지금 바로 반영돼야 조작감이 맞다.
    # 다만 오프라인 로봇이 있으면 소켓 타임아웃(수 초)까지 기다리게 되고
    # 그동안 콘솔이 "저장 실패"로 보인다 → 저장은 이미 끝났으니 전송은 뒤로 넘긴다.
    #
    # ★ mode 가 바뀔 때도 보낸다 (2026-09-09).
    #   장치 볼륨(setVoice)은 **mode 마다 별개 값**이다 — 상위기(1) 볼륨과
    #   섀시(2) 볼륨이 따로 있다. 예전에는 volume 숫자가 바뀔 때만 보내서,
    #   mode 만 1 로 바꾸면 그쪽 볼륨은 **한 번도 설정되지 않은 채** 남았다.
    #   그 상태로 재생하면 당연히 작게 들리는데, 원인이 음원인지 스피커인지
    #   구분이 안 돼 오진하기 쉽다. 현장에서 '슬라이더를 한 번 움직여야 한다'는
    #   숨은 절차를 없애는 것이 목적이다.
    if "volume" in changes or "mode" in changes:
        from app.services.thread_utils import safe_thread
        safe_thread(
            target=_push_volume_to_robots,
            args=(int(merged["volume"]), int(merged["mode"])),
            name="voice-volume-push",
        ).start()
    return VoiceSettings(**merged)


def _push_volume_to_robots(volume: int, mode: int) -> None:
    from app.database import SessionLocal
    from app.models.robot import Robot
    from app.services import robot_voice
    db = SessionLocal()
    try:
        robots = db.query(Robot).filter(
            Robot.is_active == True,      # noqa: E712
            Robot.ip_address != None,     # noqa: E711
        ).all()
        for r in robots:
            try:
                robot_voice.set_volume(r.ip_address, r.serial_number or "", volume, mode)
            except Exception:
                logger.warning(f"[settings] 볼륨 적용 실패: {r.name} ({r.ip_address})")
    finally:
        db.close()


# ── 전방 장애물 안전거리 (Yellow/Red) — LGIT 요청 4번 ──────────
# 로봇에는 '로봇을 따라다니는 2단계 링'이 없다(2026-09-04 확인). 맵에 고정된
# regionType 구역만 있어서 전방 감지는 서버가 직접 해야 한다.
#
# ★ 인증된 안전 장치가 아니다. 안전 라이다(IEC 61496-3 + PL d)가 아니라
#   소프트웨어 보조 정지다. 로봇은 사람과 사물을 구분하지 못하므로
#   (supportsVisionBasedDetector: false) '작업자 감지'가 아니라 '장애물 감지'다.
#
# 정지거리 근거(2026-09-04 실측):
#   /scan_matched_points2 1.86 Hz(0.53초) · control.max_forward_decel -2.0 m/s²
#   0.7 m/s 기준 (0.53+0.15)*0.7 + 0.49/(2*2.0) = 0.61 m
#   → red_m 하한을 0.7 로 둔다. 그보다 짧게 잡으면 물리적으로 못 멈춘다.
RED_MIN_M = 0.7

_DEFAULT_SAFETY = {
    "enabled": False,       # 기본은 꺼둔다 — 켜는 건 콘솔에서
    "yellow_m": 3.0,        # 이 안에 들어오면 서행
    "red_m": 1.0,           # 이 안이면 정지
    "slow_speed": 0.3,      # Yellow 구간 주행 속도 (m/s)
    "band_half_w": 0.6,     # 감지 밴드 반폭. 부채꼴이 아니라 직사각형이다
    "near_min": 0.45,       # 이보다 가까운 점은 무시 — 자기가 든 랙을 장애물로 오인 방지
    # 장애물이 사라진 뒤 이만큼 지나야 다시 출발한다.
    # 2026-09-08 LG 통화 — **대기 없이 즉시 출발**로 확정. 1.5 → 0.
    # (그 전 "3초 뒤 출발" 안은 통화에서 철회됨)
    # 깜빡임 방지는 히스테리시스(HYSTERESIS_M 0.30m)가 1차로 막는다.
    # 실기에서 정지/출발이 덜컹거리면 0.3~0.5 정도만 다시 주면 된다.
    "clear_hold_sec": 0.0,
}


def get_safety_settings() -> dict:
    """저장값 + 기본값. 감시 워커가 매 틱 호출한다."""
    with _lock:
        saved = (_read_settings() or {}).get("safety") or {}
    return {**_DEFAULT_SAFETY, **saved}


class SafetySettings(BaseModel):
    enabled: bool
    yellow_m: float = Field(..., ge=0.5, le=10.0)
    red_m: float = Field(..., ge=RED_MIN_M, le=5.0)
    slow_speed: float = Field(..., ge=0.1, le=1.2)
    band_half_w: float = Field(..., ge=0.2, le=2.0)
    near_min: float = Field(..., ge=0.0, le=1.0)
    clear_hold_sec: float = Field(..., ge=0.0, le=10.0)


class SafetySettingsUpdate(BaseModel):
    enabled: bool | None = None
    yellow_m: float | None = Field(None, ge=0.5, le=10.0)
    red_m: float | None = Field(None, ge=RED_MIN_M, le=5.0)
    slow_speed: float | None = Field(None, ge=0.1, le=1.2)
    band_half_w: float | None = Field(None, ge=0.2, le=2.0)
    near_min: float | None = Field(None, ge=0.0, le=1.0)
    clear_hold_sec: float | None = Field(None, ge=0.0, le=10.0)


@router.get("/safety", response_model=SafetySettings)
def get_safety():
    return SafetySettings(**get_safety_settings())


@router.patch("/safety", response_model=SafetySettings)
def update_safety(payload: SafetySettingsUpdate):
    """보낸 항목만 바꾼다."""
    changes = payload.model_dump(exclude_none=True)
    with _lock:
        data = _read_settings()
        merged = {**_DEFAULT_SAFETY, **(data.get("safety") or {}), **changes}
        # Red 가 Yellow 보다 멀면 단계가 뒤집힌다 — 저장 단계에서 막는다
        if merged["red_m"] >= merged["yellow_m"]:
            raise HTTPException(400, "정지 거리는 서행 거리보다 짧아야 합니다.")
        data["safety"] = merged
        try:
            _write_settings(data)
        except Exception as e:
            logger.exception(f"[settings] safety 저장 실패: {e}")
            raise HTTPException(500, f"설정 저장 실패: {e}")
    logger.info(f"[settings] safety 변경: {changes}")
    return SafetySettings(**merged)


@router.get("/safety/alerts")
def get_safety_alerts():
    """전방 장애물로 멈춰 있는 로봇 — 콘솔·태블릿이 폴링해 배너를 띄운다."""
    from app.services import safety_zone
    return safety_zone.active_alerts()


@router.get("/safety/status")
def get_safety_status():
    """로봇별 현재 존(clear/yellow/red)과 전방 최근접 거리 — 콘솔 표시용."""
    from app.services import safety_zone
    return safety_zone.status()


# ── POI 표시 이름(한글) · 구역 — LGIT 요청 4·5번 ──────────────
# 저장소는 BackEnd/static/poi_labels.json 이고, 실제 번역은 services/poi_label.py 가 한다.
# DB 의 POI 이름(R1/J1…)은 **절대 바꾸지 않는다** — 배경은 poi_label.py 독스트링 참조.


class PoiLabelItem(BaseModel):
    label: str = Field("", max_length=100)
    zone: str | None = Field(None, max_length=50)


@router.get("/poi-labels")
def get_poi_labels():
    """POI 영문 이름 → {표시 이름, 구역} 전체 매핑."""
    from app.services import poi_label
    return poi_label.all_labels()


@router.get("/poi-labels/editable")
def get_poi_labels_editable():
    """콘솔 '작업지점 이름' 편집창이 쓰는 목록.

    `GET /poi-labels` 는 **저장된 매핑만** 준다. 아직 이름을 안 붙인 지점은
    빠져서 화면에 뜨지 않는다. 여기서는 활성 맵의 **실제 작업지점(R·J)** 을
    전부 훑어 저장값과 합쳐 준다 — 현장에서 "R1 이 안 보인다" 가 없게.

    진입점(`R1-1` 처럼 이름에 `-` 가 든 것)과 충전소는 제외한다.
    """
    from app.database import SessionLocal
    from app.models.map import MapPOI, RobotMap
    from app.services import poi_label

    saved = poi_label.all_labels()
    rows: list[dict] = []
    seen: set[str] = set()
    db = SessionLocal()
    try:
        active = (db.query(RobotMap)
                    .filter(RobotMap.is_active == True)   # noqa: E712
                    .order_by(RobotMap.id.desc()).first())
        if active:
            pois = (db.query(MapPOI)
                      .filter(MapPOI.map_id == active.id,
                              MapPOI.is_active == True)   # noqa: E712
                      .all())
            for p in pois:
                nm = (p.name or "").strip()
                if not nm or "-" in nm:
                    continue
                if (p.poi_type or "").lower() not in ("jack", "standby"):
                    continue
                seen.add(nm)
                cur = saved.get(nm) or {}
                rows.append({"name": nm, "poi_type": p.poi_type,
                             "label": cur.get("label") or "",
                             "zone": cur.get("zone") or ""})
    except Exception as e:
        logger.warning(f"[settings] 작업지점 목록 조회 실패(저장값만 반환): {e}")
    finally:
        db.close()

    # 맵에서 사라졌지만 매핑이 남아 있는 이름도 보여준다(지울 수 있게).
    for nm, v in saved.items():
        if nm not in seen:
            rows.append({"name": nm, "poi_type": None,
                         "label": v.get("label") or "", "zone": v.get("zone") or ""})
    # 2026-09-21 — **저장된 파일 순서를 그대로 지킨다.**
    #   종전에는 `rows.sort(key=lambda r: r["name"])` 로 이름 사전순(J1,J2,R1,R2)
    #   으로 다시 세웠다. 이 목록 순서 그대로 화면이 PUT 을 만들고 save_all 이
    #   그 순서로 파일을 다시 쓰는데, 콘솔 좌/우 칸 순서는 **파일의 구역 등장
    #   순서**(poi_label.zone_order)라서, 현장에서 [작업지점 이름] 을 한 번
    #   저장하는 것만으로 좌우 칸이 뒤집혔다.
    #   저장에 없는(새로 찍은) 지점만 뒤에 이름순으로 붙인다.
    order = {nm: i for i, nm in enumerate(saved.keys())}
    rows.sort(key=lambda r: (order.get(r["name"], len(order)), r["name"]))
    return rows


@router.put("/poi-labels")
def put_poi_labels(payload: dict[str, PoiLabelItem]):
    """전체 덮어쓰기. **빈 객체를 보내면 매핑이 비워져 원래 이름으로 돌아간다**(롤백)."""
    from app.services import poi_label
    try:
        return poi_label.save_all(
            {k: v.model_dump() for k, v in (payload or {}).items()})
    except Exception as e:
        logger.exception(f"[settings] poi_labels.json 저장 실패: {e}")
        raise HTTPException(500, f"표시 이름 저장 실패: {e}")


# ── 주행 설정 (2026-09-22) ─────────────────────────────────────
# 현장에서 값을 바꿔가며 원인을 좁히려고 코드 상수를 밖으로 뺀 것이다.
# 현장 안에서는 인터넷이 없어 코드를 못 고친다. 한 번 들어가면 안에서 끝내야 한다.
#
# detour_tolerance — 경유지 경로에서 벗어나도 되는 거리(m)
#   0   = 준 경로를 그대로 따른다. 장애물을 만나면 우회하지 않고 앞에서 멈춘다.
#         LG 요구("사람이 접근하면 회피하지 않고 정지")와 일치한다.
#   0.2 = 선에서 20cm 는 흔들려도 된다. 경로는 똑같이 따라간다.
#
#   ★ 왜 빼놨나 (2026-09-22 현장 로그 분석)
#     사람이 표시한 멈칫 10건이 **전부 along_given_route** 에서 났다
#     (standard 80m·align_with_rack 11m 에서는 0건).
#     그때 앞은 5m 까지 비었고, 방향도 안 틀었고(누적 회전 정상 통과와 1.00배),
#     서버는 상한을 안 건드렸고(8건), CPU·통신·위치추정도 평소와 같았다.
#     남은 설명은 "선에서 벗어나지 마라"는 제약 때문에 로봇이 실패 대신
#     속도를 버리는 것뿐이다. 심한 경우 20초를 기어갔다.
#
#   ⚠️ 이 값은 **안전 요구와 닿아 있다.** 올리기 전에 사람이 앞을 막았을 때
#      여전히 서는지 실기로 확인할 것. 로봇 반폭이 0.5m 라 0.3m 로는
#      사람을 피해 돌아갈 수 없다(= 회피가 아니라 추종 오차 허용).
_DEFAULT_DRIVE = {
    "detour_tolerance": 0.0,
}


def get_drive_settings() -> dict:
    """저장값 + 기본값. waypoint_route 가 경로를 만들 때마다 부른다."""
    with _lock:
        saved = (_read_settings() or {}).get("drive") or {}
    return {**_DEFAULT_DRIVE, **saved}


class DriveSettings(BaseModel):
    detour_tolerance: float = Field(..., ge=0.0, le=1.0)


class DriveSettingsUpdate(BaseModel):
    detour_tolerance: float | None = Field(None, ge=0.0, le=1.0)


@router.get("/drive", response_model=DriveSettings)
def get_drive():
    return DriveSettings(**get_drive_settings())


@router.patch("/drive", response_model=DriveSettings)
def update_drive(payload: DriveSettingsUpdate):
    """보낸 항목만 바꾼다. 다음 이동 명령부터 적용된다(재시작 불필요)."""
    changes = payload.model_dump(exclude_none=True)
    with _lock:
        data = _read_settings()
        before = {**_DEFAULT_DRIVE, **(data.get("drive") or {})}
        merged = {**before, **changes}
        data["drive"] = merged
        try:
            _write_settings(data)
        except Exception as e:
            logger.exception(f"[settings] drive 저장 실패: {e}")
            raise HTTPException(500, f"설정 저장 실패: {e}")
    # 안전과 닿은 값이라 바뀐 내용을 기존값과 같이 남긴다
    logger.warning("[settings] 주행 설정 변경: detour_tolerance %.2f → %.2f m"
                   % (before["detour_tolerance"], merged["detour_tolerance"]))
    return DriveSettings(**merged)
