#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""주행 1회 = 폴더 1개. 멈칫거림(stutter) 원인 규명용 다중 스트림 기록. (v2)

v1 과 무엇이 달라졌나
  v1 은 "속도가 떨어졌다" 까지만 알 수 있었다. 왜 떨어졌는지는 몰랐다.
  2026-09-16 로봇 3대(crawler_s300_op5 x2, hawk_longtray)를 직접 조사해서
  **로봇이 스스로 감속하는 근거를 내주는 토픽**을 찾아 넣었다.

  ★ /fused_sensor_state  - suggested_speed(로봇이 정한 속도) · accelerability ·
                           collide_dir · pushed · slipping. 값이 변할 때 발행된다.
  ★ /planning_state      - remaining_distance(목표까지 남은 거리) · fail_reason ·
                           viewport_blocked. 이걸로 "도착 감속"과 "이유 없는 멈칫"이
                           추정 없이 갈린다. v1 은 POI 거리로 추측했다.
  ★ /slam/state          - lidar_matching_score · position_quality · wheel_slipping
  ★ /maps/1cm/1hz        - 로봇 자신의 충돌 감지 코스트맵. 전방 1.5m 만 덮는다.
  ★ /maps/5cm/1hz        - 경로계획 코스트맵. 1cm 맵보다 멀리 본다. (v4 추가)
  ★ /alerts /chassis_state - 정지 원인 배제용 (범퍼·주변로봇은 v4에서 뺐다)

  그리고 v1 의 결함 3가지를 고쳤다.
    1) band_half_w 를 0.30 으로 하드코딩했다 - 서버 설정(/api/settings/safety)에서
       읽어온다. 서버가 0.5 로 보는데 0.3 으로 재면 장애물을 놓친다.
    2) scan_lag 에 시계 오프셋을 안 뺐다 - 로봇 시계가 수십 초 어긋나 있어서
       지연이 38초로 보였다. 관측 최솟값을 오프셋으로 잡아 뺀다.
    3) 요약에서 "스캔 미수신"과 "정면이 비어 있음"을 구분하지 못했다.

설계 원칙 3가지 (v1 과 동일)
  1) 단일 시계 - 모든 줄에 같은 기준 t(epoch, 측정 PC 시각)를 박는다.
  2) 평소엔 요약, 이벤트 근처엔 원본 - 점군·코스트맵을 다 남기면 수백 MB 다.
  3) 즉시 flush - 언제 꺼져도 그때까지는 남는다.

주행 1회의 기준
  **충전소 출발 -> 충전소 복귀** 를 1회로 본다. 사이클마다 폴더가 하나씩 생긴다.
  기준점은 충전소 POI 를 자동으로 찾고, 없으면 --home x,y 로 준다.

만들어지는 것
  _logs/run_YYYYmmdd_HHMMSS_<tag>/
    c01_HHMMSS/
      meta.json          실행 정보 + 서버 안전설정 + 로봇 파라미터(읽기만)
      motion.jsonl       10Hz  속도·가속도·위치·상한·제안속도·남은거리 등 통합
      sensor.jsonl       변화시 /fused_sensor_state 전체
      plan.jsonl         변화시 /planning_state 전체
      slam.jsonl          1Hz  /slam/state 전체
      obstacle.jsonl    ~1.9Hz 전방/좌/우 최근접 + 밴드 내 점 개수
      comm.jsonl          1Hz  수신율·지연·왕복시간
      events.jsonl      이벤트  감속구간 · 상한변경 · 경보 · 수동표시
      costmap.jsonl       1Hz  /maps/1cm/1hz 원본 PNG(base64)
      costmap5.jsonl      1Hz  /maps/5cm/1hz 원본 PNG(base64)
      moves.jsonl       변화시  /chassis/moves/{id} 이동명령 원문
      snapshots/        이벤트 +-N초 원본 점군 + 코스트맵 PNG
      backend_slice.log  이 사이클 시간대의 backend.log
      safety_slice.log   이 사이클 시간대의 [safety] 줄만
      summary.md         사람이 읽는 요약

사용
  python scripts/drive_log.py --ip auto
  python scripts/drive_log.py --ip 192.168.30.110 --cycles 3
  python scripts/drive_log.py --ip auto --probe          # 30초 사전 점검
  주행 중 멈칫하면 이 창에서 **스페이스바** - 그 시각이 기록된다.
  (사람은 느끼고 나서 누른다. 실측 지연 0.9~4.2초 - 요약은 t_onset 으로 맞춘다)

v5 (2026-09-19) 에서 고친 것 - 2026-09-18 LG 현장 로그가 통째로 빈 사건
  1) sampler 가 첫 샘플 직후 TypeError 로 죽어 motion.jsonl 이 비었다
     (c01 1줄, c02~c05 0줄). dist_poi 가 None 인데 첨자 접근을 했다.
  2) 그 스레드가 죽자 **스냅샷(원본 점군)도 같이** 날아갔다 - 트리거가
     sampler 안에 있었다. snap_writer 로 분리했다.
  3) 기록 스레드를 _spawn 으로 감쌌다. 죽으면 예외를 찍고 되살아난다.
  4) /fused_sensor_state 를 8개 필드만 남기고 버렸다 - 원문을 남긴다.
     처음 보는 필드는 events.jsonl 에 fused_new_keys 로 찍는다.
  5) 로봇이 **감속을 결정한 순간**(제안속도 하락)에도 스냅샷을 찍는다.
     종전에는 실속도가 떨어진 뒤에만 찍혀서 원인 시점을 놓쳤다.

v4 (2026-09-17) 에서 고친 것 - 전부 LG 현장 로그에서 드러난 실제 결함
  1) moves.jsonl 이 4사이클 전부 0바이트였다. _get_json 의 예외가 while 을
     뚫고 나가 move_writer 스레드가 첫 실패에 죽었다.
  2) backend_slice.log 가 4사이클 전부 0바이트인데 아무 경고가 없었다.
     백엔드를 띄운 폴더가 달라 시각대가 안 겹쳤다. 이제 후보를 찾고,
     0줄이면 왜 0줄인지를 파일에 적는다.
  3) decel 의 t_start 가 급감 시작이 아니었다(최대 11초 앞섬). t_onset 추가.
  4) 분류가 선회를 안 걸렀다. 사람이 "코너 부근"이라 넘겨짚고 넘어갔는데
     실제 방향변화는 0.6~3.4도였다. ang_max/heading_deg 로 기계가 거른다.
  5) 분류가 로봇 자신의 코스트맵을 안 봤다. 2D 스캔이 비어도 코스트맵은
     막혀 있는 경우가 18% 있었다. clear1/clear5 로 거른다.
  6) 요약이 건수만 냈다. 길이가 다른 사이클끼리 비교가 안 된다. 100m당 추가.
  7) 남은 미제를 "정면 비어 있는데 감속" 이라는 그럴듯한 이름으로 덮었다.
     이제 "★ 미설명" 으로 부르고 맨 위에 건수를 띄운다.

