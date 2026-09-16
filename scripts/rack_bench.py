"""LG2 랙 인식 벤치 — 접지 체인이 라이다에 '가짜 다리'로 잡히는지 확인/조치 검증용.

배차(세션·워커·경유지)를 전혀 거치지 않고 **랙 인식만** 반복한다.
스페이스바 한 번에 [랙 진입], 다시 한 번에 [랙 반납]. 매 시도의 결과와
로봇이 실제로 잰 치수(detected)를 기록해 조건별로 비교한다.

── 왜 이렇게 만드는가 ────────────────────────────────────────
접지 체인은 바닥에 닿아 있어야 하므로 **수직으로 늘어져 라이다 평면을 반드시 통과**한다.
높이로는 피할 수 없고, 수평 위치를 진짜 다리와 겹치게 만드는 것만이 방법이다.
그래서 검증해야 할 것은 "체인 상태를 바꾸면 detected 가 달라지는가" 하나다.

  조건 C (체인 제거)   = 기준선. 순수하게 랙만의 치수
  조건 A (현상태)      = C 와 다르면 → 체인이 원인 확정
  조건 B (기둥에 밀착) = C 와 같아지면 → 해결 (접지 유지한 채)

'a' / 'b' / 'c' 키로 현재 조건을 지정하면 모든 기록에 라벨이 붙고,
종료 시 조건별 집계표가 나온다.

── 설계상 중요한 점 ─────────────────────────────────────────
* jack_service.align_with_retry() 를 쓰지 않는다.
  그 함수는 실패 시 재로컬화 → 후진 0.5m → 측면 우회로 4번까지 시도하므로,
  체인 때문에 1차가 실패해도 3차에서 우연히 성공해 "된다"로 보인다.
  여기서 재려는 것은 **1차 시도 성공률**이라 align_with_rack 을 딱 1회만 보낸다.
* 로봇(8090)에 직접 붙는다. 백엔드를 띄울 필요가 없고, zone_guard·워커 상태 같은
  부수효과가 섞이지 않는다. DB 는 R1 좌표를 읽는 데만 쓴다.

── 안전 ─────────────────────────────────────────────────────
실기 로봇이 실제로 움직인다. 옆에서 지켜볼 것.
'x' 키 또는 Ctrl+C 로 현재 이동을 즉시 취소한다(그 자리 정지, 잭은 그대로).

── 사용 ─────────────────────────────────────────────────────
  BackEnd\\venv\\Scripts\\python.exe scripts\\rack_bench.py
  BackEnd\\venv\\Scripts\\python.exe scripts\\rack_bench.py --dry-run   # 연결만 확인
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "BackEnd"))

# ── DB 접속 (run_local.ps1 과 동일한 운영 DB) — app import 전에 설정해야 한다 ──
os.environ.setdefault("DB_NAME", "rcs_lg_db")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_USER", "root")
os.environ.setdefault("DB_PASSWORD", "1234")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests  # noqa: E402

try:
    import msvcrt  # Windows 전용 — 엔터 없이 키 하나 읽기
except ImportError:
    msvcrt = None

ROBOT_PORT = 8090
HTTP_TIMEOUT = 15          # LTE 지연 대응 (jack_service 와 동일)
JACK_SETTLE_SEC = 10       # 잭 업/다운 후 안정화 (jack_service.JACK_WAIT_SEC 와 동일)
JACK_CMD_TIMEOUT = 30      # 잭 명령 응답 대기 — 동작이 10초 이상이라 넉넉히
JACK_WEIGHT_THRESHOLD = 30 # 이 이상이면 랙을 들고 있다고 본다 (실측 65~69)
ALIGN_TIMEOUT = 120

# ── 재시도(회복) 파라미터 — jack_service.align_with_retry 와 같은 값 ──
ALIGN_BACKOFF_M = 0.5          # 3차: 후진 거리
ALIGN_LATERAL_OFFSET_M = 0.3   # 4차: 측면 우회 거리
RETRY_STAGE_NAME = {
    1: "1차(그대로)",
    2: "2차(재로컬화)",
    3: "3차(후진+재로컬화)",
    4: "4차(측면우회+재로컬화)",
}
# AutoXing 기본 시크릿 — BackEnd/app/routers/robot.py:39 의 DEFAULT_SECRET 과 같은 값.
# (app.routers.robot 을 import 하면 FastAPI 라우터가 통째로 딸려오므로 값만 옮겨둔다)
DEFAULT_SECRET = "19a11878aaab420fba94577ce3620dce"

LOG_DIR = ROOT / "_logs"
RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")


# ══════════════════════════════════════════════════════════
# 출력 / 로그
# ══════════════════════════════════════════════════════════

class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fp = path.open("a", encoding="utf-8")

    def event(self, kind: str, **fields):
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, **fields}
        self.fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fp.flush()

    def close(self):
        try:
            self.fp.close()
        except Exception:
            pass


def say(msg: str, mark: str = "·"):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {mark} {msg}", flush=True)


# ══════════════════════════════════════════════════════════
# 로봇 통신 (직접)
# ══════════════════════════════════════════════════════════

class Robot:
    def __init__(self, ip: str, secret: str = DEFAULT_SECRET, dry: bool = False):
        self.ip = ip
        self.secret = secret
        self.dry = dry

    def _url(self, path: str) -> str:
        return f"http://{self.ip}:{ROBOT_PORT}{path}"

    def get(self, path: str, timeout: int = HTTP_TIMEOUT) -> dict:
        r = requests.get(self._url(path), timeout=timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict | None = None, timeout: int = HTTP_TIMEOUT) -> dict:
        if self.dry:
            say(f"[dry-run] POST {path} {body or ''}", "→")
            return {"dry": True}
        r = requests.post(self._url(path), json=body or {}, timeout=timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def patch_settings(self, body: dict) -> dict:
        """/system/settings/user PATCH — 맵 동기화 쪽과 동일하게 Secret 헤더를 붙인다.

        (백엔드의 jack_service.robot_patch 는 헤더를 안 붙인다. 인증이 필요한 펌웨어면
         그쪽 경로는 실패하므로, 여기서는 map.py 가 쓰는 방식을 그대로 따른다)
        """
        if self.dry:
            say(f"[dry-run] PATCH /system/settings/user {list(body)}", "→")
            return {"dry": True}
        r = requests.patch(
            self._url("/system/settings/user"),
            headers={"Authorization": f"Secret {self.secret}"},
            json=body, timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    # ── 랙 스펙 ──
    def get_rack_specs(self) -> list:
        return self.get("/system/settings/user").get("rack.specs", [])

    def set_rack_spec(self, spec: dict) -> dict:
        return self.patch_settings({"rack.specs": [spec]})

    # ── 잭 ──
    # ⚠️ 잭 동작은 10초 이상 걸린다. 기본 타임아웃(15초)으로는 응답을 놓치기 쉽고,
    #    응답을 못 받았다고 해서 **명령이 실행 안 된 게 아니다**(2026-08-13 실제 사고).
    def jack_up(self) -> dict:
        return self.post("/services/jack_up", timeout=JACK_CMD_TIMEOUT)

    def jack_down(self) -> dict:
        return self.post("/services/jack_down", timeout=JACK_CMD_TIMEOUT)

    def jack_state(self) -> dict | None:
        """WS /jack_state 로 잭 실제 상태. REST 에는 없다.

        {"state": "hold", "progress": 1.0, "weight": 65, ...}
        `/tracked_pose` 와 달리 **정지 상태에서도 발행된다**(2026-08-13 실측).
        """
        for pkt in reversed(ws_collect(self.ip, "/jack_state", 4.0)):
            if pkt.get("state") is not None or pkt.get("weight") is not None:
                return pkt
        return None

    def is_jack_up(self) -> bool | None:
        """잭이 올라가 있는가. True/False, 판정 불가면 None.

        ★ 판정 기준은 **progress** 다. weight 가 아니다.

        2026-08-13 사고: weight(적재 하중)로 판정했다가 **"잭은 올라갔는데 랙을 못 받친"**
        경우를 '내려감'으로 오판했다. 그 상태로 후진해 랙을 끌었고, 이후 모든 정렬이
        `506 jack is in up state` 로 거부됐다.

        실측:
            랙 적재:   state=hold, progress=1.0, weight=65
            내려놓음:  state=hold, progress=0.0, weight=0
            잭만 올림: state=hold, progress=1.0, weight=0   ← weight 로는 구분 불가
        """
        st = self.jack_state()
        if st is None:
            return None
        state = str(st.get("state", "")).lower()
        if state in ("jacking_up", "up"):
            return True
        if state in ("jacking_down", "down", "lowered"):
            return False
        if state == "hold":
            # hold = '동작 완료 후 유지'. 어느 쪽으로 완료했는지는 progress 가 말해준다.
            try:
                progress = float(st.get("progress", -1))
            except (TypeError, ValueError):
                return None
            if progress >= 0.9:
                return True
            if 0 <= progress <= 0.1:
                return False
            return None      # 중간값 = 동작 중이거나 이상 → 판정 불가(보수적으로 처리됨)
        return None

    # ── 이동 ──
    def create_move(self, move_type: str, x: float, y: float, ori: float) -> int | None:
        resp = self.post("/chassis/moves", {
            "creator": "rcs",          # 검증된 값. 임의 문자열은 펌웨어 동작 미확인
            "type": move_type,
            "target_x": x, "target_y": y, "target_ori": ori,
        })
        return resp.get("id")

    def move_state(self, move_id: int) -> dict:
        return self.get(f"/chassis/moves/{move_id}")

    def cancel_move(self) -> dict:
        r = requests.patch(self._url("/chassis/moves/current"),
                           json={"state": "cancelled"}, timeout=10)
        return r.json() if r.content else {"status": r.status_code}

    # ── 섀시 상태 (비상정지 감지) ──
    def chassis_status(self) -> dict:
        """control_mode / emergency_stop_pressed / wheel_overloaded (실측 확인된 필드)."""
        return self.get("/chassis/status", timeout=8)

    # ── 위치 재보정 (재시도 2~4차의 공통 회복 동작) ──
    def relocalize(self, max_wait_sec: int = 15) -> bool:
        """start_global_positioning 후 /slam/state 로 매칭 확인.

        jack_service.recover_positioning 과 같은 방식. 실패해도 다음 시도는 진행한다.
        """
        import websocket as _ws
        try:
            self.post("/services/start_global_positioning",
                      {"use_barcode": True, "use_base_map_match": True}, timeout=8)
        except Exception as e:
            say(f"  위치 재보정 호출 실패(무시): {e}", "~")
            return False
        ws = None
        try:
            ws = _ws.create_connection(f"ws://{self.ip}:{ROBOT_PORT}/ws/v2/topics", timeout=3)
            for t in ("/global_positioning_state", "/slam/state"):
                try:
                    ws.send(json.dumps({"enable_topic": t}))
                except Exception:
                    pass
            ws.settimeout(2)
            end = time.time() + max_wait_sec
            while time.time() < end:
                try:
                    pkt = json.loads(ws.recv())
                except Exception:
                    continue
                topic = pkt.get("topic", "")
                if topic == "/global_positioning_state":
                    st = pkt.get("state", "")
                    if st in ("succeeded", "success", "done"):
                        return True
                    if st in ("failed", "error"):
                        return False
                elif topic == "/slam/state":
                    if pkt.get("lidar_matched") or pkt.get("reliable"):
                        return True
        except Exception:
            pass
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        return False

    def latest_move_id(self) -> int:
        try:
            moves = self.get("/chassis/moves?page=1&page_size=20")
            items = moves if isinstance(moves, list) else moves.get("items", moves.get("list", []))
            return max((m.get("id") or 0 for m in items), default=0)
        except Exception:
            return 0


# ══════════════════════════════════════════════════════════
# 정렬 중 랙 검출 관찰
# ══════════════════════════════════════════════════════════
# 정렬이 실패하면 로봇은 `MoveFailReason::rack_detection_error` 만 돌려준다.
# "치수가 안 맞았다" 인지 "아예 못 찾았다" 인지 구분할 근거가 전혀 없다(2026-08-12 실측).
# 그래서 정렬이 도는 동안 /detected_rack 을 옆에서 같이 구독해 둔다.
#   → 실패 순간 로봇이 랙을 보긴 봤는지, 봤다면 몇 x 몇 으로 봤는지가 남는다.
#
# ※ /tracked_pose 로 랙 각도를 역산하는 방식은 폐기했다.
#   로봇이 정지 상태면 그 토픽이 아예 발행되지 않아(12초 구독 0건) 값을 못 얻는다.
#   자세 구분은 세트 라벨(사람이 직접 입력)로 한다.

class RackWatch:
    """align 이 도는 동안 백그라운드로 /detected_rack 을 모은다."""

    def __init__(self, ip: str):
        self.ip = ip
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.seen = 0          # 받은 패킷 수
        self.detected = 0      # rack_detected=True 였던 횟수
        self.sizes: list[dict] = []

    def start(self) -> None:
        self._stop.clear()
        self.seen = self.detected = 0
        self.sizes = []
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import websocket as _ws
        ws = None
        try:
            ws = _ws.create_connection(f"ws://{self.ip}:{ROBOT_PORT}/ws/v2/topics", timeout=3)
            ws.send(json.dumps({"enable_topic": "/detected_rack"}))
            ws.settimeout(1.5)
            while not self._stop.is_set():
                try:
                    pkt = json.loads(ws.recv())
                except Exception:
                    continue
                if pkt.get("topic") != "/detected_rack":
                    continue
                self.seen += 1
                if pkt.get("rack_detected"):
                    self.detected += 1
                    s = _extract_size(pkt)
                    if s:
                        self.sizes.append(s)
        except Exception:
            pass
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def stop(self) -> dict:
        """수집 종료 + 요약. 실패 원인 판단용."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        out = {"packets": self.seen, "detected": self.detected}
        if self.sizes:
            w = sorted(s["width"] for s in self.sizes)
            d = sorted(s["depth"] for s in self.sizes)
            mid = len(w) // 2
            out["median"] = [w[mid], d[mid]]
            out["range"] = [[w[0], w[-1]], [d[0], d[-1]]]
        return out


