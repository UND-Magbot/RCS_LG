"""경유지 배치 점검 — 서버에서 실행한다 (DB 접속이 되는 PC).

    python scripts/check_waypoints.py [area_id]

읽기만 한다. 아무것도 바꾸지 않는다.

무엇을 보나
  1) 작업지점(R/J/충전소/대기)별 '최근접 경유지' 와 거리
  2) ★ 작업지점 둘이 같은 경유지를 공유하는가
       — waypoint_route.plan() 은 그 둘 사이 이동에 경유지를 **안 쓴다**(직행).
         거리 제한도 벽 검사도 없다.
  3) 안전 감시가 꺼지는 반경(2.0 m) 안에 들어온 경유지
  4) 번호순 체인의 각 구간 길이와 **벽 관통 여부**
       — 체인은 벽 검사 없이 번호순으로 잇는다. 번호가 곧 통로 순서여야 한다.
"""
import math
import sys

try:                       # 콘솔이 cp949 면 한글/기호가 깨진다
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "BackEnd"))

from app.database import SessionLocal                      # noqa: E402
from app.models.map import MapPOI, RobotMap                # noqa: E402
from app.services import map_image, waypoint_route         # noqa: E402
from app.services.safety_zone import work_skip_radius, WORK_POI_TYPES  # noqa: E402
from app.constants.rack_specs import RACK_SPECS, DEFAULT_SPEC_NAME     # noqa: E402

ROBOT_W = 0.50      # 로봇 폭(m). longjack 0.50 / S300 0.46 — 큰 쪽으로 둔다


