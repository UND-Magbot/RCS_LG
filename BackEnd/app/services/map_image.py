"""맵 이미지(occupancy grid PNG) 판독 — 벽 픽셀 찾기.

맵 편집기에서 "벽 스냅"으로 가상벽을 그릴 때, 클릭 지점에서 가장 가까운
실제 벽을 찾아주기 위한 것이다.

★ 외부 라이브러리를 쓰지 않는다.
  현장 PC 는 인터넷이 없어 pip install 이 안 된다. Pillow 는 requirements.txt 에도
  없어서 현장 venv 에 있을 거라 기대할 수 없다. 그래서 표준 zlib 만으로 PNG 를 푼다.

★ 주의 — 이 이미지는 '표시용' 이다.
  로봇의 경로계획은 carto_map(pbstream) 을 쓰므로, 여기에 뭘 그려도 로봇은 모른다.
  이 모듈은 "벽이 어디 있는지 읽기"에만 쓴다. 쓰기 용도가 아니다.
"""
from __future__ import annotations

import logging
import math
import os
import struct
import zlib

logger = logging.getLogger(__name__)

# occupancy grid 관례: 0=점유(벽, 검정), 255=자유(흰색), 중간=미지(회색)
WALL_THRESHOLD = 100

_cache: dict[str, tuple[float, int, int, bytes]] = {}   # path → (mtime, w, h, gray)