※ 로봇 설정을 바꾸는 호출은 이 파일에 없다. 전부 읽기(GET/구독)만 한다.
"""

import argparse
import base64
import collections
import io
import json
import math
import os
import socket
import ssl
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import queue
import zlib
from collections import deque

# 콘솔 인코딩 안전망 - 한국어 윈도우 콘솔(cp949)은 일부 기호를 못 쓴다.
# 그대로 두면 print 한 줄 때문에 측정 전체가 죽는다(2026-09-16 실측).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(_ROOT, "_logs")
DEFAULT_BACKEND_LOG = os.path.join(_ROOT, "BackEnd", "_logs", "backend.log")

# 서버(safety_zone.py)와 같은 기준. 서버에서 못 읽어오면 이 값을 쓴다.
ROBOT_FRONT_M = 0.379      # 로봇 단독 풋프린트 앞끝 (/robot_model 실측)
RACK_FRONT_M = 0.475       # 랙 실었을 때 앞쪽 돌출
SELF_CLEARANCE_M = 0.25    # 이 안쪽 점은 자기 몸으로 본다
FALLBACK_BAND_HALF_W = 0.5
SCAN_FAR_M = 6.0           # 이 밖은 안 본다
SIDE_FWD_M = 1.0           # 좌/우 최근접을 볼 전방 범위

RISE_END = 0.02            # 이만큼 다시 오르면 하락 구간 종료
CAP_POLL_SEC = 0.2         # 서버 속도 상한 폴링 주기
TURN_ANG = 0.25            # 이 이상이면 선회로 본다 (rad/s)
TURN_DEG = 15.0            # 또는 구간 방향변화가 이 이상이면 선회
LETHAL = 200               # 코스트맵 PNG 에서 이 값 이상이면 점유

# 구독할 토픽. 2026-09-16 로봇 3대에서 전부 수신 확인(코스트맵은 충전 중엔 안 옴).
TOPICS = [
    "/motion_metrics", "/tracked_pose", "/planning_state", "/slam/state",
    "/scan_matched_points2", "/fused_sensor_state",
    "/chassis_state", "/wheel_state", "/alerts",
    "/battery_state", "/robot_model", "/jack_state",
    "/maps/1cm/1hz", "/maps/5cm/1hz",
]
# 2026-09-17 정리
#   뺌 : /bumper_state  - 눌렸을 때만 기록되는데 접촉 사례가 없어 판정에 무용
#         /nearby_robots - 현장 로봇 1대라 항상 0. 변화가 없어 아무것도 안 남음
#   넣음: /maps/5cm/1hz - 경로계획 코스트맵. 1cm 맵은 창이 3m(전방 1.5m)뿐이라
#         로봇이 실제로 보고 반응하는 거리를 못 본다(2026-09-17 LG 로그에서 확인).


# ==================================================================
#  최소 WebSocket 클라이언트 (v1 에서 검증된 것 그대로)
# ==================================================================
# WS 가 끊긴 뒤 다시 붙기까지 기다리는 시간(초). 2026-09-21 신설.
RECONNECT_WAIT_SEC = 2.0


class _WS(object):
    def __init__(self, url, on_message=None, on_open=None, on_error=None):
        self.url, self.on_message = url, on_message
        self.on_open, self.on_error = on_open, on_error
        self.sock, self._alive, self._buf = None, True, b""
        self._msg_err, self._reconn = 0, 0

    def connect(self, timeout=10):
        u = urllib.parse.urlparse(self.url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        s = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            s = ssl._create_unverified_context().wrap_socket(s, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        s.send(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
                % (path, host, port, key)).encode())
        hdr = b""
        while b"\r\n\r\n" not in hdr:
            c = s.recv(1)
            if not c:
                raise ConnectionError("handshake closed")
            hdr += c
        if b"101" not in hdr.split(b"\r\n")[0]:
            raise ConnectionError("handshake failed: %s" % hdr.split(b"\r\n")[0])
        s.settimeout(None)
        self.sock = s

    def send(self, data, opcode=0x1):
        if isinstance(data, str):
            data = data.encode()
        hdr = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        hdr += mask
        hdr += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.send(bytes(hdr))

    def _read(self, n):
        while len(self._buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise ConnectionError("closed")
            self._buf += c
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self):
        b0, b1 = self._read(2)
        fin, opcode = b0 & 0x80, b0 & 0x0F
        masked, ln = b1 & 0x80, b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if masked else None
        payload = self._read(ln) if ln else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def run_forever(self, reconnect=True):
        """수신 루프. **끊기면 다시 붙는다.**

        2026-09-21 — 이름이 `run_forever` 인데 실제로는 한 번 끊기면 끝이었다.
          그리고 `on_message` 안에서 난 예외가 바깥 `except` 로 새어나가
          **메시지 하나가 WS 스레드를 통째로 죽였다.** 그러면
            · `/tracked_pose` 가 안 들어와 `S["pose"]` 가 그 자리에 멈추고
            · `cycle_watch` 는 충전소 출발을 영영 못 봐서 **사이클 폴더가 0개**
            · `/motion_metrics` 도 끊겨 화면 속도가 **0.00 에 고정**
          된다. 2026-09-21 현장에서 "움직이는데 로그가 안 남는다" 가 이것이다.

          이제 ① 메시지 예외는 그 메시지만 버리고 ② 연결이 끊기면 다시 붙는다.
        """
        while self._alive:
            try:
                if self.sock is None:
                    self.connect()
                if self.on_open:
                    self.on_open(self)
                frag, fragop = b"", None
                while self._alive:
                    fin, op, payload = self._frame()
                    if op == 0x8:
                        break
                    if op == 0x9:
                        self.send(payload, opcode=0xA)
                        continue
                    if op == 0xA:
                        continue
                    if op == 0x0:
                        frag += payload
                    else:
                        frag, fragop = payload, op
                    if not fin:
                        continue
                    data, frag = frag, b""
                    if fragop == 0x1 and self.on_message:
                        try:
                            self.on_message(self, data.decode("utf-8"))
                        except UnicodeDecodeError:
                            pass
                        except Exception as me:
                            # ★ 이 메시지만 버린다. 수신은 계속한다.
                            self._msg_err += 1
                            if self._msg_err <= 3:
                                import traceback
                                print("  ★ 메시지 처리 오류(%d번째, 수신은 계속): %s"
                                      % (self._msg_err, me), flush=True)
                                traceback.print_exc()
            except Exception as e:
                if self.on_error:
                    self.on_error(self, e)
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock, self._buf = None, b""
            if not reconnect or not self._alive:
                break
            self._reconn += 1
            print("  WS 재연결 시도 %d회째 …" % self._reconn, flush=True)
            time.sleep(RECONNECT_WAIT_SEC)
        self._alive = False

    def close(self):
        self._alive = False
        try:
            self.sock.close()
        except Exception:
            pass


# ==================================================================
#  공유 상태
# ==================================================================
lock = threading.Lock()
stop_flag = threading.Event()
ARGS = None
CFG = {}        # 서버 안전설정 (/api/settings/safety)
META = {}

S = {
    "pose": None, "pose_t": 0.0,            # (x, y, ori)
    "v": None, "acc": None, "ang": None, "motion_t": 0.0,
    "cap": None,                            # 서버가 건 속도 상한
    "plan": {}, "slam": {}, "fused": {}, "fused_raw": None,
    "chassis": {}, "wheel": {}, "jack": {},
    "battery": {},
    "costmap1": None, "costmap5": None,   # 최신 코스트맵 1장씩 (감속 분류용)
    "footprint": None, "half_w": None, "front_ext": None,
    "obs": None, "scan_t": 0.0, "scan_lag": None,
    "plan_t": 0.0, "fused_t": 0.0, "_alert_sig": None,
    "_last_msg_t": 0.0,
}

scans = deque(maxlen=60)        # (t, points)  스냅샷용
costmaps = deque(maxlen=20)     # (t, dict)    코스트맵 스냅샷용
ws_gaps = deque(maxlen=400)
rest_rtt = deque(maxlen=60)
counters = collections.Counter()
events_n = [0]
snaps_n = [0]
marks = []                      # 사용자가 스페이스바로 찍은 시각
scan_off = {"v": None, "t": 0.0}   # 로봇-PC 시계 오프셋

OUT = {}
ARGS = [None]          # 파싱된 인자. WS 스레드에서도 봐야 해서 전역으로 둔다.


def w(name, obj):
    """한 줄 쓰고 즉시 flush. 어떤 이유로 꺼져도 여기까지는 남는다."""
    f = OUT.get(name)
    if f is None:
        return
    try:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
    except Exception:
        pass


def _get_json(url, timeout=3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ==================================================================
#  코스트맵 PNG 해독 - 표준 라이브러리(zlib)만 쓴다
#  로봇이 주는 건 8비트 흑백·비인터레이스 PNG 다(실측 확인).
#  Pillow 를 깔면 현장 노트북마다 설치가 필요해지므로 직접 푼다.
#  2026-09-17: 실제 로그 300프레임에서 Pillow 결과와 바이트 단위 일치 확인.
# ==================================================================
def decode_gray_png(raw):
    """8비트 흑백 PNG -> (w, h, bytearray). 못 풀면 None."""
    if not raw or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    i, w, h, idat = 8, 0, 0, []
    try:
        while i + 8 <= len(raw):
            ln = struct.unpack(">I", raw[i:i + 4])[0]
            typ = raw[i + 4:i + 8]
            body = raw[i + 8:i + 8 + ln]
            if typ == b"IHDR":
                w, h, bd, ct, _c, _f, il = struct.unpack(">IIBBBBB", body[:13])
                if bd != 8 or ct != 0 or il != 0:
                    return None          # 예상과 다른 형식이면 손대지 않는다
            elif typ == b"IDAT":
                idat.append(body)
            elif typ == b"IEND":
                break
            i += 12 + ln
        if not w or not idat:
            return None
        d = zlib.decompress(b"".join(idat))
        out = bytearray(w * h)
        prev = bytearray(w)
        p = 0
        for y in range(h):
            f = d[p]
            p += 1
            line = bytearray(d[p:p + w])
            p += w
            if f == 1:
                for x in range(1, w):
                    line[x] = (line[x] + line[x - 1]) & 255
            elif f == 2:
                for x in range(w):
                    line[x] = (line[x] + prev[x]) & 255
            elif f == 3:
                for x in range(w):
                    a = line[x - 1] if x else 0
                    line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
            elif f == 4:
                for x in range(w):
                    a = line[x - 1] if x else 0
                    b = prev[x]
                    cc = prev[x - 1] if x else 0
                    pa, pb, pc = abs(b - cc), abs(a - cc), abs(a + b - 2 * cc)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else cc)
                    line[x] = (line[x] + pr) & 255
            out[y * w:(y + 1) * w] = line
            prev = line
        return w, h, out
    except Exception:
        return None


def costmap_clearance(cm, x, y, ori, fwd_m=1.5, side_m=1.0):
    """로봇이 제 코스트맵에서 본 통로 여유를 잰다.

    되돌려주는 것
      left_m / right_m : 진행축 기준 좌·우 최근접 점유까지 측방거리
      ahead_m          : 밴드(좌우 0.5m) 안 정면 최근접 점유까지 거리
      cover_m          : 이 코스트맵이 전방으로 실제 덮는 거리

    ※ cover_m 을 같이 주는 이유 - 1cm 맵은 창이 3m(전방 1.5m)뿐이라
      "정면이 비었다"가 그 거리까지만 유효하다. 이걸 모르고 판정하면
      2026-09-17 처럼 "장애물 없음"을 잘못 단정하게 된다.
    """
    if not cm or not cm.get("data"):
        return None
    try:
        got = decode_gray_png(base64.b64decode(cm["data"]))
        if not got:
            return None
        W, H, buf = got
        ox, oy = cm["origin"]
        res = float(cm["resolution"])
    except Exception:
        return None
    cover = min(W, H) * res / 2.0
    fwd_m = min(fwd_m, cover)
    co, si = math.cos(ori), math.sin(ori)

    def occupied(d, lat):
        wx = x + co * d - si * lat
        wy = y + si * d + co * lat
        cx = int((wx - ox) / res)
        cy = int((wy - oy) / res)
        if not (0 <= cx < W and 0 <= cy < H):
            return None
        return buf[(H - 1 - cy) * W + cx] >= LETHAL

    left = right = ahead = None
    steps = max(1, int(fwd_m / 0.04))
    for di in range(steps + 1):
        d = di * 0.04
        for jl in range(0, int(side_m / 0.04) + 1):
            lat = jl * 0.04
            for sgn in (-1, 1):
                hit = occupied(d, sgn * lat)
                if not hit:
                    continue
                if sgn < 0 and (left is None or lat < left):
                    left = lat
                if sgn > 0 and (right is None or lat < right):
                    right = lat
                if lat <= 0.5 and d >= 0.45 and (ahead is None or d < ahead):
                    ahead = d
    return {"left_m": left, "right_m": right, "ahead_m": ahead,
            "cover_m": round(cover, 2), "res": res}


def _changed(old, new, keys):
    """관심 있는 키만 비교. 좌표 같은 건 매번 변하니 넣지 않는다."""
    for k in keys:
        if old.get(k) != new.get(k):
            return True
    return False


# ==================================================================
#  장애물 - 점군을 요약한다 (원본은 이벤트 근처에서만 뜬다)
# ==================================================================
def _xy(q):
    if isinstance(q, dict):
        return q.get("x"), q.get("y")
    if isinstance(q, (list, tuple)) and len(q) >= 2:
        return q[0], q[1]
    return None, None


def band_half_now():
    """밴드 반폭 = max(서버 설정, 지금 풋프린트 반폭).

    서버 safety_zone.py:1040 과 같은 식이다. 랙을 실으면 로봇이 보고하는
    풋프린트가 커지므로 밴드도 따라 커진다.
    """
    base = float(CFG.get("band_half_w") or FALLBACK_BAND_HALF_W)
    with lock:
        half = S.get("half_w")
    return max(base, half) if half else base


def front_ext_now():
    with lock:
        fe = S.get("front_ext")
    return fe if fe else (ARGS.front if ARGS else ROBOT_FRONT_M)


def summarize_scan(points, pose, front_ext, half_w):
    """정면 밴드 최근접 + 좌/우 최근접 + 밴드 내 점 개수.

    front_ext = 자기 앞끝까지 거리. 로봇 중심 기준 거리에서 이걸 빼야
    '앞끝에서 장애물까지' 가 된다 - 서버 safety_zone 과 같은 기준이다.
    """
    if not pose or points is None:
        return None
    px, py, ori = pose
    c, s = math.cos(ori), math.sin(ori)
    near_min = max(SELF_CLEARANCE_M, front_ext - 0.05)

    front = None
    n_band = 0
    left = right = None
    n_all = 0

    for q in points:
        qx, qy = _xy(q)
        if qx is None or qy is None:
            continue
        n_all += 1
        dx, dy = qx - px, qy - py
        fwd = dx * c + dy * s
        if fwd > SCAN_FAR_M or fwd < -0.5:
            continue
        lat = -dx * s + dy * c
        if fwd >= near_min and abs(lat) <= half_w:
            n_band += 1
            d = fwd - front_ext
            if front is None or d < front[0]:
                front = (round(d, 3), round(lat, 3), round(qx, 3), round(qy, 3))
        if 0.0 <= fwd <= SIDE_FWD_M and abs(lat) <= 1.2:
            if lat > 0 and (left is None or lat < left):
                left = round(lat, 3)
            elif lat < 0 and (right is None or -lat < -right):
                right = round(lat, 3)

    return {
        "front_m": front[0] if front else None,
        "front_lat": front[1] if front else None,
        "front_x": front[2] if front else None,
        "front_y": front[3] if front else None,
        "n_band": n_band,
        "left_m": left, "right_m": right,
        "n_points": n_all,
        "half_w": round(half_w, 3),
    }


# ==================================================================
#  WS 수신
# ==================================================================
# 이동 명령 원문을 받아올 대기열. 토픽 처리 스레드를 막지 않도록 따로 돈다.
move_q = queue.Queue(maxsize=64)
last_aid = [None]

PLAN_KEYS = ("move_state", "stuck_state", "action_type", "action_id",
             "fail_reason", "fail_reason_str", "move_intent",
             "viewport_blocked", "in_elevator", "is_waiting_for_dest",
             "going_back_to_charger")
FUSED_KEYS = ("suggested_speed", "accelerability", "collide_dir", "pushed",
              "slipping", "major_slipping", "is_still", "odom_source")
SLAM_KEYS = ("state", "reliable", "lidar_reliable", "lidar_matched",
             "position_quality", "wheel_slipping")


# --light 용 최소 구독. 로봇에 거의 부하를 안 주면서
#   속도(멈칫 검출) · 위치 · CPU 경보만 본다.
# 목적: "로봇 CPU 과부하가 원인인가" 를 우리 측정이 부하를 보태지 않은
#       상태에서 확인하기 위한 것. 전량 구독은 초당 약 42메시지인데
#       이건 약 20메시지이고, 코스트맵 PNG 인코딩을 로봇에 안 시킨다.
LIGHT_TOPICS = ["/motion_metrics", "/tracked_pose", "/alerts"]


def active_topics():
    return LIGHT_TOPICS if (ARGS and getattr(ARGS, "light", False)) else TOPICS


def on_open(ws):
    for t in active_topics():
        try:
            ws.send(json.dumps({"enable_topic": t}))
            time.sleep(0.04)
        except Exception:
            pass
    ts = active_topics()
    print("구독 완료 (%d개 토픽%s) - 기록 시작"
          % (len(ts), " · --light 최소구독" if ts is LIGHT_TOPICS else ""),
          flush=True)
    print("")


def on_msg(_w, raw_msg):
    now = time.time()
    with lock:
        if counters["ws_msg"]:
            ws_gaps.append(now - (S.get("_last_msg_t") or now))
        S["_last_msg_t"] = now
        counters["ws_msg"] += 1
    try:
        m = json.loads(raw_msg)
    except Exception:
        return
    tp = m.get("topic")

    if tp == "/motion_metrics":
        with lock:
            S["v"] = m.get("linear_velocity")
            S["acc"] = m.get("linear_acc")
            S["ang"] = m.get("angular_velocity")
            S["motion_t"] = now

    elif tp == "/tracked_pose":
        p = m.get("pos")
        if p:
            with lock:
                S["pose"] = (float(p[0]), float(p[1]), float(m.get("ori", 0.0)))
                S["pose_t"] = now

    elif tp == "/planning_state":
        d = {k: m.get(k) for k in PLAN_KEYS}
        d["remaining_distance"] = m.get("remaining_distance")
        with lock:
            old = S["plan"]
            S["plan"] = d
            S["plan_t"] = now
            ch = _changed(old, d, PLAN_KEYS)
        if ch or not old:
            d2 = dict(d)
            d2["t"] = now
            d2["target_poses"] = m.get("target_poses")
            w("plan", d2)
            aid = d.get("action_id")
            if aid is not None and aid != last_aid[0]:
                last_aid[0] = aid
                try:
                    move_q.put_nowait((now, aid))
                except Exception:
                    pass
            fr = d.get("fail_reason")
            if fr:
                w("events", {"t": now, "kind": "plan_fail", "fail_reason": fr,
                             "fail_reason_str": d.get("fail_reason_str"),
                             "move_state": d.get("move_state")})
            if d.get("stuck_state") not in (None, "none"):
                w("events", {"t": now, "kind": "stuck",
                             "stuck_state": d.get("stuck_state")})

    elif tp == "/fused_sensor_state":
        d = {k: m.get(k) for k in FUSED_KEYS}
        # 2026-09-19: 원문을 통째로 남긴다.
        #   종전에는 FUSED_KEYS 8개만 뽑고 나머지를 버렸다. 그래서 로봇이
        #   "왜 느리게 가라고 하는지"를 같은 메시지에 담아 보내고 있어도
        #   우리가 볼 수 없었다. 2026-09-18 LG 로그에서 로봇 자체 감속
        #   18회의 원인을 하나도 못 짚은 이유 중 하나다. 판정은 종전대로
        #   FUSED_KEYS 로 하되, 기록은 원문으로 한다.
        raw = {k: v for k, v in m.items() if k != "topic"}
        _note_new_fused_keys(raw, now)
        with lock:
            old = S["fused"]
            old_raw = S.get("fused_raw")
            S["fused"] = d
            S["fused_raw"] = raw
            S["fused_t"] = now
            ch = _changed(old, d, FUSED_KEYS) or (old_raw != raw)
            counters["fused"] += 1
        if ch or not old:
            d2 = dict(raw)
            d2["t"] = now
            w("sensor", d2)
            # 로봇이 스스로 속도를 낮춘 순간 - 이게 자체 감속의 직접 근거다
            if old and old.get("suggested_speed") is not None \
                    and d.get("suggested_speed") is not None \
                    and d["suggested_speed"] < old["suggested_speed"] - 0.005:
                with lock:
                    pose = S["pose"]
                    obs = dict(S["obs"]) if S["obs"] else None
                w("events", {"t": now, "kind": "suggest_drop",
                             "frm": old["suggested_speed"],
                             "to": d["suggested_speed"],
                             "collide_dir": d.get("collide_dir"),
                             "pushed": d.get("pushed"), "slipping": d.get("slipping"),
                             "x": round(pose[0], 3) if pose else None,
                             "y": round(pose[1], 3) if pose else None,
                             "front_m": (obs or {}).get("front_m"),
                             "n_band": (obs or {}).get("n_band")})
                # ★ 2026-09-19 - 로봇이 감속을 **결정한 순간**의 원본 점군.
                #   종전 스냅샷은 "실속도가 떨어진 뒤"에만 찍혔다. 로봇 자체
                #   감속은 제안속도가 먼저 떨어지므로 그때는 이미 늦다.
                #   2026-09-18 LG: 로봇이 제안속도를 0.10 까지 떨궜는데
                #   정면은 비어 있어 원인을 못 짚었다. 그 순간을 남긴다.
                _maybe_snap_on_suggest(d["suggested_speed"], now)

    elif tp == "/slam/state":
        d = {k: m.get(k) for k in SLAM_KEYS}
        d["lidar_matching_score"] = m.get("lidar_matching_score")
        d["position_loss_progress"] = m.get("position_loss_progress")
        d["t"] = now
        with lock:
            old = S["slam"]
            S["slam"] = d
        w("slam", d)
        if old and old.get("position_quality") is not None \
                and d.get("position_quality") is not None \
                and d["position_quality"] < old["position_quality"]:
            w("events", {"t": now, "kind": "slam_drop",
                         "frm": old["position_quality"], "to": d["position_quality"],
                         "score": d.get("lidar_matching_score"),
                         "wheel_slipping": d.get("wheel_slipping")})

    elif tp in ("/chassis_state", "/wheel_state"):
        key = "chassis" if tp == "/chassis_state" else "wheel"
        d = {"control_mode": m.get("control_mode"),
             "estop": m.get("emergency_stop_pressed"),
             "error_msg": m.get("error_msg")}
        with lock:
            old = S[key]
            S[key] = d
        if old and old != d:
            w("events", {"t": now, "kind": key, **d})

    elif tp == "/alerts":
        al = m.get("alerts") or []
        sig = tuple(sorted(str(x.get("code")) for x in al
                           if isinstance(x, dict)))
        with lock:
            prev = S.get("_alert_sig")
            S["_alert_sig"] = sig
        if al and sig != prev:
            w("events", {"t": now, "kind": "alerts", "alerts": al})

    elif tp == "/battery_state":
        with lock:
            S["battery"] = {"pct": m.get("percentage"), "v": m.get("voltage"),
                            "status": m.get("power_supply_status")}

    elif tp == "/jack_state":
        d = {"state": m.get("state"), "weight": m.get("weight"),
             "self_check": m.get("self_check_state")}
        with lock:
            old = S["jack"]
            S["jack"] = d
        if old and old.get("state") != d.get("state"):
            w("events", {"t": now, "kind": "jack", **d})

    elif tp == "/robot_model":
        fp = m.get("expanded_footprint") or m.get("footprint")
        if fp:
            try:
                half = max(abs(float(p[0])) for p in fp)
                front = max(float(p[1]) for p in fp)
            except Exception:
                return
            with lock:
                changed = (S["half_w"] is None
                           or abs((S["half_w"] or 0) - half) > 0.005)
                S["half_w"], S["front_ext"] = half, front
                S["footprint"] = fp
            if changed:
                w("events", {"t": now, "kind": "footprint",
                             "half_w": round(half, 3), "front_ext": round(front, 3),
                             "width": m.get("width")})

    elif tp in ("/maps/1cm/1hz", "/maps/5cm/1hz"):
        fine = (tp == "/maps/1cm/1hz")
        cm = {"origin": m.get("origin"), "resolution": m.get("resolution"),
              "size": m.get("size"), "data": m.get("data")}
        with lock:
            if fine:
                costmaps.append((now, cm))
                counters["costmap"] += 1
                S["costmap1"] = (now, cm)
            else:
                counters["costmap5"] += 1
                S["costmap5"] = (now, cm)
        # 스냅샷용 링버퍼에만 두면 사이클이 끝날 때 사라진다.
        # 1Hz x 약 5.7KB 라 사이클(3분)당 1MB 남짓이다 - 그냥 전부 남긴다.
        cmr = dict(cm)
        cmr["t"] = now
        cmr["stamp"] = m.get("stamp")
        w("costmap" if fine else "costmap5", cmr)

    elif tp == "/scan_matched_points2":
        pts = m.get("points") or m.get("data") or []
        stamp = m.get("stamp")
        lag = None
        if stamp:
            try:
                raw = now - float(stamp)
                # 시계 오프셋 = 관측된 차이의 최솟값. 주기적으로 다시 잡는다.
                if scan_off["v"] is None or now - scan_off["t"] > 60.0:
                    scan_off["v"], scan_off["t"] = raw, now
                elif raw < scan_off["v"]:
                    scan_off["v"] = raw
                lag = round(raw - scan_off["v"], 3)
            except Exception:
                lag = None
        with lock:
            pose = S["pose"]
            S["scan_t"] = now
            S["scan_lag"] = lag
            scans.append((now, pts))
            counters["scan"] += 1
        obs = summarize_scan(pts, pose, front_ext_now(), band_half_now())
        if obs is not None:
            obs["t"] = now
            obs["scan_lag"] = lag
            obs["x"] = round(pose[0], 3)
            obs["y"] = round(pose[1], 3)
            obs["ori"] = round(pose[2], 4)
            with lock:
                S["obs"] = obs
            w("obstacle", obs)


# ==================================================================
#  REST 폴링 - 속도 상한 + 왕복시간(통신 품질). 읽기(GET)만 한다.
# ==================================================================
def poll_params(ip):
    last_cap = None
    while not stop_flag.is_set():
        t0 = time.time()
        try:
            d = _get_json("http://%s:8090/robot-params" % ip, 3)
            rtt = (time.time() - t0) * 1000.0
            cap = d.get("/wheel_control/max_forward_velocity")
            with lock:
                rest_rtt.append(rtt)
                if cap is not None:
                    S["cap"] = cap
            if (cap is not None and last_cap is not None
                    and abs(cap - last_cap) > 0.005):
                with lock:
                    pose = S["pose"]
                    obs = dict(S["obs"]) if S["obs"] else None
                w("events", {
                    "t": time.time(), "kind": "cap_change",
                    "frm": last_cap, "to": cap,
                    "x": round(pose[0], 3) if pose else None,
                    "y": round(pose[1], 3) if pose else None,
                    "front_m": (obs or {}).get("front_m"),
                    "n_band": (obs or {}).get("n_band"),
                })
            if cap is not None:
                last_cap = cap
        except Exception:
            with lock:
                counters["rest_fail"] += 1
        # 2026-09-17 1.0 -> 0.2 초.
        # REST 왕복이 0.3~0.7초(LG 현장 실측)라 1초 폴링으로는
        # 1.5초보다 짧은 서버 속도 쓰기가 구조적으로 안 보였다.
        stop_flag.wait(CAP_POLL_SEC)


def comm_writer():
    """1 Hz 로 통신 품질을 남긴다. 판정 지연이 원인인지 가리는 근거."""
    last = {"ws": 0, "scan": 0, "fused": 0, "cm": 0}
    while not stop_flag.is_set():
        stop_flag.wait(1.0)
        now = time.time()
        with lock:
            gaps = list(ws_gaps)
            rtts = list(rest_rtt)
            d_ws = counters["ws_msg"] - last["ws"]
            d_scan = counters["scan"] - last["scan"]
            d_fu = counters["fused"] - last["fused"]
            d_cm = counters["costmap"] - last["cm"]
            last["ws"] = counters["ws_msg"]
            last["scan"] = counters["scan"]
            last["fused"] = counters["fused"]
            last["cm"] = counters["costmap"]
            scan_age = now - S["scan_t"] if S["scan_t"] else None
            pose_age = now - S["pose_t"] if S["pose_t"] else None
            rest_fail = counters["rest_fail"]
            ws_err = counters["ws_err"]
            slag = S["scan_lag"]
        w("comm", {
            "t": now,
            "ws_hz": d_ws, "scan_hz": d_scan, "fused_hz": d_fu, "costmap_hz": d_cm,
            "ws_gap_max": round(max(gaps), 3) if gaps else None,
            "ws_gap_avg": round(sum(gaps) / len(gaps), 4) if gaps else None,
            "scan_age": round(scan_age, 3) if scan_age is not None else None,
            "scan_lag": slag,
            "pose_age": round(pose_age, 3) if pose_age is not None else None,
            "rest_rtt_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
            "rest_rtt_max_ms": round(max(rtts), 1) if rtts else None,
            "rest_fail": rest_fail, "ws_err": ws_err,
        })


def move_writer(ip):
    """이동 명령이 바뀔 때마다 로봇에서 그 명령의 원문을 받아 남긴다.

    `/planning_state` 는 action_id 와 목표만 준다. **무슨 파라미터로 보냈는지**
    (route_coordinates, detour_tolerance 등)는 `/chassis/moves/{id}` 에만 있다.
    """
    # 2026-09-17 수정 - 여기에 try 가 없어서 _get_json 이 던지는 예외(404/타임아웃)가
    # while 을 뚫고 나가 스레드가 죽었다. 첫 실패 이후 전부 유실되어
    # LG 현장 4사이클 moves.jsonl 이 전부 0바이트였다.
    hold = []            # OUT["moves"] 가 열리기 전에 온 것 (사이클 시작 직전)
    while not stop_flag.is_set():
        try:
            try:
                t, aid = move_q.get(timeout=0.5)
            except Exception:
                t = aid = None

            if aid is not None:
                rec = {"t": t, "action_id": aid}
                try:
                    d = _get_json("http://%s:8090/chassis/moves/%s" % (ip, aid), 5)
                    rec["move"] = d if isinstance(d, dict) else None
                    if not isinstance(d, dict):
                        rec["error"] = "응답이 dict 가 아님: %s" % type(d).__name__
                except Exception as e:
                    rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
                hold.append(rec)

            # 파일이 열려 있을 때만 흘려보낸다. 아직이면 최대 200건까지 들고 있는다.
            if OUT.get("moves") is not None:
                for r in hold:
                    w("moves", r)
                del hold[:]
            elif len(hold) > 200:
                del hold[:100]
        except Exception as e:
            # 어떤 일이 있어도 이 스레드는 죽지 않는다.
            try:
                print("  moves 기록 오류(계속 진행): %s" % str(e)[:110], flush=True)
            except Exception:
                pass
            stop_flag.wait(0.5)


def dump_map_context(a, base):
    """맵 원점·해상도·overlays·맵그림·POI 를 한 번 받아 둔다.

    로그의 x, y 는 맵 좌표계인데, 원점과 해상도가 없으면 그 값이 통로 어디인지
    알 수 없다. 주행 중에는 안 바뀌므로 시작할 때 한 번만 받으면 된다.
    """
    out = os.path.join(base, "map_context")
    try:
        os.makedirs(out, exist_ok=True)
    except Exception:
        return
    try:
        cur = _get_json("http://%s:8090/chassis/current-map" % a.ip, 6)
        if not isinstance(cur, dict) or cur.get("id") is None:
            print("맵 정보를 못 받았습니다(계속 진행)", flush=True)
            return
        full = _get_json("http://%s:8090/maps/%s" % (a.ip, cur["id"]), 8)
        full = full if isinstance(full, dict) else {}
        ov = full.pop("overlays", None)
        with io.open(os.path.join(out, "map_meta.json"), "w",
                     encoding="utf-8") as f:
            json.dump({"current_map": cur, "map": full}, f,
                      ensure_ascii=False, indent=2)
        if ov:
            with io.open(os.path.join(out, "map_overlays.json"), "w",
                         encoding="utf-8") as f:
                f.write(ov if isinstance(ov, str)
                        else json.dumps(ov, ensure_ascii=False, indent=2))
        pois = META.get("pois") or []
        if pois:
            with io.open(os.path.join(out, "pois.json"), "w",
                         encoding="utf-8") as f:
                json.dump(pois, f, ensure_ascii=False, indent=2, default=str)
        u = full.get("image_url")
        if u:
            if u.startswith("/"):
                u = "http://%s:8090%s" % (a.ip, u)
            try:
                import urllib.request
                with urllib.request.urlopen(u, timeout=10) as r:
                    io.open(os.path.join(out, "map.png"), "wb").write(r.read())
            except Exception:
                pass
        print("맵 정보 저장 - %s (원점 %s / 해상도 %s m/px) · POI %d개"
              % (full.get("map_name"), full.get("grid_origin_x"),
                 full.get("grid_resolution"), len(pois)), flush=True)
    except Exception as e:
        print("맵 정보 저장 실패(계속 진행): %s" % str(e)[:80], flush=True)


def key_watcher():
    """스페이스바를 누르면 그 시각을 '멈칫' 으로 기록한다.

    v1 의 가장 큰 한계가 "어느 감속이 그 멈칫인지 모른다" 였다.
    사람이 직접 찍어주면 200건을 추측으로 거를 필요가 없다.
    """
    try:
        import msvcrt
    except ImportError:
        return
    while not stop_flag.is_set():
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b'\x03':          # Ctrl+C - getch 가 가로채므로 직접 처리
                    print("  (Ctrl+C - 종료합니다)", flush=True)
                    stop_flag.set()
                    return
                if ch in (b" ", b"\r", b"\n"):
                    now = time.time()
                    with lock:
                        pose = S["pose"]
                        v = S["v"]
                        obs = dict(S["obs"]) if S["obs"] else None
                        fu = dict(S["fused"])
                    marks.append(now)
                    w("events", {"t": now, "kind": "mark", "index": len(marks),
                                 "v": v,
                                 "x": round(pose[0], 3) if pose else None,
                                 "y": round(pose[1], 3) if pose else None,
                                 "front_m": (obs or {}).get("front_m"),
                                 "n_band": (obs or {}).get("n_band"),
                                 "suggested_speed": fu.get("suggested_speed")})
                    print("  [표시 %d] %s  속도 %s  앞 %s"
                          % (len(marks), time.strftime("%H:%M:%S"), v,
                             (obs or {}).get("front_m")), flush=True)
        except Exception:
            pass
        time.sleep(0.05)


# ==================================================================
#  스냅샷 - 이벤트 앞뒤 window 초의 원본 점군 + 코스트맵
# ==================================================================
pending_snaps = []

# 로봇 응답에서 우리가 몰랐던 필드를 처음 본 순간 한 번만 기록한다.
_seen_fused_keys = set(FUSED_KEYS)
_sug_snap = {"n": 0, "last": 0.0}


def _note_new_fused_keys(raw, now):
    """`/fused_sensor_state` 에 FUSED_KEYS 말고 뭐가 더 오는지 남긴다.

    2026-09-19: "로봇이 더 보내주는 게 있나?" 를 매번 로봇에 붙어서
    확인할 수 없으니, 로그 자체가 답하게 한다. 새 필드가 보이면
    events.jsonl 에 한 번 찍고 화면에도 띄운다. 없으면 없다고 확정된다.
    """
    new = [k for k in raw if k not in _seen_fused_keys]
    if not new:
        return
    print("  ★ /fused_sensor_state 에 몰랐던 필드: %s" % ", ".join(sorted(new)),
          flush=True)
    # 사이클이 아직 안 열렸으면 events 파일이 없다. 그때는 '봤다' 표시를
    # 하지 않고 다음 사이클에서 다시 기록되게 둔다.
    if OUT.get("events") is None:
        return
    _seen_fused_keys.update(new)
    w("events", {"t": now, "kind": "fused_new_keys", "keys": sorted(new),
                 "sample": {k: raw.get(k) for k in sorted(new)}})


def _maybe_snap_on_suggest(sug, now, a=None):
    """로봇이 제안속도를 확 떨군 순간의 원본 점군을 예약한다.

    같은 감속 구간에서 수십 번 발행되므로 쿨다운을 둔다.
    """
    a = a or ARGS[0]
    if a is None or not CY["active"] or sug is None:
        return
    if sug >= a.sug_snap:
        return
    if now - _sug_snap["last"] < a.sug_snap_gap:
        return
    if _sug_snap["n"] >= a.max_snaps:
        return
    _sug_snap["last"] = now
    _sug_snap["n"] += 1
    pending_snaps.append((_sug_snap["n"], now, now + a.snap_window, "_sug"))


def dump_snapshot(idx, t_center, window, outdir, tag=""):
    with lock:
        sel = [(t, p) for (t, p) in scans if abs(t - t_center) <= window]
        cms = [(t, d) for (t, d) in costmaps if abs(t - t_center) <= window]
    if not sel and not cms:
        return
    if not outdir:
        return
    path = os.path.join(outdir, "evt_%03d%s_t%.0f.json" % (idx, tag, t_center))
    try:
        out = []
        for (t, p) in sel:
            pts = []
            for q in p:
                a, b = _xy(q)
                if a is not None and b is not None:
                    pts.append([round(float(a), 3), round(float(b), 3)])
            out.append({"t": t, "n": len(pts), "points": pts})
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump({"event_index": idx, "t_center": t_center,
                       "window": window, "scans": out,
                       "costmaps": [{"t": t, **d} for (t, d) in cms[-3:]]},
                      f, ensure_ascii=False)
        snaps_n[0] += 1
    except Exception as e:
        print("  스냅샷 실패: %s" % e, flush=True)


# ==================================================================
#  사이클 = 충전소 출발 -> 충전소 복귀
# ==================================================================
CY = {"active": False, "idx": 0, "dir": None, "t0": 0.0,
      "dist": 0.0, "dist_route": 0.0, "plan_seen": 0, "last_pose": None,
      "dist_poi": None}


def _costmap_cover():
    """지금 받고 있는 코스트맵이 전방으로 몇 m 를 덮는지."""
    out = {}
    with lock:
        pair = (("1cm", S["costmap1"]), ("5cm", S["costmap5"]))
    for name, got in pair:
        if not got:
            out[name] = None
            continue
        cm = got[1]
        try:
            sz, res = cm["size"], float(cm["resolution"])
            out[name] = {"size": sz, "resolution": res,
                         "span_m": round(min(sz) * res, 2),
                         "forward_m": round(min(sz) * res / 2.0, 2)}
        except Exception:
            out[name] = None
    return out


def open_cycle(a, base, idx, home):
    outdir = os.path.join(base, "c%02d_%s" % (idx, time.strftime("%H%M%S")))
    os.makedirs(os.path.join(outdir, "snapshots"), exist_ok=True)
    for key, fn in (("motion", "motion.jsonl"), ("obstacle", "obstacle.jsonl"),
                    ("comm", "comm.jsonl"), ("events", "events.jsonl"),
                    ("sensor", "sensor.jsonl"), ("plan", "plan.jsonl"),
                    ("slam", "slam.jsonl"),
                    ("costmap", "costmap.jsonl"),
                    ("costmap5", "costmap5.jsonl"),
                    ("moves", "moves.jsonl")):
        OUT[key] = io.open(os.path.join(outdir, fn), "w", encoding="utf-8")
    events_n[0] = 0
    snaps_n[0] = 0
    _sug_snap["n"] = 0
    _sug_snap["last"] = 0.0
    del pending_snaps[:]
    del marks[:]
    CY.update({"active": True, "idx": idx, "dir": outdir, "t0": time.time(),
           "dist": 0.0, "dist_route": 0.0, "plan_seen": 0, "last_pose": None,
      "dist_poi": None})
    with lock:
        fp = S["footprint"]
        half = S["half_w"]
        front = S["front_ext"]
    with io.open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({
            "cycle": idx, "started_at": CY["t0"],
            "started_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "robot_ip": a.ip, "home": home,
            "leave_r": a.leave_r, "arrive_r": a.arrive_r,
            "server_safety_settings": CFG,
            "band_half_w_used": band_half_now(),
            "front_extent_used": front or a.front,
            "footprint_half_w": half, "footprint": fp,
            "self_clearance": SELF_CLEARANCE_M, "scan_far_m": SCAN_FAR_M,
            "robot_params": META.get("robot_params"),
            "poi_count": len(META.get("pois") or []),
            "topics": active_topics(),
            "map_context": "../map_context (맵 원점·해상도·overlays·POI)",
            # 코스트맵이 전방으로 실제 덮는 거리. 이걸 안 남기면 나중에
            # "정면이 비었다"가 어디까지 유효한 말인지 알 수 없다.
            "costmap_cover": _costmap_cover(),
            "backend_log": find_backend_log(),
            "script_version": "v5 (2026-09-19)",
        }, f, ensure_ascii=False, indent=2)
    print("")
    print(">> 사이클 %02d 시작 - %s" % (idx, os.path.basename(outdir)), flush=True)
    return outdir


def close_cycle(a, reason):
    if not CY["active"]:
        return
    outdir = CY["dir"]
    snapdir = os.path.join(outdir, "snapshots")
    for it in list(pending_snaps):
        dump_snapshot(it[0], it[1], a.snap_window, snapdir, it[3] if len(it) > 3 else "")
    del pending_snaps[:]
    for f in list(OUT.values()):
        try:
            f.close()
        except Exception:
            pass
    OUT.clear()
    t1 = time.time()
    evs = []
    try:
        with io.open(os.path.join(outdir, "events.jsonl"), "r",
                     encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    evs.append(json.loads(line))
    except Exception:
        pass
    n_bk, n_sf = slice_backend_log(outdir, CY["t0"], t1)
    write_summary(outdir, a, CY["t0"], t1, evs, n_bk, n_sf, CY["idx"], reason)
    dec = len([e for e in evs if e.get("kind") == "decel"])
    mk = len([e for e in evs if e.get("kind") == "mark"])
    print("<< 사이클 %02d 종료(%s) - %.0f초 · 감속 %d건 · 표시 %d건 · 스냅샷 %d"
          % (CY["idx"], reason, t1 - CY["t0"], dec, mk, snaps_n[0]), flush=True)
    CY.update({"active": False, "dir": None})


def cycle_watch(a, base, home):
    """충전소 반경을 드나드는 것을 보고 사이클을 열고 닫는다."""
    hx, hy = home
    n = 0
    was_far = None
    while not stop_flag.is_set():
        stop_flag.wait(0.3)
        with lock:
            pose = S["pose"]
        if pose is None:
            continue
        d = math.hypot(pose[0] - hx, pose[1] - hy)
        far = d > a.leave_r
        near = d < a.arrive_r
        if was_far is None:
            was_far = far
            if far:
                print("  (지금 충전소 밖 %.2fm - 복귀부터 기다립니다)" % d, flush=True)
            continue
        if not CY["active"]:
            if far and not was_far:
                n += 1
                open_cycle(a, base, n, {"x": hx, "y": hy})
        else:
            if near:
                close_cycle(a, "충전소 복귀")
                if a.cycles and CY["idx"] >= a.cycles:
                    print("  요청한 사이클 수(%d) 도달 - 종료합니다" % a.cycles,
                          flush=True)
                    stop_flag.set()
                    return
            elif time.time() - CY["t0"] > a.max_cycle_sec:
                close_cycle(a, "시간 초과(복귀 못 함)")
        was_far = far


# ==================================================================
#  10 Hz 샘플러 + 감속 구간 판정
# ==================================================================
def _who(cap_start, cap_end):
    if (cap_start is not None and cap_end is not None
            and abs(cap_start - cap_end) > 0.005):
        return "서버(상한 %.2f->%.2f)" % (cap_start, cap_end)
    if cap_end is not None:
        return "로봇 자체(상한 %.2f)" % cap_end
    return "로봇 자체(상한 미상)"


def _new_seg(now, v, cap, obs, fu, plan, pose=None):
    return {"t_start": now, "t_end": now, "v_peak": v, "v_min": v,
            "peak_acc": 0.0, "cap_start": cap, "obs_start": obs,
            "sug_start": (fu or {}).get("suggested_speed"),
            "rem_start": (plan or {}).get("remaining_distance"),
            # t_onset = 마지막으로 최고속이었던 시각 = 진짜 급감 시작.
            # t_start 는 구간 시작이라 최고속을 오래 유지하면 최대 20초까지
            # 앞서 버린다(2026-09-17 LG 로그에서 최대 11초 차이 확인).
            "t_onset": now,
            "ang_max": 0.0,
            "ori_start": (pose[2] if pose else None),
            "cap_changed": False,
            "cm_onset": None}


POI_BANDS = ((0.0, 1.0, "경유지 1m 이내"), (1.0, 2.0, "1~2m"),
             (2.0, 4.0, "2~4m"), (4.0, 1e9, "4m 밖"))


def _poi_band(d):
    for lo, hi, lab in POI_BANDS:
        if lo <= d < hi:
            return lab
    return POI_BANDS[-1][2]


def _pick_active_map_pois(pois):
    """`/api/map/active-pois` 가 여러 맵의 POI 를 한꺼번에 준다.

    2026-09-17 LG 로그: 34개 중 14개가 이름 중복이고, map_id 27(구)과
    30(현)이 섞여 있었다. `W5` 가 37 m 떨어진 두 곳에 존재해서 요약의
    "위치" 칸이 엉뚱한 POI 를 가리켰다. 로봇이 쓰는 맵 하나로 좁힌다.

    판단 기준은 '로봇의 현재 위치에서 가장 가까운 POI 를 가진 map_id'.
    map_id 가 없으면 원본 그대로 돌려준다(예전 동작 유지).
    """
    if not isinstance(pois, list) or not pois:
        return pois
    ids = set(p.get("map_id") for p in pois if p.get("map_id") is not None)
    if len(ids) <= 1:
        return pois
    with lock:
        pose = S["pose"]
    if not pose:
        return pois
    best_id, best_d = None, 1e9
    for p in pois:
        px, py = p.get("world_x"), p.get("world_y")
        if px is None or py is None:
            continue
        d = math.hypot(px - pose[0], py - pose[1])
        if d < best_d:
            best_id, best_d = p.get("map_id"), d
    if best_id is None:
        return pois
    kept = [p for p in pois if p.get("map_id") == best_id]
    print("  ! POI 가 %d개 맵에 섞여 있습니다(map_id %s)."
          " 로봇에 가장 가까운 map_id=%s 만 씁니다 (%d/%d개)."
          % (len(ids), sorted(str(i) for i in ids), best_id, len(kept), len(pois)))
    return kept or pois


def nearest_poi(pois, x, y):
    best, bd = None, 1e9
    for p in pois or []:
        px, py = p.get("world_x"), p.get("world_y")
        if px is None or py is None:
            continue
        dd = math.hypot(px - x, py - y)
        if dd < bd:
            best, bd = p, dd
    if best is None:
        return None, None, None
    return "%s(%.1fm)" % (best.get("name") or "?", bd), best.get("poi_type"), round(bd, 2)


def _spawn(fn, *args):
    """죽으면 다시 살아나는 스레드.

    2026-09-18 LG 현장: sampler 가 첫 샘플 직후 TypeError 로 조용히 죽어
    5사이클 전부 motion.jsonl 이 비었다. 예외는 stderr 로만 흘러가 아무도
    못 봤고, 측정 자체가 통째로 날아갔다. 기록 도구가 한 줄 버그로 전멸하면
    안 된다. 예외를 눈에 보이게 찍고 스레드를 되살린다.
    """
    def run():
        while not stop_flag.is_set():
            try:
                fn(*args)
                return                      # 정상 종료
            except Exception as e:
                print("  ★ %s 스레드 예외 - 되살립니다: %s: %s"
                      % (fn.__name__, type(e).__name__, e), flush=True)
                traceback.print_exc()
                time.sleep(0.5)
    threading.Thread(target=run, daemon=True).start()


def sampler(a):
    cur = None
    pois = META.get("pois") or []
    while not stop_flag.is_set():
        now = time.time()
        with lock:
            v, acc, ang = S["v"], S["acc"], S["ang"]
            pose, cap = S["pose"], S["cap"]
            plan = dict(S["plan"])
            slam = dict(S["slam"])
            fu = dict(S["fused"])
            obs = dict(S["obs"]) if S["obs"] else None
            scan_lag = S["scan_lag"]
            gaps = list(ws_gaps)[-10:]
            bat = dict(S["battery"])
            plan_t, fused_t = S["plan_t"], S["fused_t"]

        if CY["active"]:
            # 주행거리 적산 - 요약에서 "100 m 당 몇 건"을 내기 위한 분모.
            # 2026-09-16 에 절대 건수(3건)만 보고 "적다"고 판단했다가
            # 거리로 나눠 보니 100m당 24.8건이었던 일이 있다. 분모를 남긴다.
            lp = CY["last_pose"]
            if pose and lp:
                step = math.hypot(pose[0] - lp[0], pose[1] - lp[1])
                if step < 1.0:
                    CY["dist"] += step
                    if plan.get("action_type") == "along_given_route":
                        CY["dist_route"] += step
                    # 경유지 거리대별 '분모'. 건수만 세면 POI 가 촘촘한 곳이
                    # 무조건 많아 보인다. 거리로 나눠야 비교가 된다.
                    _n, _t, pdist = nearest_poi(pois, pose[0], pose[1])
                    if pdist is not None:
                        # 2026-09-19: open_cycle 이 dist_poi 를 None 으로 두는데
                        # 여기서 바로 첨자 접근을 해 TypeError 로 sampler 가
                        # 죽었다. 2026-09-18 LG 현장 5사이클의 motion.jsonl 이
                        # 통째로 비었던 원인(c01 1줄, c02~c05 0줄). 여기서 만든다.
                        if CY["dist_poi"] is None:
                            CY["dist_poi"] = collections.OrderedDict(
                                (b[2], 0.0) for b in POI_BANDS)
                        CY["dist_poi"][_poi_band(pdist)] += step
            if pose:
                CY["last_pose"] = pose
            if plan_t and now - plan_t < 1.0:
                CY["plan_seen"] += 1
            w("motion", {
                "t": now, "v": v, "acc": acc, "ang": ang,
                "x": round(pose[0], 3) if pose else None,
                "y": round(pose[1], 3) if pose else None,
                "ori": round(pose[2], 4) if pose else None,
                "cap": cap,
                "sug": fu.get("suggested_speed"),
                "accel_ok": fu.get("accelerability"),
                "collide_dir": fu.get("collide_dir"),
                "pushed": fu.get("pushed"), "slipping": fu.get("slipping"),
                "move": plan.get("move_state"), "stuck": plan.get("stuck_state"),
                "rem": plan.get("remaining_distance"),
                "action": plan.get("action_type"),
                "vb": plan.get("viewport_blocked"),
                "pq": slam.get("position_quality"),
                "lms": slam.get("lidar_matching_score"),
                "wslip": slam.get("wheel_slipping"),
                "front_m": (obs or {}).get("front_m"),
                "n_band": (obs or {}).get("n_band"),
                "scan_lag": scan_lag,
                "ws_gap": round(max(gaps), 3) if gaps else None,
                "bat": bat.get("pct"),
                # 이 값들이 얼마나 낡았는지. 크면 rem/sug 를 믿으면 안 된다.
                "plan_age": round(now - plan_t, 2) if plan_t else None,
                "fused_age": round(now - fused_t, 2) if fused_t else None,
            })

        if v is not None and CY["active"]:
            if cur is None:
                cur = _new_seg(now, v, cap, obs, fu, plan, pose)
            elif v > cur["v_min"] + RISE_END or (now - cur["t_start"]) > 20:
                drop = cur["v_peak"] - cur["v_min"]
                if drop > 0.0:
                    events_n[0] += 1
                    idx = events_n[0]
                    dur = max(1e-3, cur["t_end"] - cur["t_start"])
                    pn, pt, pd = (nearest_poi(pois, pose[0], pose[1])
                                  if pose else (None, None, None))
                    nb = (obs or {}).get("n_band")
                    # --- 판정 근거를 이벤트에 같이 박는다 -------------------
                    # 2026-09-16 사무실 측정에서 "전부 코너 부근이라 정상 코너
                    # 감속" 이라고 각속도도 안 보고 단정했다가, 실제로는
                    # 방향변화 0.6~3.4도(직진)였던 일이 있다. 라벨만 남기면
                    # 같은 실수가 반복되므로 근거 숫자를 항상 함께 남긴다.
                    dori = None
                    if pose and cur.get("ori_start") is not None:
                        dori = abs(math.degrees(math.atan2(
                            math.sin(pose[2] - cur["ori_start"]),
                            math.cos(pose[2] - cur["ori_start"]))))
                    cm1, cm5 = (cur.get("cm_onset") or (None, None))
                    clr1 = clr5 = None
                    if pose:
                        try:
                            if cm1:
                                clr1 = costmap_clearance(cm1[1], pose[0], pose[1],
                                                         pose[2])
                            if cm5:
                                clr5 = costmap_clearance(cm5[1], pose[0], pose[1],
                                                         pose[2], fwd_m=4.0)
                        except Exception:
                            pass
                    ev = {
                        "t": cur["t_end"], "kind": "decel", "index": idx,
                        "cycle": CY["idx"], "t_start": cur["t_start"],
                        "t_onset": round(cur["t_onset"], 3),
                        "onset_lead": round(cur["t_end"] - cur["t_onset"], 2),
                        "ang_max": round(cur["ang_max"], 3),
                        "heading_deg": round(dori, 1) if dori is not None else None,
                        "cap_changed": cur["cap_changed"],
                        "clear1": clr1, "clear5": clr5,
                        "v_from": round(cur["v_peak"], 3),
                        "v_to": round(cur["v_min"], 3),
                        "drop": round(drop, 3), "dur": round(dur, 3),
                        "decel_avg": round(drop / dur, 3),
                        "decel_peak": round(abs(cur["peak_acc"]), 3),
                        "stop": cur["v_min"] <= 0.02,
                        "cap_start": cur["cap_start"], "cap_end": cap,
                        "who": _who(cur["cap_start"], cap),
                        "sug_start": cur["sug_start"],
                        "sug_end": fu.get("suggested_speed"),
                        "rem_start": cur["rem_start"],
                        "rem_end": plan.get("remaining_distance"),
                        "move": plan.get("move_state"),
                        "stuck": plan.get("stuck_state"),
                        "action": plan.get("action_type"),
                        "viewport_blocked": plan.get("viewport_blocked"),
                        "fail_reason": plan.get("fail_reason"),
                        "collide_dir": fu.get("collide_dir"),
                        "pushed": fu.get("pushed"), "slipping": fu.get("slipping"),
                        "position_quality": slam.get("position_quality"),
                        "lidar_matching_score": slam.get("lidar_matching_score"),
                        "wheel_slipping": slam.get("wheel_slipping"),
                        "x": round(pose[0], 3) if pose else None,
                        "y": round(pose[1], 3) if pose else None,
                        "poi": pn, "poi_type": pt, "poi_dist": pd,
                        "front_m_start": (cur["obs_start"] or {}).get("front_m"),
                        "front_m_end": (obs or {}).get("front_m"),
                        "n_band_end": nb,
                        "band_half_w": (obs or {}).get("half_w"),
                        "scan_lag": scan_lag,
                        "ws_gap_max": round(max(gaps), 3) if gaps else None,
                        "plan_age": round(now - plan_t, 2) if plan_t else None,
                        "fused_age": round(now - fused_t, 2) if fused_t else None,
                    }
                    w("events", ev)
                    if drop >= a.snap_min_drop and snaps_n[0] < a.max_snaps:
                        pending_snaps.append((idx, ev["t"], now + a.snap_window, ""))
                    if drop >= a.show:
                        fm = ev["front_m_end"]
                        print("  %s  %.2f->%.2f  낙폭 %.2f  피크 %.2f  앞 %s  남은 %s  %s  %s"
                              % (time.strftime("%H:%M:%S"), ev["v_from"],
                                 ev["v_to"], ev["drop"], ev["decel_peak"],
                                 ("%.2fm" % fm) if fm is not None else ("빔" if nb == 0 else "-"),
                                 ev["rem_end"], ev["who"], pn or ""), flush=True)
                cur = _new_seg(now, v, cap, obs, fu, plan, pose)
            else:
                if v >= cur["v_peak"]:
                    # 아직 최고속을 유지/갱신 중 - 급감은 여기부터 센다
                    cur["v_peak"] = v
                    cur["t_onset"] = now
                    cur["ori_start"] = pose[2] if pose else cur["ori_start"]
                    cur["ang_max"] = 0.0
                    cur["cm_onset"] = None
                if cur["cm_onset"] is None:
                    with lock:
                        c1 = S["costmap1"]
                        c5 = S["costmap5"]
                    cur["cm_onset"] = (c1, c5)
                if ang is not None and abs(ang) > cur["ang_max"]:
                    cur["ang_max"] = abs(ang)
                if cap is not None and cur["cap_start"] is not None \
                        and abs(cap - cur["cap_start"]) > 0.005:
                    cur["cap_changed"] = True
                if v < cur["v_min"]:
                    cur["v_min"] = v
                    cur["t_end"] = now
                if acc is not None and acc < cur["peak_acc"]:
                    cur["peak_acc"] = acc
        elif not CY["active"]:
            cur = None

        time.sleep(0.1)


_mark_done = set()


def snap_writer(a):
    """스냅샷(원본 점군) 전담 스레드.

    2026-09-19 분리. 종전에는 이 블록이 sampler 안에 있었다.
    2026-09-18 LG 현장에서 sampler 가 죽자 10Hz 기록과 **스냅샷이 같이**
    날아갔다(5사이클 전부 0개). 원인 규명에 제일 필요한 원본 점군이
    없어져서 "로봇 자체 감속 18회"를 하나도 못 짚었다.
    기록 경로를 서로 독립시켜 한쪽이 죽어도 다른 쪽은 남게 한다.
    """
    while not stop_flag.is_set():
        now = time.time()
        if CY["active"] and CY["dir"]:
            snapdir = os.path.join(CY["dir"], "snapshots")
            for it in list(pending_snaps):
                if now >= it[2]:
                    try:
                        pending_snaps.remove(it)
                    except ValueError:
                        continue
                    dump_snapshot(it[0], it[1], a.snap_window, snapdir, it[3])
            # 사용자가 찍은 표시도 스냅샷을 남긴다
            for mt in list(marks):
                if now - mt > a.snap_window and mt not in _mark_done:
                    _mark_done.add(mt)
                    if snaps_n[0] < a.max_snaps + 20:
                        try:
                            idx = marks.index(mt) + 1
                        except ValueError:
                            idx = 0
                        dump_snapshot(idx, mt, a.snap_window, snapdir, "_mark")
        time.sleep(0.1)


# ==================================================================
#  백엔드 로그 잘라 담기 + 요약
# ==================================================================
def backend_log_candidates():
    """backend.log 가 있을 만한 곳. log_config.json 의 filename 이 상대경로라
    '백엔드를 띄운 작업 디렉터리' 에 생긴다. 서버 노트북에 저장소 사본이
    둘 있으면(RCS_LGIT 실행 / RCS_LG_GIT 업로드) 엉뚱한 쪽을 보게 된다."""
    out = []
    if ARGS and getattr(ARGS, "backend_log", ""):
        out.append(ARGS.backend_log)
    out.append(DEFAULT_BACKEND_LOG)
    out.append(os.path.join(os.getcwd(), "_logs", "backend.log"))
    out.append(os.path.join(os.getcwd(), "BackEnd", "_logs", "backend.log"))
    parent = os.path.dirname(_ROOT)
    try:
        for name in sorted(os.listdir(parent)):
            p = os.path.join(parent, name, "BackEnd", "_logs", "backend.log")
            if p not in out:
                out.append(p)
    except Exception:
        pass
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def find_backend_log(verbose=False):
    """후보 중 '가장 최근에 쓰인' 것을 고른다. 사용자가 명시하면 그것만 쓴다."""
    if ARGS and getattr(ARGS, "backend_log", ""):
        return ARGS.backend_log
    best, best_m = None, 0
    for p in backend_log_candidates():
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                m = os.path.getmtime(p)
                if m > best_m:
                    best, best_m = p, m
        except Exception:
            continue
    if verbose:
        print("  백엔드 로그 후보:")
        for p in backend_log_candidates():
            ok = os.path.exists(p)
            mark = "선택" if p == best else ("있음" if ok else "없음")
            extra = ""
            if ok:
                try:
                    extra = " (%d bytes, 마지막 기록 %s)" % (
                        os.path.getsize(p),
                        time.strftime("%m-%d %H:%M:%S",
                                      time.localtime(os.path.getmtime(p))))
                except Exception:
                    pass
            print("    [%s] %s%s" % (mark, p, extra))
        if best and time.time() - best_m > 300:
            print("    ! 마지막 기록이 5분 이상 과거입니다. 백엔드가 다른 폴더에서"
                  " 돌고 있을 수 있습니다 - `--backend-log` 로 지정하세요.")
        if not best:
            print("    ! 못 찾았습니다. 서버가 무엇을 했는지 확인할 수 없습니다.")
    return best


def slice_backend_log(outdir, t0, t1):
    """backend.log 에서 이 사이클 시간대만 잘라 온다.

    형식: "09-10 22:47:50  INFO  uvicorn.access  ..."  (연도가 없다)
    연도는 측정 시작 시점의 연도로 본다.
    """
    src_path = find_backend_log()
    if not src_path or not os.path.exists(src_path):
        try:
            with io.open(os.path.join(outdir, "safety_slice.log"), "w",
                         encoding="utf-8") as f:
                f.write("# backend.log 를 못 찾았습니다.\n")
                f.write("# 찾아본 곳:\n")
                for p in backend_log_candidates():
                    f.write("#   %s\n" % p)
                f.write("# --backend-log 로 실제 경로를 지정하세요.\n")
                f.write("# 백엔드를 띄운 폴더의 _logs/backend.log 입니다"
                        " (log_config.json 의 filename 이 상대경로).\n")
        except Exception:
            pass
        return 0, 0
    year = time.localtime(t0).tm_year
    n_all = n_safety = 0
    n_lines = n_parsed = 0
    first_st = last_st = None
    try:
        fa = io.open(os.path.join(outdir, "backend_slice.log"), "w",
                     encoding="utf-8")
        fs = io.open(os.path.join(outdir, "safety_slice.log"), "w",
                     encoding="utf-8")
        with io.open(src_path, "r", encoding="utf-8", errors="replace") as src:
            for line in src:
                n_lines += 1
                head = line[:14]
                try:
                    st = time.mktime(time.strptime(
                        "%d-%s" % (year, head), "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    continue
                n_parsed += 1
                if first_st is None:
                    first_st = st
                last_st = st
                if t0 - 2 <= st <= t1 + 2:
                    fa.write(line)
                    n_all += 1
                    if "[safety]" in line:
                        fs.write(line)
                        n_safety += 1
        # 0줄이면 왜 0줄인지를 파일에 남긴다. 빈 파일만 남으면 원인을 알 수 없다.
        # 2026-09-17 LG 현장에서 4사이클 내내 0바이트였는데 이유를 못 밝혔다.
        if n_all == 0:
            def _fmt(x):
                return time.strftime("%m-%d %H:%M:%S", time.localtime(x)) if x else "?"
            msg = [
                "# 이 사이클 시간대에 해당하는 backend.log 줄이 0개입니다.",
                "# 원본      : %s" % src_path,
                "# 원본 크기 : %d bytes / %d줄 (시각 해독 성공 %d줄)"
                % (os.path.getsize(src_path), n_lines, n_parsed),
                "# 원본 시각 : %s ~ %s" % (_fmt(first_st), _fmt(last_st)),
                "# 사이클    : %s ~ %s" % (_fmt(t0), _fmt(t1)),
                "#",
                "# 해독 0줄이면 로그 형식이 '%m-%d %H:%M:%S' 가 아닙니다.",
                "# 시각대가 안 겹치면 백엔드가 다른 폴더에서 돌고 있습니다.",
                "#   log_config.json 의 filename 이 상대경로라 '백엔드를 띄운'",
                "#   작업 디렉터리에 _logs/backend.log 가 생깁니다.",
            ]
            for f in (fa, fs):
                f.write("\n".join(msg) + "\n")
            print("  ! 백엔드 로그 0줄 - safety_slice.log 에 이유를 적었습니다.",
                  flush=True)
        fa.close()
        fs.close()
    except Exception as e:
        print("  백엔드 로그 추출 실패(무시): %s" % e, flush=True)
    return n_all, n_safety


def _classify(e):
    """감속 하나를 성격별로 분류한다.

    판정 순서는 "배제하기 쉬운 것부터". 마지막에 남는 것이 진짜 미제다.

    2026-09-17 개정 — 그전 판정이 틀렸던 이유 2가지를 막는다.
      1) 선회를 안 걸렀다. 사람이 로그를 보고 "코너 부근"이라 넘겨짚었을 뿐
         각속도를 안 봤다. -> ang_max / heading_deg 로 기계가 거른다.
      2) 로봇 자신이 본 장애물을 안 봤다. 2D 스캔이 비어 있어도 로봇
         코스트맵에는 통로가 막혀 있는 경우가 실제로 있었다(LG 현장 18%).
         -> clear1 / clear5 로 거른다.
    """
    if e.get("cap_changed") or str(e.get("who", "")).startswith("서버"):
        return "서버가 상한을 내림"

    rem = e.get("rem_end")
    age = e.get("plan_age")
    stale = age is not None and age > 3.0
    if rem is not None and rem <= 0.6 and not stale:
        return "목표 도착 감속(정상)"
    if rem is not None and rem <= 0.6 and stale:
        # 초를 라벨에 넣으면 1초마다 다른 항목이 되어 요약이 50줄로 터진다
        # (2026-09-17 c02 실제 사례). 초는 상세 표에만 둔다.
        return "도착 감속으로 보이나 값이 낡음 ?"

    ang = e.get("ang_max")
    deg = e.get("heading_deg")
    if (ang is not None and ang > TURN_ANG) or (deg is not None and deg > TURN_DEG):
        return "선회 감속"

    if e.get("stuck") not in (None, "none"):
        return "로봇이 끼임 판단"
    if e.get("viewport_blocked"):
        return "시야 막힘"
    if e.get("pushed") or e.get("slipping") or e.get("wheel_slipping"):
        return "밀림/미끄러짐"
    pq = e.get("position_quality")
    if pq is not None and pq <= 3:
        return "위치추정 불량"

    # 로봇 자신의 코스트맵 - 2D 스캔보다 이쪽을 먼저 믿는다
    half = e.get("footprint_half_w") or 0.44
    for key, lab in (("clear5", "5cm"), ("clear1", "1cm")):
        c = e.get(key)
        if not c:
            continue
        near = [v for v in (c.get("left_m"), c.get("right_m")) if v is not None]
        if near and min(near) < half:
            return "코스트맵: 통로 여유<로봇 반폭(%s)" % lab
        if c.get("ahead_m") is not None:
            return "코스트맵: 정면 %.2fm 에 장애물(%s)" % (c["ahead_m"], lab)

    nb = e.get("n_band_end")
    if nb is None:
        return "스캔 미수신"
    fm = e.get("front_m_end")
    if nb and fm is not None and fm <= 1.0:
        return "앞 1m 안에 장애물"
    if nb:
        return "앞 1m 밖 장애물"

    # 여기까지 왔으면 아무것도 설명하지 못한 것이다. 숨기지 않는다.
    cov = None
    for key in ("clear5", "clear1"):
        c = e.get(key)
        if c and c.get("cover_m"):
            cov = max(cov or 0, c["cover_m"])
    if cov:
        return "★ 미설명 (정면 %.1fm 까지 비어 있음)" % cov
    return "★ 미설명 (코스트맵 없음 - 전방 확인 불가)"


def write_summary(outdir, a, t0, t1, evs, n_bk, n_sf, cyc, reason):
    dec = [e for e in evs if e.get("kind") == "decel"]
    caps = [e for e in evs if e.get("kind") == "cap_change"]
    mks = [e for e in evs if e.get("kind") == "mark"]
    sug = [e for e in evs if e.get("kind") == "suggest_drop"]
    L = []
    L.append("# 사이클 %02d - %s" % (cyc, os.path.basename(outdir)))
    L.append("")
    L.append("주행 1회 = **충전소 출발 -> 충전소 복귀**. 종료 사유: %s" % reason)
    L.append("")
    L.append("- 로봇 `%s` · %s ~ %s (**%.0f초**)"
             % (a.ip, time.strftime("%H:%M:%S", time.localtime(t0)),
                time.strftime("%H:%M:%S", time.localtime(t1)), t1 - t0))
    L.append("- 감속 **%d건** · 로봇 제안속도 하락 **%d회** · 서버 상한변경 **%d회**"
             % (len(dec), len(sug), len(caps)))
    L.append("- **사용자 표시(멈칫) %d건** · 스냅샷 %d개" % (len(mks), snaps_n[0]))
    L.append("- 백엔드 로그 %d줄 (그중 `[safety]` %d줄)" % (n_bk, n_sf))
    bh = [e.get("band_half_w") for e in dec if e.get("band_half_w")]
    L.append("- 밴드 반폭 실제 사용 %s m (서버 설정 %s, 풋프린트 반폭과 큰 값)"
             % (max(bh) if bh else "?", CFG.get("band_half_w")))
    L.append("- 주행거리 **%.1f m** (그중 경유지 경로 %.1f m)"
             % (CY.get("dist", 0.0), CY.get("dist_route", 0.0)))
    L.append("")

    # ------------------------------------------------------------------
    #  맨 위 경고 - 이 사이클을 믿고 분석해도 되는지 먼저 말한다.
    #  2026-09-17: planning_state 가 325초 끊긴 사이클의 요약이
    #  "도착 감속으로 보이나 값이 낡음(276초)" 50줄로 채워져
    #  허수인 줄 모르고 읽을 뻔했다.
    # ------------------------------------------------------------------
    warn = []
    dur_s = max(1.0, t1 - t0)
    seen_ratio = CY.get("plan_seen", 0) / (dur_s * 10.0)
    if seen_ratio < 0.3:
        warn.append("`/planning_state` 수신율 %.0f%% - 남은거리·도착판정을 "
                    "믿으면 안 됩니다. 이 사이클의 원인 분해는 무효입니다."
                    % (seen_ratio * 100))
    if n_bk == 0:
        warn.append("백엔드 로그 0줄 - 서버가 무엇을 했는지 확인할 수 없습니다. "
                    "`--backend-log` 로 실제 경로를 지정하세요.")
    unresolved = [e for e in dec if _classify(e).startswith("★ 미설명")]
    if unresolved:
        warn.append("**원인 미설명 %d건** - 아직 설명하지 못한 감속입니다. "
                    "0건이 될 때까지 분석이 끝난 것이 아닙니다." % len(unresolved))
    cov = [c.get("cover_m") for e in dec
           for c in (e.get("clear5"), e.get("clear1")) if c and c.get("cover_m")]
    if cov:
        warn.append("코스트맵이 덮는 전방 거리 최대 **%.1f m** - "
                    "\"정면이 비었다\"는 이 거리까지만 유효합니다." % max(cov))
    if warn:
        L.append("> ## 먼저 읽을 것")
        for x in warn:
            L.append("> - %s" % x)
        L.append("")

    if mks:
        L.append("## ★ 사용자가 표시한 멈칫 %d건" % len(mks))
        L.append("")
        L.append("| 시각 | 속도 | 앞거리 | 밴드점 | 제안속도 | 가장 가까운 감속 |")
        L.append("|---|---:|---:|---:|---:|---|")
        for m in mks:
            # 사람은 멈칫을 느끼고 나서 누른다. 실측 지연 0.9~4.2초(중앙 1.8초).
            # 그래서 '표시 시각에 가장 가까운 감속'이 아니라
            # '표시 직전에 시작해서 표시 시각 언저리에 바닥을 친 감속'을 찾는다.
            cand = [e for e in dec
                    if e.get("t_onset", e["t"]) <= m["t"] + 0.5
                    and -2.0 <= m["t"] - e["t"] <= 6.0]
            # 후보 중 '가장 크게 떨어진 것'. 시각만으로 고르면 표시 직후의
            # 0.01 짜리 잔물결을 집어 온다(실제로 그랬다).
            near = max(cand, key=lambda e: e["drop"]) if cand else None
            ns = "-"
            if near:
                ns = "시작 %+.1fs · 바닥 %+.1fs  %.2f->%.2f  %s" % (
                    near.get("t_onset", near["t"]) - m["t"], near["t"] - m["t"],
                    near["v_from"], near["v_to"], _classify(near))
            fm = m.get("front_m")
            L.append("| %s | %s | %s | %s | %s | %s |"
                     % (time.strftime("%H:%M:%S", time.localtime(m["t"])),
                        m.get("v"), ("%.2f m" % fm) if fm is not None else "빔",
                        m.get("n_band"), m.get("suggested_speed"), ns))
        L.append("")

    if not dec:
        L.append("> 감속 이벤트가 없습니다. 주행을 안 했거나 데이터가 안 왔습니다.")
    else:
        L.append("## 원인 분해 (추측 아님 - 로봇이 준 값으로 분류)")
        L.append("")
        cnt = collections.Counter(_classify(e) for e in dec)
        km = CY.get("dist", 0.0)
        L.append("| 구분 | 건수 | 비율 | 100m당 |")
        L.append("|---|---:|---:|---:|")
        for k, v in cnt.most_common():
            per = ("%.1f" % (100.0 * v / km)) if km > 1 else "-"
            L.append("| %s | %d | %.0f%% | %s |" % (k, v, 100.0 * v / len(dec), per))
        L.append("")

        # 경유지 거리대역별 정규화 - 건수만 보면 길이가 다른 사이클끼리
        # 비교가 안 된다. 분모(주행거리)를 항상 같이 낸다.
        bc = collections.OrderedDict((b[2], 0) for b in POI_BANDS)
        for e in dec:
            pd = e.get("poi_dist")
            if pd is not None:
                bc[_poi_band(pd)] += 1
        bd = CY.get("dist_poi") or {}
        if sum(bc.values()) and sum(bd.values()) > 1:
            base = None
            L.append("### 경유지(POI) 거리대별 - 주행거리로 정규화")
            L.append("")
            L.append("| 구간 | 주행거리 | 감속 | 100m당 | 배율 |")
            L.append("|---|---:|---:|---:|---:|")
            rows = []
            for _lo, _hi, lab in POI_BANDS:
                d, n = bd.get(lab, 0.0), bc[lab]
                per = (100.0 * n / d) if d > 1 else None
                if lab == POI_BANDS[-1][2] and per:
                    base = per
                rows.append((lab, d, n, per))
            for lab, d, n, per in rows:
                L.append("| %s | %.1f m | %d | %s | %s |"
                         % (lab, d, n,
                            ("%.1f" % per) if per else "-",
                            ("%.2fx" % (per / base)) if (per and base) else "-"))
            L.append("")
            L.append("> 배율은 '경유지에서 먼 구간' 대비. 1.0 에 가까우면"
                     " 경유지와 무관하게 어디서나 나는 감속이다.")
            L.append("")
        st = [e for e in dec if e.get("stop")]
        ds = [e["drop"] for e in dec]
        ps = [e["decel_peak"] for e in dec]
        L.append("- 완전 정지 **%d건**" % len(st))
        L.append("- 속도강하 최대 %.2f / 평균 %.3f m/s" % (max(ds), sum(ds) / len(ds)))
        L.append("- 감속도 피크 최대 %.2f / 평균 %.2f m/s^2"
                 % (max(ps), sum(ps) / len(ps)))
        L.append("")
        if unresolved:
            L.append("## ★ 원인 미설명 %d건 - 여기가 남은 숙제다" % len(unresolved))
            L.append("")
            L.append("| 시각 | 급감시작 | 속도 | 각속도 | 방향변화 | 코스트맵 좌/우 | 정면 | POI | 상한 |")
            L.append("|---|---|---|---:|---:|---|---|---|---:|")
            for e in sorted(unresolved, key=lambda x: -x["drop"])[:20]:
                c = e.get("clear5") or e.get("clear1") or {}
                lr = "%s / %s" % (c.get("left_m"), c.get("right_m"))
                L.append("| %s | %s | %.2f->%.2f | %s | %s | %s | %s m 까지 빔 | %s | %s |"
                         % (time.strftime("%H:%M:%S", time.localtime(e["t"])),
                            ("-%.1fs" % e.get("onset_lead", 0)),
                            e["v_from"], e["v_to"],
                            e.get("ang_max"), e.get("heading_deg"), lr,
                            c.get("cover_m", "?"),
                            (e.get("poi") or "-"), e.get("cap_end")))
            L.append("")

        L.append("## 낙폭 큰 순 상위 15건")
        L.append("")
        L.append("| 시각 | 속도 | 낙폭 | 피크 | 앞거리 | 남은거리 | 제안속도 | 분류 | 위치 |")
        L.append("|---|---|---:|---:|---:|---:|---:|---|---|")
        for e in sorted(dec, key=lambda x: -x["drop"])[:15]:
            fm = e.get("front_m_end")
            nb = e.get("n_band_end")
            fs = ("%.2f m" % fm) if fm is not None else ("빔" if nb == 0 else "-")
            L.append("| %s | %.2f->%.2f | %.2f | %.2f | %s | %s | %s | %s | %s |"
                     % (time.strftime("%H:%M:%S", time.localtime(e["t"])),
                        e["v_from"], e["v_to"], e["drop"], e["decel_peak"],
                        fs, e.get("rem_end"), e.get("sug_end"),
                        _classify(e), (e.get("poi") or "-")))
    L.append("")
    L.append("## 읽는 법")
    L.append("")
    L.append("모든 파일의 `t` 는 같은 기준(측정 PC 의 epoch)이다. `t` 로 맞춰 보면")
    L.append("`motion`(속도) · `sensor`(로봇이 정한 제안속도) · `plan`(남은거리·실패사유) ·")
    L.append("`slam`(위치추정 품질) · `obstacle`(앞에 뭐가 있었나) · `comm`(데이터가 제때 왔나) ·")
    L.append("`safety_slice.log`(서버가 뭘 했나) 를 한 시점으로 붙일 수 있다.")
    L.append("")
    L.append("`정면 비어 있는데 감속 ★` 이 많으면 로봇 내부 판단이 원인이다.")
    L.append("그때 `sensor.jsonl` 의 `suggested_speed` 와 `collide_dir` 을 보면 된다.")
    with io.open(os.path.join(outdir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# ==================================================================
def probe(a):
    """본 측정 전 30초 사전 점검.

    2026-09-17 변경 - 결과를 파일로도 남긴다.
    전에는 화면에만 찍혀서, 나중에 "어떤 토픽이 없다고 나왔었지?" 를
    되짚을 방법이 아예 없었다.
    """
    seen = collections.Counter()
    first = {}

    def _on_msg(_w, msg):
        try:
            m = json.loads(msg)
        except Exception:
            return
        tp = m.get("topic")
        if tp:
            if tp not in seen:
                first[tp] = m
            seen[tp] += 1

    def _on_open(ws):
        for t in TOPICS:
            ws.send(json.dumps({"enable_topic": t}))
            time.sleep(0.04)

    _plog = []
    _real_print = print

    def print(*args, **kw):       # noqa: A001 - 이 함수 안에서만 가린다
        s = " ".join(str(x) for x in args)
        _plog.append(s)
        _real_print(*args, **kw)

    print("사전 점검 30초 - 아무것도 기록하지 않습니다.")
    print("")
    ws = _WS("ws://%s:8090/ws/v2/topics" % a.ip, _on_msg, _on_open,
             lambda _w, e: print("  WS 오류: %s" % str(e)[:110], flush=True))
    threading.Thread(target=ws.run_forever, daemon=True).start()
    t0 = time.time()
    try:
        while time.time() - t0 < 30:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    try:
        ws.close()
    except Exception:
        pass

    NEED = [
        ("/motion_metrics", "fatal", "속도. 없으면 감속을 아예 못 잡는다"),
        ("/tracked_pose", "fatal", "위치. 없으면 사이클 감지도 장애물 계산도 안 된다"),
        ("/fused_sensor_state", "partial",
         "로봇이 정한 제안속도. 없으면 자체 감속 원인을 못 본다"),
        ("/planning_state", "partial", "남은거리·실패사유만 빈다"),
        ("/slam/state", "partial", "위치추정 품질만 빈다"),
        ("/scan_matched_points2", "partial", "장애물 거리가 빈다"),
        ("/maps/1cm/1hz", "partial",
         "충돌 코스트맵이 빈다 (충전 도킹 중이면 원래 안 온다 - 로봇을 떼고 다시)"),
        ("/maps/5cm/1hz", "partial",
         "경로계획 코스트맵이 빈다. 1cm 맵은 전방 1.5m 뿐이라 "
         "이게 없으면 '정면이 비었다'를 1.5m 까지만 말할 수 있다"),
        ("/chassis_state", "partial", "비상정지 여부만 빈다"),
        ("/alerts", "partial", "로봇 경보만 빈다"),
    ]
    fatal, partial = [], []
    print("=" * 76)
    for t, sev, why in NEED:
        n = seen.get(t, 0)
        if n:
            print("  [OK  ] %-26s %5d건 (%.2f Hz)" % (t, n, n / 30.0))
        else:
            print("  [없음] %-26s %5d건" % (t, n))
            (fatal if sev == "fatal" else partial).append((t, why))
    extra = [t for t in seen if t not in [x[0] for x in NEED]]
    if extra:
        print("")
        print("  그 외 수신: %s" % ", ".join(sorted(extra)[:12]))

    print("")
    sc = first.get("/scan_matched_points2")
    if sc:
        pts = sc.get("points") or sc.get("data")
        if pts is None:
            partial.append(("스캔 필드명",
                            "points/data 가 아님. 필드: %s"
                            % sorted(k for k in sc if k != "topic")))
        elif pts:
            x, y = _xy(pts[0])
            if x is None:
                partial.append(("점 좌표 형식", "예시: %s" % (pts[0],)))
            else:
                print("  스캔 점 %d개, 좌표 파싱 OK (x=%.3f, y=%.3f)" % (len(pts), x, y))
        else:
            print("  (스캔은 오는데 점이 0개 - 주변이 트여 있으면 정상)")

    home_ok = True
    if a.server:
        try:
            cfg = _get_json(a.server.rstrip("/") + "/api/settings/safety", 5)
            print("  서버 안전설정: yellow %s / red %s / band_half_w %s"
                  % (cfg.get("yellow_m"), cfg.get("red_m"), cfg.get("band_half_w")))
        except Exception as e:
            partial.append(("서버 안전설정 조회", str(e)[:70]))
        try:
            pois = _get_json(a.server.rstrip("/") + "/api/map/active-pois", 5)
            ch = [p for p in pois
                  if str(p.get("poi_type") or "").lower() == "charging"]
            print("  POI %d개 / 충전소 %d개" % (len(pois), len(ch)))
            if not ch:
                home_ok = False
        except Exception as e:
            home_ok = False
            partial.append(("POI 조회", str(e)[:70]))
    else:
        home_ok = False

    print("")
    print("-" * 76)
    if fatal:
        print("  판정: 진행 불가")
        print("")
        for t, why in fatal:
            print("    X  %s" % t)
            print("       %s" % why)
        print("")
        print("  로봇에 제대로 못 붙었습니다. IP·네트워크부터 확인하세요.")
    elif partial:
        print("  판정: 측정은 되지만 일부가 빕니다")
        print("")
        for t, why in partial:
            print("    !  %s" % t)
            print("       %s" % why)
    else:
        print("  판정: 전부 정상 - 본 측정을 진행하세요")
    if not home_ok:
        print("")
        print("  ※ 충전소 기준점을 자동으로 못 잡습니다.")
        print("     --home x,y 로 주시거나, 로봇을 충전소에 둔 채로 시작하세요.")
    print("=" * 76)

    # 화면에 찍은 그대로 + 수신한 토픽 전부를 파일로 남긴다.
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        pp = os.path.join(LOG_DIR, "probe_%s.txt"
                          % time.strftime("%Y%m%d_%H%M%S"))
        with io.open(pp, "w", encoding="utf-8") as f:
            f.write("로봇 %s · %s\n" % (a.ip, time.strftime("%Y-%m-%d %H:%M:%S")))
            f.write("=" * 76 + "\n")
            f.write("\n".join(_plog) + "\n")
            f.write("\n[30초간 수신한 토픽 전부]\n")
            for t, n in seen.most_common():
                f.write("  %-30s %6d건 (%.2f Hz)\n" % (t, n, n / 30.0))
            f.write("\n[구독 요청한 토픽]\n")
            for t in TOPICS:
                f.write("  %-30s %s\n" % (t, "수신" if seen.get(t) else "무수신"))
        _real_print("  점검 결과 저장: %s" % pp)
    except Exception as e:
        _real_print("  점검 결과 저장 실패(무시): %s" % str(e)[:90])
    return 0 if not fatal else 1


def _drop_if_empty(base):
    try:
        if os.path.isdir(base) and not os.listdir(base):
            os.rmdir(base)
            return True
    except Exception:
        pass
    return False


def resolve_ip(a):
    """--ip auto 면 백엔드에서 로봇 목록을 받아 IP 를 고른다."""
    if a.ip.lower() != "auto":
        return a.ip
    if not a.server:
        print("★ --ip auto 는 --server 가 있어야 합니다.")
        return None
    try:
        d = _get_json(a.server.rstrip("/") + "/api/robots?limit=50", 5)
    except Exception as e:
        print("★ 로봇 목록 조회 실패: %s" % str(e)[:100])
        return None
    items = d.get("items") if isinstance(d, dict) else d
    ips = [(r.get("name"), r.get("ip_address")) for r in (items or [])
           if r.get("ip_address")]
    if not ips:
        print("★ IP 가 등록된 로봇이 없습니다. --ip 로 직접 지정하세요.")
        return None
    if len(ips) > 1:
        print("  로봇 %d대 발견: %s" % (len(ips), ", ".join(
            "%s(%s)" % (n or "?", i) for n, i in ips)))
        print("  -> 첫 번째(%s)로 진행합니다. 다른 로봇은 --ip 로 지정하세요." % ips[0][1])
    return ips[0][1]


def find_home(a, pois):
    if a.home:
        try:
            x, y = [float(s) for s in a.home.split(",")]
            return (x, y), "--home 인자"
        except Exception:
            print("  --home 형식이 잘못됐습니다 (예: --home 1.2,-3.4)")
    ch = [p for p in (pois or [])
          if str(p.get("poi_type") or "").lower() == "charging"
          and p.get("world_x") is not None]
    with lock:
        pose = S["pose"]
    if ch:
        if pose:
            ch.sort(key=lambda p: math.hypot(p["world_x"] - pose[0],
                                             p["world_y"] - pose[1]))
        p = ch[0]
        return (float(p["world_x"]), float(p["world_y"])), \
               "충전소 POI '%s'" % (p.get("name") or "?")
    if pose:
        return (pose[0], pose[1]), "지금 로봇 위치(충전소 POI 없음)"
    return None, None


def main():
    global ARGS
    ap = argparse.ArgumentParser(
        description="주행 1회(충전소 출발~복귀)를 폴더 하나로 기록한다 (v2)")
    ap.add_argument("--ip", required=True,
                    help="로봇 IP. auto 로 주면 백엔드에서 찾아온다")
    ap.add_argument("--sec", type=float, default=0.0,
                    help="전체 실행 시간(초). 0 이면 Ctrl+C 까지 계속")
    ap.add_argument("--cycles", type=int, default=0,
                    help="이 횟수만큼 왕복하면 종료. 0 이면 무제한")
    ap.add_argument("--server", default="http://127.0.0.1:8002",
                    help="백엔드 주소. 안 쓰려면 --server \"\"")
    ap.add_argument("--backend-log", default="",
                    help="backend.log 실제 경로 (기본: <저장소>/BackEnd/_logs/backend.log)")
    ap.add_argument("--home", default="",
                    help="기준점 좌표 x,y (안 주면 충전소 POI 자동 탐색)")
    ap.add_argument("--leave-r", type=float, default=1.5)
    ap.add_argument("--arrive-r", type=float, default=1.2)
    ap.add_argument("--max-cycle-sec", type=float, default=1800.0)
    ap.add_argument("--front", type=float, default=ROBOT_FRONT_M,
                    help="풋프린트를 못 받을 때 쓸 앞끝 거리")
    ap.add_argument("--show", type=float, default=0.10,
                    help="화면에 띄울 최소 낙폭 m/s (파일엔 언제나 전부 남는다)")
    ap.add_argument("--snap-window", type=float, default=2.0)
    ap.add_argument("--snap-min-drop", type=float, default=0.15)
    ap.add_argument("--max-snaps", type=int, default=40)
    # 2026-09-19 - 로봇 자체 감속용 스냅샷.
    #   0.25 는 2026-09-18 LG 로그에서 "로봇이 스스로 떨군" 구간의 기준값.
    #   그 18회는 최저가 전부 0.10 이었다.
    ap.add_argument("--sug-snap", type=float, default=0.25,
                    help="로봇 제안속도가 이 값 밑으로 떨어지면 원본 점군 스냅샷")
    ap.add_argument("--sug-snap-gap", type=float, default=5.0,
                    help="제안속도 스냅샷 최소 간격(초)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--probe", action="store_true",
                    help="30초 사전 점검 - 토픽별 수신 여부를 보고 끝낸다")
    ap.add_argument("--light", action="store_true",
                    help="최소 구독(속도·위치·경보만). 로봇 CPU 부하가 원인인지"
                         " 가릴 때 쓴다 - 우리 측정이 부하를 거의 안 보탠다")
    a = ap.parse_args()
    ARGS[0] = a
    ARGS = a

    ip = resolve_ip(a)
    if not ip:
        return 1
    a.ip = ip

    if a.probe:
        return probe(a)

    t_start = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(t_start))
    base = os.path.join(LOG_DIR, "run_%s%s"
                        % (stamp, ("_" + a.tag) if a.tag else ""))
    os.makedirs(base, exist_ok=True)

    print("=" * 76)
    print("[drive_log v2] 로봇 %s" % a.ip)
    print("주행 1회 = 충전소 출발 -> 충전소 복귀. 사이클마다 폴더가 하나씩 생깁니다.")
    print("상위 폴더: %s" % base)
    print("★ 주행 중 멈칫하면 이 창에서 스페이스바를 누르세요 - 그 시각이 기록됩니다.")
    print("=" * 76)

    try:
        META["robot_params"] = _get_json("http://%s:8090/robot-params" % a.ip, 5)
        print("로봇 파라미터 %d개 기록(읽기만)" % len(META["robot_params"]))
    except Exception as e:
        META["robot_params"] = None
        print("로봇 파라미터 조회 실패(계속 진행): %s" % str(e)[:90])

    META["pois"] = []
    if a.server:
        try:
            CFG.update(_get_json(a.server.rstrip("/") + "/api/settings/safety", 5))
            print("서버 안전설정: yellow %s / red %s / band_half_w %s"
                  % (CFG.get("yellow_m"), CFG.get("red_m"), CFG.get("band_half_w")))
        except Exception as e:
            print("서버 안전설정 조회 실패 - 기본값 %s 사용: %s"
                  % (FALLBACK_BAND_HALF_W, str(e)[:70]))
        try:
            # 맵 필터는 로봇 위치를 알아야 하므로 WS 연결 뒤에 건다(아래 참조).
            META["pois"] = _get_json(
                a.server.rstrip("/") + "/api/map/active-pois", 5)
            ch = [p for p in META["pois"]
                  if str(p.get("poi_type") or "").lower() == "charging"]
            print("POI %d개 (충전소 %d개)" % (len(META["pois"]), len(ch)))
        except Exception as e:
            print("POI 조회 실패: %s" % str(e)[:90])

    # 서버가 무엇을 했는지 볼 수 있는지 시작 시점에 확인한다.
    # 2026-09-17 LG 현장에서 4사이클 내내 0바이트였는데 아무 경고가 없었다.
    find_backend_log(verbose=True)

    def on_err(_w, e):
        with lock:
            counters["ws_err"] += 1
        print("  WS 오류: %s" % str(e)[:110], flush=True)

    ws = _WS("ws://%s:8090/ws/v2/topics" % a.ip, on_msg, on_open, on_err)
    threading.Thread(target=ws.run_forever, daemon=True).start()
    _spawn(poll_params, a.ip)
    _spawn(comm_writer)
    _spawn(key_watcher)
    _spawn(move_writer, a.ip)

    print("로봇 위치 수신 대기...", flush=True)
    for _ in range(100):
        with lock:
            if S["pose"] is not None:
                break
        time.sleep(0.2)
    with lock:
        got = S["pose"] is not None
    if not got:
        print("★ 20초 동안 로봇 위치를 못 받았습니다. IP·네트워크를 확인하세요.")
        print("  그래도 계속 기다립니다 (Ctrl+C 로 중단).")

    dump_map_context(a, base)

    # 위치를 받은 뒤에야 "어느 맵의 POI 인지" 를 고를 수 있다.
    # (이 위에서 걸면 pose 가 None 이라 필터가 그냥 통과해 버린다)
    META["pois"] = _pick_active_map_pois(META.get("pois") or [])

    home, how = find_home(a, META["pois"])
    if home is None:
        print("★ 기준점을 정할 수 없습니다. --home x,y 로 직접 주세요.")
        _drop_if_empty(base)
        return 1
    print("기준점(충전소): (%.2f, %.2f)  <- %s" % (home[0], home[1], how))
    print("출발 판정 %.1fm 밖 / 복귀 판정 %.1fm 안" % (a.leave_r, a.arrive_r))
    print("밴드 반폭 %.2f m (서버 설정 기준)" % band_half_now())

    _spawn(sampler, a)
    _spawn(snap_writer, a)
    _spawn(cycle_watch, a, base, home)

    try:
        while not stop_flag.is_set():
            if a.sec and time.time() - t_start >= a.sec:
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("")
        print("(사용자 중단)")
    stop_flag.set()
    time.sleep(0.6)
    if CY["active"]:
        close_cycle(a, "측정 종료")
    try:
        ws.close()
    except Exception:
        pass

    with lock:
        c = dict(counters)
    print("")
    print("=" * 76)
    print("완료. WS %d · 스캔 %d · fused %d · 코스트맵 %d · REST실패 %d · WS오류 %d"
          % (c.get("ws_msg", 0), c.get("scan", 0), c.get("fused", 0),
             c.get("costmap", 0), c.get("rest_fail", 0), c.get("ws_err", 0)))
    if _drop_if_empty(base):
        print("사이클이 하나도 없어 빈 폴더를 지웠습니다.")
    else:
        print("상위 폴더: %s" % base)
    if c.get("ws_msg", 0) == 0:
        print("")
        print("★ WS 메시지가 0건입니다 - 로봇에 못 붙었습니다.")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