def main() -> None:
    area_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    db = SessionLocal()
    try:
        q = db.query(RobotMap).filter(RobotMap.is_active == True)   # noqa: E712
        if area_id is not None:
            q = q.filter(RobotMap.area_id == area_id)
        amap = q.order_by(RobotMap.id.desc()).first()
        if not amap:
            print("활성 맵이 없다. area_id 를 확인할 것.")
            return
        print(f"맵 id={amap.id} area={amap.area_id} name={amap.name!r}\n")

        rows = (db.query(MapPOI)
                .filter(MapPOI.map_id == amap.id, MapPOI.is_active == True)  # noqa: E712
                .all())
    finally:
        db.close()

    pois = [p for p in rows if p.world_x is not None and p.world_y is not None]
    wps = waypoint_route.waypoints(amap.area_id)
    if not wps:
        print("★ 경유지(W+숫자)를 하나도 못 찾았다. 이름 형식과 area 를 확인할 것.")
        return
    works = [p for p in pois if (p.poi_type or "") in WORK_POI_TYPES]
    meta = map_image.map_meta_for_area(amap.area_id)
    skip_r = work_skip_radius(1.0)

    def dist(ax, ay, bx, by):
        return math.hypot(bx - ax, by - ay)

    # ── 1·2) 작업지점별 최근접 경유지 ─────────────────────────
    print("── 작업지점별 최근접 경유지 " + "─" * 30)
    owner: dict[str, list[str]] = {}
    for p in sorted(works, key=lambda p: p.name):
        i = waypoint_route.nearest_index(wps, p.world_x, p.world_y)
        d = dist(p.world_x, p.world_y, wps[i]["x"], wps[i]["y"])
        mark = "  ← 감시 OFF 반경 안" if d <= skip_r else ""
        print(f"  {p.name:<8} ({p.poi_type:<8}) → {wps[i]['name']:<4} {d:5.2f} m{mark}")
        owner.setdefault(wps[i]["name"], []).append(p.name)

    print()
    shared = {w: names for w, names in owner.items() if len(names) > 1}
    if shared:
        print("★ 경유지를 공유하는 작업지점 — 이 둘 사이 이동은 **경유지를 안 쓴다**")
        for w, names in shared.items():
            print(f"   {w} 를 공유: {', '.join(names)}")
            for a in range(len(names)):
                for b in range(a + 1, len(names)):
                    pa = next(p for p in works if p.name == names[a])
                    pb = next(p for p in works if p.name == names[b])
                    d = dist(pa.world_x, pa.world_y, pb.world_x, pb.world_y)
                    blocked = map_image.segment_blocked(
                        meta, pa.world_x, pa.world_y, pb.world_x, pb.world_y,
                        waypoint_route.LOS_CLEAR_M)
                    verdict = "★ 사이에 벽 — 위험" if blocked else "사이는 트임"
                    print(f"      {names[a]} ↔ {names[b]}  직선 {d:5.2f} m  {verdict}")
        print("   → 작업지점마다 전용 경유지를 하나씩 두면 해소된다.")
    else:
        print("작업지점이 경유지를 공유하지 않는다 — 직행 규칙에 걸릴 쌍이 없다. OK")

    # ── 3) 감시 OFF 반경 안의 경유지 ─────────────────────────
    print(f"\n── 안전감시 OFF 반경({skip_r:.2f} m) 안의 경유지 " + "─" * 16)
    hit = False
    for w in wps:
        near = [(dist(w["x"], w["y"], p.world_x, p.world_y), p.name)
                for p in works]
        near = [(d, n) for d, n in near if d <= skip_r]
        if near:
            hit = True
            near.sort()
            s = ", ".join(f"{n} {d:.2f} m" for d, n in near)
            print(f"  {w['name']:<4} — {s}")
    if not hit:
        print("  없음")
    else:
        print("  → 이 경유지를 지나는 동안 서행/정지 판정이 쉰다(로봇 자체 회피만 남는다).")

    # ── 4) 번호순 체인 구간 ─────────────────────────────────
    print("\n── 번호순 체인 구간 (벽 검사 없이 이어진다) " + "─" * 12)
    if meta is None:
        print("  맵 이미지가 없어 벽 검사를 못 한다.")
    for i in range(len(wps) - 1):
        a, b = wps[i], wps[i + 1]
        d = dist(a["x"], a["y"], b["x"], b["y"])
        blocked = map_image.segment_blocked(meta, a["x"], a["y"], b["x"], b["y"],
                                            waypoint_route.LOS_CLEAR_M)
        flag = "  ★ 벽 관통 — 번호 순서를 의심할 것" if blocked else ""
        print(f"  {a['name']:>4} → {b['name']:<4} {d:6.2f} m{flag}")

    # ── 5) 경로가 '주차된 랙' 옆을 얼마나 가깝게 지나나 ──────────
    #   경유지를 작업지점 가까이 찍으면, 그 작업지점에 랙이 놓인 상태로
    #   **랙을 싣고** 옆을 지날 때 부딪친다. detour_tolerance=0 이라
    #   로봇이 옆으로 비켜설 수도 없다.
    # 랙 규격은 POI 의 rack_size 에서 읽는다 — 현장(LG/LG2)과 테스트(S300)가 다르다.
    def carried_half(rack_size):
        spec = RACK_SPECS.get(rack_size or DEFAULT_SPEC_NAME) or RACK_SPECS[DEFAULT_SPEC_NAME]
        m = spec["margin"][0] if isinstance(spec["margin"], (list, tuple)) else spec["margin"]
        return max(ROBOT_W, spec["width"] + m * 2) / 2.0

    BARE_HALF = ROBOT_W / 2.0
    # 지나가는 로봇이 실은 랙 — 맵에 있는 것 중 가장 큰 것으로 최악을 본다
    PASS_HALF = max((carried_half(p.rack_size) for p in works), default=BARE_HALF)

    def seg_dist(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        return math.hypot(px - (ax + dx * t), py - (ay + dy * t))

    print("\n── 체인 경로 ↔ 주차된 랙 여유 " + "─" * 26)
    print(f"   로봇 폭 {ROBOT_W:.2f} m · 지나가는 로봇의 적재 반폭 {PASS_HALF:.2f} m")
    bad = False
    for i in range(len(wps) - 1):
        a, b = wps[i], wps[i + 1]
        for p in works:
            dd = seg_dist(p.world_x, p.world_y, a["x"], a["y"], b["x"], b["y"])
            if dd >= 1.60:
                continue
            need = carried_half(p.rack_size) + PASS_HALF
            loaded, bare = dd - need, dd - carried_half(p.rack_size) - BARE_HALF
            tag = ("  ★ 충돌" if loaded < 0 else
                   "  △ 여유 15cm 미만" if loaded < 0.15 else "")
            if tag:
                bad = True
            print(f"  {a['name']:>3}→{b['name']:<3} 가 {p.name:<3} 중심에서 {dd:4.2f} m  "
                  f"[{p.rack_size or DEFAULT_SPEC_NAME}] 필요 {need:4.2f}  "
                  f"랙싣고 {loaded:+5.2f} / 빈몸 {bare:+5.2f}{tag}")
    if bad:
        print("  → 경로선이 작업지점 중심에서 '필요' + 0.15 m 는 떨어져야 한다.")


if __name__ == "__main__":
    main()
