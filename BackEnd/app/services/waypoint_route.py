"""경유지를 거쳐 가는 경로 생성 — **번호 순서에 의존하지 않는다.**

로봇에게 경로 탐색을 맡기면(`type="standard"`) 좁아진 통로에서 경로를 못 찾고
그 자리에 선다 — 2026-09-07 실측(alert 1007, 로봇→R1 직선상 최소 여유 0.212m <
로봇 외접원 반경 0.519m). 경유지를 찍어 좌표열을 직접 주면(`type="along_given_route"`)
탐색 실패라는 실패 모드 자체가 없어진다.

## 2026-09-08 개편 — 한 줄 체인 → 자동 연결 그래프

예전에는 `W1, W2 …` 를 **번호순 한 줄**로 보고 그 구간을 통째로 지나갔다.
그래서 이런 문제가 있었다.
  · 번호를 잘못 매기면 되돌아간다 (실측: W10→W11 로 11m 역주행)
  · 중간에 하나 넣으려면 **뒤 번호를 전부 다시** 매겨야 한다
  · 갈림길을 표현할 수 없다

지금은 **번호가 그냥 이름**이다. 경유지끼리 가깝고 그 사이에 벽이 없으면 자동으로
이어서 그래프를 만들고, 출발 → 목표 최단경로를 뽑는다.
  · 찍는 순서·번호와 무관하다
  · 코너·갈림길이 자동으로 처리된다 (벽을 관통하는 연결이 안 생기므로)
  · 출발점에서 목표가 그냥 보이면 경유지를 쓰지 않고 기존 동작으로 넘어간다

경유지가 하나도 없으면 `("standard", {})` — 도입 전과 동일하다.
"""
from __future__ import annotations

import heapq
import logging
import math
import re
from typing import Optional

from app.database import SessionLocal
from app.models.map import MapPOI, RobotMap
from app.services import map_image

logger = logging.getLogger(__name__)

# 경로 이탈 허용치(m).
# 0 = 준 경로를 그대로 따르고, 장애물을 만나면 **우회하지 않고 앞에서 멈춘다.**
# LG 요구("사람이 접근하면 회피하지 않고 정지")와 일치한다.
DETOUR_TOLERANCE = 0

# 이 거리 안이면 이미 그 경유지에 있다고 본다 (m)
ARRIVED_EPS = 0.30

# 출발점이 목표에서 이 거리 안이고 사이에 벽도 없으면 경유지를 거치지 않는다.
# 바로 코앞인데 경유지를 찍으러 되돌아가는 낭비를 막는 것뿐이다.
# 그 외에는 **무조건 경유지 체인을 따라간다.**
DIRECT_MAX_M = 2.0

# 위 '코앞 직행' 을 판단할 때만 쓰는 벽 여유(m). 랙 적재 외접원 0.672 기준.
LOS_CLEAR_M = 0.70

# 출발·목표가 체인에 붙을 수 있는 후보 수 — **가장 가까운 것 몇 개**만.
#
# ★ 거리로 자르면 안 된다. 경유지 간격이 좁으면 후보가 여러 개 들어와서
#   코너 경유지를 건너뛰고 그다음 것에 바로 붙는다(직선이 더 짧으므로).
#   그러면 **코너를 가로질러 벽을 통과**하는 경로가 나온다.
#   실측: 2m 간격 15개 배치에서 C1→R2 가 W10(코너)을 빼고 W9 로 붙었다.
#
# 2개인 이유 — 로봇은 보통 두 경유지 **사이**에 있다. 그 둘만 후보로 두면
#   · 목표 쪽으로 가는 쪽을 최단경로가 고르고
#   · 코너에 있으면 양쪽 갈래의 입구가 하나씩 후보가 된다(C1 → W10/W11)
# 3개 이상으로 늘리면 다시 건너뛰기 시작한다.
ENTRY_CANDIDATES = 2

# 진입·이탈 구간에 붙이는 가중치. 1보다 크면 **체인을 따라가는 쪽을 선호**한다.
# 같은 거리일 때 통로를 타도록 하는 것 — 통로가 곧 사용자가 지정한 길이기 때문이다.
ENTRY_PENALTY = 1.2

# 경로가 이보다 길어지면 뭔가 잘못된 것 — 안전장치
MAX_HOPS = 30

_NAME_RE = re.compile(r"^W(\d+)$", re.IGNORECASE)