# ── PNG 디코드 (zlib 만 사용) ───────────────────────────────────────

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _decode_png_gray(path: str) -> tuple[int, int, bytes]:
    """PNG 를 8bit 그레이스케일 바이트열로. (width, height, pixels) 반환.

    지원: bit depth 8, color type 0(gray) / 2(RGB) / 3(palette) / 4(gray+A) / 6(RGBA),
          interlace 없음. 맵 이미지는 이 범위를 벗어나지 않는다.
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 형식이 아니다")

    pos = 8
    w = h = depth = ctype = interlace = 0
    palette = b""
    idat = bytearray()
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length                      # length + tag + body + crc
        if ctag == b"IHDR":
            w, h, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
        elif ctag == b"PLTE":
            palette = body
        elif ctag == b"IDAT":
            idat += body
        elif ctag == b"IEND":
            break

    if depth != 8:
        raise ValueError(f"지원하지 않는 bit depth: {depth}")
    if interlace:
        raise ValueError("인터레이스 PNG 는 지원하지 않는다")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        raise ValueError(f"지원하지 않는 color type: {ctype}")

    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ftype = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if ftype == 1:      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:    # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:    # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                c = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        elif ftype != 0:
            raise ValueError(f"알 수 없는 필터 타입: {ftype}")
        out[y * stride:(y + 1) * stride] = line
        prev = line

    # 그레이스케일 1채널로 정규화
    gray = bytearray(w * h)
    if ctype == 0:
        gray[:] = out
    elif ctype == 3:
        for i in range(w * h):
            j = out[i] * 3
            gray[i] = (palette[j] * 299 + palette[j + 1] * 587 + palette[j + 2] * 114) // 1000
    elif ctype == 4:
        for i in range(w * h):
            gray[i] = out[i * 2]
    else:  # 2(RGB), 6(RGBA)
        for i in range(w * h):
            j = i * channels
            gray[i] = (out[j] * 299 + out[j + 1] * 587 + out[j + 2] * 114) // 1000
    return w, h, bytes(gray)


def load_gray(path: str) -> tuple[int, int, bytes]:
    """디코드 결과를 파일 수정시각 기준으로 캐시한다 (클릭마다 다시 풀지 않도록)."""
    mtime = os.path.getmtime(path)
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1], hit[2], hit[3]
    w, h, gray = _decode_png_gray(path)
    _cache[path] = (mtime, w, h, gray)
    logger.info(f"[map_image] 디코드 {os.path.basename(path)} {w}x{h}")
    return w, h, gray


# ── 벽 찾기 ────────────────────────────────────────────────────────

# 맵 메타 캐시 — area_id -> (조회시각, 값)
_META_TTL_SEC = 30.0
_meta_cache: dict = {}


def map_meta_for_area(area_id):
    """그 area 활성 맵의 (png 경로, grid_origin_x, grid_origin_y, resolution).

    벽 대조(안전거리)·선분 통과 검사(경유지 경로)가 둘 다 쓴다.
    맵이 없거나 이미지 파일이 없으면 None — 호출자는 **대조 없이 기존 동작**으로 폴백한다.
    조회 실패로 기능이 멈추면 안 되므로 예외를 삼키고 None 을 준다.
    """
    import time
    from pathlib import Path

    now = time.time()
    hit = _meta_cache.get(area_id)
    if hit and now - hit[0] < _META_TTL_SEC:
        return hit[1]

    meta = None
    try:
        from app.database import SessionLocal
        from app.models.map import RobotMap
        db = SessionLocal()
        try:
            q = db.query(RobotMap).filter(RobotMap.is_active == True)   # noqa: E712
            if area_id is not None:
                q = q.filter(RobotMap.area_id == area_id)
            m = q.order_by(RobotMap.id.desc()).first()
            if m and m.image_url and m.grid_resolution:
                # image_url 은 "/static/maps/xxx.png" — BackEnd 기준 상대경로로 바꾼다
                backend_dir = Path(__file__).resolve().parent.parent.parent
                png = backend_dir / str(m.image_url).lstrip("/")
                if png.is_file():
                    meta = {
                        "png": str(png),
                        "ox": float(m.grid_origin_x or 0.0),
                        "oy": float(m.grid_origin_y or 0.0),
                        "res": float(m.grid_resolution),
                    }
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[map_image] 맵 메타 조회 실패(대조 생략): {e}")

    _meta_cache[area_id] = (now, meta)
    return meta


def is_wall_at(meta, world_x: float, world_y: float, tolerance_m: float) -> bool:
    """(world_x, world_y) 가 **맵에 이미 그려진 벽**인가.

    `tolerance_m` 안에 벽 픽셀이 있으면 True. 위치추정 오차를 흡수하는 값이기도 하다.
    맵이 없으면 항상 False — 대조를 못 하니 **아무것도 무시하지 않는다**(안전한 쪽).
    """
    if not meta:
        return False
    try:
        return snap_to_wall(meta["png"], meta["ox"], meta["oy"], meta["res"],
                            world_x, world_y, tolerance_m) is not None
    except Exception:
        return False


def wall_distance(meta, world_x: float, world_y: float,
                  max_m: float = 1.0):
    """(world_x, world_y) 에서 **가장 가까운 맵 벽까지 거리(m)**. 없으면 None.

    `is_wall_at()` 은 "tolerance 안에 벽이 있나"만 알려줘서, 정지가 났을 때
    *얼마나* 벗어났는지를 사후에 따로 재야 했다(2026-09-15 실측 0.456 m).
    그 숫자를 로그에 같이 남기려고 만든 진단용 함수다.

    판정에는 쓰지 않는다 — 벽 여부는 그대로 `is_wall_at()` 이 정한다.
    """
    if not meta:
        return None
    try:
        hit = snap_to_wall(meta["png"], meta["ox"], meta["oy"], meta["res"],
                           world_x, world_y, max_m)
        return None if hit is None else float(hit["distance"])
    except Exception:
        return None


def segment_blocked(meta, x1: float, y1: float, x2: float, y2: float,
                    clear_m: float, step_m: float = 0.10,
                    skip_ends_m: float = 0.80) -> bool:
    """(x1,y1)-(x2,y2) 직선이 벽에 막히나. 경유지끼리 이을 수 있는지 판단할 때 쓴다.

    선분을 `step_m` 간격으로 훑어 `clear_m` 안에 벽이 있으면 막힌 것으로 본다.
    맵이 없으면 False — 막히지 않은 것으로 보고 거리만으로 잇는다(기존 동작에 가깝다).

    ★ 양 끝 `skip_ends_m` 는 검사하지 않는다 (2026-09-08 현장 — 이것 때문에 막혔다).
      끝점은 **로봇이 지금 서 있는 자리**이거나 **사람이 지정한 목표 지점**이다.
      충전소에 도킹해 있거나 작업지점에 서 있으면 벽이 코앞이라, 끝점을 같이 검사하면
      **출발점에서 나가는 모든 연결이 막혀** 그래프가 통째로 끊긴다.
      실제로 그래서 경유지가 하나도 안 쓰이고 전부 standard 로 떨어졌다.
      로봇이 이미 거기 있다는 건 그 자리가 갈 수 있는 곳이라는 뜻이므로 검사할 이유가 없다.
      우리가 알고 싶은 건 **가는 길 중간에 벽이 있느냐** 뿐이다.
    """
    if not meta:
        return False
    d = math.hypot(x2 - x1, y2 - y1)
    if d <= 1e-6:
        return False
    lo, hi = skip_ends_m, d - skip_ends_m
    if hi <= lo:
        return False          # 너무 짧아 검사할 중간 구간이 없다 = 막히지 않은 것으로 본다
    n = max(1, int((hi - lo) / max(0.01, step_m)))
    for i in range(n + 1):
        s_ = lo + (hi - lo) * (i / n)
        t = s_ / d
        if is_wall_at(meta, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, clear_m):
            return True
    return False


def snap_to_wall(png_path: str, origin_x: float, origin_y: float, resolution: float,
                 world_x: float, world_y: float, radius_m: float = 0.5):
    """(world_x, world_y) 에서 radius_m 안에 있는 가장 가까운 벽 픽셀의 월드 좌표.

    없으면 None. 좌표계는 맵 편집기와 동일하다
    (ipx = (wx-origin_x)/res,  ipy = h - (wy-origin_y)/res).
    """
    w, h, gray = load_gray(png_path)
    cx = int(round((world_x - origin_x) / resolution))
    cy = int(round(h - (world_y - origin_y) / resolution))
    r = max(1, int(math.ceil(radius_m / resolution)))

    best = None
    best_d2 = r * r + 1
    for dy in range(-r, r + 1):
        py = cy + dy
        if not (0 <= py < h):
            continue
        row = py * w
        for dx in range(-r, r + 1):
            d2 = dx * dx + dy * dy
            if d2 >= best_d2:
                continue
            px = cx + dx
            if not (0 <= px < w):
                continue
            if gray[row + px] < WALL_THRESHOLD:
                best_d2 = d2
                best = (px, py)
    if best is None:
        return None

    px, py = best
    return {
        "x": px * resolution + origin_x,
        "y": (h - py) * resolution + origin_y,
        "distance": math.sqrt(best_d2) * resolution,
    }