def ws_collect(ip: str, topic: str, seconds: float) -> list[dict]:
    """WS /ws/v2/topics 로 한 토픽을 seconds 동안 수집.

    잭 상태·랙 검출값은 REST 에 없고 WS 토픽으로만 온다(2026-08-11 실측).
    """
    import websocket as _ws

    out: list[dict] = []
    ws = None
    try:
        ws = _ws.create_connection(f"ws://{ip}:{ROBOT_PORT}/ws/v2/topics", timeout=3)
        ws.send(json.dumps({"enable_topic": topic}))
        ws.settimeout(2)
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = ws.recv()
            except Exception:
                continue
            try:
                pkt = json.loads(raw)
            except Exception:
                continue
            if pkt.get("topic") == topic:
                out.append(pkt)
    except Exception as e:
        say(f"WS 수집 실패({topic}): {e}", "~")
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    return out


# ══════════════════════════════════════════════════════════
# 랙 치수 측정
# ══════════════════════════════════════════════════════════

def _extract_size(pkt: dict) -> dict | None:
    """/detected_rack 패킷에서 폭/깊이. 펌웨어별 필드 차이를 관대하게 흡수."""
    if not pkt.get("rack_detected", False):
        return None
    for key in ("rack_box_aligned", "rack_box"):
        box = pkt.get(key)
        if not isinstance(box, dict):
            continue
        w = box.get("width")
        d = box.get("depth", box.get("height"))   # depth 가 height 로 오는 펌웨어 있음
        if w is None or d is None:
            continue
        try:
            w, d = round(float(w), 4), round(float(d), 4)
        except (TypeError, ValueError):
            continue
        if w > 0 and d > 0:
            return {"width": w, "depth": d}
    return None


