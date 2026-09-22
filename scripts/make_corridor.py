# -*- coding: utf-8 -*-
"""통로형 가상벽 생성기 — 중심선(또는 기준 벽선)과 폭을 주면 좌우 가상벽을 정확히 만든다.

마우스로 사각형 4점을 찍어서는 폭 1.2m 를 맞출 수 없다(줌 배율에 따라 오차가 달라짐).
이 스크립트는 좌표 계산으로 만들기 때문에 폭 오차가 0 이다.

만들어지는 것: map_polygons 의 shape_type="firewall" 레코드.
맵 편집기에서 그린 '가상벽'과 완전히 같은 형식이라 화면에서 보이고, 동기화도 그대로 탄다.

사용 예)
  # 0) 현재 상태 보기 — POI 좌표와 기존 가상벽 목록
  python scripts/make_corridor.py --map 30 --list

  # 1) 중심선 기준 (path 가 통로 한가운데)
  python scripts/make_corridor.py --map 30 --width 1.2 \
      --path "R2  11.0,14.4  13.4,10.8  R1" --dry-run

  # 2) 벽 기준 (path 가 실제 벽면을 따라가는 선, 거기서 gap 만큼 띄우고 통로를 낸다)
  python scripts/make_corridor.py --map 30 --width 1.2 --mode wall --gap 0.20 --side left \
      --path "..."

  # 3) 충전소 주변은 벽을 끊는다 (도킹 기동 공간 확보)
  python scripts/make_corridor.py --map 30 --width 1.2 --path "..." --exclude "C1:2.0"

  # 4) 기존 가상벽을 지우고 새로 (DB 기준)
  python scripts/make_corridor.py --map 30 --width 1.2 --path "..." --replace

  # 5) DB 의 가상벽만 전부 삭제
  python scripts/make_corridor.py --map 30 --delete

★ 주의 — DB 에서 지웠다고 로봇에서 지워지지 않는다.
  동기화 코드는 "DB 에 가상벽이 없으면 로봇의 기존 것을 보존" 하도록 되어 있다.
  로봇에서도 지우려면  python scripts/wall_probe.py clear  를 쓸 것.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import subprocess
import sys

try:
    import pymysql
except ModuleNotFoundError:
    # 시스템 python 에는 pymysql 이 없다. 백엔드 venv 로 자동 재실행한다.
    _venv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "BackEnd", "venv", "Scripts", "python.exe")
    if os.path.exists(_venv) and os.path.abspath(sys.executable) != os.path.abspath(_venv):
        print(f"[i] pymysql 이 없어 백엔드 venv 로 재실행합니다\n    {_venv}\n")
        sys.exit(subprocess.call([_venv, os.path.abspath(__file__)] + sys.argv[1:]))
    sys.exit("pymysql 이 없습니다.  BackEnd\\venv\\Scripts\\python.exe 로 실행하거나 "
             "pip install pymysql 하세요.")

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", "1234"),
    database=os.getenv("DB_NAME", "rcs_lg_db"),
    charset="utf8mb4",
)
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "BackEnd", "static")

Pt = tuple[float, float]


# ── 기하 ────────────────────────────────────────────────────────────

def _unit(a: Pt, b: Pt) -> Pt:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return (0.0, 0.0)
    return (dx / n, dy / n)


def offset_polyline(pts: list[Pt], d: float) -> list[Pt]:
    """폴리라인을 d 만큼 평행이동. d>0 이면 진행방향 기준 왼쪽.

    코너는 miter join — 인접한 두 평행선의 교점으로 잇는다.
    그냥 각 구간을 평행이동만 하면 바깥은 벌어지고 안쪽은 겹친다.
    """
    if len(pts) < 2:
        return list(pts)

    # 각 구간을 평행이동
    segs = []
    for a, b in zip(pts, pts[1:]):
        ux, uy = _unit(a, b)
        nx, ny = -uy, ux          # 왼쪽 법선
        segs.append(((a[0] + nx * d, a[1] + ny * d),
                     (b[0] + nx * d, b[1] + ny * d)))

    out = [segs[0][0]]
    for (p1, p2), (p3, p4) in zip(segs, segs[1:]):
        ip = _line_intersect(p1, p2, p3, p4)
        out.append(ip if ip else p2)   # 평행이면 그냥 이음
    out.append(segs[-1][1])
    return out


def _line_intersect(p1: Pt, p2: Pt, p3: Pt, p4: Pt):
    """두 직선(무한 연장)의 교점. 평행이면 None."""
    x1, y1 = p1; x2, y2 = p2; x3, y3 = p3; x4, y4 = p4
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-9:
        return None
    a = x1 * y2 - y1 * x2
    b = x3 * y4 - y3 * x4
    return ((a * (x3 - x4) - (x1 - x2) * b) / den,
            (a * (y3 - y4) - (y1 - y2) * b) / den)


def _seg_circle_span(a: Pt, b: Pt, c: Pt, r: float):
    """선분 a→b 중 원(c,r) 안에 들어가는 구간을 [t0,t1] (0~1) 로. 없으면 None."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    fx, fy = a[0] - c[0], a[1] - c[1]
    A = dx * dx + dy * dy
    if A < 1e-12:
        return None
    B = 2 * (fx * dx + fy * dy)
    C = fx * fx + fy * fy - r * r
    disc = B * B - 4 * A * C
    if disc < 0:
        return None
    sq = math.sqrt(disc)
    t0 = max(0.0, (-B - sq) / (2 * A))
    t1 = min(1.0, (-B + sq) / (2 * A))
    return (t0, t1) if t1 > t0 else None


