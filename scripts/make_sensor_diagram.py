# -*- coding: utf-8 -*-
"""센서 감지범위·안전영역 도면 생성 (SVG) — LG 제출용.

왜 SVG 인가
  외부 라이브러리가 필요 없고(표준 라이브러리만 씀), 벡터라 PPT 에 넣어 확대해도
  깨지지 않는다. 브라우저에서 열어 PNG 로 저장할 수도 있다.

만드는 것 (전부 실측값 기반, 2026-09-15)
  1) lidar_range.svg   라이다 감지 범위 — 360°
  2) safety_zone.svg   안전영역 — 공차와 랙 적재를 **한 장에** 겹쳐 그린다

★ 도면에 기준을 셋 다 적는 이유
  `RED 1.0 m` 는 **footprint 앞끝 기준**이다. 라이다(중심) 기준으로는 1.379 m 이고,
  거기서 확인 구간만큼 더 가서 멈추므로 **결과적으로 중심에서 약 1.0 m** 에 선다.
  숫자가 같아 보여 혼동이 나기 쉬워 표로 못박아 둔다.

수치 출처
  · 라이다   PACECAT LDS-50C-E 카탈로그 + 점군 실측(360° 전방위 수신 확인)
  · 안전영역 반복 주행 실측 — 공차 10회 / LG2 랙 적재 11회
  · 로봇·랙 치수 /robot_model 실측
"""
from __future__ import annotations

import os

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "_logs", "diagrams")

# ── 실측값 ────────────────────────────────────────────────
ROBOT_FRONT = 0.379      # footprint 앞끝 (중심 기준)
ROBOT_BACK = 0.369
ROBOT_HALF = 0.230       # footprint 반폭

# 랙 적재 = **LG 대차** (현장에 실제로 들어가는 랙. rack.specs 0.70 x 0.50 + margin 0.09)
#   LGIT 제출 도면은 LG 랙 기준으로 낸다 (2026-09-15 확정).
#   ★ 앞끝이 안 커진다. 랙 풋프린트 길이 0.680 이 로봇 전장 0.748 보다 **짧기** 때문.
#     그래서 앞으로 튀어나오는 건 여전히 로봇 코(0.379)다.
#     (S300 랙 0.95x0.95 는 앞끝이 0.475 로 커진다 — 랙이 로봇보다 길 때만 바뀐다)
#   ※ 랙별 감지 폭:  LG 0.880 / LG2 0.800 / S300 0.950 / S600 1.030
RACK_HALF = 0.440        # 랙 적재 시 반폭  → 감지 밴드도 이 값이 된다 (0.70/2 + 0.09)
RACK_FRONT = 0.340       # 랙 자체 앞끝. 로봇 앞끝 0.379 보다 안쪽이라 기준은 안 바뀐다
RACK_BACK = 0.340

YELLOW_M = 3.0
RED_M = 1.0

# 실제 정지 위치 (앞끝 기준) — min, max, 평균
#   ★ 적재 11회는 **LG2 랙**을 싣고 쟀다. LG 랙에서도 앞끝이 0.379 로 같으므로
#     (두 랙 다 로봇보다 짧다) 전방 수치는 그대로 유효하다. 랙이 바뀌어 달라지는 건
#     좌우 감지 폭뿐이다. 근거는 docs/12 §6-2 참조.
STOP_E = (0.594, 0.755, 0.676)   # 랙 미적재 10회
STOP_L = (0.571, 0.783, 0.695)   # 랙 적재 11회 (LG2 로 측정)

SLOW_STEPS = [(3.0, 0.40), (2.0, 0.25), (1.5, 0.10)]

LIDAR_CAT_90 = 40.0

FONT = "Malgun Gothic, Noto Sans KR, sans-serif"

C_YELLOW, C_YELLOW_L = "#d4a017", "#fdf2cc"
C_RED, C_RED_L = "#c0392b", "#f8d7d3"
C_BLUE, C_BLUE_L = "#1f6f8b", "#d6e9f0"
C_GRAY, C_GRAY_L = "#8a8a8a", "#ececec"
C_PURPLE, C_PURPLE_L = "#7d5ba6", "#ece5f5"


