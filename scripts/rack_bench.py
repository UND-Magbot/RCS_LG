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
ALIGN_TIMEOUT = 120
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
    def jack_up(self) -> dict:
        return self.post("/services/jack_up")

    def jack_down(self) -> dict:
        return self.post("/services/jack_down")

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

    def latest_move_id(self) -> int:
        try:
            moves = self.get("/chassis/moves?page=1&page_size=20")
            items = moves if isinstance(moves, list) else moves.get("items", moves.get("list", []))
            return max((m.get("id") or 0 for m in items), default=0)
        except Exception:
            return 0


def read_pose(ip: str, seconds: float = 2.0) -> dict | None:
    """로봇의 현재 자세 (x, y, ori[rad]).

    GET /chassis/pose 는 help 텍스트만 반환하므로(2026-08 실측) WS 로만 읽을 수 있다.
    align_with_rack 이 성공한 직후에 읽으면 로봇이 랙에 맞춰 선 상태이므로,
    이 각도가 곧 **랙이 놓인 각도**에 가깝다(정렬 허용오차만큼의 차이는 있음).
    사람이 "오른쪽으로 조금" 틀어놓은 것을 나중에 숫자로 되돌아볼 수 있게 남긴다.
    """
    for pkt in reversed(ws_collect(ip, "/tracked_pose", seconds)):
        pos = pkt.get("pos")
        ori = pkt.get("ori")
        if isinstance(pos, (list, tuple)) and len(pos) >= 2 and ori is not None:
            try:
                return {"x": round(float(pos[0]), 4), "y": round(float(pos[1]), 4),
                        "ori": round(float(ori), 4)}
            except (TypeError, ValueError):
                continue
    return None


