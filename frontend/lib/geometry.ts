/**
 * 폴리라인 기하 — 가상벽(선) 그리기에 쓴다.
 *
 * BackEnd/scripts/make_corridor.py 의 offset_polyline / ribbon 과 같은 수식이다.
 * 한쪽만 고치면 결과가 어긋나니 둘 다 같이 고칠 것.
 */

export type Pt = { x: number; y: number };

function unit(a: Pt, b: Pt): Pt {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const n = Math.hypot(dx, dy);
  if (n < 1e-9) return { x: 0, y: 0 };
  return { x: dx / n, y: dy / n };
}

/** 두 직선(무한 연장)의 교점. 평행이면 null. */
function lineIntersect(p1: Pt, p2: Pt, p3: Pt, p4: Pt): Pt | null {
  const den = (p1.x - p2.x) * (p3.y - p4.y) - (p1.y - p2.y) * (p3.x - p4.x);
  if (Math.abs(den) < 1e-9) return null;
  const a = p1.x * p2.y - p1.y * p2.x;
  const b = p3.x * p4.y - p3.y * p4.x;
  return {
    x: (a * (p3.x - p4.x) - (p1.x - p2.x) * b) / den,
    y: (a * (p3.y - p4.y) - (p1.y - p2.y) * b) / den,
  };
}

/**
 * 폴리라인을 d 만큼 평행이동. d>0 이면 진행방향 기준 왼쪽.
 * 코너는 miter join — 인접한 두 평행선의 교점으로 잇는다.
 * (그냥 구간별로 평행이동만 하면 바깥은 벌어지고 안쪽은 겹친다)
 *
 * ※ SVG 는 y 축이 아래로 향하므로 화면상 "왼쪽/오른쪽" 이 수학 좌표계와 반대로 보인다.
 *   방향 토글로 뒤집어 쓰면 되므로 여기서는 부호를 손대지 않는다.
 */
export function offsetPolyline(pts: Pt[], d: number): Pt[] {
  if (pts.length < 2) return [...pts];
  if (Math.abs(d) < 1e-9) return [...pts];

  const segs: [Pt, Pt][] = [];
  for (let i = 0; i < pts.length - 1; i++) {
    const u = unit(pts[i], pts[i + 1]);
    const nx = -u.y;
    const ny = u.x;
    segs.push([
      { x: pts[i].x + nx * d, y: pts[i].y + ny * d },
      { x: pts[i + 1].x + nx * d, y: pts[i + 1].y + ny * d },
    ]);
  }

  const out: Pt[] = [segs[0][0]];
  for (let i = 0; i < segs.length - 1; i++) {
    const ip = lineIntersect(segs[i][0], segs[i][1], segs[i + 1][0], segs[i + 1][1]);
    out.push(ip ?? segs[i][1]);
  }
  out.push(segs[segs.length - 1][1]);
  return out;
}

/**
 * 폴리라인을 얇은 닫힌 띠(폴리곤)로 만든다.
 * 백엔드가 폴리곤 둘레를 닫힌 LineString(가상벽) 으로 로봇에 보내므로,
 * 두께를 아주 얇게 주면 결과적으로 '선' 한 줄이 된다.
 */
export function ribbon(pts: Pt[], thickness: number): Pt[] {
  const h = thickness / 2;
  const left = offsetPolyline(pts, h);
  const right = offsetPolyline(pts, -h);
  return [...left, ...right.reverse()];
}

/**
 * 각도 스냅 — 새 점을 "직전 구간 방향 기준 stepDeg 배수" 로 맞춘다.
 * 손으로 찍으면 선이 미세하게 틀어져 통로가 삐뚤빼뚤해지는 걸 막는다.
 * 점이 1개뿐이면(=첫 구간) 기준 방향이 없으므로 화면 가로/세로 기준으로 스냅한다.
 */
export function snapAngle(pts: Pt[], p: Pt, stepDeg = 15): Pt {
  if (pts.length === 0) return p;
  const a = pts[pts.length - 1];
  const dx = p.x - a.x;
  const dy = p.y - a.y;
  const len = Math.hypot(dx, dy);
  if (len < 1e-6) return p;

  // 기준 각 — 직전 구간이 있으면 그 방향, 없으면 0(화면 가로)
  let base = 0;
  if (pts.length >= 2) {
    const b = pts[pts.length - 2];
    base = Math.atan2(a.y - b.y, a.x - b.x);
  }
  const step = (stepDeg * Math.PI) / 180;
  const rel = Math.atan2(dy, dx) - base;
  const snapped = base + Math.round(rel / step) * step;
  return { x: a.x + Math.cos(snapped) * len, y: a.y + Math.sin(snapped) * len };
}

/**
 * 통로 벽 2줄을 만든다.
 *  base="wall"   : 클릭한 선이 '실제 벽'. 거기서 offset 만큼 띄운 곳이 통로 한쪽, +width 가 반대쪽
 *  base="center" : 클릭한 선이 통로 한가운데. 양옆으로 width/2
 * side 는 진행방향 기준 어느 쪽으로 통로를 낼지.
 */
export function corridorWalls(
  pts: Pt[],
  offsetPx: number,
  widthPx: number,
  side: "left" | "right",
  base: "wall" | "center"
): Pt[][] {
  const s = side === "left" ? 1 : -1;
  if (base === "center") {
    return [offsetPolyline(pts, widthPx / 2), offsetPolyline(pts, -widthPx / 2)];
  }
  return [offsetPolyline(pts, s * offsetPx), offsetPolyline(pts, s * (offsetPx + widthPx))];
}