def _svg(w, h, title, sub=""):
    s = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'viewBox="0 0 {w} {h}" font-family="{FONT}">\n'
         f'<rect width="{w}" height="{h}" fill="#fff"/>\n'
         f'<text x="30" y="44" font-size="26" font-weight="700" fill="#111">'
         f'{title}</text>\n')
    if sub:
        s += (f'<text x="30" y="70" font-size="14" fill="#777">{sub}</text>\n')
    return s


def _dimh(x1, x2, y, label, color, size=15, bold=True):
    """수평 치수선 + 라벨(선 위)."""
    w = "700" if bold else "400"
    return (f'<line x1="{x1:.1f}" y1="{y}" x2="{x2:.1f}" y2="{y}" '
            f'stroke="{color}" stroke-width="1.6"/>\n'
            f'<line x1="{x1:.1f}" y1="{y-7}" x2="{x1:.1f}" y2="{y+7}" '
            f'stroke="{color}" stroke-width="1.6"/>\n'
            f'<line x1="{x2:.1f}" y1="{y-7}" x2="{x2:.1f}" y2="{y+7}" '
            f'stroke="{color}" stroke-width="1.6"/>\n'
            f'<text x="{(x1+x2)/2:.1f}" y="{y-11}" font-size="{size}" '
            f'font-weight="{w}" fill="{color}" text-anchor="middle">{label}</text>\n')


def _dim_stop(x1, x2, xa, y, head, detail, color):
    """실제 정지 범위 치수선 — 평균 위치에 ▲ 를 찍어 '어디에 몰려 서는지'를 보인다.

    ★ 범위만 그으면 "0.59~0.76 어디든 설 수 있다" 로 읽힌다. 실제로는 평균 근처에
      몰려 있고 양끝은 드문 값이라, 평균 표시가 있어야 오해가 안 난다.
    """
    return (_dimh(x1, x2, y, head, color, 15)
            + f'<polygon points="{xa-6:.1f},{y-10} {xa+6:.1f},{y-10} '
              f'{xa:.1f},{y}" fill="{color}"/>\n'
            + f'<text x="{x2+12:.1f}" y="{y+5}" font-size="12.5" fill="{color}">'
              f'{detail}</text>\n')