def measure(rb: Robot, seconds: float = 15.0) -> dict:
    """start_rack_size_detection → /detected_rack 수집 → stop. 중앙값 + 관측 범위."""
    try:
        rb.post("/services/start_rack_size_detection")
    except Exception as e:
        return {"error": f"감지 시작 실패: {e}"}

    say(f"{seconds:.0f}초간 측정 중 — 로봇/랙을 움직이지 마세요")
    pkts = ws_collect(rb.ip, "/detected_rack", seconds)

    try:
        rb.post("/services/stop_rack_size_detection")
    except Exception as e:
        say(f"감지 중지 요청 실패(무시): {e}", "~")

    sizes = [s for s in (_extract_size(p) for p in pkts) if s]
    if not sizes:
        return {"packets": len(pkts), "samples": 0,
                "error": "측정값 없음 — 랙 다리가 라이다에 보이는 위치인지 확인하세요"}

    ws_ = sorted(s["width"] for s in sizes)
    ds_ = sorted(s["depth"] for s in sizes)
    mid = len(ws_) // 2
    return {
        "packets": len(pkts), "samples": len(sizes),
        "width": ws_[mid], "depth": ds_[mid],
        "width_range": [ws_[0], ws_[-1]],
        "depth_range": [ds_[0], ds_[-1]],
    }


def parse_detected(fail_message: str | None) -> dict | None:
    """`Wrong rack size: detected(0.71,0.58), configed(0.64,0.545)` 에서 detected 추출."""
    if not fail_message:
        return None
    import re
    m = re.search(r"detected\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)", str(fail_message))
    if not m:
        return None
    try:
        return {"width": float(m.group(1)), "depth": float(m.group(2))}
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════
# 동작 — 진입 / 반납
# ══════════════════════════════════════════════════════════