/** 폴리라인 총 길이 */
export function polylineLength(pts: Pt[]): number {
  let s = 0;
  for (let i = 0; i < pts.length - 1; i++) s += Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y);
  return s;
}

/**
 * 폴리라인 위에서 p 에 가장 가까운 위치.
 * s 는 폴리라인 시작점부터 잰 호 길이, dist 는 그 지점까지의 거리.
 */
export function closestOnPolyline(pts: Pt[], p: Pt): { s: number; dist: number } | null {
  if (pts.length < 2) return null;
  let best: { s: number; dist: number } | null = null;
  let acc = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len2 = dx * dx + dy * dy;
    const len = Math.sqrt(len2);
    let t = len2 < 1e-12 ? 0 : ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    const d = Math.hypot(p.x - (a.x + dx * t), p.y - (a.y + dy * t));
    if (!best || d < best.dist) best = { s: acc + len * t, dist: d };
    acc += len;
  }
  return best;
}

/** 호 길이 s0~s1 구간만 잘라낸 부분 폴리라인. 중간 꼭짓점은 보존된다. */
function sliceByArc(pts: Pt[], s0: number, s1: number): Pt[] {
  const out: Pt[] = [];
  let acc = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    const len = Math.hypot(b.x - a.x, b.y - a.y);
    if (len < 1e-12) continue;
    const lo = Math.max(s0, acc);
    const hi = Math.min(s1, acc + len);
    if (hi > lo) {
      const at = (u: number): Pt => ({
        x: a.x + ((b.x - a.x) * (u - acc)) / len,
        y: a.y + ((b.y - a.y) * (u - acc)) / len,
      });
      const pa = at(lo);
      const last = out[out.length - 1];
      if (!last || Math.hypot(last.x - pa.x, last.y - pa.y) > 1e-9) out.push(pa);
      out.push(at(hi));
    }
    acc += len;
  }
  return out;
}

/**
 * 작업지점 앞 벽을 끊어 출입구를 낸다.
 *
 * targets 중 벽에서 maxDist 안에 있는 것만 대상으로 잡고, 가장 가까운 지점을 중심으로
 * openingLen 만큼 잘라낸 뒤 남은 조각들을 돌려준다.
 * 통로 반대쪽 벽은 통로 폭만큼 떨어져 있어 maxDist 에 안 걸리므로 저절로 온전히 남는다
 * — 그래서 "가까운 쪽 벽만" 뚫린다.
 */
export function cutPolylineNearPoints(
  pts: Pt[],
  targets: Pt[],
  openingLen: number,
  maxDist: number
): Pt[][] {
  if (pts.length < 2 || targets.length === 0 || openingLen <= 0) return [pts];

  const total = polylineLength(pts);
  const cuts: [number, number][] = [];
  for (const t of targets) {
    const c = closestOnPolyline(pts, t);
    if (!c || c.dist > maxDist) continue;
    cuts.push([c.s - openingLen / 2, c.s + openingLen / 2]);
  }
  if (cuts.length === 0) return [pts];

  // 겹치는 개구부는 하나로 합친다 (작업지점이 붙어 있을 때 조각이 잘게 쪼개지지 않게)
  cuts.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const cur of cuts) {
    const last = merged[merged.length - 1];
    if (last && cur[0] <= last[1]) last[1] = Math.max(last[1], cur[1]);
    else merged.push([cur[0], cur[1]]);
  }

  const pieces: Pt[][] = [];
  let pos = 0;
  for (const [a, b] of merged) {
    if (a > pos) {
      const piece = sliceByArc(pts, pos, Math.min(a, total));
      if (piece.length >= 2) pieces.push(piece);
    }
    pos = Math.max(pos, b);
  }
  if (pos < total) {
    const piece = sliceByArc(pts, pos, total);
    if (piece.length >= 2) pieces.push(piece);
  }
  return pieces;
}

/** 작업지점이 벽에서 이보다 멀면 그 통로로 드나드는 게 아니라고 본다 (m) */
export const WORK_POINT_MAX_DIST_M = 3.0;

/**
 * 벽 여러 줄에 작업지점 출입구를 낸다.
 *
 * 작업지점마다 '가장 가까운 벽 한 줄'만 골라 뚫는다. 작업지점이 통로 한가운데 있어도
 * 양쪽이 다 뚫리지 않는다.
 */
export function openWallsAtTargets(
  walls: Pt[][],
  targets: Pt[],
  openingLen: number,
  maxDist: number
): Pt[][] {
  if (walls.length === 0 || targets.length === 0 || openingLen <= 0) return walls;

  const perWall: Pt[][] = walls.map(() => []);
  for (const t of targets) {
    let bestIdx = -1;
    let bestDist = Infinity;
    walls.forEach((wl, i) => {
      const c = closestOnPolyline(wl, t);
      if (c && c.dist < bestDist) {
        bestDist = c.dist;
        bestIdx = i;
      }
    });
    if (bestIdx >= 0 && bestDist <= maxDist) perWall[bestIdx].push(t);
  }
  return walls.flatMap((wl, i) =>
    perWall[i].length ? cutPolylineNearPoints(wl, perWall[i], openingLen, Infinity) : [wl]
  );
}