def waypoints(area_id: Optional[int]) -> list[dict]:
    """해당 area 활성 맵의 경유지. 이름이 `W<숫자>` 인 활성 POI.

    ★ 번호는 **이름일 뿐** 순서가 아니다. 정렬은 로그를 읽기 좋게 하려는 것뿐이다.
    """
    db = SessionLocal()
    try:
        q = db.query(RobotMap).filter(RobotMap.is_active == True)   # noqa: E712
        if area_id is not None:
            q = q.filter(RobotMap.area_id == area_id)
        active_map = q.order_by(RobotMap.id.desc()).first()
        if not active_map:
            return []

        out: list[dict] = []
        rows = (
            db.query(MapPOI)
            .filter(MapPOI.map_id == active_map.id, MapPOI.is_active == True)  # noqa: E712
            .all()
        )
        for p in rows:
            m = _NAME_RE.match((p.name or "").strip())
            if not m or p.world_x is None or p.world_y is None:
                continue
            out.append({
                "seq": int(m.group(1)),
                "name": p.name,
                "x": float(p.world_x),
                "y": float(p.world_y),
            })
        out.sort(key=lambda w: w["seq"])
        return out
    finally:
        db.close()


# 예전 이름 — 밖에서 쓰던 곳이 있을 수 있어 남겨둔다
chain = waypoints


def nearest_index(wps: list[dict], x: float, y: float) -> int:
    """(x, y) 에 가장 가까운 경유지의 인덱스."""
    best = float("inf")
    bi = 0
    for i, w in enumerate(wps):
        d = math.hypot(w["x"] - x, w["y"] - y)
        if d < best:
            best, bi = d, i
    return bi


def _shortest(meta, wps: list[dict], sx: float, sy: float,
              tx: float, ty: float) -> Optional[list[int]]:
    """출발 → 목표 최단경로에서 거치는 **경유지 인덱스** 목록.

    ★ 경유지는 '로봇이 다닐 통로' 그 자체다. 그래서 **번호 순서로 인접한 것끼리만**
      잇는다. 벽이 없다고 멀리 건너뛰면 안 된다 — 사용자가 그 길로만 다니라고
      찍어둔 것이기 때문이다. (2026-09-08 현장 요구)

      W1—W2—W3—W4—W5   ← 이 체인이 통로다. 중간을 건너뛰지 않는다

    출발·목표는 **어느 경유지로든** 붙을 수 있다. 어디로 들어가고 나올지는
    최단경로가 정한다. 그래서
      · 충전소에서 출발하면 코너 쪽 경유지로 들어가고
      · 복귀 중 W2 를 지난 지점에서 호출이 오면 W2 로 되짚어 들어간다

    '인접' 은 번호 자체가 아니라 **번호로 정렬한 목록에서의 이웃**이다.
    그래서 중간 번호(예: W3)를 지워도 체인이 끊기지 않는다.
    """
    n = len(wps)
    START, GOAL = n, n + 1
    adj: dict[int, list[tuple[int, float]]] = {i: [] for i in range(n + 2)}

    def add(a: int, b: int, w: float) -> None:
        adj[a].append((b, w))
        adj[b].append((a, w))

    def d(ax, ay, bx, by):
        return math.hypot(bx - ax, by - ay)

    # ① 통로 = 인접한 경유지끼리의 체인. 벽 검사를 하지 않는다.
    #    사용자가 "이 길로 다녀라" 고 찍은 것이므로 우리가 판단할 게 아니다.
    #    (벽 검사를 넣었더니 코너에서 체인이 끊겨 경유지가 통째로 안 쓰였다)
    for i in range(n - 1):
        add(i, i + 1, d(wps[i]["x"], wps[i]["y"], wps[i + 1]["x"], wps[i + 1]["y"]))

    # ② 출발·목표는 **가까운 경유지(입구)로만** 붙는다.
    #    모든 경유지에 붙이면 직선이 항상 더 짧아 체인을 통째로 건너뛴다.
    #    후보가 여럿이면 어디로 들어갈지는 최단경로가 정한다 —
    #    그래서 충전소에서는 코너 쪽으로, 복귀 중이면 방금 지난 경유지로 들어간다.
    def attach(node: int, px: float, py: float) -> None:
        cand = sorted((d(px, py, w["x"], w["y"]), i) for i, w in enumerate(wps))
        for dd, i in cand[:ENTRY_CANDIDATES]:
            add(node, i, dd * ENTRY_PENALTY)

    attach(START, sx, sy)
    attach(GOAL, tx, ty)

    # ③ 코앞이면 경유지를 거치러 되돌아가지 않는다 (벽이 없을 때만)
    d_st = d(sx, sy, tx, ty)
    if d_st <= DIRECT_MAX_M and not map_image.segment_blocked(
            meta, sx, sy, tx, ty, LOS_CLEAR_M):
        add(START, GOAL, d_st)

    dist = {START: 0.0}
    prev: dict[int, int] = {}
    pq = [(0.0, START)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")):
            continue
        if u == GOAL:
            break
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))

    if GOAL not in dist:
        return None

    path = []
    cur = GOAL
    while cur != START:
        path.append(cur)
        cur = prev[cur]
        if len(path) > MAX_HOPS:
            return None
    path.reverse()
    return [i for i in path if i < n]      # 목표 노드는 빼고 경유지만