# ══════════════════════════════════════════════════════════
def lidar_svg() -> str:
    """라이다 감지 범위 — 원 + 지시선 라벨.

    ★ 과장 배율을 쓰지 않는다 (2026-09-15 수정).
      예전엔 안전존과 로봇을 ×6 으로 키워 그리고 "×6 과장" 이라고 적었는데,
      도면에 과장이 섞이면 치수를 그대로 못 읽는다.
      **전부 실제 축척**으로 두고, 40 m 와 3.0 m 의 대비는 아래 치수선이 담당한다.
      안전영역의 형상·치수 상세는 safety_zone.svg 가 따로 보여준다.
    """
    W, H = 900, 760
    cx, cy = 430.0, 395.0
    R = 235.0
    s = R / LIDAR_CAT_90
    import math as _m

    o = _svg(W, H, "라이다 감지 범위", "PACECAT LDS-50C-E · 360° 전방위")

    # 센서 커버리지는 원이지만 **안전존은 전방 직사각형 밴드**다.
    # 안전존을 원으로 그리면 "360° 전방향을 감시한다"는 거짓이 된다.
    o += (f'<circle cx="{cx}" cy="{cy}" r="{LIDAR_CAT_90*s:.1f}" fill="#fdf6d8" '
          f'stroke="#d4a017" stroke-width="2"/>\n')

    # 안전존 밴드 — 전방 3.0 m(앞끝 기준) × 좌우 ±0.23 m. **실제 축척**이라 얇다.
    bw = ROBOT_HALF * 2 * s
    bl = (ROBOT_FRONT + YELLOW_M) * s
    o += (f'<rect x="{cx-bw/2:.2f}" y="{cy-bl:.1f}" width="{max(bw, 1.4):.2f}" '
          f'height="{bl:.1f}" fill="{C_RED}" stroke="{C_RED}" stroke-width="0.8"/>\n')

    # 지시선 라벨 — (반경 m, 각도°, 라벨, 색, 지시선 길이)
    def leader(r_m, deg, label, color, ext=64):
        a = _m.radians(deg)
        x1, y1 = cx + r_m * s * _m.cos(a), cy - r_m * s * _m.sin(a)
        x2, y2 = cx + (r_m * s + ext) * _m.cos(a), cy - (r_m * s + ext) * _m.sin(a)
        anc = "start" if _m.cos(a) >= 0 else "end"
        dx = 6 if _m.cos(a) >= 0 else -6
        return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                f'stroke="{color}" stroke-width="1.3"/>\n'
                f'<circle cx="{x1:.1f}" cy="{y1:.1f}" r="3" fill="{color}"/>\n'
                f'<text x="{x2+dx:.1f}" y="{y2+5:.1f}" font-size="17" '
                f'font-weight="700" fill="{color}" text-anchor="{anc}">{label}</text>\n')

    o += leader(LIDAR_CAT_90, 52, "40 m", "#b8890e")
    o += (f'<text x="{cx+(LIDAR_CAT_90*s+64)*0.6157+6:.1f}" '
          f'y="{cy-(LIDAR_CAT_90*s+64)*0.788+24:.1f}" font-size="13" '
          f'fill="#b8890e">센서 최대 측정거리</text>\n')

    # 안전존 지시선 — 실제 축척이라 밴드가 얇다. 지시선이 위치를 잡아준다.
    lx2, ly2 = cx - 205, cy - bl * 0.62
    o += (f'<line x1="{cx-bw/2:.1f}" y1="{cy-bl*0.62:.1f}" x2="{lx2}" y2="{ly2}" '
          f'stroke="{C_RED}" stroke-width="1.3"/>\n'
          f'<circle cx="{cx-bw/2:.1f}" cy="{cy-bl*0.62:.1f}" r="3" fill="{C_RED}"/>\n'
          f'<text x="{lx2-6}" y="{ly2}" font-size="16" font-weight="700" '
          f'fill="{C_RED}" text-anchor="end">안전존</text>\n'
          f'<text x="{lx2-6}" y="{ly2+20}" font-size="13" fill="{C_RED}" '
          f'text-anchor="end">전방 3.0 m × 좌우 ±0.23 m</text>\n'
          f'<text x="{lx2-6}" y="{ly2+38}" font-size="12" fill="#999" '
          f'text-anchor="end">실제 축척 — 40 m 와 견주면 이만큼이다</text>\n')

    # 중심 = 로봇 = 라이다. 40 m 축척에서 로봇(0.75 m)은 점 크기다.
    o += (f'<circle cx="{cx}" cy="{cy}" r="3.5" fill="{C_BLUE}"/>\n'
          f'<line x1="{cx+5:.1f}" y1="{cy+4:.1f}" x2="{cx+92:.1f}" y2="{cy+64:.1f}" '
          f'stroke="{C_BLUE}" stroke-width="1.2"/>\n'
          f'<text x="{cx+98:.1f}" y="{cy+68:.1f}" font-size="13" '
          f'font-weight="600" fill="{C_BLUE}">로봇 = 라이다 (0.75 × 0.46 m)</text>\n')

    o += (f'<text x="{cx}" y="{cy+LIDAR_CAT_90*s-20:.1f}" font-size="21" '
          f'font-weight="700" fill="#b8890e" text-anchor="middle">360°</text>\n')

    # 전방 화살표 — 12시 방향, 라벨은 원 밖
    o += (f'<line x1="{cx}" y1="{cy-14}" x2="{cx}" y2="{cy-R-30}" stroke="#555" '
          f'stroke-width="1.4" stroke-dasharray="6 5"/>\n'
          f'<polygon points="{cx-6},{cy-R-30} {cx+6},{cy-R-30} {cx},{cy-R-43}" '
          f'fill="#555"/>\n'
          f'<text x="{cx}" y="{cy-R-52}" font-size="14" fill="#555" '
          f'text-anchor="middle">전방</text>\n')

    # 치수선 (아래) — 센서가 보는 거리와 실제 쓰는 거리의 대비
    y0 = cy + LIDAR_CAT_90 * s + 56
    o += _dimh(cx, cx + LIDAR_CAT_90 * s, y0, "센서 최대 40 m", "#b8890e", 16)
    o += _dimh(cx, cx + (ROBOT_FRONT + YELLOW_M) * s, y0 + 46,
               "안전존 전방 3.0 m", "#c0392b", 16)

    return o + "</svg>\n"