def angle_diff_deg(a: float, b: float) -> float:
    """a - b 를 [-180, 180] 도로 정규화."""
    d = math.degrees(a - b)
    while d > 180:
        d -= 360
    while d < -180:
        d += 360
    return d


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
    def __init__(self, rb: Robot, poi: dict, log: Log, backoff: float):
        self.rb = rb
        self.poi = poi
        self.log = log
        self.backoff = backoff
        self.jacked = False          # 잭이 올라가 있는가 (= 랙을 들고 있는가)
        self.cond = "A"              # 현재 실험 조건 라벨
        self.trials: list[dict] = []
        # ── 자동 반복(세트) ──
        # 세트 = "랙을 한 자세로 둔 채 N회 반복". 세트가 끝나면 사람이 랙 각도를 틀고
        # 다음 세트를 시작한다. 집계는 세트별로 나오므로 각도별 성공률을 볼 수 있다.
        self.set_no = 1
        self.set_label = ""
        self.abort = False           # 자동 루프 중단 요청

    # ── 이동 1회 (재시도 없음) ──
    def _run_move(self, move_type: str, x: float, y: float, ori: float,
                  timeout: float) -> dict:
        t0 = time.time()
        try:
            move_id = self.rb.create_move(move_type, x, y, ori)
        except Exception as e:
            return {"state": "failed", "fail_message": f"명령 전송 실패: {e}",
                    "elapsed": time.time() - t0}
        if move_id is None:
            return {"state": "failed", "fail_message": "move id 없음",
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

    # ── 랙 진입: align_with_rack 1회 + jack_up ──
    def enter(self, quiet_tail: bool = False) -> bool:
        p = self.poi
        say(f"[{self.cond}] 세트{self.set_no} #{self.set_count() + 1} 진입 — "
            f"align_with_rack 1회만 (재시도 없음)", "▶")
        res = self._run_move("align_with_rack", p["x"], p["y"], p["ori"], ALIGN_TIMEOUT)

        state = str(res.get("state", "")).lower()
        fail_msg = res.get("fail_message") or res.get("fail_reason_str") or ""
        detected = parse_detected(fail_msg)

        trial = {
            "set": self.set_no,
            "label": self.set_label,
            "cond": self.cond,
            "state": state,
            "fail_reason": res.get("fail_reason"),
            "fail_message": str(fail_msg)[:300],
            "detected": detected,
            "elapsed": round(res.get("elapsed", 0), 1),
            "move_id": res.get("move_id"),
            "pose": None,
            "yaw_delta_deg": None,
        }

        if state != "succeeded":
            self.trials.append(trial)
            self.log.event("align", **trial)
            say(f"정렬 실패 [{state}] {fail_msg}", "✗")
            if detected:
                say(f"  → 로봇이 잰 값: {detected['width']} x {detected['depth']}", "!")
            return False

        # ── 정렬 직후 자세 = 랙이 놓인 각도 (잭 업 전에 읽어야 정확) ──
        pose = read_pose(self.rb.ip)
        if pose:
            trial["pose"] = pose
            trial["yaw_delta_deg"] = round(angle_diff_deg(pose["ori"], p["ori"]), 1)
        self.trials.append(trial)
        self.log.event("align", **trial)

        d = trial["yaw_delta_deg"]
        say(f"정렬 성공 ({trial['elapsed']}초)"
            + (f" | 랙 각도 {math.degrees(pose['ori']):.1f}° (POI 대비 {d:+.1f}°)" if pose else "")
            + " — 잭 업", "✔")
        try:
            self.rb.jack_up()
        except Exception as e:
            say(f"jack_up 실패: {e}", "✗")
            self.log.event("jack_up_failed", error=str(e))
            return False
        time.sleep(JACK_SETTLE_SEC)
        self.jacked = True
        if not quiet_tail:
            say("랙을 들었습니다. 스페이스 = 반납", "✔")
        return True

    # ── 랙 반납: jack_down → 후진 (순서 중요) ──
    def leave(self, quiet_tail: bool = False) -> bool:
        say(f"[{self.cond}] 랙 반납 — 잭 다운 후 후진 {self.backoff}m", "▶")
        try:
            self.rb.jack_down()
        except Exception as e:
            say(f"jack_down 실패: {e}  ← 랙을 든 채 후진하면 안 됩니다. 확인하세요", "✗")
            self.log.event("jack_down_failed", error=str(e))
            return False
        time.sleep(JACK_SETTLE_SEC)
        self.jacked = False

        # 랙을 내려놓은 뒤에 빠져나온다 (들고 후진하면 랙째 끌려나옴)
        ok = self._back_off()
        if ok and not quiet_tail:
            say("랙 밖으로 나왔습니다. 스페이스 = 다시 진입", "✔")
        return ok

    def _back_off(self) -> bool:
        """랙 밖으로 후진. 잭이 내려간 상태에서만 부를 것."""
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

        실패해도 계속 돈다(성공률을 재는 게 목적). 다만 **연속 실패가 fail_limit 회**면
        랙이 넘어졌거나 로봇이 엉뚱한 데 있는 등 사람이 봐야 하는 상황이므로 멈춘다.
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

            ok = self.enter(quiet_tail=True)

            if ok:
                streak = 0
                self.leave(quiet_tail=True)
            else:
                streak += 1
                say(f"  연속 실패 {streak}/{fail_limit}", "!")
                # 정렬 실패면 잭은 안 올라갔다. 랙 밑에 어정쩡하게 들어가 있을 수 있으니
                # 다음 시도가 같은 조건에서 시작하도록 일단 빠져나온다.
                self._back_off()
                if streak >= fail_limit:
                    say(f"연속 {streak}회 실패 — 자동 반복을 멈춥니다. "
                        f"랙 상태를 확인하세요.", "✗")
                    self.log.event("auto_stop", reason="fail_streak",
                                   set=self.set_no, streak=streak)
                    break

            done = self.set_count()
            okc = sum(1 for t in self.trials if t["set"] == self.set_no
                      and t["state"] == "succeeded")
            say(f"  진행 {done}/{n_trials}  (성공 {okc})", "·")

        elapsed = time.time() - t_start
        okc = sum(1 for t in self.trials if t["set"] == self.set_no
                  and t["state"] == "succeeded")
        done = self.set_count()
        print()
        say("=" * 62, "◆")
        say(f"세트 {self.set_no} 완료 — 성공 {okc}/{done}  ({elapsed/60:.1f}분)", "◆")
        say("=" * 62, "◆")
        self.log.event("set_done", set=self.set_no, label=self.set_label,
                       trials=done, success=okc, elapsed_sec=round(elapsed, 1))

    def wait_next_set(self) -> bool:
        """세트 사이 대기. 다음 세트를 시작하면 True, 종료면 False."""
        if self.jacked:
            say("잭이 올라가 있습니다 — 랙을 내려놓고 빠져나옵니다", "!")
            self.leave(quiet_tail=True)

        print()
        say("★ 랙 위치/각도를 바꿔주세요.", "★")
        say("   [Space] 준비됐음 — 다음 세트 시작", "·")
        say("   [n]     이번 세트에 이름표를 달고 시작 (예: +5도)", "·")
        say("   [q]     종료하고 결과 보기", "·")

        while True:
            if not (msvcrt and msvcrt.kbhit()):
                time.sleep(0.05)
                continue
            k = msvcrt.getch()
            if k == b" ":
                self.set_no += 1
                self.set_label = ""
                return True
            if k in (b"n", b"N"):
                try:
                    lab = input("   이번 세트 이름표: ").strip()
                except EOFError:
                    lab = ""
                self.set_no += 1
                self.set_label = lab
                return True
            if k in (b"q", b"Q", b"\x1b", b"\x03"):
                return False

    def toggle(self) -> None:
        if self.jacked:
            self.leave()
        else:
            self.enter()

    # ── 집계 ──
    @staticmethod
    def _fmt_stats(ts: list[dict]) -> tuple[str, str, str, str, str]:
        """(랙각도, 성공률, 정렬시간 중앙값, detected 중앙값, detected 범위)"""
        n = len(ts)
        ok = sum(1 for t in ts if t["state"] == "succeeded")
        rate = f"{ok}/{n} ({ok * 100 // n if n else 0}%)"

        secs = sorted(t["elapsed"] for t in ts if t["state"] == "succeeded")
        tmed = f"{secs[len(secs) // 2]:.0f}s" if secs else "-"

        # 랙 각도 — 정렬 성공한 시도에서만 잰다(실패하면 랙에 못 붙어 의미 없음)
        ds = sorted(t["yaw_delta_deg"] for t in ts if t.get("yaw_delta_deg") is not None)
        if ds:
            mid = len(ds) // 2
            ang = f"{ds[mid]:+.1f}°"
            if ds[-1] - ds[0] >= 1.0:      # 시도마다 1도 이상 흔들리면 함께 보여준다
                ang += f" ({ds[0]:+.0f}~{ds[-1]:+.0f})"
        else:
            ang = "-"

        dets = [t["detected"] for t in ts if t["detected"]]
        if dets:
            w = sorted(d["width"] for d in dets)
            d_ = sorted(d["depth"] for d in dets)
            mid = len(w) // 2
            med = f"{w[mid]:.3f} x {d_[mid]:.3f}"
            rng = f"{w[0]:.2f}~{w[-1]:.2f} x {d_[0]:.2f}~{d_[-1]:.2f}"
        else:
            med, rng = "-", "-"
        return ang, rate, tmed, med, rng

    def summary(self) -> str:
        if not self.trials:
            return "시도 기록이 없습니다."

        W = 100
        L = ["", "=" * W, "  세트별 결과 (세트 = 랙을 한 자세로 둔 채 반복한 묶음)", "=" * W,
             f"  {'세트':<4}{'이름표':<14}{'랙 각도':<18}{'성공률':<14}{'정렬시간':<10}"
             f"{'실패시 detected':<20}{'범위'}",
             "-" * W]

        for s in sorted({t["set"] for t in self.trials}):
            ts = [t for t in self.trials if t["set"] == s]
            lab = (ts[0].get("label") or "")[:13]
            ang, rate, tmed, med, rng = self._fmt_stats(ts)
            L.append(f"  {s:<4}{lab:<14}{ang:<18}{rate:<14}{tmed:<10}{med:<20}{rng}")

        L += ["-" * W]
        ang, rate, tmed, med, rng = self._fmt_stats(self.trials)
        L.append(f"  {'전체':<18}{ang:<18}{rate:<14}{tmed:<10}{med:<20}{rng}")
        L.append("=" * W)

        # ── 실패 사유 분류 ──
        fails = [t for t in self.trials if t["state"] != "succeeded"]
        if fails:
            L += ["", f"  실패 {len(fails)}건의 사유:"]
            buckets: dict[str, list[dict]] = {}
            for t in fails:
                msg = t["fail_message"] or t["state"]
                if "rack size" in msg.lower() or t["detected"]:
                    key = "Wrong rack size (치수 불일치)"
                elif t["state"] == "timeout":
                    key = "시간 초과 (랙을 못 찾아 접근만 반복)"
                elif t["state"] == "cancelled":
                    key = "취소됨 (사람이 x 를 눌렀거나 다른 명령)"
                else:
                    key = f"기타 — {msg[:60]}"
                buckets.setdefault(key, []).append(t)
            for key, ts in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
                sets = sorted({t["set"] for t in ts})
                L.append(f"    {len(ts):>3}건  {key}   (세트 {sets})")

        # ── 해석 도우미 ──
        L += ["", "  읽는 법", "  " + "-" * 46,
              "  * 랙 각도 = 정렬 성공 직후 로봇이 선 방향을 POI 등록각과 비교한 값.",
              "      +는 한쪽, -는 반대쪽. 사람이 '오른쪽으로 조금'이라 한 것이 몇 도였는지 여기서 나온다.",
              "      성공한 시도가 하나도 없는 세트는 '-' (랙에 못 붙었으니 각도를 못 잰다).",
              "  * detected 는 실패 로그에만 실린다. 성공만 한 세트가 '-' 인 것은 정상.",
              "  * 세트별 성공률이 각도에 따라 갈리면 → 특정 각도에서만 가짜 다리가 보이는 것.",
              "  * 각도와 무관하게 균일하면 → 각도가 아닌 다른 요인.",
              "  * 정렬시간이 유독 긴 세트는 '겨우 성공'한 것 — 100% 여도 여유가 없다.",
              f"  * 전체 로그(시도 단위): {self.log.path}"]
        return "\n".join(L)


# ══════════════════════════════════════════════════════════

HELP = """
  [Enter] ★ 자동 반복 시작 — N회 돌고 멈춤 → 랙 각도 바꾸고 Space → 다음 세트
  [Space] 수동: 랙 진입 / 반납 토글   [d] 랙 치수 측정 (15초)
  [a/b/c] 조건 라벨 지정              [s] 로봇의 현재 rack.specs 조회
  [2]     LG2 스펙 주입               [w/e] leg_size -5mm / +5mm 후 주입
  [x]     이동 취소 (비상)            [h] 도움말   [q] 종료 + 결과 정리

  조건 A = 체인 현상태 / B = 체인을 기둥에 밀착 고정 / C = 체인 임시 제거(기준선)
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


def main() -> int:
    p = argparse.ArgumentParser(description="LG2 랙 인식 벤치")
    p.add_argument("--ip", default="192.168.30.100", help="로봇 IP")
    p.add_argument("--poi-id", type=int, default=None, help="랙 위치 POI id (기본: 이름으로 조회)")
    p.add_argument("--poi-name", default="R1", help="랙 위치 POI 이름 (기본 R1)")
    p.add_argument("--backoff", type=float, default=2, help="반납 후 후진 거리(m)")
    p.add_argument("--trials", type=int, default=20, help="한 세트당 반복 횟수 (기본 20)")
    p.add_argument("--fail-limit", type=int, default=5,
                   help="연속 이 횟수만큼 실패하면 자동 반복 중지 (기본 5)")
    p.add_argument("--cond", default="B", help="조건 라벨 초기값 (기본 B = 체인 고정)")
    p.add_argument("--secret", default=DEFAULT_SECRET)
    p.add_argument("--dry-run", action="store_true", help="연결·조회만, 로봇 안 움직임")
    cfg = p.parse_args()

    if msvcrt is None:
        print("이 스크립트는 Windows 전용입니다 (msvcrt 필요).")
        return 2

    log = Log(LOG_DIR / f"rack_bench_{RUN_TS}.jsonl")
    rb = Robot(cfg.ip, secret=cfg.secret, dry=cfg.dry_run)

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
        say(f"로봇({cfg.ip}) 응답 없음: {e}", "✗")
        return 2
    say(f"로봇 연결 OK — rack.specs {len(specs)}개 등록됨", "✔")
    for s in specs:
        say(f"  width={s.get('width')} depth={s.get('depth')} "
            f"leg_size={s.get('leg_size')} margin={s.get('margin')}", "·")
    log.event("specs_initial", specs=specs)
    if len(specs) > 1:
        say("스펙이 2개 이상입니다 — 펌웨어가 엉뚱한 것과 매칭할 수 있습니다. "
            "[2] 로 LG2 하나만 주입하는 것을 권합니다.", "!")

    bench = Bench(rb, poi, log, cfg.backoff)
    bench.cond = cfg.cond.upper()
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