def _plan_by_number(wps: list[dict], sx: float, sy: float,
                    tx: float, ty: float) -> tuple[str, dict]:
    """맵 이미지가 없을 때만 쓰는 **예전 방식** — 번호순 한 줄 체인.

    번호를 잘못 매기면 되돌아가는 그 방식이 맞다. 벽을 못 보는 상태에서
    그래프를 돌리면 무조건 직선이 최단이라 경유지가 통째로 무시되기 때문에,
    차라리 예전 동작을 유지한다. **맵 이미지를 넣는 게 정답이다.**
    """
    i = nearest_index(wps, sx, sy)
    j = nearest_index(wps, tx, ty)
    seg = wps[i:j + 1] if i <= j else list(reversed(wps[j:i + 1]))
    return _finish(seg, sx, sy, tx, ty)


def _finish(seg: list[dict], sx: float, sy: float,
            tx: float, ty: float) -> tuple[str, dict]:
    """경유지 목록을 실제 이동 인자로 만든다. 두 방식이 공유한다."""
    # 이미 그 자리에 서 있는 첫 경유지는 버린다 (제자리 이동 방지)
    while seg and math.hypot(seg[0]["x"] - sx, seg[0]["y"] - sy) < ARRIVED_EPS:
        seg.pop(0)
    # 목표와 같은 자리인 마지막 경유지도 버린다 (좌표 중복 방지)
    while seg and math.hypot(tx - seg[-1]["x"], ty - seg[-1]["y"]) < ARRIVED_EPS:
        seg.pop()
    if not seg:
        return "standard", {}

    coords: list[str] = []
    for w in seg:
        coords += [f"{w['x']:.4f}", f"{w['y']:.4f}"]
    coords += [f"{tx:.4f}", f"{ty:.4f}"]

    logger.info("[route] 경유지 경로 %s → 목표(%.2f, %.2f)",
                "→".join(w["name"] for w in seg), tx, ty)
    return "along_given_route", {
        "route_coordinates": ",".join(coords),
        "detour_tolerance": DETOUR_TOLERANCE,
    }


def plan(area_id: Optional[int], sx: float, sy: float,
         tx: float, ty: float) -> tuple[str, dict]:
    """`(move_type, create_move 추가 인자)` 를 만든다.

    경유지가 없거나 도움이 안 되면 `("standard", {})` — 기존 동작 그대로.
    """
    wps = waypoints(area_id)
    if not wps:
        # 여기가 무로그였다 — 경유지를 찍었는데 안 쓰이면 이유를 알 수가 없었다.
        # 흔한 원인: 이름이 W1 형식이 아니거나(^W\d+$), 로봇 area 와 맵 area 가 다르거나,
        #            그 맵이 활성(is_active) 이 아니다.
        logger.warning("[route] area=%s 에서 경유지(W1,W2…)를 하나도 못 찾음 → standard. "
                       "POI 이름이 W+숫자 인지, 로봇 area 와 맵 area 가 같은지 확인", area_id)
        return "standard", {}

    # ★ 출발과 목표의 '가장 가까운 경유지' 가 같으면 경유지를 거치지 않는다.
    #   둘 다 같은 구간 안에 있다는 뜻이라, 경유지를 들르면 지나쳤다 되돌아온다.
    #   2026-09-08 현장 — J1 반납 후 R2 로 갈 때 그 사이엔 경유지가 없는데
    #   J1·R2 둘 다 W1 이 최근접이라 J1 → W1 → R2 로 R2 를 지나쳤다 돌아왔다.
    if nearest_index(wps, sx, sy) == nearest_index(wps, tx, ty):
        logger.info("[route] 출발·목표가 같은 구간(최근접 경유지 %s) — 경유지 없이 직행",
                    wps[nearest_index(wps, sx, sy)]["name"])
        return "standard", {}

    # 맵 이미지는 '코앞 직행' 판단에만 쓴다. 없어도 경로 생성에는 지장이 없다.
    meta = map_image.map_meta_for_area(area_id)

    seq = _shortest(meta, wps, sx, sy, tx, ty)
    if seq is None:
        # 그래도 없으면 로봇 자체 탐색에 맡긴다(예전 동작). 막지는 않는다.
        logger.warning("[route] 경유지 그래프에서 경로를 못 찾음 → standard 로 폴백 "
                       f"(출발 {sx:.2f},{sy:.2f} → 목표 {tx:.2f},{ty:.2f})")
        return "standard", {}

    logger.info("[route] 경유지 %d개 중 %d개 사용 (출발 %.2f,%.2f → 목표 %.2f,%.2f)",
                len(wps), len(seq), sx, sy, tx, ty)
    return _finish([wps[i] for i in seq], sx, sy, tx, ty)


def approach_point(area_id: Optional[int], tx: float, ty: float) -> Optional[dict]:
    """목표 POI 에 가장 가까운 경유지 — 랙 정렬 전 접근점으로 쓴다."""
    wps = waypoints(area_id)
    if not wps:
        return None
    return wps[nearest_index(wps, tx, ty)]