# ══════════════════════════════════════════════════════════
def safety_svg() -> str:
    """안전영역 — 공차와 랙 적재를 **한 장에** 겹쳐 그린다.

    ★ 예전엔 공차/적재를 따로 그렸다. 적재가 미검증이라 같이 두면 검증된 값처럼
      보인다는 이유였는데, 2026-09-15 에 LG2 적재 11회를 실측해 그 이유가 사라졌다.
      오히려 **뭐가 같고 뭐가 다른지**는 겹쳐 그려야 한 눈에 보인다.

    ★ 겹쳐 그릴 수 있는 이유
      LG2 를 실어도 **앞끝이 0.379 로 같다**(랙이 로봇보다 짧다). 기준선이 하나라
      서행/정지 치수선을 공유할 수 있고, 다른 건 밴드 폭과 실제 정지 범위뿐이다.

    ★ 거리는 전부 **footprint 앞끝 기준**이다.
    """
    W, H = 1280, 670
    s = 250.0
    cx, cy = 150.0, 330.0

    front = ROBOT_FRONT                       # 미적재·적재 모두 같다
    near_e = max(0.45, front + 0.25) - front  # 앞끝 기준 근접 무시 = 0.25

    o = _svg(W, H, "안전영역 — 랙 미적재 · 랙 적재")

    def X(m):
        return cx + (front + m) * s

    band_e = ROBOT_HALF * s      # 공차 밴드 반폭
    band_l = RACK_HALF * s       # 적재 밴드 반폭 (랙 반폭과 같다)

    # ── 적재 밴드(바깥) — 연하게 + 보라 파선 ──
    o += (f'<rect x="{X(RED_M):.1f}" y="{cy-band_l:.1f}" '
          f'width="{(YELLOW_M-RED_M)*s:.1f}" height="{band_l*2:.1f}" '
          f'fill="{C_YELLOW_L}" fill-opacity="0.55" stroke="{C_PURPLE}" '
          f'stroke-width="1.8" stroke-dasharray="7 4"/>\n')
    o += (f'<rect x="{X(near_e):.1f}" y="{cy-band_l:.1f}" '
          f'width="{(RED_M-near_e)*s:.1f}" height="{band_l*2:.1f}" '
          f'fill="{C_RED_L}" fill-opacity="0.55" stroke="{C_PURPLE}" '
          f'stroke-width="1.8" stroke-dasharray="7 4"/>\n')

    # ── 공차 밴드(안쪽) — 진하게 + 실선 ──
    o += (f'<rect x="{X(RED_M):.1f}" y="{cy-band_e:.1f}" '
          f'width="{(YELLOW_M-RED_M)*s:.1f}" height="{band_e*2:.1f}" '
          f'fill="{C_YELLOW_L}" stroke="{C_YELLOW}" stroke-width="2"/>\n')
    o += (f'<rect x="{X(near_e):.1f}" y="{cy-band_e:.1f}" '
          f'width="{(RED_M-near_e)*s:.1f}" height="{band_e*2:.1f}" '
          f'fill="{C_RED_L}" stroke="{C_RED}" stroke-width="2"/>\n')
    # 근접 무시
    o += (f'<rect x="{X(0):.1f}" y="{cy-band_e:.1f}" '
          f'width="{near_e*s:.1f}" height="{band_e*2:.1f}" '
          f'fill="{C_GRAY_L}" stroke="{C_GRAY}" stroke-width="1"/>\n')

    # 영역 안 큰 라벨
    o += (f'<text x="{(X(RED_M)+X(YELLOW_M))/2:.1f}" y="{cy+6}" font-size="19" '
          f'font-weight="700" fill="#9a7411" text-anchor="middle">서행</text>\n')
    o += (f'<text x="{(X(near_e)+X(RED_M))/2:.1f}" y="{cy+6}" font-size="17" '
          f'font-weight="700" fill="{C_RED}" text-anchor="middle">정지</text>\n')

    # 서행 계단
    for m, v in SLOW_STEPS:
        o += (f'<line x1="{X(m):.1f}" y1="{cy-band_l:.1f}" x2="{X(m):.1f}" '
              f'y2="{cy+band_l:.1f}" stroke="{C_YELLOW}" stroke-width="1" '
              f'stroke-dasharray="4 4"/>\n'
              f'<text x="{X(m):.1f}" y="{cy-band_l-10:.1f}" font-size="13" '
              f'font-weight="600" fill="#9a7411" text-anchor="middle">{v}</text>\n')
    o += (f'<text x="{X(2.25):.1f}" y="{cy-band_l-32:.1f}" font-size="12.5" '
          f'fill="#9a7411" text-anchor="middle">서행 속도 (m/s)</text>\n')

    # ── 랙(LG2) — 로봇을 덮지만 **앞으로는 안 나온다** ──
    o += (f'<rect x="{cx-RACK_BACK*s:.1f}" y="{cy-RACK_HALF*s:.1f}" '
          f'width="{(RACK_FRONT+RACK_BACK)*s:.1f}" height="{RACK_HALF*2*s:.1f}" '
          f'fill="{C_PURPLE_L}" stroke="{C_PURPLE}" stroke-width="2"/>\n')
    # ★ 치수 라벨을 랙 **왼쪽 바깥**에 두면 도면 밖으로 나간다(2026-09-15 수정).
    #   랙 위쪽 띠(랙 상단 ~ 로봇 상단 사이, 약 42 px)가 비어 있으니 그 안에 넣는다.
    o += (f'<text x="{cx-RACK_BACK*s-8:.0f}" y="{cy-RACK_HALF*s+18:.0f}" '
          f'font-size="13" font-weight="700" fill="{C_PURPLE}" '
          f'text-anchor="end">랙</text>\n'
          f'<text x="{cx-RACK_BACK*s+8:.0f}" y="{cy-RACK_HALF*s+28:.0f}" '
          f'font-size="11.5" fill="{C_PURPLE}">'
          f'{RACK_HALF*2:.2f} × {RACK_FRONT+RACK_BACK:.3f} m</text>\n')

    # ── 로봇 — 실제 비율 (전후 0.748 × 좌우 0.460) ──
    o += (f'<rect x="{cx-ROBOT_BACK*s:.1f}" y="{cy-ROBOT_HALF*s:.1f}" '
          f'width="{(ROBOT_FRONT+ROBOT_BACK)*s:.1f}" '
          f'height="{ROBOT_HALF*2*s:.1f}" '
          f'fill="{C_BLUE}" stroke="#0d3d4f" stroke-width="2"/>\n')
    o += (f'<text x="{cx:.0f}" y="{cy+band_l+22:.0f}" '
          f'font-size="14" font-weight="600" fill="{C_BLUE}" '
          f'text-anchor="middle">로봇</text>\n')

    # 앞끝 기준선 — 공차·적재 공통이라는 게 이 도면의 핵심이다
    o += (f'<line x1="{X(0):.1f}" y1="{cy-band_l-18:.1f}" x2="{X(0):.1f}" '
          f'y2="{cy+band_l+150:.1f}" stroke="#0d3d4f" stroke-width="1.6" '
          f'stroke-dasharray="5 4"/>\n'
          f'<text x="{X(0):.1f}" y="{cy-band_l-46:.1f}" font-size="12.5" '
          f'font-weight="700" fill="#0d3d4f" text-anchor="middle">'
          f'기준 = 로봇 앞끝 ({front:.3f} m) — 적재해도 같다</text>\n')
    o += (f'<line x1="{cx}" y1="{cy}" x2="{X(YELLOW_M)+30:.1f}" y2="{cy}" '
          f'stroke="#555" stroke-width="0.9" stroke-dasharray="6 5"/>\n')

    # ── 치수선 — 전부 앞끝 X(0) 에서 시작 ──
    y0 = cy + band_l + 52
    o += _dimh(X(0), X(YELLOW_M), y0, "서행 3.0 m", C_YELLOW, 17)
    o += _dimh(X(0), X(RED_M), y0 + 48, "정지 1.0 m", C_RED, 17)
    o += _dim_stop(X(STOP_E[0]), X(STOP_E[1]), X(STOP_E[2]), y0 + 100,
                   f"실제 정지 · 랙 미적재  {STOP_E[0]:.2f} ~ {STOP_E[1]:.2f} m",
                   f"평균 {STOP_E[2]:.2f} m (10회)", C_BLUE)
    o += _dim_stop(X(STOP_L[0]), X(STOP_L[1]), X(STOP_L[2]), y0 + 146,
                   f"실제 정지 · 랙 적재  {STOP_L[0]:.2f} ~ {STOP_L[1]:.2f} m",
                   f"평균 {STOP_L[2]:.2f} m (11회)", C_PURPLE)

    # ── 밴드 폭 — 두 조건을 나란히 ──
    # ★ 치수선은 cy-band 에서 cy+band 까지, 즉 **전체 폭**을 잰다.
    #   그런데 라벨에 반폭(±0.230 / ±0.400)을 적어 두어 선과 숫자가 안 맞았다
    #   (2026-09-15 지적받아 수정). 선이 재는 그대로 전체 폭을 적는다.
    xb = X(YELLOW_M) + 26
    o += (f'<line x1="{xb:.1f}" y1="{cy-band_l:.1f}" x2="{xb:.1f}" '
          f'y2="{cy+band_l:.1f}" stroke="{C_PURPLE}" stroke-width="1.8"/>\n'
          f'<line x1="{xb-5:.1f}" y1="{cy-band_l:.1f}" x2="{xb+5:.1f}" '
          f'y2="{cy-band_l:.1f}" stroke="{C_PURPLE}" stroke-width="1.8"/>\n'
          f'<line x1="{xb-5:.1f}" y1="{cy+band_l:.1f}" x2="{xb+5:.1f}" '
          f'y2="{cy+band_l:.1f}" stroke="{C_PURPLE}" stroke-width="1.8"/>\n'
          f'<text x="{xb+10:.1f}" y="{cy-band_l+28:.1f}" font-size="15" '
          f'font-weight="700" fill="{C_PURPLE}">{RACK_HALF*2:.3f} m</text>\n'
          f'<text x="{xb+10:.1f}" y="{cy-band_l+46:.1f}" font-size="12.5" '
          f'fill="{C_PURPLE}">랙 적재</text>\n')
    xb2 = xb + 118
    o += (f'<line x1="{xb2:.1f}" y1="{cy-band_e:.1f}" x2="{xb2:.1f}" '
          f'y2="{cy+band_e:.1f}" stroke="{C_YELLOW}" stroke-width="1.8"/>\n'
          f'<line x1="{xb2-5:.1f}" y1="{cy-band_e:.1f}" x2="{xb2+5:.1f}" '
          f'y2="{cy-band_e:.1f}" stroke="{C_YELLOW}" stroke-width="1.8"/>\n'
          f'<line x1="{xb2-5:.1f}" y1="{cy+band_e:.1f}" x2="{xb2+5:.1f}" '
          f'y2="{cy+band_e:.1f}" stroke="{C_YELLOW}" stroke-width="1.8"/>\n'
          f'<text x="{xb2+10:.1f}" y="{cy-4:.1f}" font-size="15" '
          f'font-weight="700" fill="#9a7411">{ROBOT_HALF*2:.3f} m</text>\n'
          f'<text x="{xb2+10:.1f}" y="{cy+14:.1f}" font-size="12.5" '
          f'fill="#9a7411">랙 미적재</text>\n')

    # ── 범례 ──
    # ★ ly 를 내리면 아래 '기준 = 로봇 앞끝' 라벨(y = cy-band_l-46)과 겹친다.
    lx, ly = 30, 86
    o += (f'<rect x="{lx}" y="{ly}" width="300" height="66" fill="#fafafa" '
          f'stroke="#ddd" rx="5"/>\n')
    o += (f'<rect x="{lx+14}" y="{ly+14}" width="26" height="13" '
          f'fill="{C_YELLOW_L}" stroke="{C_YELLOW}" stroke-width="2"/>\n'
          f'<text x="{lx+50}" y="{ly+25}" font-size="13" fill="#333">'
          f'랙 미적재 — 실선</text>\n')
    o += (f'<rect x="{lx+14}" y="{ly+40}" width="26" height="13" '
          f'fill="{C_YELLOW_L}" fill-opacity="0.55" stroke="{C_PURPLE}" '
          f'stroke-width="2" stroke-dasharray="5 3"/>\n'
          f'<text x="{lx+50}" y="{ly+51}" font-size="13" fill="#333">'
          f'랙 적재 — 파선</text>\n')

    # ── 거리 기준 환산표 ──
    # ★ by/height 를 키우면 바로 아래 '서행 속도 (m/s)' 라벨(y = cy-band_l-32)과
    #   겹친다. 랙이 커지면 band_l 이 커져 그 라벨이 위로 올라오니 같이 확인할 것
    #   (LG 랙 기준 band_l=110 → 라벨 y=188, 표 아래끝은 172 이하).
    bx, by = 700, 76
    o += (f'<rect x="{bx}" y="{by}" width="420" height="96" fill="#fafafa" '
          f'stroke="#ddd" rx="5"/>\n')
    o += (f'<text x="{bx+16}" y="{by+23}" font-size="14" font-weight="700" '
          f'fill="#111">거리 기준</text>\n')
    for t, xx in (("정지 판정", bx + 200), ("실제 정지 미적재/적재", bx + 330)):
        o += (f'<text x="{xx}" y="{by+44}" font-size="12" fill="#999" '
              f'text-anchor="middle">{t}</text>\n')
    rowsv = [("라이다 = 로봇 중심", f"{RED_M+front:.2f}",
              f"{STOP_E[2]+front:.2f} / {STOP_L[2]+front:.2f}"),
             ("footprint 앞끝", f"{RED_M:.2f}",
              f"{STOP_E[2]:.2f} / {STOP_L[2]:.2f}")]
    for i, (c1, c2, c3) in enumerate(rowsv):
        yy = by + 64 + i * 22
        bold = "700" if i == 1 else "400"
        cc = "#111" if i == 1 else "#555"
        o += (f'<text x="{bx+16}" y="{yy}" font-size="13.5" font-weight="{bold}" '
              f'fill="{cc}">{c1}</text>\n'
              f'<text x="{bx+200}" y="{yy}" font-size="13.5" font-weight="{bold}" '
              f'fill="{cc}" text-anchor="middle">{c2} m</text>\n'
              f'<text x="{bx+330}" y="{yy}" font-size="13.5" font-weight="{bold}" '
              f'fill="{cc}" text-anchor="middle">{c3} m</text>\n')

    return o + "</svg>\n"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # 예전 분리본은 지운다 — 한 장으로 합쳤다
    for old in ("safety_zone_empty.svg", "safety_zone_laden.svg"):
        p = os.path.join(OUT_DIR, old)
        if os.path.exists(p):
            os.remove(p)
            print(f"삭제  {p}")
    for name, fn in (("lidar_range.svg", lidar_svg),
                     ("safety_zone.svg", safety_svg)):
        p = os.path.join(OUT_DIR, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(fn())
        print(f"생성  {p}")
    idx = os.path.join(OUT_DIR, "index.html")
    with open(idx, "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8">'
                '<title>센서 감지범위 도면</title>'
                '<style>body{margin:0;padding:28px;background:#eef0f3;'
                'font-family:Malgun Gothic,sans-serif}'
                'img{display:block;margin:0 0 30px;background:#fff;'
                'box-shadow:0 3px 14px rgba(0,0,0,.14);max-width:100%}</style>'
                '<img src="safety_zone.svg">'
                '<img src="lidar_range.svg">')
    print(f"생성  {idx}")


if __name__ == "__main__":
    main()
