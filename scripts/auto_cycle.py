"""LG 배차 자동 사이클 — 고정 경로를 반복해 **랙 반납 정확도**를 검증한다.

흐름 (콘솔에서 사람이 하던 것과 동일한 API):
    호출(J1) → 랙 픽업(align_with_rack + jack_up) → J1 도착
    → 경유지(J2,J3,J4) 등록 + 출발
    → 각 도착마다 dwell 초 후 자동 [확인]
    → 마지막 확인 → 복귀(랙 반납 + 충전소) → 다음 사이클

── 이 스크립트의 진짜 목적 ────────────────────────────────
매 사이클마다 랙을 내려놓고 다시 집는다. 반납 위치가 조금씩 밀리면
**사이클이 갈수록 픽업이 어려워진다.**

    1사이클: 픽업 28초 → 반납, 위치 약간 밀림
    3사이클: 픽업 71초 (한 번 실패 후 백엔드가 회복해서 성공)
    5사이클: 픽업 실패

그래서 **사이클별 픽업 소요시간**을 기록한다. 이 값이 우상향하면
"몇 사이클마다 사람이 랙을 맞춰줘야 하는지"가 정해진다.
(docs/06_테스트현황과_이슈.md 의 버그 D — 랙 반납이 standard 이동이라 자리 앞에 내려놓음)

── 안전 ───────────────────────────────────────────────────
[확인]은 원래 "이 지점 작업이 끝났고 출발해도 안전하다"는 사람의 승인이다.
이 스크립트는 그것을 시간(--dwell)으로 대체하므로 **테스트 전용**이다.
현장 운영에 쓰지 말 것.

비상정지를 누르면 실패로 세지 않고 대기한다(해제하면 이어서 진행).
이상 감지 또는 Ctrl+C 시: ①이동 취소 ②세션 정리 ③스냅샷 ④종료.
잭은 내리지 않는다(통로 한가운데 랙을 놓으면 더 나빠진다).

── 사용 ───────────────────────────────────────────────────
  python scripts/auto_cycle.py --dry-run        # 연결·POI 확인만
  python scripts/auto_cycle.py --rounds 1       # 1사이클, 옆에서 지켜보며
  python scripts/auto_cycle.py --rounds 10      # 무인 반복

  ※ 백엔드(8002)가 떠 있어야 한다.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime

from cycle_common import (  # noqa: E402
    LOG_DIR, AbortRequested, Api, Log, Stopper, Watchdog,
    check_abort_key, load_pois, pick_robot, say, wait_for,
)

RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")


def run_cycle(api: Api, cfg, robot_id: int, wd: Watchdog, stopper: Stopper,
              log: Log, idx: int, pois: dict) -> dict | None:
    """1사이클. 성공하면 단계별 소요시간 dict, 중단이면 None."""
    call = pois[cfg.call]
    route = [pois[n] for n in cfg.route]
    say(f"────── 사이클 {idx} 시작 ──────", "▶")
    log.event("cycle_start", index=idx, call=call["name"], route=[p["name"] for p in route])
    times: dict[str, float] = {}

    # 1) 호출 — 로봇이 랙을 픽업해서 호출 지점으로
    t0 = time.time()
    say(f"호출: {call['name']} (id={call['id']}, 랙 픽업 포함)")
    res = api.call_poi(call["id"], with_rack=True)
    log.event("call", poi=call["id"], result=res)
    if cfg.dry_run:
        say("[dry-run] 이후 단계는 실제 상태가 없어 건너뜁니다", "→")
        return {}
    if res.get("reserved"):
        stopper.stop("가용 로봇이 없어 예약으로 전환됨 — 로봇 상태를 확인하세요")
        return None
    if not res.get("ok"):
        stopper.stop(f"호출 거부됨 — {res.get('detail') or res}")
        return None

    rid = res.get("robot_id") or robot_id
    say(f"배정된 로봇: id={rid} ({res.get('robot_name')})", "✔")

    # 2) 픽업 + 첫 이동 — ★ 여기 소요시간이 랙 반납 정확도의 지표
    t = wait_for(api, rid, "awaiting_next", wd, stopper, cfg.pickup_timeout,
                 "랙 픽업 + 호출지점 도착", log)
    if not t:
        return None
    times["pickup_move"] = round(time.time() - t0, 1)
    say(f"{t.get('current_poi_name')} 도착 (픽업+이동 {times['pickup_move']}초)", "✔")

    # 3) 경유지 등록 + 출발
    res = api.send_route(call["id"], [p["id"] for p in route])
    log.event("route", waypoints=[p["id"] for p in route], result=res)
    if not res.get("ok"):
        stopper.stop(f"경유지 등록 거부됨 — {res.get('detail') or res}")
        return None
    say(f"경유지 등록: {[p['name'] for p in route]} → 출발", "✔")

    # 4) 각 경유지 도착마다 dwell 후 자동 확인
    for n, wp in enumerate(route):
        tw = time.time()
        t = wait_for(api, rid, "awaiting_confirm", wd, stopper, cfg.move_timeout,
                     f"{n+1}번째 경유지({wp['name']}) 도착", log)
        if not t:
            return None
        where = t.get("current_poi_name")
        is_last = t.get("is_last")
        times[f"move_{where}"] = round(time.time() - tw, 1)
        say(f"{where} 도착 ({times[f'move_{where}']}초)"
            + (" — 마지막" if is_last else f" → 다음: {t.get('next_poi_name')}")
            + f", {cfg.dwell}초 후 자동 확인")

        # 사람이 작업하는 시간을 흉내낸다. 이 사이에도 감시는 계속.
        end = time.time() + cfg.dwell
        while time.time() < end:
            if check_abort_key():
                raise AbortRequested()
            fatal = wd.wait_if_blocked(abort_check=check_abort_key)
            if fatal:
                stopper.stop(fatal)
                return None
            reason = wd.check_moves()
            if reason:
                stopper.stop(reason)
                return None
            time.sleep(0.5)

        r = api.confirm(rid)
        log.event("confirm", at=where, is_last=is_last, result=r)
        say(f"[확인] 자동 입력 — {where}", "✔")

    # 5) 복귀 (랙 반납 → 충전소)
    tr = time.time()
    say("복귀 시퀀스 대기 (랙 반납 + 충전소 도킹)")
    t = wait_for(api, rid, ("inactive",), wd, stopper, cfg.return_timeout, "복귀 완료", log)
    if not t:
        return None
    times["return"] = round(time.time() - tr, 1)
    times["total"] = round(time.time() - t0, 1)
    say(f"사이클 {idx} 완료 — 총 {times['total']}초 "
        f"(픽업 {times['pickup_move']}s / 복귀 {times['return']}s)", "✔")
    log.event("cycle_done", index=idx, times=times)
    return times


def summary(rows: list[dict], route_names: list[str]) -> str:
    if not rows:
        return "완료된 사이클이 없습니다."
    W = 88
    cols = ["pickup_move"] + [f"move_{n}" for n in route_names] + ["return", "total"]
    head = ("  " + f"{'사이클':<8}" + f"{'픽업+이동':>10}"
            + "".join(f"{('→' + n):>9}" for n in route_names)
            + f"{'복귀':>9}{'합계':>9}")
    L = ["", "=" * W, "  사이클별 단계 소요 (초)", "=" * W, head, "-" * W]
    for i, r in enumerate(rows, 1):
        L.append("  " + f"{i:<8}" + "".join(f"{r.get(c, 0):>9.0f} " if c != cols[0]
                                            else f"{r.get(c, 0):>9.0f} " for c in cols))
    # 픽업 추세 — 랙 반납 정확도의 지표
    pk = [r.get("pickup_move", 0) for r in rows]
    L += ["-" * W]
    if len(pk) >= 2:
        first, last = pk[0], pk[-1]
        trend = ("증가 ↑" if last > first * 1.3 else
                 "감소 ↓" if last < first * 0.77 else "유지 →")
        L.append(f"  픽업 소요 추세: {first:.0f}초 → {last:.0f}초  ({trend})")
        if last > first * 1.3:
            L.append("  ⚠️ 픽업이 갈수록 오래 걸립니다 — 랙 반납 위치가 밀리고 있을 가능성.")
            L.append("     (백엔드가 실패 시 재보정·후진·측면우회로 회복하므로 시간이 늘어납니다)")
    L += ["=" * W, "",
          "  * 픽업+이동 = 호출부터 호출지점 도착까지. align_with_rack 실패·회복이 여기 반영된다.",
          "  * 이 값이 사이클마다 커지면 랙 반납이 누적으로 어긋나는 것 (버그 D).",
          "  * 비상정지·통신두절 대기 시간은 위 숫자에 포함되지 않는다."]
    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser(description="LG 배차 자동 사이클 (고정 경로)")
    p.add_argument("--rounds", type=int, default=1, help="반복 횟수 (기본 1)")
    # ★ POI 는 이름으로 지정한다. id 는 맵을 다시 만들 때마다 바뀐다(2026-08-12 하루 두 번).
    p.add_argument("--call", default="J1", help="호출할 POI 이름 (기본 J1)")
    p.add_argument("--route", default="J2,J3,J4", help="경유지 POI 이름 순서")
    p.add_argument("--dwell", type=float, default=5.0, help="도착 후 자동 확인까지 대기(초)")
    p.add_argument("--rest", type=float, default=10.0, help="사이클 사이 휴식(초)")
    p.add_argument("--pickup-timeout", type=float, default=300.0)
    p.add_argument("--move-timeout", type=float, default=300.0)
    p.add_argument("--return-timeout", type=float, default=420.0)
    p.add_argument("--fail-limit", type=int, default=4, help="이동 실패 몇 회에 정지할지")
    p.add_argument("--dry-run", action="store_true", help="실제 명령 없이 흐름만 확인")
    cfg = p.parse_args()
    cfg.route = [s.strip() for s in cfg.route.split(",") if s.strip()]

    log = Log(LOG_DIR / f"auto_cycle_{RUN_TS}.jsonl")

    print("=" * 66)
    print("LG 배차 자동 사이클 (고정 경로)")
    print(f"  호출 {cfg.call} → 경유지 {cfg.route} → 자동확인({cfg.dwell}s) → 복귀")
    print(f"  반복 {cfg.rounds}회" + ("   [DRY-RUN — 로봇 안 움직임]" if cfg.dry_run else ""))
    print(f"  로그 {log.path}")
    print("=" * 66)

    api = Api(dry=cfg.dry_run)

    # ── POI 를 이름으로 조회 ──
    try:
        pois = load_pois([cfg.call] + cfg.route)
    except Exception as e:
        say(f"POI 조회 실패: {e}", "✗")
        say("  DB가 떠 있는지, 맵에 그 이름의 POI 가 있는지 확인하세요.", "·")
        return 2
    for n in [cfg.call] + cfg.route:
        say(f"  {n:<4} id={pois[n]['id']}  type={pois[n]['type']}", "·")

    # ── 로봇 ──
    try:
        picked = pick_robot(api)
    except Exception as e:
        say(f"백엔드(8002) 응답 없음: {e}", "✗")
        say("  run_local.ps1 로 백엔드를 먼저 띄우세요.", "·")
        return 2
    if not picked:
        return 2
    robot_id, robot_ip, rb = picked
    say(f"대상 로봇: id={robot_id} {rb.get('robot_name')} ({robot_ip}) "
        f"배터리={rb.get('battery')}", "✔")

    wd = Watchdog(robot_ip, log, fail_limit=cfg.fail_limit)
    stopper = Stopper(api, robot_ip, robot_id, wd, log, RUN_TS)

    def on_sigint(sig, frame):
        stopper.stop("사용자 중단(Ctrl+C)")
        log.close()
        sys.exit(130)
    signal.signal(signal.SIGINT, on_sigint)

    rows: list[dict] = []
    rc = 0
    try:
        for i in range(1, cfg.rounds + 1):
            times = run_cycle(api, cfg, robot_id, wd, stopper, log, i, pois)
            if times is None:
                say(f"사이클 {i} 에서 중단됨 — 총 {len(rows)}회 성공", "✗")
                rc = 1
                break
            if times:
                rows.append(times)
            if i < cfg.rounds:
                say(f"{cfg.rest}초 휴식 후 다음 사이클")
                time.sleep(cfg.rest)
    except AbortRequested:
        say("사용자 중단 요청(q)", "!")
        stopper.stop("사용자 중단(q)")
        rc = 130
    except Exception as e:
        stopper.stop(f"스크립트 예외: {e!r}")
        log.event("exception", error=repr(e))
        rc = 1
    finally:
        if not cfg.dry_run:
            print(summary(rows, cfg.route))
        if wd.block_total:
            say(f"참고: 비상정지·통신두절로 대기한 시간 총 {wd.block_total:.0f}초 "
                f"(집계에서 제외됨)", "·")
        say(f"로그: {log.path}")
        log.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
