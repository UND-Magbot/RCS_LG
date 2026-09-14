"""LG 배차 랜덤 사이클 — 사람이 콘솔에서 아무렇게나 조작하는 상황을 흉내낸다.

`auto_cycle.py` 가 "정해진 한 경로를 반복"이라면, 이쪽은 **매번 다르게** 간다.
정해진 순서만 돌면 안 밟히는 경로(특히 **예약 이어받기**)를 훑는 것이 목적이다.

── 흐름 (콘솔 실제 화면 기준) ────────────────────────────
    1. 호출 POI 랜덤 (J1~J4 중 하나)
    2. 랙 픽업 → 도착 → awaiting_next
    3. ★ 랜덤 분기 ①  — 콘솔 화면에 [경유지 등록] [종료] 두 버튼이 있는 지점
         (a) 경유지 1~3개 랜덤 등록 → 진행
         (b) 바로 [종료] → 복귀            ← "한 번은 그냥 복귀"
    4. 각 경유지 도착 → 랜덤 대기 → [확인]
         (낮은 확률로 중간에 [종료] 눌러 끊기도 한다)
    5. ★ 랜덤 분기 ②  — 마지막 확인 **전에** 확률 p 로 다른 POI 예약
         예약 있음 → 백엔드가 자동 이어받기(충전소·랙반납 생략) → 3번으로 (루프)
         예약 없음 → 복귀 → 사이클 종료

⚠️ 마지막 [확인] 뒤에 "복귀할지 더 할지" 고르는 화면은 **없다**(코드·화면 확인함).
   더 진행하려면 **예약이 미리 걸려 있어야** 한다. 그래서 분기 ②가 예약을 건다.

── 왜 이걸 하는가 ─────────────────────────────────────────
`_take_next_reservation_if_continuable`(예약 이어받기)은 실기에서 거의 안 밟아본
경로다. 여기서 버그가 나올 가능성이 높고, **그것을 찾는 것이 이 테스트의 목적**이다.

── 재현성 ─────────────────────────────────────────────────
`--seed` 를 반드시 고정할 것. 문제가 났을 때 같은 순서를 재현하지 못하면
원인 분석이 불가능하다.

── 사용 ───────────────────────────────────────────────────
  python scripts/random_cycle.py --dry-run
  python scripts/random_cycle.py --seed 42 --cycles 3      # 옆에서 지켜보며
  python scripts/random_cycle.py --seed 42 --cycles 20     # 무인

  ※ 백엔드(8002)가 떠 있어야 한다.
"""
from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from collections import Counter
from datetime import datetime

from cycle_common import (  # noqa: E402
    LOG_DIR, AbortRequested, Api, Log, Stopper, Watchdog,
    check_abort_key, load_pois, pick_robot, say, wait_for,
)

RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")