class Bench:
    def __init__(self, rb: Robot, poi: dict, log: Log, backoff: float,
                 max_attempts: int = 1):
        self.rb = rb
        self.poi = poi
        self.log = log
        self.backoff = backoff
        self.max_attempts = max_attempts   # 1 = 재시도 없음, 4 = 백엔드와 동일
        self.jacked = False          # 잭이 올라가 있는가 (= 랙을 들고 있는가)
        self.cond = "A"              # 현재 실험 조건 라벨
        self.trials: list[dict] = []
        # ── 자동 반복(세트) ──
        # 세트 = "랙을 한 자세로 둔 채 N회 반복". 세트가 끝나면 사람이 랙 자세를 바꾸고
        # 다음 세트를 시작한다. 자세 구분은 세트 라벨(사람이 직접 입력)로 한다.
        self.set_no = 1
        self.set_label = ""
        self.abort = False           # 자동 루프 중단 요청

    # ══════════════════════════════════════════════════
    # 방해 요인 처리 — 랙 인식 실패와 섞이지 않게 분리한다
    # ══════════════════════════════════════════════════

    def wait_if_blocked(self) -> str | None:
        """비상정지·원격모드·통신두절이면 풀릴 때까지 대기.

        2026-08-12 테스트에서 E-STOP 5건·공유기 OFF 2건이 '랙 인식 실패'로 집계되어
        성공률을 오염시켰다. 이런 건 실패로 세지 말고 **멈췄다가 이어서** 해야 한다.

        반환: 대기했으면 사유 문자열, 정상이면 None
        """
        reason = None
        waited = 0.0
        while True:
            try:
                st = self.rb.chassis_status()
            except Exception as e:
                cur = f"통신 두절 ({type(e).__name__})"
            else:
                if st.get("emergency_stop_pressed"):
                    cur = "비상정지(E-STOP) 눌림"
                elif str(st.get("control_mode", "auto")) != "auto":
                    cur = f"제어 모드가 auto 아님 ({st.get('control_mode')})"
                elif st.get("wheel_overloaded"):
                    cur = "바퀴 과부하(wheel_overloaded)"
                else:
                    cur = None

            if cur is None:
                if reason:
                    say(f"해제됨 — {waited:.0f}초 대기 후 이어서 진행합니다", "✔")
                    self.log.event("blocked_cleared", reason=reason,
                                   waited_sec=round(waited, 1), set=self.set_no)
                return reason

            if reason is None:            # 처음 감지
                reason = cur
                say("=" * 58, "!")
                say(f"{cur} — 대기합니다. 해제하면 자동으로 이어집니다.", "!")
                say("  (이 시간은 실패로 세지 않습니다.  중단하려면 q)", "·")
                say("=" * 58, "!")
                self.log.event("blocked", reason=cur, set=self.set_no)
            if self._check_abort_key():
                return reason
            time.sleep(3.0)
            waited += 3.0

    @staticmethod
    def _is_comm_error(exc: Exception) -> bool:
        return isinstance(exc, (requests.exceptions.ConnectionError,
                                requests.exceptions.Timeout))

    # ── 이동 1회 (재시도 없음) ──
    def _run_move(self, move_type: str, x: float, y: float, ori: float,
                  timeout: float) -> dict:
        t0 = time.time()
        try:
            move_id = self.rb.create_move(move_type, x, y, ori)
        except Exception as e:
            return {"state": "failed", "cls": "comm" if self._is_comm_error(e) else "other",
                    "fail_message": f"명령 전송 실패: {e}",
                    "elapsed": time.time() - t0}
        if move_id is None:
            return {"state": "failed", "cls": "other", "fail_message": "move id 없음",
                    "elapsed": time.time() - t0}

        end = time.time() + timeout
        while time.time() < end:
            # 대기 중에도 'x'(취소) 키를 받는다
            if msvcrt and msvcrt.kbhit():
                k = msvcrt.getch().lower()
                if k == b"x":
                    say("이동 취소 요청", "!")
                    try:
                        self.rb.cancel_move()
                    except Exception as e:
                        say(f"취소 실패: {e}", "✗")
            try:
                st = self.rb.move_state(move_id)
            except Exception:
                time.sleep(1.0)
                continue
            state = str(st.get("state", "")).lower()
            if state in ("succeeded", "failed", "cancelled"):
                st["elapsed"] = time.time() - t0
                st["move_id"] = move_id
                return st
            time.sleep(1.0)

        try:
            self.rb.cancel_move()
        except Exception:
            pass
        return {"state": "timeout", "move_id": move_id, "elapsed": time.time() - t0}

    # ── 회복 동작 (재시도 2~4차) — jack_service.align_with_retry 와 동일 ──
    def _recover(self, stage: int) -> None:
        p = self.poi
        # 3·4차는 로봇을 움직인다 → 랙을 든 채로 움직이지 않게 먼저 확인
        if stage >= 3 and not self.ensure_jack_down(f"{stage}차 회복 이동"):
            say("  잭을 못 내려 회복 이동을 건너뜁니다", "✗")
            return
        if stage == 2:
            say("  회복: 위치 재보정", "~")
            self.rb.relocalize(max_wait_sec=10)
        elif stage == 3:
            say(f"  회복: 후진 {ALIGN_BACKOFF_M}m + 위치 재보정", "~")
            bx = p["x"] - ALIGN_BACKOFF_M * math.cos(p["ori"])
            by = p["y"] - ALIGN_BACKOFF_M * math.sin(p["ori"])
            self._run_move("standard", bx, by, p["ori"], 60)
            self.rb.relocalize(max_wait_sec=10)
        else:
            say(f"  회복: 측면 {ALIGN_LATERAL_OFFSET_M}m 우회 + 위치 재보정", "~")
            sx = (p["x"] + ALIGN_LATERAL_OFFSET_M * math.cos(p["ori"] + math.pi / 2)
                  - ALIGN_BACKOFF_M * math.cos(p["ori"]))
            sy = (p["y"] + ALIGN_LATERAL_OFFSET_M * math.sin(p["ori"] + math.pi / 2)
                  - ALIGN_BACKOFF_M * math.sin(p["ori"]))
            self._run_move("standard", sx, sy, p["ori"], 60)
            self.rb.relocalize(max_wait_sec=10)
        time.sleep(2)

    # ── 랙 진입: align_with_rack (최대 max_attempts 회) + jack_up ──
    def enter(self, quiet_tail: bool = False) -> bool:
        p = self.poi
        mode = ("1회만 (재시도 없음)" if self.max_attempts == 1
                else f"최대 {self.max_attempts}차까지 회복 재시도")
        say(f"[{self.cond}] 세트{self.set_no} #{self.set_count() + 1} 진입 — {mode}", "▶")

        t_total = time.time()
        attempts: list[dict] = []
        success_at = None
        watch = RackWatch(self.rb.ip)

        for stage in range(1, self.max_attempts + 1):
            if stage > 1:
                self._recover(stage)
                if self.wait_if_blocked() and self.abort:
                    break

            watch.start()
            res = self._run_move("align_with_rack", p["x"], p["y"], p["ori"], ALIGN_TIMEOUT)
            rack = watch.stop()

            state = str(res.get("state", "")).lower()
            fail_msg = res.get("fail_message") or res.get("fail_reason_str") or ""
            attempts.append({
                "stage": stage,
                "state": state,
                "fail_reason": res.get("fail_reason"),
                "fail_message": str(fail_msg)[:200],
                "detected": parse_detected(fail_msg),
                "elapsed": round(res.get("elapsed", 0), 1),
                "move_id": res.get("move_id"),
                "cls": res.get("cls"),
                "rack": rack,
            })

            if state == "succeeded":
                success_at = stage
                break

            # ★ 506 = 로봇이 직접 "잭이 올라가 있어 이동 못 한다"고 거부한 것.
            #   WS 판정보다 확실한 신호다. 여기서 무조건 잭을 내리고 이어간다.
            #   (2026-08-13: 이 신호를 안 쓰다가 506 이 4차까지 반복되며 세트가 날아갔다)
            if res.get("fail_reason") == 506:
                say("  로봇이 '잭 up 상태'라며 거부 — 잭을 내리고 계속합니다", "!")
                self.log.event("jack_up_detected_by_506", stage=stage)
                self.jacked = True          # 실제로 올라가 있다
                self.ensure_jack_down("506 거부 회복")

            seen = (f"랙 {rack['detected']}/{rack['packets']}회 검출"
                    + (f" {rack['median'][0]:.3f}x{rack['median'][1]:.3f}"
                       if rack.get("median") else "")) if rack["packets"] else "랙 관측 없음"
            say(f"  {RETRY_STAGE_NAME.get(stage, stage)} 실패 [{state}] {fail_msg} | {seen}", "✗")

            if res.get("cls") == "comm":     # 통신 두절은 회복 재시도 대상이 아님
                break

        last = attempts[-1] if attempts else {}
        # 분류: ok / rack(진짜 인식 실패) / jack(잭 상태) / comm(통신) / timeout
        if success_at:
            cls = "ok"
        elif last.get("cls") == "comm":
            cls = "comm"
        # 506 으로만 실패했으면 랙 인식 문제가 아니라 잭 상태 문제다.
        # 이걸 rack 으로 세면 인식 성공률이 왜곡된다(2026-08-13 세트가 그렇게 날아갔다).
        elif attempts and all(a.get("fail_reason") == 506 for a in attempts):
            cls = "jack"
        elif last.get("state") == "timeout":
            cls = "timeout"
        else:
            cls = "rack"

        trial = {
            "set": self.set_no,
            "label": self.set_label,
            "cond": self.cond,
            "state": "succeeded" if success_at else last.get("state", "failed"),
            "cls": cls,
            "success_at": success_at,          # 몇 차에 성공했나 (None=최종 실패)
            "n_attempts": len(attempts),
            "elapsed": round(time.time() - t_total, 1),
            "first_elapsed": attempts[0]["elapsed"] if attempts else 0,
            "fail_reason": last.get("fail_reason"),
            "fail_message": last.get("fail_message", ""),
            "detected": last.get("detected"),
            "rack": last.get("rack"),
            "attempts": attempts,
        }
        self.trials.append(trial)
        self.log.event("align", **trial)

        if not success_at:
            say(f"최종 실패 ({len(attempts)}차까지 시도, {trial['elapsed']}초) — 분류: {cls}", "✗")
            return False

        stage_txt = ("1차 성공" if success_at == 1
                     else f"★ {RETRY_STAGE_NAME.get(success_at, success_at)}에 성공")
        say(f"정렬 {stage_txt} (총 {trial['elapsed']}초) — 잭 업", "✔")

        # ⚠️ 응답 타임아웃이 나도 **명령은 실행됐을 수 있다**(2026-08-13 사고).
        #    예외를 '실패'로 단정하지 말고 로봇에 실제 상태를 물어본다.
        cmd_error = None
        try:
            self.rb.jack_up()
        except Exception as e:
            cmd_error = e
            say(f"jack_up 응답 없음({type(e).__name__}) — 실제 잭 상태를 확인합니다", "~")
            self.log.event("jack_up_no_response", error=str(e))
        time.sleep(JACK_SETTLE_SEC)

        jst = self.rb.jack_state() or {}
        up = self.rb.is_jack_up()
        if up is True:
            self.jacked = True
            if cmd_error:
                say("  확인 결과 잭은 올라가 있습니다 — 정상으로 처리합니다", "✔")
            # 잭은 올라갔는데 하중이 안 실렸으면 랙을 제대로 못 받친 것.
            # (정렬은 성공했다고 나오지만 실제로는 헛든 상태 — 다음 시도가 깨진다)
            try:
                w = float(jst.get("weight", 0))
            except (TypeError, ValueError):
                w = 0.0
            trial["jack_weight"] = w
            if w < JACK_WEIGHT_THRESHOLD:
                say(f"  ⚠️ 잭은 올라갔으나 하중이 낮습니다 (weight={w:.0f}) — "
                    f"랙을 제대로 못 받쳤을 수 있습니다", "!")
                self.log.event("jack_up_no_load", weight=w)
        elif up is False:
            self.jacked = False
            say("  잭이 올라가지 않았습니다 — 이번 시도는 여기서 종료합니다", "✗")
            self.log.event("jack_up_failed", error=str(cmd_error) if cmd_error else "state=down")
            trial["cls"] = "jack"
            return False
        else:
            # 판정 불가 — 올라간 것으로 **보수적으로** 가정한다.
            # (내려간 걸로 가정했다가 틀리면 랙을 끌고 움직이게 된다)
            self.jacked = True
            say("  잭 상태 판정 불가 — 안전을 위해 '올라감'으로 간주합니다", "!")
            self.log.event("jack_state_unknown")
        if not quiet_tail:
            say("랙을 들었습니다. 스페이스 = 반납", "✔")
        return True

    # ── 랙 반납: jack_down → 후진 (순서 중요) ──
    def leave(self, quiet_tail: bool = False) -> bool:
        say(f"[{self.cond}] 랙 반납 — 잭 다운 후 후진 {self.backoff}m", "▶")
        # 랙을 내려놓은 뒤에 빠져나온다 (들고 후진하면 랙째 끌려나옴).
        # ensure_jack_down 이 로봇에 실제 상태를 물어보고 내린 것까지 확인한다.
        ok = self._back_off()
        if ok and not quiet_tail:
            say("랙 밖으로 나왔습니다. 스페이스 = 다시 진입", "✔")
        return ok

    # ══════════════════════════════════════════════════
    # ★ 이동 전 안전 가드 — 랙을 든 채로 움직이지 않게
    # ══════════════════════════════════════════════════
    # 2026-08-13 사고: jack_up 의 HTTP 응답이 타임아웃 → 스크립트가 '실패'로 단정 →
    # 잭이 실제로는 올라가 있는데 후진 명령을 보내 **랙을 끌고 나갔다.**
    # 타임아웃은 "응답을 못 받았다"이지 "실행 안 됐다"가 아니다.
    # 그래서 내부 변수(self.jacked)를 믿지 말고 **로봇에 직접 물어본다.**

    def ensure_jack_down(self, why: str = "이동") -> bool:
        """잭이 올라가 있으면 내린다. 이동 명령 전에 반드시 호출할 것."""
        up = self.rb.is_jack_up()
        if up is None:
            say(f"  잭 상태를 확인할 수 없습니다 — 안전을 위해 {why} 전 잭 다운을 시도합니다", "!")
            try:
                self.rb.jack_down()
                time.sleep(JACK_SETTLE_SEC)
            except Exception as e:
                say(f"  jack_down 실패: {e} — 수동 확인 필요", "✗")
                self.log.event("jack_down_failed", error=str(e), context=why)
                return False
            self.jacked = False
            return True

        if not up:
            self.jacked = False
            return True

        # 올라가 있다 — 스크립트가 안 올렸다고 생각했더라도 실제가 우선이다
        say(f"  ★ 잭이 올라가 있습니다(랙 적재 상태). {why} 전에 내립니다.", "!")
        self.log.event("guard_jack_was_up", context=why, script_thought=self.jacked)
        try:
            self.rb.jack_down()
        except Exception as e:
            say(f"  jack_down 실패: {e} — 랙을 든 채로 움직이면 안 됩니다. 수동 조치하세요", "✗")
            self.log.event("jack_down_failed", error=str(e), context=why)
            return False
        time.sleep(JACK_SETTLE_SEC)
        still = self.rb.is_jack_up()
        if still:
            say("  잭이 여전히 올라가 있습니다 — 이동을 중단합니다. 수동 조치하세요", "✗")
            self.log.event("guard_jack_still_up", context=why)
            return False
        self.jacked = False
        return True

    def _back_off(self) -> bool:
        """랙 밖으로 후진. ★ 잭이 내려가 있는지 로봇에 확인한 뒤에만 움직인다."""
        if not self.ensure_jack_down("후진"):
            return False
        p = self.poi
        bx = p["x"] - self.backoff * math.cos(p["ori"])
        by = p["y"] - self.backoff * math.sin(p["ori"])
        res = self._run_move("standard", bx, by, p["ori"], 60)
        state = str(res.get("state", "")).lower()
        self.log.event("backoff", state=state, elapsed=round(res.get("elapsed", 0), 1))
        if state != "succeeded":
            say(f"후진 실패 [{res.get('state')}] — 수동으로 빼주세요", "✗")
            return False
        return True

    # ══════════════════════════════════════════════════
    # 자동 반복
    # ══════════════════════════════════════════════════

    def set_count(self) -> int:
        return sum(1 for t in self.trials if t["set"] == self.set_no)

    def _check_abort_key(self) -> bool:
        """시도 사이에 q/ESC 가 눌렸으면 자동 루프를 멈춘다."""
        while msvcrt and msvcrt.kbhit():
            k = msvcrt.getch()
            if k in (b"q", b"Q", b"\x1b"):
                self.abort = True
                say("중단 요청 — 이번 시도까지만 하고 멈춥니다", "!")
        return self.abort

    def run_set(self, n_trials: int, fail_limit: int) -> None:
        """한 세트(= 랙 한 자세)에서 n_trials 회 자동 반복.

        ★ 중단 판정은 **최종 실패**(재시도까지 다 실패)만 센다.
          1차 실패는 우리가 재려는 값 그 자체이므로 중단 사유가 아니다.
          (2026-08-12: 1차 실패로 세다 보니 가장 알고 싶은 자세에서 12회만 돌고 끊겼다)
        비상정지·통신두절은 대기로 처리되어 아예 시도로 세지 않는다.
        """
        streak = 0
        t_start = time.time()
        say("=" * 62, "▶")
        label = f" [{self.set_label}]" if self.set_label else ""
        say(f"세트 {self.set_no}{label} 시작 — {n_trials}회 자동 반복", "▶")
        say("  (중단하려면 q, 이동만 취소하려면 x)", "·")
        say("=" * 62, "▶")

        for _ in range(n_trials):
            if self._check_abort_key():
                break
            self.wait_if_blocked()          # E-STOP·통신두절이면 여기서 대기
            if self.abort:
                break

            ok = self.enter(quiet_tail=True)

            if ok:
                streak = 0
                self.leave(quiet_tail=True)
            else:
                # 정렬 실패면 잭은 안 올라갔다. 랙 밑에 어정쩡하게 들어가 있을 수 있으니
                # 다음 시도가 같은 조건에서 시작하도록 일단 빠져나온다.
                self._back_off()
                cls = self.trials[-1]["cls"] if self.trials else "rack"
                if cls == "rack":
                    streak += 1
                    say(f"  연속 최종실패 {streak}/{fail_limit}", "!")
                    if streak >= fail_limit:
                        say(f"연속 {streak}회 최종 실패 — 자동 반복을 멈춥니다. "
                            f"랙 상태를 확인하세요.", "✗")
                        self.log.event("auto_stop", reason="fail_streak",
                                       set=self.set_no, streak=streak)
                        break
                else:
                    # 통신 두절 등 외부 요인 — 연속 카운터를 올리지 않는다
                    say(f"  외부 요인({cls}) — 연속 카운터에 반영하지 않음", "~")

            done = self.set_count()
            okc = sum(1 for t in self.trials if t["set"] == self.set_no
                      and t["state"] == "succeeded")
            first = sum(1 for t in self.trials if t["set"] == self.set_no
                        and t.get("success_at") == 1)
            say(f"  진행 {done}/{n_trials}  (성공 {okc}, 그중 1차 {first})", "·")

        elapsed = time.time() - t_start
        ts = [t for t in self.trials if t["set"] == self.set_no]
        okc = sum(1 for t in ts if t["state"] == "succeeded")
        first = sum(1 for t in ts if t.get("success_at") == 1)
        print()
        say("=" * 62, "◆")
        say(f"세트 {self.set_no} 완료 — 최종성공 {okc}/{len(ts)} "
            f"(1차 {first}) ({elapsed/60:.1f}분)", "◆")
        say("=" * 62, "◆")
        self.log.event("set_done", set=self.set_no, label=self.set_label,
                       trials=len(ts), success=okc, first_try=first,
                       elapsed_sec=round(elapsed, 1))

    def wait_next_set(self) -> bool:
        """세트 사이 대기. 다음 세트를 시작하면 True, 종료면 False."""
        if self.jacked:
            say("잭이 올라가 있습니다 — 랙을 내려놓고 빠져나옵니다", "!")
            self.leave(quiet_tail=True)

        print()
        say("=" * 62, "★")
        say("랙 자세를 바꿔주세요.", "★")
        say("=" * 62, "★")
        print()
        print("  다음 세트의 랙 자세를 적어주세요.")
        print("  예)  정위치  /  오른쪽으로 조금 돌림  /  오른쪽으로 많이 돌림")
        print("       왼쪽으로 조금 돌림  /  왼쪽으로 많이 돌림  /  앞으로 5cm")
        print()
        print("  ※ 그냥 Enter = 이름표 없이 시작 (나중에 어느 자세였는지 알 수 없게 됩니다)")
        print("  ※ q + Enter = 종료하고 결과 보기")
        print()
        # 세트 자세는 사람이 직접 적는다.
        # (로봇 자세로 각도를 역산하려 했으나 /tracked_pose 가 정지 상태에서 발행되지 않아
        #  2026-08-12 테스트에서 120건 전부 기록 실패. 라벨이 유일하게 확실한 방법이다)
        try:
            lab = input("  랙 자세 > ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if lab.lower() in ("q", "quit", "exit"):
            return False

        self.set_no += 1
        self.set_label = lab
        if not lab:
            say("이름표 없이 시작합니다", "~")
        return True

    def toggle(self) -> None:
        if self.jacked:
            self.leave()
        else:
            self.enter()

    # ── 집계 ──
    @staticmethod
    def _row(ts: list[dict]) -> dict:
        """세트(또는 전체) 한 줄치 통계."""
        # 유효 시도 = 랙 인식의 성패를 물을 수 있는 것만.
        # 통신 두절(comm)·잭 이상(jack)은 랙 인식과 무관하므로 성공률에서 뺀다.
        rack = [t for t in ts if t["cls"] in ("ok", "rack", "timeout")]
        ext = [t for t in ts if t["cls"] in ("comm", "jack")]
        n = len(rack)
        by_stage = {k: sum(1 for t in rack if t.get("success_at") == k) for k in (1, 2, 3, 4)}
        final_ok = sum(by_stage.values())
        secs = sorted(t["elapsed"] for t in rack if t["state"] == "succeeded")
        return {
            "n": n, "ext": len(ext),
            "stage": by_stage,
            "final_ok": final_ok,
            "final_ng": n - final_ok,
            "first_pct": (by_stage[1] * 100 // n) if n else 0,
            "final_pct": (final_ok * 100 // n) if n else 0,
            "tmed": secs[len(secs) // 2] if secs else 0,
        }

    def summary(self) -> str:
        if not self.trials:
            return "시도 기록이 없습니다."

        W = 104
        L = ["", "=" * W,
             "  세트별 결과   (세트 = 랙을 한 자세로 둔 채 반복한 묶음)", "=" * W,
             f"  {'세트':<4}{'랙 자세':<22}{'1차':>6}{'2차':>5}{'3차':>5}{'4차':>5}"
             f"{'최종실패':>8}   {'1차성공률':<11}{'최종성공률':<11}{'평균시간':>8}",
             "-" * W]

        def fmt(tag: str, r: dict) -> str:
            s = r["stage"]
            return (f"  {tag:<26}{s[1]:>6}{s[2]:>5}{s[3]:>5}{s[4]:>5}{r['final_ng']:>8}   "
                    f"{r['first_pct']:>3}% ({s[1]}/{r['n']})  "
                    f"{r['final_pct']:>3}% ({r['final_ok']}/{r['n']})  {r['tmed']:>6.0f}s")

        for st in sorted({t["set"] for t in self.trials}):
            ts = [t for t in self.trials if t["set"] == st]
            lab = (ts[0].get("label") or "(이름표 없음)")[:21]
            L.append(fmt(f"{st:<4}{lab}", self._row(ts)))

        L += ["-" * W, fmt("전체", self._row(self.trials)), "=" * W]

        # ── 최종 실패한 것들의 근거 ──
        fails = [t for t in self.trials if t["cls"] in ("rack", "timeout")
                 and t["state"] != "succeeded"]
        if fails:
            L += ["", f"  최종 실패 {len(fails)}건 — 실패 순간 로봇이 랙을 봤는가:"]
            for t in fails:
                rk = t.get("rack") or {}
                if rk.get("packets"):
                    seen = f"검출 {rk['detected']}/{rk['packets']}회"
                    if rk.get("median"):
                        seen += f", 중앙 {rk['median'][0]:.3f} x {rk['median'][1]:.3f}"
                        if rk.get("range"):
                            r0, r1 = rk["range"]
                            seen += f" (폭 {r0[0]:.2f}~{r0[1]:.2f} / 깊이 {r1[0]:.2f}~{r1[1]:.2f})"
                else:
                    seen = "랙 관측 패킷 없음"
                lab = (t.get("label") or "?")[:14]
                L.append(f"    세트{t['set']} [{lab}] {t['n_attempts']}차까지 시도 — {seen}")

        # ── 외부 요인 (성공률에서 제외된 것) ──
        comm = [t for t in self.trials if t["cls"] == "comm"]
        jack = [t for t in self.trials if t["cls"] == "jack"]
        if comm:
            L += ["", f"  ※ 통신 두절로 제외 {len(comm)}건 "
                      f"(세트 {sorted({t['set'] for t in comm})})"]
        if jack:
            L += [f"  ※ 잭 이상으로 제외 {len(jack)}건 "
                  f"(세트 {sorted({t['set'] for t in jack})}) — 정렬은 됐으나 잭이 안 올라감"]

        # ── 해석 도우미 ──
        L += ["", "  읽는 법", "  " + "-" * 50,
              "  * 1차/2차/3차/4차 = 그 단계에서 성공한 횟수.",
              "      2차=위치 재보정 후, 3차=후진 0.5m 후, 4차=측면 0.3m 우회 후.",
              "  * 1차 성공률 = 랙 인식이 얼마나 깨끗한가 (자세의 좋고 나쁨).",
              "  * 최종 성공률 = 실운영에서 실제로 성공하는 비율 ← 현장에서 중요한 숫자.",
              "  * 뒤 단계(3·4차)에 몰려 있으면 지금은 되지만 여유가 없다는 뜻.",
              "  * 비상정지·통신두절은 시도로 세지 않는다(대기 후 이어서 진행).",
              f"  * 전체 로그(시도 단위): {self.log.path}"]
        return "\n".join(L)


# ══════════════════════════════════════════════════════════

HELP = """
  [Enter] ★ 자동 반복 시작 — N회 돌고 멈춤 → 랙 자세 바꾸고 자세 입력 → 다음 세트
  [Space] 수동: 랙 진입 / 반납 토글   [d] 랙 치수 측정 (15초)
  [a/b/c] 조건 라벨 지정              [s] 로봇의 현재 rack.specs 조회
  [2]     LG2 스펙 주입               [w/e] leg_size -5mm / +5mm 후 주입
  [x]     이동 취소 (비상)            [h] 도움말   [q] 종료 + 결과 정리

  조건 A = 체인 현상태 / B = 체인을 기둥에 밀착 고정 / C = 체인 임시 제거(기준선)
  * 비상정지를 누르면 실패로 세지 않고 대기합니다. 풀면 알아서 이어집니다.
"""


def load_poi(poi_id: int | None, name: str) -> dict:
    """R1 좌표를 DB에서 조회. poi_id 를 주면 그것 우선, 아니면 이름으로 찾는다."""
    from app.database import SessionLocal
    from app.models.map import MapPOI, RobotMap

    db = SessionLocal()
    try:
        if poi_id:
            poi = db.query(MapPOI).filter(MapPOI.id == poi_id).first()
        else:
            active_map = (db.query(RobotMap)
                          .filter(RobotMap.is_active == True)      # noqa: E712
                          .order_by(RobotMap.id.desc()).first())
            if not active_map:
                raise RuntimeError("활성 맵이 없습니다")
            poi = (db.query(MapPOI)
                   .filter(MapPOI.map_id == active_map.id,
                           MapPOI.name == name,
                           MapPOI.is_active == True)               # noqa: E712
                   .first())
        if not poi or poi.world_x is None:
            raise RuntimeError(f"POI 조회 실패 (id={poi_id} name={name})")
        return {"id": poi.id, "name": poi.name, "x": poi.world_x, "y": poi.world_y,
                "ori": poi.angle or 0, "rack_size": poi.rack_size}
    finally:
        db.close()


def load_robot_ip() -> str | None:
    """DB에 등록된 활성 로봇의 IP.

    현장(사무실/LG)마다 IP가 달라서 스크립트에 박아두면 옮길 때마다 고쳐야 한다.
    관제 화면에서 로봇 IP를 고치면 스크립트도 따라오도록 DB를 기준으로 삼는다.
    """
    try:
        from app.database import SessionLocal
        from app.models.robot import Robot
        db = SessionLocal()
        try:
            r = (db.query(Robot)
                 .filter(Robot.is_active == True,                      # noqa: E712
                         Robot.ip_address.isnot(None))
                 .order_by(Robot.id).first())
            return r.ip_address if r else None
        finally:
            db.close()
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description="LG2 랙 인식 벤치")
    p.add_argument("--ip", default=None, help="로봇 IP (기본: DB에 등록된 로봇)")
    p.add_argument("--poi-id", type=int, default=None, help="랙 위치 POI id (기본: 이름으로 조회)")
    p.add_argument("--poi-name", default="R1", help="랙 위치 POI 이름 (기본 R1)")
    p.add_argument("--backoff", type=float, default=2, help="반납 후 후진 거리(m)")
    p.add_argument("--trials", type=int, default=20, help="한 세트당 반복 횟수 (기본 20)")
    p.add_argument("--retry", action="store_true",
                   help="실패 시 백엔드와 동일한 4단계 회복 재시도 "
                        "(재보정 → 후진 → 측면우회). 몇 차에 성공했는지 기록")
    p.add_argument("--fail-limit", type=int, default=3,
                   help="연속 이 횟수만큼 **최종 실패**하면 자동 반복 중지 (기본 3). "
                        "1차 실패는 세지 않는다")
    p.add_argument("--cond", default="B", help="조건 라벨 초기값 (기본 B = 체인 고정)")
    p.add_argument("--secret", default=DEFAULT_SECRET)
    p.add_argument("--dry-run", action="store_true", help="연결·조회만, 로봇 안 움직임")
    cfg = p.parse_args()

    if msvcrt is None:
        print("이 스크립트는 Windows 전용입니다 (msvcrt 필요).")
        return 2

    robot_ip = cfg.ip or load_robot_ip()
    if not robot_ip:
        print("로봇 IP를 알 수 없습니다.")
        print("  DB(robots.ip_address)에 등록된 활성 로봇이 없거나 DB가 꺼져 있습니다.")
        print("  --ip 로 직접 지정하세요.  예)  rack_bench.py --ip 192.168.0.100")
        return 1
    if not cfg.ip:
        print(f"(로봇 IP를 DB에서 읽었습니다: {robot_ip})")

    log = Log(LOG_DIR / f"rack_bench_{RUN_TS}.jsonl")
    rb = Robot(robot_ip, secret=cfg.secret, dry=cfg.dry_run)

    print("=" * 74)
    print("LG2 랙 인식 벤치 — 접지 체인 검증")
    print("=" * 74)

    # 1) 랙 위치 POI
    try:
        poi = load_poi(cfg.poi_id, cfg.poi_name)
    except Exception as e:
        say(f"POI 조회 실패: {e}", "✗")
        say("  DB가 떠 있는지, run_local.ps1 과 같은 접속 정보인지 확인하세요.", "·")
        return 2
    say(f"랙 위치: {poi['name']} (id={poi['id']}) "
        f"x={poi['x']:.3f} y={poi['y']:.3f} ori={poi['ori']:.3f} "
        f"rack_size={poi['rack_size']}", "✔")
    if poi["rack_size"] != "LG2":
        say(f"주의 — 이 POI 의 rack_size 가 'LG2' 가 아닙니다 ({poi['rack_size']}). "
            f"맵 동기화 시 다른 스펙이 로봇에 들어갑니다.", "!")
    log.event("poi", **poi)

    # 2) 로봇 연결 + 현재 스펙
    try:
        specs = rb.get_rack_specs()
    except Exception as e:
        say(f"로봇({robot_ip}) 응답 없음: {e}", "✗")
        return 2
    say(f"로봇 연결 OK — rack.specs {len(specs)}개 등록됨", "✔")
    for s in specs:
        say(f"  width={s.get('width')} depth={s.get('depth')} "
            f"leg_size={s.get('leg_size')} margin={s.get('margin')}", "·")
    log.event("specs_initial", specs=specs)
    if len(specs) > 1:
        say("스펙이 2개 이상입니다 — 펌웨어가 엉뚱한 것과 매칭할 수 있습니다. "
            "[2] 로 LG2 하나만 주입하는 것을 권합니다.", "!")

    bench = Bench(rb, poi, log, cfg.backoff,
                  max_attempts=4 if cfg.retry else 1)
    bench.cond = cfg.cond.upper()
    say(f"재시도 모드: {'ON — 최대 4차까지 회복' if cfg.retry else 'OFF — 1차만'}", "✔")

    # 첫 세트 자세 라벨
    print()
    print("  첫 세트의 랙 자세를 적어주세요. (예: 정위치 / 오른쪽으로 조금 돌림)")
    print("  그냥 Enter 를 누르면 이름표 없이 시작합니다.")
    try:
        bench.set_label = input("  랙 자세 > ").strip()
    except (EOFError, KeyboardInterrupt):
        bench.set_label = ""
    print(HELP)
    say(f"현재 조건: {bench.cond} | 로그: {log.path}")

    # 3) 키 루프
    try:
        while True:
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            key = msvcrt.getch()

            if key in (b"\r", b"\n"):
                # ★ 자동 반복 — 세트를 계속 돌린다. 세트 사이에 사람이 랙 각도를 바꾼다.
                while True:
                    bench.abort = False
                    bench.run_set(cfg.trials, cfg.fail_limit)
                    if bench.abort:
                        break
                    if not bench.wait_next_set():
                        break
                say("자동 반복 종료 — [q] 로 결과를 보거나 [Enter] 로 다시 시작", "·")
            elif key == b" ":
                bench.toggle()
            elif key in (b"a", b"b", b"c", b"A", b"B", b"C"):
                bench.cond = key.decode().upper()
                say(f"조건 → {bench.cond}", "✔")
                log.event("condition", cond=bench.cond)
            elif key in (b"d", b"D"):
                r = measure(rb)
                log.event("measure", cond=bench.cond, **r)
                if r.get("error"):
                    say(r["error"], "✗")
                else:
                    say(f"[{bench.cond}] 중앙값 {r['width']} x {r['depth']} "
                        f"(샘플 {r['samples']}개, 범위 {r['width_range']} x {r['depth_range']})", "✔")
            elif key in (b"s", b"S"):
                try:
                    cur = rb.get_rack_specs()
                    for s in cur:
                        say(f"  width={s.get('width')} depth={s.get('depth')} "
                            f"leg_size={s.get('leg_size')} foot_radius={s.get('foot_radius')} "
                            f"margin={s.get('margin')}", "·")
                except Exception as e:
                    say(f"조회 실패: {e}", "✗")
            elif key == b"2":
                from app.constants.rack_specs import RACK_SPECS
                spec = dict(RACK_SPECS["LG2"])
                try:
                    rb.set_rack_spec(spec)
                    say(f"LG2 스펙 주입 완료 — {spec['width']} x {spec['depth']} "
                        f"leg_size={spec['leg_size']}", "✔")
                    log.event("spec_pushed", name="LG2", spec=spec)
                except Exception as e:
                    say(f"주입 실패: {e}", "✗")
            elif key in (b"w", b"e", b"W", b"E"):
                delta = -0.005 if key.lower() == b"w" else 0.005
                try:
                    cur = rb.get_rack_specs()
                    if not cur:
                        say("등록된 스펙이 없습니다. 먼저 [2] 로 주입하세요.", "✗")
                        continue
                    spec = dict(cur[0])
                    spec["leg_size"] = round(float(spec.get("leg_size", 0.025)) + delta, 4)
                    spec["foot_radius"] = round(spec["leg_size"] / 2, 4)
                    rb.set_rack_spec(spec)
                    say(f"leg_size → {spec['leg_size']} (foot_radius {spec['foot_radius']})", "✔")
                    log.event("spec_tweak", leg_size=spec["leg_size"])
                except Exception as e:
                    say(f"조정 실패: {e}", "✗")
            elif key in (b"x", b"X"):
                try:
                    say(f"이동 취소 — {rb.cancel_move()}", "!")
                except Exception as e:
                    say(f"취소 실패: {e}", "✗")
            elif key in (b"h", b"H", b"?"):
                print(HELP)
            elif key in (b"q", b"Q", b"\x1b"):
                break
            elif key == b"\x03":   # Ctrl+C
                raise KeyboardInterrupt

    except KeyboardInterrupt:
        print()
        say("중단 — 현재 이동을 취소합니다", "!")
        try:
            rb.cancel_move()
        except Exception:
            pass
    finally:
        print(bench.summary())
        log.event("summary", trials=bench.trials)
        say(f"로그: {log.path}")
        if bench.jacked:
            say("잭이 올라간 상태입니다 — 랙을 들고 있습니다. 반납 후 종료하세요.", "!")
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
