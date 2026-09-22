"""배차 사이클 자동화 공통 부품 — auto_cycle.py / random_cycle.py 가 함께 쓴다.

두 스크립트 모두 **콘솔·태블릿이 쓰는 것과 똑같은 백엔드 API** 만 호출한다.
백엔드 코드를 건드리지 않으므로, 여기서 통과한 흐름은 사람이 조작한 것과 동일하다.

⚠️ 실기 로봇이 실제로 움직인다. 첫 회차는 반드시 옆에서 지켜볼 것.

── E-STOP / 통신두절 처리 ────────────────────────────────
비상정지를 누르면 **실패로 세지 않고 대기**한다(해제하면 이어서 진행).
백엔드도 사실상 같게 동작한다 — `safe_move` 가 이동 실패 시 5초 간격으로
최대 200회(≈17분) 재시도하므로 세션이 죽지 않는다. 다만 백엔드는 E-STOP 인지
모르고 재시도할 뿐이라 화면에 사유가 안 뜬다. 여기서는 사유를 정확히 알려준다.
백엔드의 17분보다 먼저 끊도록 대기 한도를 5분으로 둔다.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "BackEnd"))

# ── DB 접속 (run_local.ps1 과 동일) — app import 전에 설정해야 한다 ──
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
    import msvcrt
except ImportError:
    msvcrt = None

API_BASE = "http://127.0.0.1:8002"
ROBOT_PORT = 8090
LOG_DIR = ROOT / "_logs"

BLOCK_WAIT_LIMIT = 300.0   # E-STOP 등으로 이만큼 대기하면 포기 (백엔드 17분보다 먼저 끊는다)


def say(msg: str, mark: str = "·"):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {mark} {msg}", flush=True)


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fp = path.open("a", encoding="utf-8")

    def event(self, kind: str, **fields):
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, **fields}
        self.fp.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.fp.flush()

    def close(self):
        try:
            self.fp.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════
# POI 조회 — ★ 이름으로 찾는다
# ══════════════════════════════════════════════════════════
# 2026-08-12 하루에만 POI id 가 두 번 바뀌었다(985→988→1009).
# id 를 스크립트에 박아두면 맵을 다시 만들 때마다 깨진다. 이름은 안 바뀐다.

def load_pois(names: list[str] | None = None) -> dict[str, dict]:
    """활성 맵의 POI 를 {이름: {...}} 로. names 를 주면 그것만, 없으면 전부."""
    from app.database import SessionLocal
    from app.models.map import MapPOI, RobotMap

    db = SessionLocal()
    try:
        active_map = (db.query(RobotMap)
                      .filter(RobotMap.is_active == True)        # noqa: E712
                      .order_by(RobotMap.id.desc()).first())
        if not active_map:
            raise RuntimeError("활성 맵이 없습니다")
        q = db.query(MapPOI).filter(MapPOI.map_id == active_map.id,
                                    MapPOI.is_active == True)    # noqa: E712
        out: dict[str, dict] = {}
        for p in q.all():
            if names and p.name not in names:
                continue
            out[p.name] = {"id": p.id, "name": p.name, "type": p.poi_type,
                           "x": p.world_x, "y": p.world_y, "ori": p.angle or 0,
                           "rack_size": p.rack_size, "map_id": active_map.id}
        if names:
            missing = [n for n in names if n not in out]
            if missing:
                raise RuntimeError(f"POI 를 찾지 못했습니다: {missing} (맵 {active_map.id})")
        return out
    finally:
        db.close()


# ══════════════════════════════════════════════════════════
# 백엔드 API (콘솔/태블릿과 동일)
# ══════════════════════════════════════════════════════════

class Api:
    def __init__(self, base: str = API_BASE, dry: bool = False):
        self.base = base.rstrip("/")
        self.dry = dry

    def get(self, path: str, timeout=15):
        r = requests.get(self.base + path, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body=None, timeout=30, allow_fail=False):
        if self.dry:
            say(f"[dry-run] POST {path} {body if body else ''}", "→")
            return {"ok": True, "dry": True}
        r = requests.post(self.base + path, json=body, timeout=timeout)
        if not allow_fail:
            r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status_code": r.status_code, "text": r.text[:300]}

    def delete(self, path: str, timeout=15, allow_fail=True):
        if self.dry:
            say(f"[dry-run] DELETE {path}", "→")
            return {"ok": True, "dry": True}
        r = requests.delete(self.base + path, timeout=timeout)
        if not allow_fail:
            r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status_code": r.status_code}

    # ── 콘솔 ──
    def console_status(self):
        return self.get("/api/dispatch/console/status", timeout=25)

    def call_poi(self, poi_id: int, with_rack=True):
        """호출. 가용 로봇이 없으면 백엔드가 자동으로 '예약'으로 바꾼다."""
        return self.post(f"/api/dispatch/poi/{poi_id}/call",
                         {"with_rack": with_rack, "robot_type": "lifting"},
                         timeout=40, allow_fail=True)

    def send_route(self, poi_id: int, waypoints: list[int]):
        return self.post(f"/api/dispatch/poi/{poi_id}/route", {"waypoints": waypoints},
                         allow_fail=True)

    def cancel_reservation(self, poi_id: int):
        return self.delete(f"/api/dispatch/poi/{poi_id}/reserve")

    # ── 로봇 태블릿 ──
    def tablet_status(self, robot_id: int):
        return self.get(f"/api/dispatch/robot/{robot_id}/tablet-status")

    def confirm(self, robot_id: int):
        return self.post(f"/api/dispatch/robot/{robot_id}/confirm", None, allow_fail=True)

    def end_job(self, robot_id: int):
        """[작업 종료] — 복귀 시퀀스 시작 (랙 반납 + 충전소)."""
        return self.post(f"/api/dispatch/robot/{robot_id}/end", None, allow_fail=True)

    # ── 비상 ──
    def cancel_move(self, robot_ip: str):
        """로봇을 그 자리에 세운다. dry-run 이어도 실제로 보낸다(안전 동작은 항상 진짜)."""
        r = requests.post(f"{self.base}/api/robots/cancel-move/{robot_ip}", timeout=15)
        return r.json() if r.content else {}

    def clear_dispatch(self, robot_ip: str):
        r = requests.post(f"{self.base}/api/robots/remote/clear-dispatch/{robot_ip}", timeout=15)
        return r.json() if r.content else {}


def robot_get(ip: str, path: str, timeout=8):
    """로봇 직접 조회 (백엔드를 거치지 않는 진단용)."""
    r = requests.get(f"http://{ip}:{ROBOT_PORT}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


# ══════════════════════════════════════════════════════════
# 감시 — E-STOP 대기 + 이동 실패
# ══════════════════════════════════════════════════════════

class Watchdog:
    """이상 감시.

    분류가 핵심이다(2026-08-12 랙 테스트 교훈):
      * E-STOP / 원격모드     → **대기**. 실패로 세지 않는다
      * 통신 두절             → **대기**. 공유기 재부팅 등
      * 이동 실패 연속        → **정지**. 진짜 문제
    """

    def __init__(self, robot_ip: str, log: Log, fail_limit: int = 4):
        self.ip = robot_ip
        self.log = log
        self.fail_limit = fail_limit
        self._seen: set = set()
        self._fail = 0
        self.block_total = 0.0     # 이번 실행에서 대기한 총 시간
        # ⚠️ 로봇이 page_size 를 무시하고 이동 이력 전체를 돌려준다(실측).
        #    시작 시점 최신 id 를 기준선으로 잡고 그 이후만 판정한다.
        #    (없으면 예전에 쌓인 실패 기록을 보고 즉시 오탐한다)
        self.baseline_id = self._latest_id()
        say(f"이동 이력 기준선: id > {self.baseline_id} 부터 감시", "·")
        log.event("watchdog_baseline", baseline_id=self.baseline_id)

    def _fetch(self) -> list:
        moves = robot_get(self.ip, "/chassis/moves?page=1&page_size=20")
        items = moves if isinstance(moves, list) else moves.get("items", moves.get("list", []))
        return items if isinstance(items, list) else []

    def _latest_id(self) -> int:
        try:
            return max((m.get("id") or 0 for m in self._fetch()), default=0)
        except Exception as e:
            say(f"이동 이력 조회 실패 — 기준선 0 으로 시작: {e}", "~")
            return 0

    # ── 진행을 막는 상태인가 (E-STOP·원격·통신) ──
    def blocking_reason(self) -> str | None:
        try:
            st = robot_get(self.ip, "/chassis/status")
        except Exception as e:
            return f"로봇 통신 두절 ({type(e).__name__})"
        if st.get("emergency_stop_pressed"):
            return "비상정지(E-STOP) 눌림"
        mode = str(st.get("control_mode", "auto"))
        if mode != "auto":
            return f"제어 모드가 auto 아님 ({mode}) — 원격 조작 중일 수 있음"
        if st.get("wheel_overloaded"):
            return "바퀴 과부하(wheel_overloaded)"
        return None

    def wait_if_blocked(self, abort_check=None) -> str | None:
        """막힌 상태면 풀릴 때까지 대기. 한도를 넘기면 사유를 반환(=정지 사유)."""
        reason = self.blocking_reason()
        if reason is None:
            return None

        say("=" * 58, "!")
        say(f"{reason}", "!")
        say("  실패로 세지 않고 대기합니다. 해제하면 자동으로 이어집니다.", "·")
        say(f"  (최대 {BLOCK_WAIT_LIMIT/60:.0f}분. 중단하려면 q)", "·")
        say("=" * 58, "!")
        self.log.event("blocked", reason=reason)

        waited = 0.0
        while waited < BLOCK_WAIT_LIMIT:
            if abort_check and abort_check():
                return None
            time.sleep(3.0)
            waited += 3.0
            self.block_total += 3.0
            cur = self.blocking_reason()
            if cur is None:
                say(f"해제됨 — {waited:.0f}초 대기 후 계속 진행합니다", "✔")
                self.log.event("blocked_cleared", reason=reason, waited_sec=waited)
                return None
            if int(waited) % 30 == 0:
                say(f"  대기 중... {waited:.0f}초 ({cur})", "·")

        self.log.event("blocked_timeout", reason=reason, waited_sec=waited)
        return f"{reason} — {BLOCK_WAIT_LIMIT/60:.0f}분 넘게 안 풀림"

    # ── 이동 실패 감시 ──
    def check_moves(self) -> str | None:
        """연속 이동 실패면 사유, 정상이면 None."""
        try:
            items = self._fetch()
        except Exception:
            return None      # 통신 문제는 wait_if_blocked 가 맡는다

        fresh = [m for m in items
                 if (m.get("id") or 0) > self.baseline_id and m.get("id") not in self._seen]
        fresh.sort(key=lambda m: m.get("id") or 0)

        for m in fresh:
            mid = m.get("id")
            reason = m.get("fail_reason")
            state = str(m.get("state", "")).lower()
            self._seen.add(mid)

            if state in ("succeeded", "cancelled"):
                # 한 번이라도 정상 종료했으면 그 전 실패는 일시적이었다 → 리셋
                if self._fail:
                    self.log.event("fail_streak_reset", move_id=mid, was=self._fail)
                    self._fail = 0
                continue

            if state == "failed" or (reason not in (None, 0)):
                self._fail += 1
                desc = m.get("fail_reason_str") or m.get("fail_message") or ""
                self.log.event("move_failed", move_id=mid, fail_reason=reason,
                               desc=str(desc)[:200], state=state, streak=self._fail)
                say(f"이동 실패 (id={mid} reason={reason} {desc}) — 연속 {self._fail}회", "!")

                if reason == 9 and self._fail >= self.fail_limit:
                    return (f"경로 계산 실패(calculation_failed) 연속 {self._fail}회 — "
                            f"공간이 막혔을 가능성. 백엔드는 최대 17분 재시도합니다")
                # 501 = 랙 인식 실패(MoveFailReason::rack_detection_error) — 2026-08-12 실측.
                # 잭 상태 문제가 아니다. 백엔드가 재시도하므로 여기서 바로 멈추지 않는다.
                if self._fail >= self.fail_limit:
                    return f"이동 실패 연속 {self._fail}회 (reason={reason} {desc})"
        return None

    def snapshot(self) -> dict:
        """정지 시점 상태 보존 — 원인 분석용."""
        snap = {}
        try:
            snap["moves"] = sorted(self._fetch(), key=lambda m: m.get("id") or 0,
                                   reverse=True)[:15]
        except Exception as e:
            snap["moves"] = f"조회 실패: {e}"
        for name, path in (("chassis_status", "/chassis/status"),
                           ("current_move", "/chassis/moves/current"),   # 이동 중 아니면 404 정상
                           ("current_map", "/chassis/current-map")):
            try:
                snap[name] = robot_get(self.ip, path)
            except Exception as e:
                snap[name] = f"조회 실패: {e}"
        try:
            snap["quick_status"] = requests.get(
                f"{API_BASE}/api/robots/quick-status/{self.ip}", timeout=15).json()
        except Exception as e:
            snap["quick_status"] = f"조회 실패: {e}"
        return snap


# ══════════════════════════════════════════════════════════
# 비상 정지
# ══════════════════════════════════════════════════════════

class Stopper:
    """정지 절차 — 순서가 중요하다.

    `force_clear`(세션 정리)만으로는 **로봇이 멈추지 않는다.**
    코드 주석에 "로봇 이동/잭 동작 없이 세션만 종료"라고 명시돼 있다.
    그래서 ①이동 취소로 먼저 세우고 ②세션을 정리한다.
    잭은 내리지 않는다 — 통로 한가운데 랙을 놓으면 상황이 더 나빠진다.
    """

    def __init__(self, api: Api, robot_ip: str, robot_id: int, wd: Watchdog, log: Log,
                 run_ts: str):
        self.api, self.ip, self.rid, self.wd, self.log = api, robot_ip, robot_id, wd, log
        self.run_ts = run_ts
        self.stopped = False
        self.reason = ""

    def stop(self, reason: str, snapshot: bool = True):
        if self.stopped:
            return
        self.stopped = True
        self.reason = reason
        print()
        say("=" * 58, "!")
        say(f"비상 정지: {reason}", "!")
        say("=" * 58, "!")
        self.log.event("emergency_stop", reason=reason)

        try:
            r = self.api.cancel_move(self.ip)
            say(f"1) 이동 취소 완료 — {r}", "✔")
            self.log.event("cancel_move", result=r)
        except Exception as e:
            say(f"1) 이동 취소 실패: {e}  ← 로봇이 계속 움직일 수 있습니다. 수동 정지하세요", "✗")
            self.log.event("cancel_move_failed", error=str(e))

        try:
            r = self.api.clear_dispatch(self.ip)
            say(f"2) 세션 강제 정리 완료 — {r}", "✔")
            self.log.event("clear_dispatch", result=r)
        except Exception as e:
            say(f"2) 세션 정리 실패: {e}", "✗")
            self.log.event("clear_dispatch_failed", error=str(e))

        if snapshot:
            snap = self.wd.snapshot()
            try:
                snap["console"] = self.api.console_status()
                snap["tablet"] = self.api.tablet_status(self.rid)
            except Exception as e:
                snap["api"] = f"조회 실패: {e}"
            path = LOG_DIR / f"snapshot_{self.run_ts}.json"
            path.write_text(json.dumps(snap, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
            say(f"3) 스냅샷 저장 — {path}", "✔")
            self.log.event("snapshot", path=str(path))

        say("로봇은 그 자리에 랙을 든 채 멈춰 있습니다. 상태 확인 후 조치하세요.", "!")


# ══════════════════════════════════════════════════════════
# 상태 대기 (E-STOP 대기 포함)
# ══════════════════════════════════════════════════════════

class AbortRequested(Exception):
    """사용자가 q 를 눌러 중단을 요청했다."""


def check_abort_key() -> bool:
    while msvcrt and msvcrt.kbhit():
        k = msvcrt.getch()
        if k in (b"q", b"Q", b"\x1b", b"\x03"):
            return True
    return False


def wait_for(api: Api, robot_id: int, want, wd: Watchdog, stopper: Stopper,
             timeout: float, what: str, log: Log) -> dict | None:
    """태블릿 상태가 want 가 될 때까지 대기.

    E-STOP·통신두절로 대기한 시간은 timeout 에서 빼준다(그 시간까지 세면 억울하게 죽는다).
    """
    if isinstance(want, str):
        want = (want,)
    t0 = time.time()
    blocked_total = 0.0
    last = None
    last_probe = 0.0

    while True:
        if check_abort_key():
            raise AbortRequested()

        if (time.time() - t0 - blocked_total) > timeout:
            stopper.stop(f"{what} 대기 시간 초과 ({timeout:.0f}초) — 마지막 상태 {last}")
            return None

        # 3초에 한 번 로봇 상태 확인 (E-STOP / 통신)
        if time.time() - last_probe > 3.0:
            last_probe = time.time()
            b0 = time.time()
            fatal = wd.wait_if_blocked(abort_check=check_abort_key)
            blocked_total += time.time() - b0
            if fatal:
                stopper.stop(fatal)
                return None

        try:
            t = api.tablet_status(robot_id)
        except Exception as e:
            say(f"태블릿 상태 조회 실패(재시도): {e}", "~")
            time.sleep(1.0)
            continue

        st = t.get("session_status")
        if st != last:
            say(f"상태: {last} → {st}  (현재={t.get('current_poi_name')}"
                f"{', 다음=' + str(t.get('next_poi_name')) if t.get('next_poi_name') else ''})")
            log.event("status", session_status=st, current=t.get("current_poi_name"),
                      next=t.get("next_poi_name"), is_last=t.get("is_last"),
                      can_confirm=t.get("can_confirm"))
            last = st

        if st == "failed":
            stopper.stop(f"세션이 failed 로 끝남 (대기: {what})")
            return None
        if st in want:
            return t
        if "inactive" in want and not t.get("active"):
            return t

        reason = wd.check_moves()
        if reason:
            stopper.stop(reason)
            return None
        time.sleep(1.0)


def pick_robot(api: Api) -> tuple[int, str, dict] | None:
    """온라인 + 유휴 로봇 1대. 없으면 None."""
    cs = api.console_status()
    online = [r for r in cs.get("robots", []) if r.get("online")]
    if not online:
        say("온라인 로봇이 없습니다. 로봇 연결을 먼저 확인하세요.", "✗")
        return None
    rb = online[0]
    if rb.get("busy"):
        say(f"이미 작업 중인 로봇입니다 (상태={rb.get('session_status')}). "
            f"콘솔에서 정리 후 다시 실행하세요.", "✗")
        return None
    return rb["robot_id"], rb["robot_ip"], rb