class RandomRunner:
    def __init__(self, api: Api, cfg, pois: dict, jacks: list[str],
                 wd: Watchdog, stopper: Stopper, log: Log, rng: random.Random):
        self.api, self.cfg, self.pois, self.jacks = api, cfg, pois, jacks
        self.wd, self.stopper, self.log, self.rng = wd, stopper, log, rng
        self.reserved: list[int] = []      # 이 스크립트가 건 예약 (정리용)
        self.events: list[dict] = []       # 사이클 요약
        self.violations: list[str] = []    # 불변식 위반

    # ── 감시하며 쉬기 ──
    def _sleep_watching(self, sec: float) -> bool:
        end = time.time() + sec
        while time.time() < end:
            if check_abort_key():
                raise AbortRequested()
            fatal = self.wd.wait_if_blocked(abort_check=check_abort_key)
            if fatal:
                self.stopper.stop(fatal)
                return False
            reason = self.wd.check_moves()
            if reason:
                self.stopper.stop(reason)
                return False
            time.sleep(0.5)
        return True

    # ── 예약 걸기 (분기 ②) ──
    def _make_reservation(self, exclude: set[int]) -> int | None:
        """비어있는 작업 POI 하나를 예약. 로봇이 작업 중이라 백엔드가 예약으로 처리한다."""
        try:
            cs = self.api.console_status()
        except Exception as e:
            say(f"콘솔 상태 조회 실패(예약 생략): {e}", "~")
            return None
        busy = set(cs.get("occupied_poi_ids", [])) | set(cs.get("reserved_poi_ids", []))
        cands = [self.pois[n]["id"] for n in self.jacks
                 if self.pois[n]["id"] not in busy and self.pois[n]["id"] not in exclude]
        if not cands:
            say("예약 가능한 빈 위치가 없습니다 — 이번엔 예약 생략", "~")
            return None
        pid = self.rng.choice(cands)
        res = self.api.call_poi(pid, with_rack=True)
        name = next(n for n in self.jacks if self.pois[n]["id"] == pid)
        if res.get("reserved"):
            self.reserved.append(pid)
            say(f"★ 예약 생성: {name} — 마지막 확인 후 이어받기가 일어나야 합니다", "★")
            self.log.event("reserve", poi=pid, name=name)
            return pid
        # 예약이 아니라 즉시 배차됐다면 로봇이 하나 더 있다는 뜻(지금은 1대라 발생 안 함)
        say(f"예약이 아니라 즉시 배차됨 — 이번엔 무시 ({res})", "~")
        return None

    def _cleanup_reservations(self) -> None:
        for pid in list(self.reserved):
            try:
                self.api.cancel_reservation(pid)
            except Exception:
                pass
        self.reserved.clear()

    # ── 한 사이클 ──
    def run_cycle(self, idx: int) -> bool:
        cfg, rng = self.cfg, self.rng
        call_name = rng.choice(self.jacks)
        call = self.pois[call_name]
        say(f"────── 사이클 {idx} — 호출 {call_name} ──────", "▶")
        self.log.event("cycle_start", index=idx, call=call_name)
        ev = {"cycle": idx, "call": call_name, "legs": [], "end_reason": None,
              "handovers": 0}
        t0 = time.time()

        res = self.api.call_poi(call["id"], with_rack=True)
        self.log.event("call", poi=call["id"], name=call_name, result=res)
        if cfg.dry_run:
            say("[dry-run] 이후 단계는 실제 상태가 없어 건너뜁니다", "→")
            return True
        if res.get("reserved"):
            self.stopper.stop("가용 로봇이 없어 예약으로 전환됨 — 로봇 상태를 확인하세요")
            return False
        if not res.get("ok"):
            self.stopper.stop(f"호출 거부됨 — {res.get('detail') or res}")
            return False
        rid = res["robot_id"]

        t = wait_for(self.api, rid, "awaiting_next", self.wd, self.stopper,
                     cfg.pickup_timeout, "랙 픽업 + 호출지점 도착", self.log)
        if not t:
            return False
        ev["pickup_sec"] = round(time.time() - t0, 1)
        say(f"{t.get('current_poi_name')} 도착 (픽업+이동 {ev['pickup_sec']}초)", "✔")

        here_id = t.get("current_poi_id") or call["id"]
        visited = {here_id}

        # ── awaiting_next 루프 (예약 이어받기로 여러 번 돌 수 있다) ──
        for leg in range(1, cfg.max_legs + 1):
            # ★ 분기 ① — 콘솔의 [경유지 등록] vs [종료]
            go_more = rng.random() < cfg.route_prob
            if not go_more:
                say("분기① → [종료] 선택 (경유지 없이 바로 복귀)", "◆")
                self.log.event("branch1", leg=leg, choice="end")
                self.api.end_job(rid)
                ev["end_reason"] = "랜덤: 경유지 없이 종료"
                break

            n_wp = rng.randint(1, min(cfg.max_waypoints, max(1, len(self.jacks) - 1)))
            cands = [n for n in self.jacks if self.pois[n]["id"] not in visited]
            rng.shuffle(cands)
            route = cands[:n_wp]
            if not route:
                say("갈 수 있는 빈 위치가 없어 종료합니다", "~")
                self.api.end_job(rid)
                ev["end_reason"] = "빈 위치 없음"
                break

            say(f"분기① → [경유지 등록] {route}", "◆")
            self.log.event("branch1", leg=leg, choice="route", route=route)
            r = self.api.send_route(here_id, [self.pois[n]["id"] for n in route])
            if not r.get("ok"):
                self.stopper.stop(f"경유지 등록 거부됨 — {r.get('detail') or r}")
                return False
            ev["legs"].append(route)

            # ── 경유지 순회 ──
            aborted = False
            for n, wp_name in enumerate(route):
                is_last_wp = (n == len(route) - 1)
                t = wait_for(self.api, rid, "awaiting_confirm", self.wd, self.stopper,
                             cfg.move_timeout, f"{wp_name} 도착", self.log)
                if not t:
                    return False
                where = t.get("current_poi_name")
                visited.add(t.get("current_poi_id"))

                # ── 불변식: 도착한 곳이 우리가 보낸 순서와 같은가 ──
                if where != wp_name:
                    v = (f"[사이클{idx}] 경유지 순서 불일치 — "
                         f"보낸 것={wp_name}, 실제 도착={where}")
                    self.violations.append(v)
                    say(v, "✗")
                    self.log.event("violation", kind="waypoint_order",
                                   expected=wp_name, actual=where)
                # ── 불변식: 마지막 여부 표시가 맞는가 ──
                if bool(t.get("is_last")) != is_last_wp:
                    v = (f"[사이클{idx}] is_last 불일치 — {where}: "
                         f"서버={t.get('is_last')}, 실제={is_last_wp}")
                    self.violations.append(v)
                    say(v, "✗")

                dwell = rng.uniform(cfg.dwell_min, cfg.dwell_max)
                say(f"{where} 도착 — {dwell:.1f}초 후 확인"
                    + ("  (마지막)" if is_last_wp else f" → 다음 {route[n+1]}"))
                if not self._sleep_watching(dwell):
                    return False

                # 낮은 확률로 중간에 [종료]
                if not is_last_wp and rng.random() < cfg.abort_prob:
                    say("랜덤 → 경유지 도중 [종료] 선택", "◆")
                    self.log.event("mid_end", at=where)
                    self.api.end_job(rid)
                    ev["end_reason"] = "랜덤: 경유지 도중 종료"
                    aborted = True
                    break

                # ★ 분기 ② — 마지막 확인 **전에** 예약을 걸어야 이어받기가 열린다
                if is_last_wp and rng.random() < cfg.reserve_prob:
                    self._make_reservation(exclude=visited)

                self.api.confirm(rid)
                self.log.event("confirm", at=where, is_last=is_last_wp)
                say(f"[확인] — {where}", "✔")

            if aborted:
                break

            # ── 마지막 확인 후: 이어받기가 일어났는가? ──
            # 백엔드가 예약을 소비하면 잠시 뒤 awaiting_next 로 돌아온다.
            got = self._await_handover(rid, timeout=cfg.handover_timeout)
            if got is None:
                return False
            if not got:
                ev["end_reason"] = ev["end_reason"] or (
                    "예약 있었으나 이어받기 안 됨" if self.reserved else "예약 없음 → 자동 복귀")
                break
            ev["handovers"] += 1
            here_id = got
            visited.add(here_id)
            name = next((n for n in self.jacks if self.pois[n]["id"] == here_id), str(here_id))
            say(f"★ 예약 이어받기 성공 → {name} 도착. 계속 진행합니다", "★")
            self.log.event("handover", to=here_id, name=name)
            if here_id in self.reserved:
                self.reserved.remove(here_id)
        else:
            say(f"최대 구간({cfg.max_legs})에 도달 — 종료합니다", "~")
            self.api.end_job(rid)
            ev["end_reason"] = f"최대 구간 {cfg.max_legs} 도달"

        # ── 복귀 완료 대기 ──
        t = wait_for(self.api, rid, ("inactive",), self.wd, self.stopper,
                     cfg.return_timeout, "복귀 완료", self.log)
        if not t:
            return False
        ev["total_sec"] = round(time.time() - t0, 1)
        self.events.append(ev)
        say(f"사이클 {idx} 완료 — {ev['total_sec']}초, 구간 {len(ev['legs'])}개, "
            f"이어받기 {ev['handovers']}회 ({ev['end_reason']})", "✔")
        self.log.event("cycle_done", **ev)
        return True

    def _await_handover(self, rid: int, timeout: float) -> int | None | bool:
        """마지막 확인 후 이어받기를 기다린다.

        반환: POI id = 이어받기 성공 / False = 복귀로 감 / None = 정지됨
        """
        end = time.time() + timeout
        while time.time() < end:
            if check_abort_key():
                raise AbortRequested()
            fatal = self.wd.wait_if_blocked(abort_check=check_abort_key)
            if fatal:
                self.stopper.stop(fatal)
                return None
            try:
                t = self.api.tablet_status(rid)
            except Exception:
                time.sleep(1.0)
                continue
            st = t.get("session_status")
            if st == "awaiting_next":
                return t.get("current_poi_id")
            if st in ("returning",) or not t.get("active"):
                return False
            if st == "failed":
                self.stopper.stop("마지막 확인 후 세션 failed")
                return None
            reason = self.wd.check_moves()
            if reason:
                self.stopper.stop(reason)
                return None
            time.sleep(1.0)
        say("이어받기/복귀 판정 시간 초과 — 복귀로 간주", "~")
        return False

    # ── 집계 ──
    def summary(self) -> str:
        if not self.events:
            return "완료된 사이클이 없습니다."
        W = 92
        L = ["", "=" * W, "  랜덤 사이클 결과", "=" * W,
             f"  {'#':<4}{'호출':<6}{'구간':<28}{'이어받기':>8}{'총시간':>9}   종료 사유",
             "-" * W]
        for e in self.events:
            legs = " / ".join("→".join(x) for x in e["legs"]) or "(없음)"
            L.append(f"  {e['cycle']:<4}{e['call']:<6}{legs[:27]:<28}"
                     f"{e['handovers']:>8}{e.get('total_sec', 0):>9.0f}   {e['end_reason']}")
        L += ["-" * W]

        n = len(self.events)
        hv = sum(e["handovers"] for e in self.events)
        pk = [e.get("pickup_sec", 0) for e in self.events if e.get("pickup_sec")]
        L.append(f"  사이클 {n}회 / 예약 이어받기 {hv}회 발생")
        if pk:
            L.append(f"  픽업 소요: 첫 {pk[0]:.0f}초 → 끝 {pk[-1]:.0f}초 "
                     f"(중앙 {sorted(pk)[len(pk)//2]:.0f}초)")
        reasons = Counter(e["end_reason"] for e in self.events)
        L.append("  종료 사유 분포:")
        for r, c in reasons.most_common():
            L.append(f"    {c:>3}회  {r}")
        L += ["=" * W]

        if self.violations:
            L += ["", f"  ⚠️ 불변식 위반 {len(self.violations)}건 — 백엔드 결함 후보:"]
            L += [f"    - {v}" for v in self.violations]
        else:
            L += ["", "  ✔ 불변식 위반 없음 (경유지 순서·마지막 표시 모두 일치)"]

        L += ["", "  읽는 법", "  " + "-" * 46,
              "  * 이어받기 = 마지막 확인 시점에 예약이 있어 충전소를 안 거치고 이어서 간 것.",
              "      이 경로가 실기에서 거의 안 밟아본 코드라 이번 테스트의 핵심 대상이다.",
              "  * '예약 있었으나 이어받기 안 됨' 이 나오면 원인을 봐야 한다",
              "      (배터리 부족 / 그 POI 가 점유됨 / 이어받기 로직 결함).",
              "  * 픽업 소요가 사이클마다 커지면 랙 반납이 밀리는 것 (auto_cycle 과 같은 지표).",
              f"  * 전체 로그: {self.log.path}"]
        return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser(description="LG 배차 랜덤 사이클")
    p.add_argument("--cycles", type=int, default=3, help="사이클 수 (기본 3)")
    p.add_argument("--seed", type=int, default=42, help="난수 시드 — 재현에 필수")
    p.add_argument("--jacks", default="J1,J2,J3,J4", help="사용할 작업 POI 이름들")
    p.add_argument("--route-prob", type=float, default=0.8,
                   help="분기①에서 [경유지 등록]을 고를 확률 (나머지는 바로 종료)")
    p.add_argument("--reserve-prob", type=float, default=0.5,
                   help="마지막 확인 전에 예약을 걸 확률 (= 이어받기 유발)")
    p.add_argument("--abort-prob", type=float, default=0.1,
                   help="경유지 도중 [종료]를 누를 확률")
    p.add_argument("--max-waypoints", type=int, default=3, help="한 구간의 최대 경유지 수")
    p.add_argument("--max-legs", type=int, default=4, help="한 사이클의 최대 구간 수(무한루프 방지)")
    p.add_argument("--dwell-min", type=float, default=3.0)
    p.add_argument("--dwell-max", type=float, default=12.0)
    p.add_argument("--rest", type=float, default=10.0, help="사이클 사이 휴식(초)")
    p.add_argument("--pickup-timeout", type=float, default=300.0)
    p.add_argument("--move-timeout", type=float, default=300.0)
    p.add_argument("--return-timeout", type=float, default=420.0)
    p.add_argument("--handover-timeout", type=float, default=90.0,
                   help="마지막 확인 후 이어받기/복귀 판정 대기(초)")
    p.add_argument("--fail-limit", type=int, default=4)
    p.add_argument("--dry-run", action="store_true")
    cfg = p.parse_args()
    jacks = [s.strip() for s in cfg.jacks.split(",") if s.strip()]

    log = Log(LOG_DIR / f"random_cycle_{RUN_TS}.jsonl")
    rng = random.Random(cfg.seed)

    print("=" * 70)
    print("LG 배차 랜덤 사이클")
    print(f"  작업 위치 {jacks} | 시드 {cfg.seed} | {cfg.cycles}사이클")
    print(f"  경유지 등록 확률 {cfg.route_prob} / 예약(이어받기) 확률 {cfg.reserve_prob}"
          f" / 도중종료 확률 {cfg.abort_prob}")
    print(("  [DRY-RUN — 로봇 안 움직임]" if cfg.dry_run else "  ※ 실기 로봇이 움직입니다"))
    print(f"  로그 {log.path}")
    print("=" * 70)
    log.event("config", config={**vars(cfg), "jacks": jacks})

    api = Api(dry=cfg.dry_run)

    try:
        pois = load_pois(jacks)
    except Exception as e:
        say(f"POI 조회 실패: {e}", "✗")
        return 2
    for n in jacks:
        say(f"  {n:<4} id={pois[n]['id']}  type={pois[n]['type']}", "·")

    try:
        picked = pick_robot(api)
    except Exception as e:
        say(f"백엔드(8002) 응답 없음: {e}", "✗")
        return 2
    if not picked:
        return 2
    robot_id, robot_ip, rb = picked
    say(f"대상 로봇: id={robot_id} {rb.get('robot_name')} ({robot_ip}) "
        f"배터리={rb.get('battery')}", "✔")

    wd = Watchdog(robot_ip, log, fail_limit=cfg.fail_limit)
    stopper = Stopper(api, robot_ip, robot_id, wd, log, RUN_TS)
    runner = RandomRunner(api, cfg, pois, jacks, wd, stopper, log, rng)

    def on_sigint(sig, frame):
        stopper.stop("사용자 중단(Ctrl+C)")
        runner._cleanup_reservations()
        log.close()
        sys.exit(130)
    signal.signal(signal.SIGINT, on_sigint)

    rc = 0
    try:
        for i in range(1, cfg.cycles + 1):
            if not runner.run_cycle(i):
                rc = 1
                break
            if i < cfg.cycles:
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
        runner._cleanup_reservations()      # 남은 예약 정리 (안 하면 다음 실행에 영향)
        if not cfg.dry_run:
            print(runner.summary())
        if wd.block_total:
            say(f"참고: 비상정지·통신두절 대기 총 {wd.block_total:.0f}초", "·")
        say(f"로그: {log.path}")
        log.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
