"""전방 장애물 안전거리 — Yellow(서행) / Red(정지). LGIT 요청 4번.

로봇에는 '로봇을 따라다니는 2단계 링'이 없다(2026-09-04 확인). 맵에 고정된
`regionType` 구역만 있어서, **전방 감지는 서버가 직접 해야 한다.**

★ 인증된 안전 장치가 아니다. 안전 라이다(IEC 61496-3 + PL d)가 아니라
  소프트웨어 보조 정지다. 로봇은 사람과 사물을 구분하지 못하므로
  (`supportsVisionBasedDetector: false`) '작업자 감지'가 아니라 '장애물 감지'다.
  로봇 자체 회피·정지는 이것과 별개로 항상 동작한다. 이 기능은 그보다
  **더 일찍, 단계적으로** 줄이는 보조 수단이다.

판정 (2026-09-04 실측 근거)
  · 라이다 `/scan_matched_points2` 1.86 Hz(0.53초) — 더 빠른 대안 없음
  · `control.max_forward_decel` -2.0 m/s²
  · 0.7 m/s 기준 정지거리 (0.53+0.15)*0.7 + 0.49/(2*2.0) = 0.61 m
  · 판정 영역은 **부채꼴이 아니라 직사각형 밴드**다. 부채꼴로 하면 멀어질수록
    넓어져 통로 벽이 계속 잡히고 로봇이 내내 감속한다.

거리 기준
  설정값(yellow_m/red_m)은 **자기 앞끝에서의 거리**다. 로봇 중심에서 재면
  랙을 들었을 때 랙이 앞으로 더 나온 만큼 실제 여유가 줄어드는데 설정은
  그대로라 위험하다. 앞끝 기준이면 랙 유무와 무관하게 "내 앞에서 몇 m"가 된다.

동작 — 서행도 정지도 **속도로만** 건다
  Yellow 진입 → `max_forward_velocity` 를 `slow_speed` 로
  Red   진입 → `max_forward_velocity` 를 **0** 으로 (2026-09-07 실기에서 0 수용 확인)
  해제      → 원래 속도 복구. **잭 상태별 2단 속도**다 (2026-09-14, LGIT 요청 1번)
              — 랙을 들었으면 적재 속도, 빈 몸이면 공차 속도. `_base_speed()` 참조
  전이할 때만 명령을 보낸다. 매 틱 쏘지 않는다.

작업지점 주변에서는 판정하지 않는다 (2026-09-07 추가)
  랙 밑 이탈·후진 재시도·측면 우회는 전부 작업지점에서 **일부러** 하는 동작인데
  이동 type 이 그냥 `standard` 라 종류로 구분할 수 없다. 그래서 **위치**로 뺀다.
  R/J·충전소·대기장소 반경 안에서는 존이 `SKIP` 이 되고 아무 제약도 걸지 않는다.
  자세한 반경 산출은 아래 `RACK_FRONT_M` 주석 참조.

★ 이동 명령을 취소하지 않는 이유 (2026-09-07 실기 사고)
  처음에는 RED 에서 `jack_service.pause_robot_job()` 을 썼는데, 이건 진행 중인
  이동을 **취소**한다. 그러면 `safe_move` 가 재시도하면서 처음 만든
  `route_coordinates` 를 그대로 다시 쓰는데, 그 사이 로봇은 앞으로 가 있다.
  결국 로봇이 경로 시작점으로 되돌아가려 해서 **우회**가 생겼다
  (현대 프로젝트 fail_reason 표의 `401 트랙 시작점에서 너무 멀리 있음` 과 같은 상황).
  속도만 0 으로 만들면 이동 명령과 경로가 그대로 살아 있어 장애물이 비키는 즉시
  그 자리에서 이어서 간다.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Optional

import requests
from websocket import WebSocketException, create_connection

from app.services import map_image   # 맵 벽 대조 (DB 접근은 함수 안에서만 한다)

logger = logging.getLogger(__name__)

ROSTER_REFRESH_SEC = 30.0
RECONNECT_WAIT_SEC = 5.0
WS_TIMEOUT_SEC = 5.0

# 상태. SKIP 은 "작업지점 주변이라 판정을 하지 않는 중" — CLEAR(앞이 비었다)와 다르다.
# 둘을 같은 값으로 두면 콘솔에 "정상"으로 보여 감시 중인 줄 알게 된다.
CLEAR, YELLOW, RED, SKIP = "clear", "yellow", "red", "skip"

# 자기 몸(랙 포함) 앞끝에서 이만큼 더 띄운 곳부터 장애물로 본다.
# 0.10 → 0.25 (2026-09-07). 이유 두 가지.
#  ① `/robot_model` 이 **0.08 Hz(12초에 1건)** 라 랙을 든 직후 한동안 옛 풋프린트
#     (앞끝 0.379)를 쓰게 된다. 그동안 자기 랙(앞끝 0.475)이 장애물로 잡혔다.
#     0.379 + 0.25 = 0.629 > 0.475 라 그 창에서도 자기 랙이 안 잡힌다.
#  ② 안전상 손해가 없다 — 0.7 m/s 정지거리가 0.61 m 라 앞끝 0.25 m 안쪽은
#     감지해도 못 멈춘다. 그 구간은 로봇 자체 회피가 담당한다.
SELF_CLEARANCE_M = 0.25

# 포즈가 이보다 낡으면 판정을 건너뛴다.
# `/tracked_pose` 는 **1.02 Hz** 밖에 안 온다(2026-09-07 실측). 0.7 m/s 로 달리면
# 1초에 70 cm 를 가므로, 낡은 포즈로 맵 좌표 스캔을 변환하면 이미 지나친 벽이
# 여전히 '앞'에 있는 것으로 계산된다 — '아무것도 없는데 정지'의 주범.
#
# ★ 처음엔 0.5 로 뒀는데 스캔의 절반이 버려졌다(실측 로그 '포즈가 낡아 판정 보류'
#   0.65~0.97초). 판정 주기가 반토막나면 실효 지연이 1.15초가 되고 정지거리가
#   1.03 m 로 늘어 Red 1.0 m 로는 못 멈춘다.
#   → 버리는 대신 **속도로 외삽**해서 쓰고, 한계만 1 Hz 주기를 덮게 넉넉히 둔다.
POSE_MAX_AGE_SEC = 1.2

# ══════════════════════════════════════════════════════════════════
#  ★ 밀린 스캔은 버린다 (2026-09-15 실측으로 추가)
#
#  WS 는 받은 순서대로 쌓인다. 한 스캔을 처리하는 데 드는 시간(밴드 계산·벽 대조·
#  설정 파일 읽기·REST 호출)이 스캔 주기(0.53초)를 넘으면 큐가 계속 불어나고,
#  그때부터 **과거 상황으로 현재를 판단**하게 된다.
#
#  10회 반복 시험에서 실제로 사고가 났다:
#    · 지연이 1.5초 → 11.5초로 누적 증가
#    · 7회차 — 로봇이 물체에서 3.2 m 떨어진 출발점에 있는데 직전 회차 상황에 대한
#      `RED 앞 0.91 m` 가 뒤늦게 확정돼 속도 0 이 걸렸고, 로봇이 0.27 m 만에 섰다.
#      **아무것도 없는 곳에서 멈추는 '멈칫'의 정체가 이것이다.**
#    · 같은 WS 를 구독하되 계산이 가벼운 프로브는 밀리지 않았다 → 처리 부하 문제
#
#  낡은 스캔은 **무거운 계산에 들어가기 전에** 버린다. 버리는 비용이 거의 0 이라
#  큐가 빠르게 소진되고, 결과적으로 **항상 최신 스캔으로 판정**하게 된다.
#  (스캔을 건너뛴다고 감시가 느슨해지지 않는다 — 어차피 그 스캔은 이미 낡아서
#   지금 위치와 맞지 않는다. 최신 것으로 보는 편이 정확하고 안전하다.)
SCAN_MAX_LAG_SEC = 0.5

# 로봇 시계가 서버와 어긋나 있어 `stamp` 를 그대로 빼면 안 된다
# (실측: 로봇이 1.1초 빠름 → 차이가 음수로 나온다).
# 관측된 차이의 **최솟값**이 곧 시계 오프셋이므로, 그걸 기준으로 상대 지연만 본다.
SCAN_OFFSET_RELAX_SEC = 60.0      # 이 주기로 오프셋을 다시 잡는다(시계 드리프트 대비)

# 외삽에 쓸 전진 속도의 상한 — 추정이 튀어도 이 이상은 밀지 않는다
MAX_EXTRAPOLATE_SPEED = 1.2

# 회전 판정 — 이 값은 이제 **외삽을 쉬는 기준**으로만 쓴다.
# 회전 중에는 헤딩이 계속 바뀌어 포즈 외삽이 틀리기 때문이다.
#
# (2026-09-08 변경) 예전에는 "회전 중이면 RED 대신 서행" 규칙이 있었다.
#   코너에서 전방 밴드가 **맵에 있는 벽**을 훑어 로봇이 멈춰버려서 넣은 예외였는데,
#   아래 맵 벽 제외를 넣으면서 그 원인이 사라졌다. 예외를 남겨두면 회전 중에 사람이
#   앞에 있어도 서행까지만 걸려 오히려 위험하다 → **제거했다. 회전 중에도 정지한다.**
TURN_RATE_RAD_S = 0.25

# ══════════════════════════════════════════════════════════════════
#  맵에 이미 있는 벽은 장애물로 보지 않는다 (2026-09-08 현장)
#
#  라이다는 벽도 똑같이 찍는다. 그래서 "새로 생긴 것"과 "원래 있던 것"을 구분하지
#  못해, 주행 중·회전 중에 맵의 벽면을 보고 그냥 멈춰버렸다.
#
#  맵(점유 격자 PNG)과 대조해서 **이미 그려진 벽이면 건너뛴다.**
#
#  ★ 충돌 위험이 늘지 않는다 — 정지 장치가 둘이기 때문이다.
#     ① 로봇 자체 회피·정지 : 자기 외곽 기준으로 **항상** 동작. 벽에 안 부딪히게 하는
#        건 원래 이쪽 일이고, 여기서 끄는 게 아니다.
#     ② 이 기능(서버 안전거리) : 그보다 더 일찍·더 멀리서 줄이는 **보조** 수단.
#        벽을 빼는 건 ② 뿐이다.
#
#  무시 범위는 벽 표면에서 이 거리 안쪽뿐이다. 사람이 벽 앞에 서 있으면
#  (몸이 보통 벽에서 0.2~0.5 m 떨어진다) 그대로 잡힌다.
#  이 값은 **위치추정 오차를 흡수하는 값**이기도 하다 — 맵과 실제가 어긋난 만큼
#  벽이 '새 장애물'로 보이므로, 현장에서 오정지가 남으면 조금 키운다.
WALL_TOLERANCE_M = 0.25

# 벽인지 대조할 후보 점 수 상한. 최근접부터 이만큼만 보고, 전부 벽이면 '앞이 비었다'로 본다.
# 밴드 안 점을 전부 대조하면 틱마다 수백 번 조회하게 된다.
WALL_CHECK_MAX = 12

# ── 정적 장애물 알림 (2026-09-08 LG 통화로 확정) ──
# 로봇은 동적·정적을 구분하지 못한다. **정지가 이만큼 이어지면** 정적으로 보고 알린다.
# 알림은 로봇을 멈추지 않는다 — 이미 멈춰 있고, "사람이 와서 치워달라"를 알릴 뿐이다.
STATIC_ALERT_SEC = 20.0

# 알림은 **치울 때까지 계속** 떠 있어야 한다(LG 요청).
#   · 화면 : 태블릿·콘솔이 상태 API 를 폴링해 배너를 계속 띄운다 (`active_alerts()`)
#   · 이력 : 그대로 매번 쌓으면 알람 목록이 도배되므로 이 주기로만 기록한다
ALERT_REPEAT_SEC = 60.0

# 이동 정보(type/state) 캐시 주기.
# `/planning_state` 는 **0.08 Hz(12초에 1건)** 라 WS 로는 못 쓴다 —
# 랙 정렬에 들어가도 12초 뒤에나 알아채 그동안 랙을 장애물로 본다.
# REST 로 직접 읽는다.
MOVE_POLL_SEC = 1.0

# ★ 정밀 동작 중에는 적용하지 않는다 (2026-09-07 실기에서 사고).
#   align_with_rack 은 랙에, charge 는 충전독에 **일부러 다가가는** 동작이다.
#   그 대상을 장애물로 보고 멈추면 동작이 영영 끝나지 않고,
#   pause 가 진행 중인 정렬을 계속 취소해 `[align_retry] pause/resume` 가 반복된다.
#   deadlock_monitor 가 같은 이유로 이 동작들을 제외하는 것과 같은 취지다.
#   문자열은 `/planning_state.action_type` 이 이동 type 을 그대로 쓰는 것을
#   2026-09-07 실기로 확인했다(align_with_rack / charge / standard / along_given_route).
PRECISE_ACTIONS = {"align_with_rack", "charge", "to_unload_point"}

# 히스테리시스 — 나갈 때는 들어올 때보다 멀어야 한다.
# 없으면 거리가 임계값 근처에서 흔들릴 때 RED↔YELLOW 를 초 단위로 왕복하며
# pause/resume 를 반복한다(2026-09-07 실측: 0.99 → 1.16 → 0.91).
HYSTERESIS_M = 0.30

# ══════════════════════════════════════════════════════════════════
#  A-3) RED 은 **연속으로 잡혀야** 확정한다 (2026-09-14)
#
#  증상 — 랙을 싣고 R1 앞에 갔을 때 이런 일이 반복됐다.
#      16:59:24  YELLOW  — 앞 2.82 m 서행
#      16:59:31  ★ RED   — 앞 0.89 m 정지   (점=맵(2.20,3.11))
#      16:59:32  해제    — 속도 복구          ← **1초 만에**
#      16:59:58  [safe_move] 이동 미완료 (timeout)
#    바로 앞 틱에 `맵에 있는 벽 12개 건너뜀` 이 찍혀 있었다. WALL_TOLERANCE_M(0.25)
#    경계에 걸친 점이 스캔마다 '벽' 과 '새 장애물' 사이를 오간 것이다.
#    한 번 RED 이 걸리면 속도 0 이 로봇에 들어가고, 1초 뒤 풀려도 그 사이
#    `along_given_route` 가 진행을 못 해 safe_move 가 타임아웃으로 죽었다.
#
#  ★ 2026-09-14 2차 개정 — 적용 조건을 'YELLOW 였나' 에서 **'지금 속도'** 로 바꿨다.
#    처음에는 "RED 직전엔 거의 항상 YELLOW(서행)를 거치니 YELLOW→RED 에만 붙이면
#    된다" 고 봤는데, 그날 로그 전수를 세어보니 **19건 중 9건(47%)이
#    CLEAR/SKIP 에서 곧바로 RED** 였다.
#      이유 — 거리가 점점 가까워지는 게 아니라, **같은 점이 '벽' 과 '장애물' 사이를
#      오가기 때문**이다. 벽으로 보던 점이 갑자기 벽이 아니게 되면 거리가
#      "없음 → 1 m 안" 으로 **점프**한다. 중간(YELLOW)이 없다.
#    그래서 존이 아니라 **실제 전진 속도**로 판단한다. 정지 여력이 있으면 붙이고,
#    빠르면 안 붙인다. 그날 RED 19건의 속도 분포가 근거다.
#      0.05 m/s 이하 10건 (이미 서 있었다) · 0.05~0.35 7건 · 0.58/0.62 2건
#    → 17건에 확인이 붙고, 빠른 2건은 종전대로 즉시 정지한다.
#
#  ★ 확인을 기다리는 동안 존을 **YELLOW 로 올려 서행을 건다.**
#    CLEAR 에서 직행하는 경우까지 대상이 됐으므로, 아무 제약 없이 달리면서
#    기다리는 상태가 생기면 안 된다. 서행부터 걸어두고 다음 스캔을 본다.
RED_CONFIRM_TICKS = 3

# 확인을 붙여도 되는지 판단할 때 쓰는 물리값 (2026-09-04 실측)
SCAN_PERIOD_SEC = 0.53     # 라이다 /scan_matched_points2 1.86 Hz
REACTION_SEC = 0.68        # 스캔 지연 0.53 + 판정·POST 0.15
DECEL_MS2 = 2.0            # control.max_forward_decel -2.0
# 확인 지연까지 더한 정지거리가 red_m 의 이 비율 안에 들어와야 확인을 붙인다.
# 1.0 으로 두면 딱 red_m 에서 멈추는 셈이라 여유가 없다.
CONFIRM_MARGIN = 0.7


def confirm_allowed(v: float, red_m: float,
                    ticks: int = RED_CONFIRM_TICKS) -> bool:
    """지금 속도 `v` 에서 `ticks` 회 확인을 붙여도 `red_m` 안에 멈출 수 있나.

        정지거리 = (반응 + 확인지연) * v + v^2 / (2 * 감속도)

    | v (m/s) | 3회 확인 시 정지거리 | red_m 1.0 * 0.7 = 0.7 | 확인 |
    |---------|--------------------|----------------------|------|
    | 0.10    | 0.177 m            | 통과                  | ⭕   |
    | 0.25    | 0.451 m            | 통과                  | ⭕   |
    | 0.35    | 0.640 m            | 통과                  | ⭕   |
    | 0.40    | 0.736 m            | 초과                  | ❌   |
    | 0.62    | 1.175 m            | 초과                  | ❌   |
    """
    v = abs(float(v))
    delay = (max(1, ticks) - 1) * SCAN_PERIOD_SEC
    stop = (REACTION_SEC + delay) * v + (v * v) / (2.0 * DECEL_MS2)
    return stop <= float(red_m) * CONFIRM_MARGIN


# ══════════════════════════════════════════════════════════════════
#  C안) YELLOW 안에서 **거리별로 속도를 계단으로** 내린다 (2026-09-14)
#
#  왜 — 예전에는 YELLOW 전 구간이 `slow_speed`(0.3) 단일값이었다. 그러면
#    RED 로 넘어가는 순간 0.3 → 0 이라 급정지가 되고, 3 m 지점에서도 이미
#    0.3 까지 떨어져 통로가 느렸다.
#
#  계단으로 하면 **부드러워지면서 동시에 안전해진다.**
#    · RED 직전 속도가 0.3 → 0.1 로 내려가 정지 폭이 1/3
#    · 그때 정지거리도 0.227 → 0.071 m 로 짧아진다
#    · 반대로 3~2 m 구간은 0.3 → 0.4 로 **빨라진다**
#
#  안전 검증 (정지거리 = 0.68*v + v^2/4)
#    | 구간        | 속도 | 정지거리 | 다음 경계까지 | 여유    |
#    | 3.0 → 2.0 m | 0.50 | 0.403 m  | 1.0 m        | 0.60 m |
#    | 2.0 → 1.5 m | 0.30 | 0.227 m  | 0.5 m        | 0.27 m |
#    | 1.5 → 1.0 m | 0.10 | 0.071 m  | 0.5 m        | 0.43 m |
#    최악(3 m 밖 0.8 로 달리다 1 m 에 갑자기 출현)도 0.704 m 라 0.3 m 남기고 선다.
#
#  ※ `slow_speed` 설정값은 **계단 계산이 실패했을 때의 폴백**으로 남는다.
#
# ══════════════════════════════════════════════════════════════════
#  ★ 2026-09-15 — 한 계단씩 올렸다 (0.40/0.25/0.10 → 0.50/0.30/0.10)
#
#  요청: 기본 주행 0.8 m/s, 통로를 지금보다 빠르게. 민감한 제품을 실어
#    **멈칫(급감속)이 없어야** 하고, 속도가 오르내리는 것 자체는 무방하다.
#
#  왜 마지막 계단만 0.10 으로 남겼나 — 여기가 정지 정밀도를 만든다.
#    2026-09-15 실측 21회(공차 10 · 적재 11)에서 RED 임계를 0.10 m/s 로
#    통과했고 실제 정지가 0.571~0.783 m, 편차 0.06 이었다.
#    이 계단을 0.30 으로 올리면 RED 확인(3회 = 1.06초) 동안 전진량이 3배가 된다.
#      0.10 m/s  이론 0.177 m · 실측 0.263(최악 0.352)   → 1.0 m 안에 여유
#      0.30 m/s  이론 0.544 m · 실측환산 0.811(최악 1.086) → ★ 최악 시 못 선다
#    (실측/이론 = 1.5~2.0 배. REACTION_SEC 0.68 이 낙관적이라는 뜻이다)
#
#  0.30 이 특히 나쁜 이유 — `confirm_allowed` 가 O 를 주는 상한이 0.381 m/s 다.
#    0.30 은 아슬아슬하게 확인이 **붙어서** 1.06초를 기다리며 계속 전진한다.
#    0.50 은 상한을 넘어 **즉시 정지**라 오히려 짧게 선다. 그래서 0.30 을
#    RED 직전 계단으로 쓰면 안 된다.
#
#  ※ 급감속 자체(체감 멈칫)는 계단 수로는 못 줄인다. 로봇 감속 한계가
#    `control.max_forward_decel` -2.0 m/s² 인데 가속은 0.3 m/s² 라 7배 비대칭이다.
#    부드럽게 하려면 로봇 쪽 `control.acc_smoother.smooth_level`(현재 normal,
#    higher 까지 가능) 이나 감속 한계를 손봐야 한다 — 서버 코드가 아니다.
# ══════════════════════════════════════════════════════════════════
SLOW_STEPS: list[tuple[float, float]] = [
    (1.5, 0.10),      # 앞끝 1.5 m 안 → 0.10 m/s  ← 정지 정밀도. 올리지 말 것
    (2.0, 0.30),      #        2.0 m 안 → 0.30
    (3.0, 0.50),      #        3.0 m 안 → 0.50
]

# 계단을 **빠른 쪽으로 올라갈 때만** 경계를 이만큼 민다.
# 없으면 2.0 m 경계에서 0.4 ↔ 0.25 가 초 단위로 왕복해 주행이 덜컹거린다.
# 가까워지는 쪽(느려지는 쪽)은 즉시 적용한다 — 안전은 늦추지 않는다.
SLOW_STEP_HYST_M = 0.30


# ══════════════════════════════════════════════════════════════════
#  속도 복구 램프 — 풀릴 때 한 번에 전속으로 올리지 않는다 (2026-09-14)
#
#  왜 — 해제 순간 `base_speed`(0.7)를 한 번에 쏘면, 1초 뒤 장애물이 다시 잡힐 때
#    그 사이 가속하던 것을 되돌리느라 **울컥거린다.**
#      19:45:33  해제 — 속도 복구 0.7 m/s      ← 0.1 에서 곧바로 0.7
#      19:45:34  YELLOW — 앞 1.14 m 서행 0.1   ← 1초 뒤 도로 0.1
#      19:45:34  해제 — 속도 복구 0.7          ← 또
#    같은 1초 안에 0.7 → 0.1 → 0.7 이 오갔다. 그런 왕복이 그 주행에서 4회 있었다.
#
#  계단으로 올리면 부수 효과가 크다 — 1초 뒤 다시 잡혀도 그때 속도가 0.25 밖에
#  안 되어 0.25 → 0 은 거의 느껴지지 않는다.
#
#  ★ 올리는 쪽만 늦춘다. **YELLOW/RED 진입은 램프와 무관하게 즉시**다 —
#    감속·정지를 늦추는 일은 절대 없다. 로봇이 더 천천히 빨라질 뿐이라
#    안전은 오히려 좋아진다.
#
#  ※ 로봇 자체 가속 한계(`max_forward_acc` 0.3 m/s²)는 건드리지 않는다.
#    그건 이미 부드럽고, 문제는 **명령이 왔다갔다 한 것**이었다.
#  ★ 2026-09-15 — 계단을 3개에서 6개로 늘리고 간격을 0.6 → 0.7 로 했다.
#    민감한 제품을 싣는다는 요구 때문이다. 종전 [0.10, 0.25, 0.40] 은 마지막
#    **0.40 → 0.80 이 한 번에 +0.40** 이라 거기서 울컥했다.
#      종전  0.10 →0.25 →0.40 →0.80   (+0.15 +0.15 **+0.40**)  1.8초
#      지금  0.10 →0.20 →0.30 →0.42 →0.55 →0.68 →0.80
#                                     (+0.10 +0.10 +0.12 +0.13 +0.13 +0.12)  4.2초
#    한 계단이 0.10~0.13 이라 로봇 가속 한계(0.3 m/s²)로 0.4초쯤 걸리고,
#    간격이 0.7초라 **거의 끊김 없이 이어서 올라간다**(평균 0.17 m/s²).
#    전속 복귀가 1.8 → 4.2초로 늦어지지만, 올리는 쪽만 늦추는 것이라
#    안전에는 영향이 없다(감속·정지는 여전히 즉시).
SPEED_RAMP_STEPS: list[float] = [0.10, 0.20, 0.30, 0.42, 0.55, 0.68]
SPEED_RAMP_INTERVAL = 0.7      # 한 단계 올리는 간격(초). 스캔 주기 0.53 보다 약간 길게


def ramp_next(cur: float, base: float) -> float:
    """`cur` 다음 단계 속도. 더 올릴 계단이 없으면 `base`(원래 속도)."""
    try:
        for v in SPEED_RAMP_STEPS:
            if v > float(cur) + 1e-6 and v < float(base) - 1e-6:
                return v
        return float(base)
    except Exception:
        return float(base)


def slow_speed_for(dist, cur, default_slow: float) -> float:
    """앞끝 거리 `dist` 에 맞는 서행 속도.

    `cur` 는 지금 걸어둔 서행 속도(처음 진입이면 None). 지금 있는 계단에서
    **벗어나려 할 때만** 그 계단의 경계를 `SLOW_STEP_HYST_M` 만큼 늘려
    경계 왕복을 막는다.

      예) 0.10 단계(1.5 m)에 있으면 1.8 m 를 넘어야 0.25 로 올라간다.
          반대로 0.25 단계에서 1.4 m 로 가까워지면 곧바로 0.10 이 된다.
    """
    try:
        if dist is None:
            return float(default_slow)
        d = float(dist)
        for lim, spd in SLOW_STEPS:
            edge = lim
            if cur is not None and abs(float(cur) - spd) < 1e-6:
                edge = lim + SLOW_STEP_HYST_M      # 지금 이 계단 — 벗어나기 어렵게
            if d <= edge:
                return spd
        return SLOW_STEPS[-1][1]
    except Exception:
        return float(default_slow)          # 어떤 이유로든 실패하면 종전 단일값


# RED 에서 거는 속도. 0 = 완전 정지 (로봇이 0 을 받아주는 것을 2026-09-07 실기 확인).
# 이동 명령은 살아 있으므로 장애물이 비키면 그 자리에서 이어서 간다.
RED_SPEED = 0.0

# RED 이 이만큼 이어지면 '고정 장애물'로 보고 풀어준다.
# 로봇은 동적/정적을 구분하지 못한다 — 서버가 지속시간으로 판정해야 한다.
# 안 풀면 벽·설비 앞에서 영원히 멈춰 선다(작업 자체가 멈춘다).
# (제거됨) RED_MAX_SEC / RED_COOLDOWN_SEC —
#   RED 이 이동을 취소하던 시절, 벽 앞 영구 정지를 막으려던 쿨다운이었다.
#   지금은 속도만 0 으로 두고 이동 명령은 살아 있어 그 위험이 없고,
#   오히려 앞 0.14 m 인데 '서행' 으로 강등돼 위험했다(2026-09-07 실측). 삭제.

# ══════════════════════════════════════════════════════════════════
#  작업지점 주변에서는 판정하지 않는다 (2026-09-07 추가)
#
#  작업지점(랙 위치 R/J · 충전소 · 대기장소) 주변에서 로봇이 하는 동작은 전부
#  **대상에 일부러 다가가거나 그 자리에서 빠져나오는** 동작이다.
#    · 랙 밑에서 전진 이탈        1.05 m  (dispatch_service.RACK_ESCAPE_M)
#    · 랙 인식 실패 후 후진 재시도  0.50 m  (jack_service.ALIGN_BACKOFF_M)
#    · 랙 인식 실패 후 측면 우회    0.30 m  (jack_service.ALIGN_LATERAL_OFFSET_M)
#  이걸 장애물로 보고 멈추면 로봇이 랙 밑에 남거나 복구 동작이 통째로 막힌다.
#  이동 type 으로는 구분할 수 없다 — 전부 그냥 "standard" 다. 그래서 **위치**로 뺀다.
#
#  ★ 반경을 정하는 건 위 이동 거리들이 아니라 **랙 자체가 RED 를 유발하는 거리** 다.
#    랙을 가지러 갈 때 앞에 있는 게 바로 그 랙이라, 반경이 이보다 작으면 로봇이
#    반경 밖에서 멈춰버려 **반경 안으로 들어오지도 못한다.**
#
#      반경 = red_m + 로봇 앞끝(0.379) + 랙이 로봇 쪽으로 나온 길이 + 여유
#
#    | 랙          | 앞쪽 돌출(반깊이+여유) | 필요 반경(red 1.0) |
#    | LG   (현장)  | 0.340 m              | 1.72 m |
#    | LG2  (현장)  | 0.353 m              | 1.73 m |
#    | S300 (테스트) | 0.475 m              | 1.85 m |
#    | S600 (미사용) | 0.535 m              | 1.91 m |
#
#  ★ 현장 이관 시 — 지금은 S300 으로 테스트 중이라 0.475(→ 반경 1.90)를 쓴다.
#    현장은 LG/LG2 이므로 `RACK_FRONT_M` 을 **0.353 으로 낮추면 반경 1.78** 이 된다.
#    이 상수 하나만 고치면 되고 다른 곳은 건드릴 필요가 없다.
#
#  ※ 맞바꿈 — 이 반경 안에서는 작업자가 로봇 앞에 서 있어도 서행/정지가 걸리지 않는다.
#    J 는 사람이 랙에서 물건을 빼는 자리라 사람이 있는 게 정상이다. 그 구간은
#    로봇 자체 회피가 담당한다(이 기능과 무관하게 항상 동작). 사람이 이동하는
#    통로 구간에서는 서행/정지가 그대로 동작한다.
RACK_FRONT_M = 0.475          # 사용 중인 랙의 앞쪽 돌출. 현장(LG/LG2) 이관 시 0.353
ROBOT_FRONT_M = 0.379         # 로봇 단독 풋프린트 앞끝 (/robot_model 실측)
WORK_SKIP_MARGIN_M = 0.05     # 계산값에 붙이는 여유

# ★ 반경 하한 (2026-09-08 현장 요구).
#   계산식만 쓰면 red 1.0 기준 1.904 m 인데, **랙이 RED 를 유발하는 거리가 1.854 m**
#   라 여유가 5 cm 뿐이다. 랙이 작업지점보다 조금만 앞에 놓여도 제외 반경 **밖에서**
#   RED 가 걸린다 — 실제로 J1 가는 길에 R2 의 랙을 보고 멈췄다.
#   2.0 m 로 올리면 여유가 15 cm 가 된다.
#   ※ 통로 등 다른 곳에서는 서행 3 m / 정지 1 m 가 그대로 적용된다.
#     작업지점 반경 2 m 안에서만 판정을 쉬는 것이다.
WORK_SKIP_MIN_M = 2.0

# 반경을 벗어날 때는 이만큼 더 나가야 한다 — 경계에서 켜졌다 꺼졌다 하는 것 방지
WORK_SKIP_HYST_M = 0.20

# 판정에서 뺄 POI 종류. waypoint(W1~Wn 경유지)는 **넣지 않는다** —
# 통로 한복판이라 거기서까지 감시를 끄면 기능 자체가 무의미해진다.
WORK_POI_TYPES = ("jack", "standby", "charging")

# 작업지점 좌표 캐시 주기(초). 매 스캔마다 DB 를 치면 안 된다.
POI_REFRESH_SEC = 30.0

# 걸어둔 속도를 다시 보내는 주기(초).
# 전이할 때 한 번만 보내면 두 가지가 깨진다(2026-09-07 검토).
#   ① POST 가 LTE 에서 실패하면 존은 RED 인데 로봇은 그대로 달린다
#      — 화면엔 "정지", 실제로는 주행. 표시와 실제가 어긋난다.
#   ② 콘솔 속도 슬라이더(POST /api/robot/speed)가 로봇에 직접 속도를 쓴다.
#      RED 중에 만지면 그 속도로 즉시 출발하는데 safety 는 다시 안 쏜다.
# 같은 값을 다시 보내는 것뿐이라 멱등이다.
SPEED_REASSERT_SEC = 3.0

_stop_event = threading.Event()
_workers: dict[str, threading.Thread] = {}
_workers_lock = threading.Lock()
_state: dict[str, dict] = {}          # ip -> {"zone", "dist", "poi", "since"}
_state_lock = threading.Lock()

# 지금 알림 중인 로봇 — 태블릿·콘솔이 이걸 폴링해서 배너를 띄운다.
# 장애물이 치워져 정지가 풀리면 여기서 빠지고 배너도 자동으로 사라진다.
_alerts: dict[str, dict] = {}         # ip -> {"name", "since", "dist"}
_alerts_lock = threading.Lock()


def status() -> dict[str, dict]:
    """콘솔 표시용 — 로봇별 현재 존과 앞끝 기준 최근접 거리.

    `zone == "skip"` 이면 `poi` 에 **어느 작업지점 때문에 껐는지** 이름이 들어간다.
    이게 없으면 콘솔에 "정상"으로만 보여서 '왜 안 멈추지'를 또 추적하게 된다.
    """
    with _state_lock:
        return {ip: dict(v) for ip, v in _state.items()}


def active_alerts() -> list[dict]:
    """지금 '장애물로 멈춰 있음' 알림 중인 로봇 목록.

    태블릿·콘솔이 폴링해서 **치울 때까지 계속** 배너를 띄운다(2026-09-08 LG 요청).
    정지가 풀리면 목록에서 빠지므로 화면도 알아서 사라진다.
    """
    now = time.time()
    with _alerts_lock:
        return [
            {"robot_ip": ip, "robot_name": v.get("name") or ip,
             "seconds": int(now - v["since"]),
             "distance_m": v.get("dist")}
            for ip, v in _alerts.items()
        ]


def _clear_alert(ip: str) -> None:
    with _alerts_lock:
        if _alerts.pop(ip, None) is not None:
            logger.info(f"[safety] {ip} 정지 해소 — 알림 내림")


def _set_state(ip: str, zone: str, dist: Optional[float],
               poi: Optional[str] = None) -> None:
    with _state_lock:
        _state[ip] = {"zone": zone, "dist": dist, "poi": poi,
                      "since": time.time()}


def _base_speed(ip: str) -> float:
    """이 로봇의 '원래' 주행 속도. 서행·정지를 풀 때 이 값으로 되돌린다.

    ★ 2026-09-14 — **잭 상태에 따라 값이 달라진다** (LGIT 요청 1번).
      랙을 들고 있으면 적재 속도(기본 0.8), 빈 몸이면 공차 속도(기본 1.2)다.

      여기를 안 고치면 2단 속도가 **동작하지 않는다.** 잭 업 직후 적재 속도를
      쏴도, 이 모듈이 YELLOW/RED 해제나 작업지점 SKIP 진입 때마다 옛 단일값으로
      덮어써 버리기 때문이다(`_release` / SKIP 진입 / CLEAR 복귀 세 군데).

      Yellow 0.3 / Red 0.0 은 **그대로다** — 이 함수는 '해제 후 돌아갈 값' 만 정한다.

    순환 import 주의: safety_zone ↔ jack_service 를 모듈 최상단에서 서로 부르면
    안 되므로 여기서 지연 import 한다.
    """
    try:
        from app.services import jack_service, speed_settings
        return speed_settings.speed_for(ip, jack_service.is_laden(ip))
    except Exception:
        # 설정·판정이 어떤 이유로든 실패하면 종전 동작(DB 단일값)으로 떨어진다
        from app.database import SessionLocal
        from app.models.robot import Robot
        db = SessionLocal()
        try:
            r = db.query(Robot).filter(Robot.ip_address == ip).first()
            return float(r.max_speed) if (r and r.max_speed) else 1.2
        except Exception:
            return 1.2
        finally:
            db.close()


def _current_move(ip: str, cache: dict) -> tuple[str, bool]:
    """현재 이동의 (type, moving 여부). REST 로 직접 읽고 `MOVE_POLL_SEC` 만큼 캐시.

    `/planning_state` WS 는 0.08 Hz 라 최대 12초 낡은 값을 준다 —
    그 사이 랙 정렬에 들어가도 모르고 자기 랙을 장애물로 잡는다.
    """
    now = time.time()
    if now - cache.get("t", 0.0) < MOVE_POLL_SEC:
        return cache.get("type", ""), cache.get("moving", False)
    try:
        r = requests.get(f"http://{ip}:8090/chassis/moves/current", timeout=3)
        if r.status_code == 200:
            d = r.json()
            cache["type"] = str(d.get("type") or "").lower()
            cache["moving"] = str(d.get("state") or "").lower() == "moving"
        else:
            cache["type"], cache["moving"] = "", False
    except Exception:
        pass          # 실패하면 직전 값을 그대로 쓴다
    cache["t"] = now
    return cache.get("type", ""), cache.get("moving", False)


def _set_speed(ip: str, v: float) -> bool:
    """속도를 로봇에 적용한다. **성공 여부를 돌려준다.**

    예전에는 실패를 warning 만 찍고 넘어갔는데, 그러면 존은 RED 로 바뀌었는데
    로봇은 그대로 달리는 상태가 된다 — 화면엔 "정지", 실제로는 주행.
    호출자는 실패 시 존 전이를 **확정하지 않고** 다음 틱에 다시 시도한다.
    """
    try:
        r = requests.post(f"http://{ip}:8090/robot-params",
                          json={"/wheel_control/max_forward_velocity": float(v)},
                          timeout=5)
        if r.status_code >= 400:
            logger.warning(f"[safety] {ip} 속도 적용 거부({v}): "
                           f"HTTP {r.status_code} {r.text[:120]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"[safety] {ip} 속도 적용 실패({v}): {e}")
        return False


def work_skip_radius(red_m: float) -> float:
    """작업지점 판정 제외 반경(m). `red_m` 이 바뀌면 같이 따라간다.

    계산값이 `WORK_SKIP_MIN_M`(2.0 m) 보다 작으면 하한을 쓴다.
    red 1.0 · RACK_FRONT_M 0.475(S300) → 1.90 → **2.00**
    red 1.0 · RACK_FRONT_M 0.353(LG)   → 1.78 → **2.00**
    """
    return max(red_m + ROBOT_FRONT_M + RACK_FRONT_M + WORK_SKIP_MARGIN_M,
               WORK_SKIP_MIN_M)


def _work_points(ip: str, cache: dict) -> list[tuple[float, float, str]]:
    """이 로봇 area 의 작업지점 좌표 [(x, y, 이름)]. `POI_REFRESH_SEC` 만큼 캐시.

    조회에 실패해도 **직전 값을 그대로 쓴다.** 여기서 빈 리스트를 돌려주면
    작업지점 제외가 통째로 풀려 랙 밑 이탈이 다시 막힌다.
    """
    now = time.time()
    if now - cache.get("t", 0.0) < POI_REFRESH_SEC:
        return cache.get("pts", [])

    from app.database import SessionLocal
    from app.models.map import MapPOI, RobotMap
    from app.models.robot import Robot

    db = SessionLocal()
    try:
        robot = db.query(Robot).filter(Robot.ip_address == ip).first()
        area_id = None
        if robot is not None and robot.area_id is not None:
            try:
                area_id = int(robot.area_id)      # Robot.area_id 는 문자열 컬럼이다
            except (TypeError, ValueError):
                area_id = None

        q = db.query(RobotMap).filter(RobotMap.is_active == True)   # noqa: E712
        if area_id is not None:
            q = q.filter(RobotMap.area_id == area_id)
        active_map = q.order_by(RobotMap.id.desc()).first()

        pts: list[tuple[float, float, str]] = []
        if active_map is not None:
            rows = db.query(MapPOI).filter(
                MapPOI.map_id == active_map.id,
                MapPOI.is_active == True,                          # noqa: E712
                MapPOI.poi_type.in_(WORK_POI_TYPES),
            ).all()
            pts = sorted(          # 순서를 고정해야 아래 '바뀌었나' 비교가 안 튄다
                (float(p.world_x), float(p.world_y), p.name or "?")
                for p in rows
                if p.world_x is not None and p.world_y is not None)
        if pts != cache.get("pts"):
            logger.info(f"[safety] {ip} 작업지점 {len(pts)}곳 — "
                        f"{', '.join(n for _, _, n in pts) or '(없음)'}")
        cache["pts"] = pts
    except Exception as e:
        logger.warning(f"[safety] {ip} 작업지점 조회 실패(직전 값 유지): {e}")
    finally:
        db.close()
        cache["t"] = now          # 실패해도 매 틱 DB 를 다시 치지 않는다
    return cache.get("pts", [])


def worker_area_id(ip: str, cache: dict):
    """이 로봇의 area_id. 맵 벽 대조에 쓴다. `POI_REFRESH_SEC` 만큼 캐시."""
    now = time.time()
    if now - cache.get("t", 0.0) < POI_REFRESH_SEC:
        return cache.get("v")
    from app.database import SessionLocal
    from app.models.robot import Robot
    db = SessionLocal()
    try:
        r = db.query(Robot).filter(Robot.ip_address == ip).first()
        v = None
        if r is not None and r.area_id is not None:
            try:
                v = int(r.area_id)          # Robot.area_id 는 문자열 컬럼이다
            except (TypeError, ValueError):
                v = None
        cache["v"] = v
    except Exception as e:
        logger.warning(f"[safety] {ip} area 조회 실패(직전 값 유지): {e}")
    finally:
        db.close()
        cache["t"] = now
    return cache.get("v")


def nearest_work_point(pts, px: float, py: float):
    """(거리, 이름) — 가장 가까운 작업지점. 없으면 None."""
    best = None
    for x, y, name in pts:
        d = math.hypot(x - px, y - py)
        if best is None or d < best[0]:
            best = (d, name)
    return best


def self_front_extent(footprint) -> float:
    """풋프린트의 앞끝까지 거리(m). 로봇 좌표계는 X=우 / Y=전 이다.

    랙을 들면 로봇이 보고하는 footprint 자체가 랙 크기까지 커지므로
    (`supportsDynamicFootprints: true`, 2026-09-07 실측 0.46 → 0.80),
    이 값만 보면 '싣고 있는 랙'의 앞끝을 그대로 알 수 있다.
    """
    try:
        return max(float(p[1]) for p in footprint)
    except Exception:
        return 0.379          # 로봇 단독 풋프린트의 앞끝


def self_half_width(footprint) -> float:
    """풋프린트 반폭(m). 랙을 들면 랙 폭까지 커진다."""
    try:
        return max(abs(float(p[0])) for p in footprint)
    except Exception:
        return 0.23


def candidates_in_band(points, px: float, py: float, ori: float,
                       half_w: float, near_min: float, far: float,
                       limit: int = WALL_CHECK_MAX):
    """밴드 안 점들을 **가까운 순으로** 최대 `limit` 개. 각 원소는 (전방거리, 좌우, x, y).

    맵에 있는 벽을 건너뛰려면 최근접 하나로는 부족하다 — 그게 벽이면 그다음 점을
    봐야 한다. 그래서 nearest_in_band 대신 이걸 쓴다.
    """
    c, s = math.cos(ori), math.sin(ori)
    out = []
    for q in points:
        if isinstance(q, dict):
            qx, qy = q.get("x"), q.get("y")
        elif isinstance(q, (list, tuple)) and len(q) >= 2:
            qx, qy = q[0], q[1]
        else:
            continue
        if qx is None or qy is None:
            continue
        dx, dy = qx - px, qy - py
        fwd = dx * c + dy * s
        if fwd < near_min or fwd > far:
            continue
        lat = -dx * s + dy * c
        if abs(lat) > half_w:
            continue
        out.append((fwd, lat, qx, qy))
    out.sort(key=lambda t: t[0])
    return out[:limit]


def nearest_in_band(points, px: float, py: float, ori: float,
                    half_w: float, near_min: float, far: float):
    """`_nearest_in_band` 와 같되 **어디였는지**까지 준다 — (전방거리, 좌우, 맵x, 맵y).

    '아무것도 없는데 멈춘다'를 규명하려면 잡힌 점이 자기 몸인지 진짜 장애물인지
    알아야 한다. 그래서 좌표를 함께 남긴다.
    """
    c, s = math.cos(ori), math.sin(ori)
    best = None
    for q in points:
        if isinstance(q, dict):
            qx, qy = q.get("x"), q.get("y")
        elif isinstance(q, (list, tuple)) and len(q) >= 2:
            qx, qy = q[0], q[1]
        else:
            continue
        if qx is None or qy is None:
            continue
        dx, dy = qx - px, qy - py
        fwd = dx * c + dy * s
        if fwd < near_min or fwd > far:
            continue
        lat = -dx * s + dy * c
        if abs(lat) > half_w:
            continue
        if best is None or fwd < best[0]:
            best = (fwd, lat, qx, qy)
    return best


def _nearest_in_band(points, px: float, py: float, ori: float,
                     half_w: float, near_min: float, far: float) -> Optional[float]:
    """로봇 정면 직사각형 밴드 안에서 가장 가까운 장애물까지의 전방 거리.

    반환값은 **로봇 중심 기준**이다(라이다 점이 그 좌표계로 온다).
    호출자가 앞끝 기준으로 환산해서 쓴다.
    near_min 안쪽은 무시한다 — 자기가 든 랙을 장애물로 잡지 않기 위한 것이라
    호출자가 현재 풋프린트에 맞춰 넘겨준다.
    """
    c, s = math.cos(ori), math.sin(ori)
    best = None
    for q in points:
        if isinstance(q, dict):
            qx, qy = q.get("x"), q.get("y")
        elif isinstance(q, (list, tuple)) and len(q) >= 2:
            qx, qy = q[0], q[1]
        else:
            continue
        if qx is None or qy is None:
            continue
        dx, dy = qx - px, qy - py
        fwd = dx * c + dy * s
        if fwd < near_min or fwd > far:
            continue
        lat = -dx * s + dy * c
        if abs(lat) > half_w:
            continue
        if best is None or fwd < best:
            best = fwd
    return best


def decide_zone(current: str, dist: Optional[float], action: str,
                red_in: float, yellow_in: float) -> str:
    """다음 존을 정한다. `dist` 는 **앞끝 기준** 거리다.

    나갈 때 쓰는 임계값은 들어올 때보다 `HYSTERESIS_M` 만큼 멀다. 그래야 거리가
    임계값 근처에서 흔들려도 존이 왕복하지 않는다.
    정밀 동작(랙 정렬·충전 도킹) 중에는 항상 CLEAR — 대상에 일부러 다가가는 중이다.
    """
    if action in PRECISE_ACTIONS or dist is None:
        return CLEAR
    red_out = red_in + HYSTERESIS_M
    yellow_out = yellow_in + HYSTERESIS_M
    if current == RED:
        return RED if dist <= red_out else (YELLOW if dist <= yellow_out else CLEAR)
    if current == YELLOW:
        return RED if dist <= red_in else (YELLOW if dist <= yellow_out else CLEAR)
    return RED if dist <= red_in else (YELLOW if dist <= yellow_in else CLEAR)


def _release(ip: str, zone: str) -> None:
    """걸어둔 속도 제약을 푼다. 어떤 경로로 빠져나가든 반드시 거쳐야 한다.

    이동 명령은 애초에 건드리지 않으므로 속도만 되돌리면 된다.
    """
    try:
        _set_speed(ip, _base_speed(ip))
    except Exception:
        pass


def _raise_static_alert(ip: str, dist: Optional[float], secs: float) -> None:
    """정지가 오래 이어지면 관제에 알린다. 실패해도 주행 판정에는 영향을 주지 않는다."""
    from app.database import SessionLocal
    from app.models.alarm_log import AlarmLog
    from app.models.robot import Robot

    db = SessionLocal()
    try:
        r = db.query(Robot).filter(Robot.ip_address == ip).first()
        name = (r.name if r else None) or ip
        d = "-" if dist is None else f"{dist:.2f} m"
        db.add(AlarmLog(
            error_code="SAFETY-STATIC",
            error_type="robot",
            severity="warning",
            message=f"{name} 전방 장애물로 {int(secs)}초째 정지 — 확인이 필요합니다",
            description=(f"전방 최근접 {d}. 맵에 없는 장애물이 치워지지 않아 로봇이 "
                         f"멈춰 있습니다. 장애물을 치우면 자동으로 다시 출발합니다."),
            source="safety_zone",
            robot_sn=(r.serial_number if r else None),
        ))
        db.commit()
        with _alerts_lock:
            cur = _alerts.get(ip) or {}
            _alerts[ip] = {"name": name,
                           "since": cur.get("since", time.time() - secs),
                           "dist": dist}
        logger.warning(f"[safety] {ip} ★ 정적 장애물 알림 — {int(secs)}초 정지, 앞 {d}")
    except Exception as e:
        logger.warning(f"[safety] {ip} 정적 장애물 알림 발행 실패(무시): {e}")
    finally:
        db.close()


def _worker(ip: str) -> None:
    from app.routers.settings import get_safety_settings

    zone = CLEAR
    clear_since = 0.0
    fail_streak = 0
    logger.info(f"[safety] 감시 시작 — {ip}")

    while not _stop_event.is_set():
        ws = None
        try:
            ws = create_connection(f"ws://{ip}:8090/ws/v2/topics", timeout=WS_TIMEOUT_SEC)
            # /planning_state 는 0.08 Hz 라 쓸모가 없어 구독하지 않는다 — REST 로 읽는다.
            for t in ("/tracked_pose", "/scan_matched_points2", "/robot_model"):
                ws.send(json.dumps({"enable_topic": t}))
            ws.settimeout(WS_TIMEOUT_SEC)
            fail_streak = 0

            pose = None
            pose_t = 0.0         # 포즈 수신 시각 — 낡은 포즈로 판정하지 않으려고
            turn_rate = 0.0      # 회전 각속도(rad/s) — 회전 중엔 RED 을 걸지 않는다
            fwd_speed = 0.0      # 전진 속도(m/s) — 낡은 포즈를 이만큼 앞으로 밀어 보정
            move_cache: dict = {}
            poi_cache: dict = {}     # 작업지점 좌표 캐시 (POI_REFRESH_SEC)
            area_cache: dict = {}    # 이 로봇의 area_id 캐시 (맵 벽 대조용)
            last_speed_sent = 0.0    # 걸어둔 속도를 마지막으로 보낸 시각
            last_speed_fail_log = 0.0
            red_since = 0.0          # RED 이 시작된 시각 — 정적 장애물 알림 판정용
            red_streak = 0           # RED 이 연속으로 잡힌 횟수 (A-3)
            last_confirm_log = 0.0
            slow_now = 0.0           # 지금 걸어둔 서행 속도 (C안 계단)
            ramp_v = 0.0             # 복구 램프 진행 중인 속도 (0 = 램프 아님)
            alerted = 0.0            # 알림 이력을 마지막으로 남긴 시각
            last_wall_log = 0.0
            front = 0.379        # 자기 앞끝. /robot_model 오면 갱신된다
            half = 0.23          # 자기 반폭. 랙을 들면 커진다
            last_size_log = 0.0
            last_skip_log = 0.0
            scan_offset = None       # 로봇↔서버 시계 차 (관측 최솟값)
            scan_offset_t = 0.0      # 오프셋을 잡은 시각 — 주기적으로 다시 잡는다
            stale_drop = 0           # 버린 스캔 수
            last_stale_log = 0.0

            while not _stop_event.is_set():
                raw = ws.recv()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                topic = msg.get("topic")

                if topic == "/tracked_pose" and msg.get("pos"):
                    now_p = time.time()
                    new_pose = (float(msg["pos"][0]), float(msg["pos"][1]),
                                float(msg.get("ori", 0.0)))
                    # 포즈가 1 Hz 라 회전·전진 속도를 이 델타에서 추정할 수밖에 없다
                    if pose is not None and now_p > pose_t:
                        dt = max(1e-3, now_p - pose_t)
                        d_ori = (new_pose[2] - pose[2] + math.pi) % (2 * math.pi) - math.pi
                        turn_rate = abs(d_ori) / dt
                        # 전진 속도 = 변위를 헤딩에 투영 (옆으로 미끄러지는 성분은 뺀다)
                        dx, dy = new_pose[0] - pose[0], new_pose[1] - pose[1]
                        v = (dx * math.cos(new_pose[2]) + dy * math.sin(new_pose[2])) / dt
                        fwd_speed = max(-MAX_EXTRAPOLATE_SPEED,
                                        min(MAX_EXTRAPOLATE_SPEED, v))
                    pose = new_pose
                    pose_t = now_p
                    continue
                if topic == "/robot_model":
                    fpz = msg.get("footprint") or []
                    if fpz:
                        new_front = self_front_extent(fpz)
                        new_half = self_half_width(fpz)
                        if abs(new_front - front) > 0.02 or abs(new_half - half) > 0.02:
                            front, half = new_front, new_half
                            if time.time() - last_size_log > 5:
                                logger.info(f"[safety] {ip} 자기 크기 갱신 — 앞끝 {front:.3f} m "
                                            f"/ 반폭 {half:.3f} m (폭 {msg.get('width')})")
                                last_size_log = time.time()
                    continue
                if topic != "/scan_matched_points2":
                    continue

                # ── ★ 밀린 스캔은 여기서 버린다 (무거운 계산 전) ──
                #   설정 파일 읽기·밴드 계산·벽 대조보다 **앞**에 둬야 큐가 빨리 빈다.
                #   자세한 배경은 위 SCAN_MAX_LAG_SEC 주석 참조.
                _stamp = msg.get("stamp")
                if _stamp:
                    _d = time.time() - float(_stamp)
                    # 시계 오프셋 = 관측된 차이의 **최솟값**(= 지연이 0 이던 순간).
                    # 주기가 지나면 기준을 새로 잡아 시계 드리프트를 따라간다.
                    # ※ 계속 밀리는 상황이면 오프셋도 같이 내려가 상대 지연이 0 이 된다
                    #    → 전부 버려서 판정이 멈추는 일은 생기지 않는다(자기 보정).
                    if (scan_offset is None
                            or time.time() - scan_offset_t > SCAN_OFFSET_RELAX_SEC):
                        scan_offset = _d
                        scan_offset_t = time.time()
                    elif _d < scan_offset:
                        scan_offset = _d
                    if _d - scan_offset > SCAN_MAX_LAG_SEC:
                        stale_drop += 1
                        if time.time() - last_stale_log > 10:
                            logger.info(
                                f"[safety] {ip} 밀린 스캔 {stale_drop}건 버림 "
                                f"(지연 {_d - scan_offset:.2f}초 > {SCAN_MAX_LAG_SEC}) "
                                f"— 최신 스캔으로 판정한다")
                            last_stale_log = time.time()
                            stale_drop = 0
                        continue

                cfg = get_safety_settings()
                if not cfg.get("enabled", False):
                    if zone != CLEAR:
                        # 기능을 끄면 걸어둔 제약을 반드시 풀고 나간다.
                        # SKIP 은 애초에 아무 제약도 안 걸어둔 상태라 POST 가 필요 없다.
                        if zone in (YELLOW, RED):
                            _release(ip, zone)
                        _clear_alert(ip)
                        zone = CLEAR
                        _set_state(ip, zone, None)
                        logger.info(f"[safety] {ip} 기능 꺼짐 — 제약 해제")
                    continue

                # ★ 낡은 포즈로는 판정하지 않는다.
                #   /tracked_pose 가 1 Hz 라, 늦은 포즈로 맵 좌표 스캔을 변환하면
                #   이미 지나친 벽이 '앞'에 있는 것으로 계산된다.
                age = time.time() - pose_t
                if pose is None or age > POSE_MAX_AGE_SEC:
                    if time.time() - last_skip_log > 10:
                        logger.info(f"[safety] {ip} 포즈가 낡아 판정 보류 "
                                    f"({age:.2f}초 > {POSE_MAX_AGE_SEC})")
                        last_skip_log = time.time()
                    continue

                # 포즈를 그 사이 움직인 만큼 앞으로 밀어 보정한다.
                # 회전 중에는 헤딩이 계속 바뀌어 외삽이 틀리므로 하지 않는다.
                if turn_rate <= TURN_RATE_RAD_S and abs(fwd_speed) > 0.05:
                    adv = fwd_speed * age
                    px_e = pose[0] + adv * math.cos(pose[2])
                    py_e = pose[1] + adv * math.sin(pose[2])
                else:
                    px_e, py_e = pose[0], pose[1]

                # ── 작업지점 주변이면 판정 자체를 하지 않는다 ──
                # 랙 밑 이탈·후진 재시도·측면 우회는 전부 여기서 일어나는데,
                # 이동 type 은 그냥 "standard" 라 종류로는 구분할 수 없다.
                # 위치로 빼는 게 유일하게 빠짐 없는 방법이다.
                skip_r = work_skip_radius(float(cfg["red_m"]))
                nw = nearest_work_point(_work_points(ip, poi_cache),
                                        pose[0], pose[1])
                limit = skip_r + WORK_SKIP_HYST_M if zone == SKIP else skip_r
                if nw is not None and nw[0] <= limit:
                    if zone != SKIP:
                        # 걸어둔 제약을 먼저 푼다. 안 풀면 속도 0 인 채로 작업지점에
                        # 들어가 랙 밑 이탈이 그대로 막힌다.
                        #
                        # ★ 2026-09-15 — `or ramp_v` 추가.
                        #   종전에는 YELLOW/RED 일 때만 원속도로 되돌렸다. 그런데
                        #   **복구 램프가 진행 중일 때는 존이 이미 CLEAR** 다
                        #   (0.10 → 0.25 → 0.40 → base 를 0.6초 간격으로 올리는 중).
                        #   그 상태로 작업지점에 들어가면 아래에서 ramp_v 를 버려서
                        #   **0.40 같은 중간 계단값에 갇힌 채로** 빠져나갔다.
                        #   작업지점을 수시로 드나드는 구조라 이 타이밍이 자주 걸린다.
                        #
                        #   실측(2026-09-15 14:19) — RED 해제 후 0.4 까지만 오르고
                        #   58초간 1.2 로 복구되지 않았다. 슬라이더를 올려도
                        #   "속도가 안 바뀐다" 고 느껴지던 원인이다.
                        if (zone in (YELLOW, RED) or ramp_v) and not _set_speed(
                                ip, _base_speed(ip)):
                            continue          # 해제 실패 — 존을 바꾸지 않고 다음 틱 재시도
                        logger.info(f"[safety] {ip} 작업지점 {nw[1]} 근처"
                                    f"({nw[0]:.2f} m ≤ {skip_r:.2f}) — 판정 제외")
                        zone = SKIP
                        clear_since = 0.0
                        slow_now = 0.0
                        ramp_v = 0.0
                    _set_state(ip, SKIP, None, poi=nw[1])
                    continue
                if zone == SKIP:
                    logger.info(f"[safety] {ip} 작업지점 벗어남 — 판정 재개")
                    zone = CLEAR
                    _set_state(ip, CLEAR, None)

                action, moving = _current_move(ip, move_cache)

                # 자기가 든 랙을 장애물로 잡지 않도록, 무시 범위를 현재 풋프린트에서 뽑는다.
                # 고정값(near_min)만 쓰면 랙을 들었을 때 랙 앞끝이 그 밖으로 나와
                # 자기 랙을 계속 장애물로 본다(2026-09-07 현장 발생).
                near = max(float(cfg["near_min"]), front + SELF_CLEARANCE_M)
                # 밴드 폭은 설정값을 그대로 쓴다. 하한만 자기 반폭 —
                # 그보다 좁으면 랙 모서리가 부딪칠 장애물이 사각이 된다.
                # ★ 여유(+SELF_CLEARANCE)를 더하지 않는다. 랙 적재 시 0.725 m 가 되어
                #   좌우 1.45 m 를 보는 바람에 1.4 m 통로에서 벽이 상시로 잡히고
                #   코너에서 로봇이 멈춰버렸다(2026-09-07 현장).
                #   옆·뒤 장애물은 로봇 자체 회피가 담당한다 — 랙을 들면 footprint 가
                #   랙 크기까지 커져서(0.46→0.95 실측) 그 몸으로 전방향 회피한다.
                band_half = max(float(cfg["band_half_w"]), half)
                # 스캔 상한도 중심 기준으로 환산한다. 앞끝과 히스테리시스만큼 더 봐야
                # YELLOW 이탈 판정(yellow + 0.3)에 쓸 점이 잘리지 않는다.
                far = float(cfg["yellow_m"]) + front + HYSTERESIS_M

                pts = msg.get("points") or msg.get("data") or []
                # ★ 맵에 이미 있는 벽은 건너뛴다. 가까운 순으로 훑다가 벽이 아닌
                #   첫 점을 채택한다. 전부 벽이면 '앞이 비었다'로 본다.
                #   (로봇 자체 회피·정지는 이것과 무관하게 항상 동작한다)
                meta = map_image.map_meta_for_area(worker_area_id(ip, area_cache))
                hit = None
                walls = 0
                for cand in candidates_in_band(pts, px_e, py_e, pose[2],
                                               band_half, near, far):
                    if map_image.is_wall_at(meta, cand[2], cand[3], WALL_TOLERANCE_M):
                        walls += 1
                        continue
                    hit = cand
                    break
                if walls and time.time() - last_wall_log > 10:
                    logger.info(f"[safety] {ip} 맵에 있는 벽 {walls}개 건너뜀"
                                f"{'' if hit else ' — 앞이 비었다고 판정'}")
                    last_wall_log = time.time()
                raw_dist = None if hit is None else hit[0]
                # 판정·표시는 앞끝 기준 거리로 한다 (모듈 독스트링 참조)
                edge = None if raw_dist is None else max(0.0, raw_dist - front)

                now = time.time()

                # 걸어둔 속도를 주기적으로 다시 보낸다.
                # 전이할 때 한 번만 보내면 ① POST 실패 ② 콘솔 속도 슬라이더가
                # 덮어쓰는 경우에 "화면엔 정지, 실제로는 주행" 이 된다.
                if zone in (YELLOW, RED) and now - last_speed_sent >= SPEED_REASSERT_SEC:
                    # 서행은 **지금 걸어둔 계단값**을 다시 보낸다. 설정값(slow_speed)을
                    # 보내면 계단으로 내려둔 속도가 매 3초마다 0.3 으로 되돌아간다.
                    if _set_speed(ip, RED_SPEED if zone == RED
                                  else (slow_now or float(cfg["slow_speed"]))):
                        last_speed_sent = now

                want = decide_zone(zone, edge, action,
                                   float(cfg["red_m"]), float(cfg["yellow_m"]))

                # ── A-3) RED 은 연속으로 잡혀야 확정한다 (2026-09-14) ──
                # 적용 여부는 **지금 속도**로 정한다 — 존이 아니다.
                # 그날 RED 19건 중 9건이 CLEAR/SKIP 에서 직행이라, 존으로 거르면
                # 절반을 놓친다(같은 점이 벽↔장애물로 뒤집히면 거리가 점프한다).
                # 확인을 기다리는 동안에는 **YELLOW 로 올려 서행을 걸어둔다.**
                # 자세한 근거·계산표는 위 RED_CONFIRM_TICKS / confirm_allowed 참조.
                # ★ 판정에 쓰는 속도는 **우리가 걸어둔 지령값**이다 (2026-09-14 19:01 실측).
                #   포즈 델타 추정(fwd_speed)은 튄다 — 계단으로 0.10 m/s 를 걸어둔
                #   상태인데 추정이 클램프 상한 1.20 으로 찍혀 확인이 안 붙었다.
                #     19:01:06  서행 단계 0.25 → 0.10 m/s (앞 1.45 m)
                #     19:01:08  ★ RED — 앞 0.85 m 정지 ... 속도+1.20   ← 실제는 0.10
                #   포즈가 1 Hz 라 dt 가 작을 때 몇 cm 의 위치추정 흔들림도 큰 속도가 된다.
                #   지령값은 로봇이 그 이상 못 내므로 **보수적으로 안전**하다.
                #   YELLOW 가 아니면(CLEAR/SKIP 에서 직행) 전속일 수 있으니 즉시 정지한다.
                cmd_v = ((slow_now or float(cfg["slow_speed"])) if zone == YELLOW
                         else MAX_EXTRAPOLATE_SPEED)
                if want == RED and zone != RED and confirm_allowed(
                        cmd_v, float(cfg["red_m"])):
                    red_streak += 1
                    if red_streak < RED_CONFIRM_TICKS:
                        if now - last_confirm_log > 3:
                            logger.info(
                                f"[safety] {ip} RED 후보 {red_streak}/{RED_CONFIRM_TICKS}"
                                f" — 앞 {edge:.2f} m, 지령 {cmd_v:.2f} m/s"
                                f" (추정 {fwd_speed:+.2f}) — 서행 걸고 다음 스캔 확인")
                            last_confirm_log = now
                        want = YELLOW      # 확정 전까지 서행. CLEAR 였어도 올린다
                else:
                    red_streak = 0

                # (2026-09-08 제거) '회전 중이면 RED 대신 서행' 예외.
                #   코너에서 밴드가 **맵의 벽**을 훑어 멈추던 것을 막으려던 규칙인데,
                #   위에서 벽을 건너뛰게 되면서 원인이 사라졌다. 예외를 남기면 회전 중에
                #   사람이 앞에 있어도 서행까지만 걸려 오히려 위험하다.
                #   → 회전 중에도 정지한다.

                # ※ 예전에 있던 'RED 15초 지속 → 서행으로 강등' 쿨다운은 제거했다.
                #   RED 이 이동을 취소하던 시절의 안전장치였는데, 지금은 속도만 0 으로
                #   두고 이동 명령은 살려두므로 영구 정지가 되지 않는다.
                #   오히려 앞 0.14 m 인데 '서행' 으로 강등돼 위험했다(2026-09-07 실측).
                #   벽 앞에서 계속 멈춰 있으면 safe_move 자체 타임아웃이 처리한다.

                # 해제는 곧바로 하지 않는다 — 점 하나에 깜빡이면 주행이 덜컹거린다
                #
                # ★ 2026-09-15 — 유지 시간을 **YELLOW 해제에만** 건다 (종전엔 RED 도 포함).
                #   RED 해제(정지에서 다시 출발)는 2026-09-08 LG 통화에서
                #   **"대기 없이 즉시 출발"** 로 확정한 사항이라 늦추면 안 된다.
                #   그런데 설정값이 1.0 이라 지금까지 정지 후 1초를 기다리고 있었다.
                #
                #   반대로 YELLOW 해제는 늦출수록 좋다. 물건이 늘어선 통로에서
                #   서행↔복구가 왕복하며 덜컹거리는 걸 막고 **느린 채로 통과**한다.
                #   느리게 두는 방향이라 안전은 나빠지지 않는다.
                #   (실측 2026-09-15: 해제→재진입 163회 중 2초 이내 18%, 3초 이내 24%)
                if want == CLEAR and zone == YELLOW:
                    if clear_since == 0.0:
                        clear_since = now
                    if now - clear_since < float(cfg["clear_hold_sec"]):
                        # 존은 아직 안 바꾸되 거리는 계속 갱신한다.
                        # 안 그러면 해제 대기 동안 콘솔 표시가 얼어붙어
                        # 실제보다 훨씬 느려 보인다.
                        _set_state(ip, zone, edge)
                        continue
                else:
                    clear_since = 0.0

                # 진입은 로봇이 실제로 이동 중일 때만 (서 있는 로봇을 붙잡지 않는다)
                if want != CLEAR and zone == CLEAR and not moving:
                    _set_state(ip, CLEAR, edge)
                    continue

                # ── 정적 장애물 알림 ── 정지가 이어지면 사람이 와서 봐야 한다.
                #   로봇은 동적·정적을 구분하지 못하므로 **지속 시간으로 사후 판정**한다.
                #   알림은 로봇을 멈추지 않는다. 이미 멈춰 있고, 알리기만 한다.
                if zone == RED:
                    if red_since == 0.0:
                        red_since = now
                    elif now - red_since >= STATIC_ALERT_SEC and now - alerted >= ALERT_REPEAT_SEC:
                        # 화면 배너는 계속 떠 있고(active_alerts), 이력만 주기로 남긴다
                        _raise_static_alert(ip, edge, now - red_since)
                        alerted = now
                else:
                    if red_since:
                        _clear_alert(ip)
                    red_since = 0.0
                    alerted = 0.0

                if want == zone:
                    # 풀린 뒤 속도를 한 단계씩 올린다 (복구 램프).
                    # 램프가 끝나면 ramp_v 가 0 이 되고 여기는 아무 일도 하지 않는다.
                    if zone == CLEAR and ramp_v:
                        if now - last_speed_sent >= SPEED_RAMP_INTERVAL:
                            base = _base_speed(ip)
                            nxt = ramp_next(ramp_v, base)
                            if _set_speed(ip, nxt):
                                logger.info(f"[safety] {ip} 속도 복구 {ramp_v:.2f}"
                                            f" → {nxt:.2f} m/s")
                                ramp_v = nxt if nxt < base - 1e-6 else 0.0
                                last_speed_sent = now
                    # ★ YELLOW 안에서도 거리가 바뀌면 계단을 다시 고른다 (C안).
                    #   예전에는 여기서 아무것도 안 해서, 한 번 건 서행 속도가
                    #   존을 벗어날 때까지 그대로였다.
                    if zone == YELLOW:
                        spd = slow_speed_for(edge, slow_now,
                                             float(cfg["slow_speed"]))
                        if abs(spd - slow_now) > 1e-6 and _set_speed(ip, spd):
                            logger.info(f"[safety] {ip} 서행 단계 {slow_now:.2f} → "
                                        f"{spd:.2f} m/s (앞 {edge:.2f} m)")
                            slow_now = spd
                            last_speed_sent = now
                    _set_state(ip, zone, edge)
                    continue

                # ── 상태 전이 ──
                # ★ 속도 적용에 **성공했을 때만** 존을 바꾼다.
                #   실패했는데 존만 바꾸면 화면엔 "정지" 인데 로봇은 그대로 달린다.
                #   실패하면 존을 그대로 두고 다음 틱(약 0.6초)에 다시 시도한다.
                if want == RED:
                    if not _set_speed(ip, RED_SPEED):
                        if now - last_speed_fail_log > 5:
                            logger.warning(f"[safety] {ip} 정지 적용 실패 — "
                                           f"존 유지({zone}) 후 재시도")
                            last_speed_fail_log = now
                        continue
                    # 무엇을 보고 멈췄는지 남긴다 — 자기 몸인지 진짜 장애물인지 구분하려면
                    # 좌표가 있어야 한다. 로봇 pos·앞끝과 같이 봐야 판단이 된다.
                    # ★ 맵 벽까지 거리를 같이 남긴다 (2026-09-15 추가).
                    #   이 숫자 하나로 정지 원인이 로그에서 바로 갈린다.
                    #     <= WALL_TOLERANCE_M  : 경계에 걸친 벽 — tolerance 문제
                    #     그보다 크다          : 실물 장애물이거나 위치추정 오차
                    #   (2026-09-15 실측 건은 0.46 m 로 tolerance 0.25 의 약 2배였다
                    #    — tolerance 를 올릴 일이 아니라 맵/위치추정 문제였다.)
                    #   판정에는 쓰지 않는다. 표시만 한다.
                    wd = (map_image.wall_distance(meta, hit[2], hit[3])
                          if hit else None)
                    where = (f" 점=맵({hit[2]:.2f},{hit[3]:.2f}) 좌우{hit[1]:+.2f}m"
                             f" 중심거리{hit[0]:.2f}m"
                             f" 맵벽까지{'%.2fm' % wd if wd is not None else '1m밖'}"
                             if hit else "")
                    ramp_v = 0.0        # 정지는 램프와 무관하게 즉시
                    logger.warning(
                        f"[safety] {ip} ★ RED — 앞 {edge:.2f} m 정지"
                        f" | 로봇({px_e:.2f},{py_e:.2f}) 앞끝{front:.3f}"
                        f" 포즈나이{age:.2f}s 속도{fwd_speed:+.2f}{where}")
                elif want == YELLOW:
                    # 거리별 계단 속도 (C안). 계단 계산이 실패하면 설정값 폴백.
                    spd = slow_speed_for(edge, None, float(cfg["slow_speed"]))
                    if not _set_speed(ip, spd):
                        if now - last_speed_fail_log > 5:
                            logger.warning(f"[safety] {ip} 서행 적용 실패 — "
                                           f"존 유지({zone}) 후 재시도")
                            last_speed_fail_log = now
                        continue
                    slow_now = spd
                    ramp_v = 0.0        # 서행 진입도 램프와 무관하게 즉시
                    logger.info(f"[safety] {ip} YELLOW — 앞 {edge:.2f} m 서행 "
                                f"{spd} m/s")
                else:  # CLEAR
                    base = _base_speed(ip)
                    # ★ 한 번에 base 로 올리지 않는다. 직전 서행 속도의 **한 단계 위**
                    #   부터 시작해 0.6초마다 올린다. 근거는 SPEED_RAMP_STEPS 주석.
                    start = ramp_next(slow_now if zone == YELLOW else 0.0, base)
                    if not _set_speed(ip, start):
                        if now - last_speed_fail_log > 5:
                            logger.warning(f"[safety] {ip} 속도 복구 실패 — "
                                           f"존 유지({zone}) 후 재시도")
                            last_speed_fail_log = now
                        continue
                    slow_now = 0.0          # 계단 상태 초기화
                    ramp_v = start if start < base - 1e-6 else 0.0
                    if ramp_v:
                        logger.info(f"[safety] {ip} 해제 — {start:.2f} m/s 부터 "
                                    f"단계 복구 (목표 {base} m/s)")
                    else:
                        logger.info(f"[safety] {ip} 해제 — 속도 복구 {base} m/s")

                zone = want
                last_speed_sent = now
                _set_state(ip, zone, edge)

        except (WebSocketException, OSError) as e:
            if fail_streak == 0:
                logger.info(f"[safety] {ip} 접속 실패 — {RECONNECT_WAIT_SEC:.0f}초마다 재시도 ({e})")
            fail_streak += 1
        except Exception as e:
            logger.exception(f"[safety] {ip} 감시 오류: {e}")
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass

        # 연결이 끊긴 채로 제약이 걸려 있으면 로봇이 영영 멈춰 있게 된다 — 반드시 푼다.
        # SKIP 은 애초에 제약을 안 걸어둔 상태라 존만 되돌린다.
        _clear_alert(ip)          # 감시가 끊긴 채로 배너만 남으면 안 된다
        if zone in (YELLOW, RED):
            _release(ip, zone)
            logger.warning(f"[safety] {ip} 연결 끊김 — 걸어둔 제약 해제")
            zone = CLEAR
            _set_state(ip, zone, None)
        elif zone == SKIP:
            zone = CLEAR
            _set_state(ip, zone, None)

        if not _stop_event.is_set():
            _stop_event.wait(RECONNECT_WAIT_SEC)

    # 서비스 종료 시에도 제약을 남기지 않는다
    if zone in (YELLOW, RED):
        _release(ip, zone)
    logger.info(f"[safety] 감시 종료 — {ip}")


def _supervisor_loop() -> None:
    from app.database import SessionLocal
    from app.models.robot import Robot

    while not _stop_event.is_set():
        try:
            db = SessionLocal()
            try:
                robots = db.query(Robot).filter(
                    Robot.is_active == True,       # noqa: E712
                    Robot.ip_address != None,      # noqa: E711
                ).all()
                roster = [r.ip_address for r in robots]
            finally:
                db.close()

            with _workers_lock:
                for ip in roster:
                    t = _workers.get(ip)
                    if t is None or not t.is_alive():
                        t = threading.Thread(target=_worker, args=(ip,),
                                             name=f"safety-{ip}", daemon=True)
                        _workers[ip] = t
                        t.start()
                for ip in [k for k, v in _workers.items()
                           if k not in roster and not v.is_alive()]:
                    _workers.pop(ip, None)
        except Exception as e:
            logger.warning(f"[safety] 로봇 목록 갱신 실패: {e}")
        _stop_event.wait(ROSTER_REFRESH_SEC)


_supervisor: Optional[threading.Thread] = None


def start() -> None:
    global _supervisor
    if _supervisor and _supervisor.is_alive():
        return
    _stop_event.clear()
    _supervisor = threading.Thread(target=_supervisor_loop,
                                   name="safety-supervisor", daemon=True)
    _supervisor.start()
    logger.info("[safety] 전방 안전거리 감시 시작")


def stop() -> None:
    _stop_event.set()
    logger.info("[safety] 전방 안전거리 감시 정지 요청")