def split_by_circles(pts: list[Pt], circles: list[tuple[Pt, float]]) -> list[list[Pt]]:
    """폴리라인에서 원 안에 들어가는 부분을 잘라내고 남은 조각들을 반환."""
    if not circles:
        return [pts]

    # 누적 거리 기준으로 '잘라낼 구간' 을 모은다
    lengths = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    starts = [0.0]
    for L in lengths:
        starts.append(starts[-1] + L)
    total = starts[-1]

    cuts: list[tuple[float, float]] = []
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        for c, r in circles:
            span = _seg_circle_span(a, b, c, r)
            if span:
                cuts.append((starts[i] + span[0] * lengths[i],
                             starts[i] + span[1] * lengths[i]))
    if not cuts:
        return [pts]

    cuts.sort()
    merged = [list(cuts[0])]
    for s, e in cuts[1:]:
        if s <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # 잘라낸 구간의 여집합 = 남길 조각
    keep: list[tuple[float, float]] = []
    cur = 0.0
    for s, e in merged:
        if s - cur > 1e-6:
            keep.append((cur, s))
        cur = max(cur, e)
    if total - cur > 1e-6:
        keep.append((cur, total))

    return [_subpolyline(pts, starts, s, e) for s, e in keep if e - s > 0.05]


def _point_at(pts: list[Pt], starts: list[float], s: float) -> Pt:
    for i in range(len(pts) - 1):
        if starts[i] <= s <= starts[i + 1]:
            seg = starts[i + 1] - starts[i]
            t = 0.0 if seg < 1e-9 else (s - starts[i]) / seg
            return (pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t,
                    pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t)
    return pts[-1]


def _subpolyline(pts: list[Pt], starts: list[float], s: float, e: float) -> list[Pt]:
    out = [_point_at(pts, starts, s)]
    for i, st in enumerate(starts):
        if s < st < e:
            out.append(pts[i])
    out.append(_point_at(pts, starts, e))
    return out


def ribbon(pts: list[Pt], thickness: float) -> list[Pt]:
    """폴리라인을 얇은 닫힌 띠(폴리곤)로. 백엔드가 이걸 닫힌 LineString 으로 보낸다."""
    h = thickness / 2
    left = offset_polyline(pts, h)
    right = offset_polyline(pts, -h)
    return left + list(reversed(right))


# ── 맵 메타 ─────────────────────────────────────────────────────────

def png_size(path: str) -> tuple[int, int]:
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"PNG 아님: {path}")
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def world_to_svg(wx: float, wy: float, meta: dict) -> tuple[float, float]:
    """맵 편집기와 동일한 변환 (frontend/app/map/page.tsx svgToWorld 의 역)."""
    ipx = (wx - meta["ox"]) / meta["res"]
    ipy = meta["ih"] - (wy - meta["oy"]) / meta["res"]
    return (ipx - meta["iw"] / 2, ipy - meta["ih"] / 2)


# ── DB ──────────────────────────────────────────────────────────────

