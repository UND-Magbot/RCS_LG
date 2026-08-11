"""LG 배차 자동 사이클 — 사람이 [확인]을 누르지 않고 전체 흐름을 반복 실행한다.

흐름 (콘솔에서 사람이 하던 것과 동일한 API 를 그대로 호출):
    호출(J1) → 로봇이 랙 픽업 → J1 도착
    → 경유지(J2,J3,J4) 등록 + 출발
    → 각 도착마다 5초 대기 후 자동 [확인]
    → 마지막 확인 → 복귀(랙 반납 + 충전소) → 다음 사이클

⚠️ 이 스크립트는 **실기 로봇을 실제로 움직인다.**
   백엔드 코드는 건드리지 않고 콘솔이 쓰는 API 만 호출한다.

── 안전 ─────────────────────────────────────────────────────
[확인] 버튼은 원래 "이 지점 작업이 끝났고 출발해도 안전하다"는 사람의 승인이다.
이 스크립트는 그 승인을 시간(--dwell)으로 대체하므로 **테스트 전용**이다.
현장 운영에 쓰지 말 것. 첫 회차는 반드시 옆에서 지켜볼 것.

이상을 감지하면 즉시 아래 순서로 정지한다 (Ctrl+C 도 같은 경로):
    1) 이동 취소  → 로봇을 그 자리에 세운다 (랙은 든 채로 유지)
    2) 강제 정리  → 세션·워커·POI 점유 해제
    3) 스냅샷 저장 → 정지 시점의 세션/로봇/최근 이동 기록
    4) 사유 출력 후 종료
잭은 내리지 않는다 (통로 한가운데 랙을 놓으면 상황이 더 나빠진다).

── 사용 ─────────────────────────────────────────────────────
  # 1) 흐름만 확인 (로봇 안 움직임)
  python scripts/auto_cycle.py --dry-run

  # 2) 1사이클만, 옆에서 지켜보며
  python scripts/auto_cycle.py --rounds 1

  # 3) 무인 반복
  python scripts/auto_cycle.py --rounds 10
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "http://127.0.0.1:8002"
ROBOT_PORT = 8090

LOG_DIR = Path(__file__).resolve().parent.parent / "_logs"
RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")


# ══════════════════════════════════════════════════════════
# 로그
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
# API 래퍼
# ══════════════════════════════════════════════════════════

class Api:
    def __init__(self, base: str, dry: bool = False):
        self.base = base.rstrip("/")
        self.dry = dry

    def get(self, path: str, timeout=10):
        r = requests.get(self.base + path, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body=None, timeout=20, allow_fail=False):
        if self.dry:
            say(f"[dry-run] POST {path} {body if body else ''}", "→")
            return {"ok": True, "dry": True}
        r = requests.post(self.base + path, json=body, timeout=timeout)
        if not allow_fail:
            r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status_code": r.status_code, "text": r.text[:200]}

    # ── 배차 ──
    def console_status(self):
        return self.get("/api/dispatch/console/status", timeout=20)

    def call_poi(self, poi_id: int, with_rack=True):
        return self.post(f"/api/dispatch/poi/{poi_id}/call",
                         {"with_rack": with_rack, "robot_type": "lifting"}, timeout=30)

    def send_route(self, poi_id: int, waypoints: list[int]):
        return self.post(f"/api/dispatch/poi/{poi_id}/route", {"waypoints": waypoints})

    def tablet_status(self, robot_id: int):
        return self.get(f"/api/dispatch/robot/{robot_id}/tablet-status")

    def confirm(self, robot_id: int):
        return self.post(f"/api/dispatch/robot/{robot_id}/confirm", None, allow_fail=True)

    # ── 비상 정지 ──
    def cancel_move(self, robot_ip: str):
        """로봇을 그 자리에 세운다. dry-run 이어도 실행 (안전 동작은 항상 진짜)."""
        r = requests.post(f"{self.base}/api/robots/cancel-move/{robot_ip}", timeout=15)
        return r.json() if r.content else {}

    def clear_dispatch(self, robot_ip: str):
        r = requests.post(f"{self.base}/api/robots/remote/clear-dispatch/{robot_ip}", timeout=15)
        return r.json() if r.content else {}


def robot_get(ip: str, path: str, timeout=8):
    """로봇에 직접 조회 (백엔드를 거치지 않는 진단용)."""
    r = requests.get(f"http://{ip}:{ROBOT_PORT}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


# ══════════════════════════════════════════════════════════
# 감시 — 이상 판정
# ══════════════════════════════════════════════════════════

class Watchdog:
    """이동 실패·정체를 감시한다.

    calculation_failed(fail_reason 9) 는 백엔드가 최대 200회(≈17분) 재시도하는데,
    공간이 막힌 게 원인이면 절대 풀리지 않고 화면에도 안내가 없다.
    그래서 몇 회만 반복돼도 바로 멈춘다. (2026-08-11 랙 다리 문제가 이 증상이었음)
    """

    def __init__(self, robot_ip: str, log: Log, fail_limit: int = 4):
        self.ip = robot_ip
        self.log = log
        self.fail_limit = fail_limit
        self._seen_ids: set = set()
        self._fail_count = 0
        # ⚠️ 로봇이 page_size 파라미터를 무시하고 이동 이력 전체를 돌려준다(실측).
        #    그래서 "지금 실패 중"과 "예전에 실패했던 기록"을 id 로 구분해야 한다.
        #    시작 시점의 최신 id 를 기준선으로 잡고, 그보다 뒤에 생긴 이동만 판정한다.
        #    (이 기준선이 없으면 어제 쌓인 calculation_failed 기록을 보고 즉시 오탐한다)
        self.baseline_id = self._latest_id()
        say(f"이동 이력 기준선: id > {self.baseline_id} 부터 감시", "·")
        self.log.event("watchdog_baseline", baseline_id=self.baseline_id)

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

    def check(self) -> str | None:
        """이상이면 사유 문자열, 정상이면 None."""
        # ── 섀시 상태: 비상정지/원격전환/바퀴 과부하 ──
        # 사람이 비상정지를 눌렀거나 원격으로 조작을 뺏은 상태에서
        # 스크립트가 계속 [확인]을 누르면 안 된다.
        try:
            st = robot_get(self.ip, "/chassis/status")
            if st.get("emergency_stop_pressed"):
                return "로봇 비상정지 버튼이 눌렸습니다"
            mode = str(st.get("control_mode", "auto"))
            if mode != "auto":
                return f"로봇 제어 모드가 auto 가 아님 (현재 {mode}) — 누군가 원격 조작 중일 수 있습니다"
            if st.get("wheel_overloaded"):
                return "바퀴 과부하(wheel_overloaded) — 랙이 끼었거나 하중 이상"
        except Exception:
            pass  # 통신 일시 실패는 여기서 판정하지 않음 (아래 이동 실패로 잡힌다)

        try:
            items = self._fetch()
        except Exception:
            return None  # 조회 실패는 통신 일시 문제일 수 있어 여기서 판정하지 않음

        # 기준선 이후에 새로 생긴 이동만, 오래된 것부터 순서대로 본다
        fresh = [m for m in items
                 if (m.get("id") or 0) > self.baseline_id and m.get("id") not in self._seen_ids]
        fresh.sort(key=lambda m: m.get("id") or 0)

        for m in fresh:
            mid = m.get("id")
            reason = m.get("fail_reason")
            state = str(m.get("state", "")).lower()
            self._seen_ids.add(mid)

            if state in ("succeeded", "cancelled"):
                # 한 번이라도 정상적으로 끝났으면 그 전 실패는 일시적인 것이었다 → 카운터 리셋.
                # (연속으로 실패할 때만 진짜 문제로 본다)
                if self._fail_count:
                    self.log.event("fail_streak_reset", move_id=mid, state=state,
                                   was=self._fail_count)
                    self._fail_count = 0
                continue

            if state == "failed" or (reason not in (None, 0)):
                self._fail_count += 1
                desc = m.get("fail_reason_str") or m.get("fail_message") or ""
                self.log.event("move_failed", move_id=mid, fail_reason=reason,
                               desc=str(desc)[:200], state=state, streak=self._fail_count)
                say(f"이동 실패 (id={mid} fail_reason={reason} {desc}) — 연속 {self._fail_count}회", "!")
                if reason == 9:
                    if self._fail_count >= self.fail_limit:
                        return (f"경로 계산 실패(calculation_failed)가 연속 {self._fail_count}회 — "
                                f"공간이 막혔을 가능성. 백엔드는 최대 200회(약 17분) 재시도합니다")
                elif reason in (501, 506):
                    # 506 jack_in_up_state / 501 계열 — 잭 상태 때문에 이동 거부
                    return f"잭 상태로 이동 거부 (fail_reason={reason} {desc})"
                elif self._fail_count >= self.fail_limit:
                    return f"이동 실패가 연속 {self._fail_count}회 (fail_reason={reason} {desc})"
        return None

    def snapshot(self) -> dict:
        """정지 시점 상태 보존 — 원인 분석용.

        잭/위치추정/배터리는 로봇 REST 에 없고 WS 토픽(/ws/v2/topics)으로만 온다.
        배터리는 백엔드의 quick-status 가 WS 로 받아오므로 그쪽을 쓴다.
        """
        snap = {}
        try:
            # 이력 전체가 오므로 최신 15건만 남긴다
            snap["moves"] = sorted(self._fetch(), key=lambda m: m.get("id") or 0,
                                   reverse=True)[:15]
        except Exception as e:
            snap["moves"] = f"조회 실패: {e}"
        for name, path in (("chassis_status", "/chassis/status"),
                           # 이동 중이 아니면 404 가 정상이다
                           ("current_move", "/chassis/moves/current"),
                           ("current_map", "/chassis/current-map")):
            try:
                snap[name] = robot_get(self.ip, path)
            except Exception as e:
                snap[name] = f"조회 실패: {e}"
        try:
            snap["quick_status"] = requests.get(
                f"{API}/api/robots/quick-status/{self.ip}", timeout=15).json()
        except Exception as e:
            snap["quick_status"] = f"조회 실패: {e}"
        return snap


# ══════════════════════════════════════════════════════════
# 비상 정지
# ══════════════════════════════════════════════════════════

class Stopper:
    def __init__(self, api: Api, robot_ip: str, robot_id: int, wd: Watchdog, log: Log):
        self.api, self.ip, self.rid, self.wd, self.log = api, robot_ip, robot_id, wd, log
        self.stopped = False

    def stop(self, reason: str, snapshot: bool = True):
        if self.stopped:
            return
        self.stopped = True
        print()
        say("=" * 58, "!")
        say(f"비상 정지: {reason}", "!")
        say("=" * 58, "!")
        self.log.event("emergency_stop", reason=reason)

        # 1) 로봇을 그 자리에 세운다 (랙은 든 채로 — 잭 다운 안 함)
        try:
            r = self.api.cancel_move(self.ip)
            say(f"1) 이동 취소 완료 — {r}", "✔")
            self.log.event("cancel_move", result=r)
        except Exception as e:
            say(f"1) 이동 취소 실패: {e}  ← 로봇이 계속 움직일 수 있습니다. 수동 정지하세요", "✗")
            self.log.event("cancel_move_failed", error=str(e))

        # 2) 세션/워커/POI 점유 정리
        try:
            r = self.api.clear_dispatch(self.ip)
            say(f"2) 세션 강제 정리 완료 — {r}", "✔")
            self.log.event("clear_dispatch", result=r)
        except Exception as e:
            say(f"2) 세션 정리 실패: {e}", "✗")
            self.log.event("clear_dispatch_failed", error=str(e))

        # 3) 스냅샷 (원인 분석용)
        if snapshot:
            snap = self.wd.snapshot()
            try:
                snap["console"] = self.api.console_status()
                snap["tablet"] = self.api.tablet_status(self.rid)
            except Exception as e:
                snap["api"] = f"조회 실패: {e}"
            path = LOG_DIR / f"snapshot_{RUN_TS}.json"
            path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
            say(f"3) 스냅샷 저장 — {path}", "✔")
            self.log.event("snapshot", path=str(path))

        say("로봇은 그 자리에 랙을 든 채 멈춰 있습니다. 상태를 확인한 뒤 조치하세요.", "!")


# ══════════════════════════════════════════════════════════
# 사이클
# ══════════════════════════════════════════════════════════

def wait_for(api: Api, robot_id: int, want, wd: Watchdog, stopper: Stopper,
             timeout: float, what: str, log: Log):
    """태블릿 상태가 want 가 될 때까지 대기. 그동안 이상을 감시한다."""
    if isinstance(want, str):
        want = (want,)
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            t = api.tablet_status(robot_id)
        except Exception as e:
            say(f"태블릿 상태 조회 실패(재시도): {e}", "~")
            time.sleep(1.0)
            continue

        st = t.get("session_status")
        if st != last:
            say(f"상태: {last} → {st}  (현재위치={t.get('current_poi_name')})")
            log.event("status", session_status=st, current=t.get("current_poi_name"),
                      next=t.get("next_poi_name"), can_confirm=t.get("can_confirm"))
            last = st

        if st == "failed":
            stopper.stop(f"세션이 failed 로 끝남 (대기: {what})")
            return None
        if st in want:
            return t

        reason = wd.check()
        if reason:
            stopper.stop(reason)
            return None
        time.sleep(1.0)

    stopper.stop(f"{what} 대기 시간 초과 ({timeout:.0f}초) — 마지막 상태 {last}")
    return None


def run_cycle(api: Api, cfg, robot_id: int, robot_ip: str,
              wd: Watchdog, stopper: Stopper, log: Log, idx: int) -> bool:
    say(f"────── 사이클 {idx} 시작 ──────", "▶")
    log.event("cycle_start", index=idx)

    # 1) 호출 — 로봇이 랙을 픽업해서 호출 지점으로
    say(f"호출: POI {cfg.call_poi} (랙 픽업 포함)")
    res = api.call_poi(cfg.call_poi, with_rack=True)
    log.event("call", poi=cfg.call_poi, result=res)
    if cfg.dry_run:
        say("[dry-run] 이후 단계는 실제 상태가 없어 건너뜁니다", "→")
        return True
    if res.get("reserved"):
        stopper.stop("가용 로봇이 없어 예약으로 전환됨 — 로봇 상태를 확인하세요")
        return False

    rid = res.get("robot_id") or robot_id
    say(f"배정된 로봇: id={rid} ({res.get('robot_name')})", "✔")

    # 2) 픽업 + 이동 완료 대기 (align_with_rack → jack_up → 호출지점 이동)
    t = wait_for(api, rid, "awaiting_next", wd, stopper, cfg.pickup_timeout,
                 "랙 픽업 + 호출지점 도착", log)
    if not t:
        return False
    say(f"{t.get('current_poi_name')} 도착 — 경유지 등록", "✔")

    # 3) 경유지 등록 + 출발
    res = api.send_route(cfg.call_poi, cfg.route)
    log.event("route", waypoints=cfg.route, result=res)
    say(f"경유지 등록: {cfg.route} → 출발", "✔")

    # 4) 각 경유지 도착마다 dwell 초 대기 후 자동 확인
    for n in range(len(cfg.route)):
        t = wait_for(api, rid, "awaiting_confirm", wd, stopper, cfg.move_timeout,
                     f"{n+1}번째 경유지 도착", log)
        if not t:
            return False
        where = t.get("current_poi_name")
        is_last = t.get("is_last")
        say(f"{where} 도착 — {cfg.dwell}초 후 자동 확인"
            + (" (마지막)" if is_last else f" → 다음: {t.get('next_poi_name')}"))

        # 사람이 작업하는 시간을 흉내낸다. 이 사이에도 감시는 계속.
        end = time.time() + cfg.dwell
        while time.time() < end:
            reason = wd.check()
            if reason:
                stopper.stop(reason)
                return False
            time.sleep(0.5)

        r = api.confirm(rid)
        log.event("confirm", at=where, is_last=is_last, result=r)
        say(f"[확인] 자동 입력 — {where}", "✔")

    # 5) 복귀 (랙 반납 → 충전소) 완료 대기
    say("복귀 시퀀스 대기 (랙 반납 + 충전소 도킹)")
    end = time.time() + cfg.return_timeout
    while time.time() < end:
        try:
            t = api.tablet_status(rid)
        except Exception:
            time.sleep(1.0)
            continue
        if not t.get("active"):
            say(f"사이클 {idx} 완료", "✔")
            log.event("cycle_done", index=idx)
            return True
        if t.get("session_status") == "failed":
            stopper.stop("복귀 중 세션 failed")
            return False
        reason = wd.check()
        if reason:
            stopper.stop(reason)
            return False
        time.sleep(2.0)

    stopper.stop(f"복귀가 {cfg.return_timeout:.0f}초 안에 끝나지 않음")
    return False


# ══════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="LG 배차 자동 사이클")
    p.add_argument("--rounds", type=int, default=1, help="반복 횟수 (기본 1)")
    p.add_argument("--call-poi", type=int, default=985, help="호출할 POI id (기본 985=J1)")
    p.add_argument("--route", type=str, default="982,983,984",
                   help="경유지 POI id 순서 (기본 982,983,984 = J2,J3,J4)")
    p.add_argument("--dwell", type=float, default=5.0, help="도착 후 자동 확인까지 대기(초)")
    p.add_argument("--rest", type=float, default=10.0, help="사이클 사이 휴식(초)")
    p.add_argument("--pickup-timeout", type=float, default=300.0)
    p.add_argument("--move-timeout", type=float, default=300.0)
    p.add_argument("--return-timeout", type=float, default=420.0)
    p.add_argument("--fail-limit", type=int, default=4, help="이동 실패 몇 회에 정지할지")
    p.add_argument("--dry-run", action="store_true", help="실제 명령 없이 흐름만 확인")
    cfg = p.parse_args()
    cfg.route = [int(x) for x in cfg.route.split(",") if x.strip()]

    log = Log(LOG_DIR / f"auto_cycle_{RUN_TS}.jsonl")

    print("=" * 62)
    print("LG 배차 자동 사이클")
    print(f"  호출 {cfg.call_poi} → 경유지 {cfg.route} → 자동확인({cfg.dwell}s) → 복귀")
    print(f"  반복 {cfg.rounds}회" + ("   [DRY-RUN — 로봇 안 움직임]" if cfg.dry_run else ""))
    print(f"  로그 {log.path}")
    print("=" * 62)

    api = Api(API, dry=cfg.dry_run)

    # 로봇 확인
    try:
        cs = api.console_status()
    except Exception as e:
        say(f"백엔드({API}) 응답 없음: {e}", "✗")
        return 2
    robots = cs.get("robots", [])
    online = [r for r in robots if r.get("online")]
    if not online:
        say("온라인 로봇이 없습니다. 로봇 연결을 먼저 확인하세요.", "✗")
        return 2
    rb = online[0]
    robot_id, robot_ip = rb["robot_id"], rb["robot_ip"]
    say(f"대상 로봇: id={robot_id} {rb.get('robot_name')} ({robot_ip}) "
        f"배터리={rb.get('battery')}", "✔")
    if rb.get("busy"):
        say("이미 작업 중인 로봇입니다. 정리 후 다시 실행하세요.", "✗")
        return 2

    wd = Watchdog(robot_ip, log, fail_limit=cfg.fail_limit)
    stopper = Stopper(api, robot_ip, robot_id, wd, log)

    # Ctrl+C 도 같은 정지 절차를 타게 한다
    def on_sigint(sig, frame):
        stopper.stop("사용자 중단(Ctrl+C)")
        log.close()
        sys.exit(130)
    signal.signal(signal.SIGINT, on_sigint)

    ok_count = 0
    try:
        for i in range(1, cfg.rounds + 1):
            if not run_cycle(api, cfg, robot_id, robot_ip, wd, stopper, log, i):
                say(f"사이클 {i} 에서 중단됨 — 총 {ok_count}회 성공", "✗")
                return 1
            ok_count += 1
            if i < cfg.rounds:
                say(f"{cfg.rest}초 휴식 후 다음 사이클")
                time.sleep(cfg.rest)
    except Exception as e:
        stopper.stop(f"스크립트 예외: {e!r}")
        log.event("exception", error=repr(e))
        return 1
    finally:
        log.close()

    print()
    say(f"전체 완료 — {ok_count}/{cfg.rounds} 사이클 성공", "✔")
    say(f"로그: {log.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