def load_map(cur, map_id: int) -> dict:
    cur.execute("SELECT grid_origin_x, grid_origin_y, grid_resolution, image_url "
                "FROM robot_maps WHERE id=%s", (map_id,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"맵 {map_id} 없음")
    ox, oy, res, image_url = row
    if not res:
        sys.exit(f"맵 {map_id} 에 grid_resolution 이 없다")
    img = os.path.join(STATIC_DIR, (image_url or "").replace("/static/", "").replace("/", os.sep))
    if not os.path.exists(img):
        sys.exit(f"맵 이미지 없음: {img}")
    iw, ih = png_size(img)
    return {"ox": float(ox), "oy": float(oy), "res": float(res), "iw": iw, "ih": ih, "img": img}


def load_pois(cur, map_id: int) -> dict[str, Pt]:
    cur.execute("SELECT name, world_x, world_y FROM map_pois "
                "WHERE map_id=%s AND world_x IS NOT NULL", (map_id,))
    return {n: (float(x), float(y)) for n, x, y in cur.fetchall()}


# ── 메인 ────────────────────────────────────────────────────────────

def parse_path(spec: str, pois: dict[str, Pt]) -> list[Pt]:
    out: list[Pt] = []
    for tok in spec.replace(";", " ").split():
        if "," in tok:
            x, y = tok.split(",")
            out.append((float(x), float(y)))
        elif tok in pois:
            out.append(pois[tok])
        else:
            sys.exit(f"좌표도 아니고 POI 이름도 아님: '{tok}'  (가능한 POI: {', '.join(sorted(pois))})")
    if len(out) < 2:
        sys.exit("--path 에 점이 2개 이상 필요하다")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="통로형 가상벽 생성기")
    ap.add_argument("--map", type=int, required=True, help="DB 의 robot_maps.id")
    ap.add_argument("--width", type=float, default=1.2, help="통로 폭 (m). 기본 1.2")
    ap.add_argument("--path", help="중심선(또는 기준 벽선). 'R2 11.0,14.4 J1' 처럼 POI 이름/좌표 나열")
    ap.add_argument("--mode", choices=["center", "wall"], default="center",
                    help="center: path 가 통로 한가운데 / wall: path 가 벽면")
    ap.add_argument("--gap", type=float, default=0.0, help="mode=wall 일 때 벽에서 띄울 거리 (m)")
    ap.add_argument("--side", choices=["left", "right"], default="left",
                    help="mode=wall 일 때 통로가 벽의 어느 쪽인지 (path 진행방향 기준)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="벽을 끊을 구역. 'C1:2.0' (POI이름:반경m) 또는 'x,y:2.0'. 여러 번 지정 가능")
    ap.add_argument("--thickness", type=float, default=0.02, help="벽 두께 (m). 기본 0.02")
    ap.add_argument("--name-prefix", default="CW", help="생성될 가상벽 이름 접두사")
    ap.add_argument("--replace", action="store_true", help="이 맵의 기존 firewall 폴리곤을 지우고 생성")
    ap.add_argument("--delete", action="store_true", help="이 맵의 firewall 폴리곤을 전부 삭제만 하고 끝")
    ap.add_argument("--list", action="store_true", help="POI 좌표와 기존 가상벽 목록만 출력")
    ap.add_argument("--dry-run", action="store_true", help="DB 에 쓰지 않고 좌표만 출력")
    a = ap.parse_args()

    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    print(f"[DB] {DB['user']}@{DB['host']}:{DB['port']}/{DB['database']}")

    meta = load_map(cur, a.map)
    pois = load_pois(cur, a.map)
    print(f"[맵 {a.map}] origin=({meta['ox']}, {meta['oy']})  res={meta['res']}  "
          f"이미지={meta['iw']}x{meta['ih']}px")

    if a.list:
        print(f"\n── POI {len(pois)}개 ──")
        for n, (x, y) in sorted(pois.items()):
            print(f"   {n:14} ({x:8.3f}, {y:8.3f})")
        cur.execute("SELECT id, name FROM map_polygons "
                    "WHERE map_id=%s AND shape_type='firewall' AND is_active=1", (a.map,))
        rows = cur.fetchall()
        print(f"\n── 기존 가상벽 {len(rows)}개 ──")
        for i, n in rows:
            print(f"   id={i}  {n}")
        conn.close()
        return

    if a.delete:
        cur.execute("DELETE FROM map_polygons WHERE map_id=%s AND shape_type='firewall'", (a.map,))
        conn.commit()
        print(f"\n삭제 완료: {cur.rowcount}개")
        print("★ 로봇에서도 지우려면  python scripts/wall_probe.py clear")
        conn.close()
        return

    if not a.path:
        sys.exit("--path 가 필요하다 (또는 --list / --delete)")

    path = parse_path(a.path, pois)
    print(f"\n[기준선] {len(path)}점")
    for x, y in path:
        print(f"   ({x:8.3f}, {y:8.3f})")

    # 제외 구역
    circles: list[tuple[Pt, float]] = []
    for spec in a.exclude:
        key, _, rad = spec.rpartition(":")
        r = float(rad)
        if "," in key:
            x, y = key.split(",")
            c = (float(x), float(y))
        elif key in pois:
            c = pois[key]
        else:
            sys.exit(f"--exclude 대상 없음: '{key}'")
        circles.append((c, r))
        print(f"[제외] ({c[0]:.3f}, {c[1]:.3f}) 반경 {r}m — 이 안에서는 벽을 끊는다")

    # 중심선 결정
    if a.mode == "wall":
        sign = 1.0 if a.side == "left" else -1.0
        center = offset_polyline(path, sign * (a.gap + a.width / 2))
        print(f"\n[중심선] 벽에서 {a.gap}m 띄우고 폭 {a.width}m → 벽 기준 {a.side}쪽으로 "
              f"{a.gap + a.width / 2:.3f}m 이동")
    else:
        center = path
        print(f"\n[중심선] path 를 그대로 사용, 폭 {a.width}m")

    walls = {
        "L": offset_polyline(center, +a.width / 2),
        "R": offset_polyline(center, -a.width / 2),
    }

    # 폭 검산 — 중심선의 각 꼭짓점에서 두 벽까지의 수직 거리
    # (코너 miter 로 늘어난 지점은 폭이 아니라 대각선이라 제외하고, 시작·끝점으로 본다)
    w_start = math.dist(walls["L"][0], walls["R"][0])
    w_end = math.dist(walls["L"][-1], walls["R"][-1])
    print(f"[검산] 시작점 폭 = {w_start:.4f} m,  끝점 폭 = {w_end:.4f} m  (목표 {a.width})")
    if max(abs(w_start - a.width), abs(w_end - a.width)) > 1e-6:
        print("       ⚠ 오차 발생 — 확인 필요")

    # 제외 구역으로 잘라내기 → 조각별 폴리곤
    made: list[tuple[str, list[Pt]]] = []
    for tag, wl in walls.items():
        for k, piece in enumerate(split_by_circles(wl, circles), 1):
            name = f"{a.name_prefix}_{tag}{k}"
            made.append((name, ribbon(piece, a.thickness)))

    print(f"\n[생성] 가상벽 {len(made)}개")
    for name, poly in made:
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        print(f"   {name:10} {len(poly):3}점  x[{min(xs):7.3f},{max(xs):7.3f}] "
              f"y[{min(ys):7.3f},{max(ys):7.3f}]")

    if a.dry_run:
        print("\n--dry-run 이므로 DB 에 쓰지 않았다.")
        conn.close()
        return

    if a.replace:
        cur.execute("DELETE FROM map_polygons WHERE map_id=%s AND shape_type='firewall'", (a.map,))
        print(f"\n[교체] 기존 firewall {cur.rowcount}개 삭제")

    for name, poly in made:
        pts_json = []
        for wx, wy in poly:
            sx, sy = world_to_svg(wx, wy, meta)
            pts_json.append({"x": sx, "y": sy, "worldX": wx, "worldY": wy})
        cur.execute(
            "INSERT INTO map_polygons (map_id, name, shape_type, points_json, is_active) "
            "VALUES (%s, %s, 'firewall', %s, 1)",
            (a.map, name, json.dumps(pts_json)),
        )
    conn.commit()
    print(f"\nDB 저장 완료 — map_polygons {len(made)}건")
    print("다음: 맵 편집기에서 확인 → [맵 동기화] 로 로봇 전송")
    conn.close()


if __name__ == "__main__":
    main()
