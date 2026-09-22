# -*- coding: utf-8 -*-
"""
RCS LG - "ping 은 되는데 로봇이 온라인이 안 뜬다" 원인 판별 (2026-08-28)

ping(ICMP) 과 HTTP(TCP) 를 같은 조건에서 나란히 재서, 원인을 다음 넷 중 하나로 좁힌다.

  (A) MTU 블랙홀   ping 정상 + 작은 HTTP 정상 + 큰 HTTP 만 실패
                   -> 양쪽 라우터 MTU 를 1400 으로

  (B) 링크 품질    ping 부터 손실이 있음. HTTP 도 같이 흔들림
                   -> 전파 문제. 라우터 위치 / 신호세기(-71dBm 기준)

  (C) TCP 차단     ping 은 100% 인데 작은 HTTP 도 안 됨
                   -> 포워딩(WAN 8090 -> 192.168.39.100:8090) / 방화벽

  (D) 정상         전부 통과 -> 원인은 네트워크 밖 (DB IP / 캐시 / 매핑)

사용법
  백엔드와 관제 화면을 끄고 실행하세요.
  켜져 있으면 온라인 프로브 / 15초짜리 맵 보정 / WS 릴레이가 로봇을 물고 있어
  측정값이 오염됩니다.

      python mtu_check.py
      python mtu_check.py --ip 192.168.39.100      # 로봇 라우터에 직접 붙었을 때
      python mtu_check.py --count 20               # 더 오래 관찰

표준 라이브러리만 씁니다. 인터넷 불필요.
결과는 mtu_check_<날짜시각>.txt 로 같은 폴더에 저장됩니다.
"""

import argparse
import datetime
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_IP = "10.115.244.12"
DEFAULT_SECRET = "19a11878aaab420fba94577ce3620dce"
PORT = 8090

# 관제의 온라인 판정이 쓰는 바로 그 경로(작은 응답)와, LTE 에서 막히던 경로(큰 응답)
SMALL_PATH = "/chassis/current-map"
LARGE_PATH = "/device/info"
SMALL_TIMEOUT = 12
LARGE_TIMEOUT = 20

_lines = []


def out(s=""):
    print(s)
    _lines.append(s)


def head(t):
    out()
    out("===== %s =====" % t)


def _decode(b):
    for enc in ("cp949", "utf-8"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def ping_once(ip, size=None, df=False, wait_ms=2000):
    """ping 1회. (성공여부, 응답시간ms, 조각화필요여부) 를 돌려준다."""
    cmd = ["ping", "-n", "1", "-w", str(wait_ms)]
    if df:
        cmd.append("-f")
    if size is not None:
        cmd += ["-l", str(size)]
    cmd.append(ip)
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except Exception:
        return False, None, False
    text = _decode(p.stdout)
    # TTL= 은 한국어/영어 윈도우 양쪽 출력에 공통으로 나온다
    ok = "TTL=" in text.upper()
    frag = ("fragmented" in text.lower()) or ("조각화" in text)
    ms = None
    m = re.search(r"(?:time|시간)[=<]\s*(\d+)\s*ms", text, re.IGNORECASE)
    if m:
        ms = int(m.group(1))
    elif ok:
        ms = 0  # "<1ms"
    return ok, ms, frag


def http_probe(ip, path, timeout, secret):
    """HTTP GET 1회. (응답왔나, 상태코드, 바이트수, 걸린초) 를 돌려준다.

    상태코드가 4xx 여도 '응답이 온 것'은 True 다.
    관제의 온라인 판정이 상태코드를 보지 않고 응답 유무만 보기 때문에,
    같은 기준으로 재야 화면과 일치한다.
    """
    url = "http://%s:%d%s" % (ip, PORT, path)
    req = urllib.request.Request(url)
    req.add_header("Secret", secret)
    req.add_header("Authorization", "Secret %s" % secret)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return True, r.status, len(body), time.time() - t0
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return True, e.code, len(body), time.time() - t0
    except Exception:
        return False, None, 0, time.time() - t0


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--ip", default=DEFAULT_IP, help="로봇 주소 (기본: 로봇 라우터 WAN)")
    ap.add_argument("--count", type=int, default=10, help="HTTP 반복 횟수")
    ap.add_argument("--pings", type=int, default=20, help="ping 반복 횟수")
    ap.add_argument("--secret", default=DEFAULT_SECRET)
    ap.add_argument("--gap", type=float, default=2.0, help="회차 간 대기(초)")
    a = ap.parse_args()

    out()
    out("###########################################################")
    out("  RCS LG 링크 진단   대상 %s:%d   %s"
        % (a.ip, PORT, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    out("###########################################################")
    out("  백엔드와 관제 화면을 꺼두었는지 확인하세요. 켜져 있으면 측정이 오염됩니다.")

    # ── 1. ping ── 링크 품질의 직접 지표. HTTP 가 흔들리는 이유가 여기 있을 수 있다
    head("1. ping (ICMP) %d회 - 링크 품질" % a.pings)
    ok_n, times = 0, []
    for i in range(1, a.pings + 1):
        ok, ms, _ = ping_once(a.ip)
        if ok:
            ok_n += 1
            if ms is not None:
                times.append(ms)
        mark = "." if ok else "X"
        sys.stdout.write(mark)
        sys.stdout.flush()
        if i % 20 == 0:
            sys.stdout.write("\n")
    if a.pings % 20 != 0:
        sys.stdout.write("\n")
    _lines.append("   " + ("." * ok_n) + ("X" * (a.pings - ok_n)) + "  (. = 성공, X = 실패)")

    loss = 100.0 * (a.pings - ok_n) / a.pings
    out("   성공 %d/%d   손실률 %.0f%%" % (ok_n, a.pings, loss))
    if times:
        jitter = (statistics.pstdev(times) if len(times) > 1 else 0.0)
        out("   응답시간  최소 %dms / 평균 %dms / 최대 %dms / 흔들림(표준편차) %.0fms"
            % (min(times), int(statistics.mean(times)), max(times), jitter))
    out("   ※ ping 은 라우터 자신이 답합니다. 포트포워딩도 로봇도 거치지 않습니다.")

    # ── 2. HTTP ── 작은 요청과 큰 응답을 같은 조건에서 번갈아
    head("2. HTTP 작은 요청 / 큰 응답 %d회" % a.count)
    out("   작은 요청 = %s   (관제 온라인 판정이 쓰는 경로)" % SMALL_PATH)
    out("   큰 응답   = %s          (LTE 에서 막히던 경로)" % LARGE_PATH)
    out()
    out("   %3s  %-24s  %s" % ("#", "작은요청", "큰응답"))
    out("   " + "-" * 62)

    s_ok = l_ok = 0
    s_ms = []
    for i in range(1, a.count + 1):
        so, sc, ssz, ssec = http_probe(a.ip, SMALL_PATH, SMALL_TIMEOUT, a.secret)
        lo, lc, lsz, lsec = http_probe(a.ip, LARGE_PATH, LARGE_TIMEOUT, a.secret)
        if so:
            s_ok += 1
            s_ms.append(int(ssec * 1000))
        # 큰 응답은 '200 + 실제 바이트' 여야 통과. 0바이트면 블랙홀이다
        if lo and lc == 200 and lsz > 0:
            l_ok += 1
        s_txt = ("%3s %6dms" % (sc, int(ssec * 1000))) if so else "  실패(타임아웃)"
        l_txt = ("%3s %7dB %5.1fs" % (lc, lsz, lsec)) if lo else "  실패(타임아웃/0바이트)"
        out("   %3d  %-24s  %s" % (i, s_txt, l_txt))
        if i < a.count:
            time.sleep(a.gap)

    out()
    out("   작은 요청 성공 : %d / %d" % (s_ok, a.count))
    out("   큰 응답  성공 : %d / %d" % (l_ok, a.count))
    if s_ms:
        out("   작은 요청 응답시간 : 평균 %dms / 최대 %dms" % (int(statistics.mean(s_ms)), max(s_ms)))
        out("   ※ 온라인 판정 타임아웃은 10초입니다. 최대값이 여기 가까우면 화면이 깜빡입니다.")

    # ── 3. 경로 MTU ── DF 비트를 세운 ping 으로 통과하는 최대 크기
    head("3. 경로 MTU (DF 비트 ping)")
    out("   표시값 + 28 = 실제 MTU. ICMP 가 막혀 있으면 전부 실패하니 그때는 무시하세요.")
    best = 0
    for sz in (1472, 1440, 1412, 1372, 1332, 1272, 1172, 972):
        ok, _, frag = ping_once(a.ip, size=sz, df=True)
        if ok:
            out("   [OK]   payload %d 통과  ->  MTU %d" % (sz, sz + 28))
            best = sz
            break
        out("   payload %-5d %s" % (sz, "조각화 필요(너무 큼)" if frag else "응답 없음"))
    if best == 0:
        out("   통과하는 크기를 못 찾음 - ICMP 차단이거나 링크 불통")

    # ── 4. 판정 ──
    head("4. 판정")
    s_rate = s_ok / float(a.count)
    l_rate = l_ok / float(a.count)
    ping_ok = loss <= 5.0

    if ok_n == 0 and s_ok == 0:
        out("   [FAIL] 로봇 라우터에 전혀 못 닿습니다.")
        out("          라우터 전원 / WAN 고정 IP / 내 PC 가 붙은 망부터 확인하세요.")
        out("          field_diag.ps1 -Mode Robot 으로 로봇 실제 IP 를 먼저 확정할 것.")
    elif not ping_ok:
        out("   [FAIL] (B) 링크 품질 문제입니다. ping 부터 손실이 %.0f%% 입니다." % loss)
        out("          MTU 가 아닙니다. MTU 는 작은 패킷인 ping 에는 영향을 주지 않습니다.")
        out("          손실이 있으면 작은 HTTP 도 같이 실패하고, 그래서 온라인이 깜빡입니다.")
        out("          조치: 라우터 위치를 옮겨 신호세기를 확인하세요.")
        out("                LGIT 정상 장비 기준값은 -71 dBm 입니다.")
        out("          이 상태면 맵 작업은 로봇 라우터에 직접 붙어서 하는 편이 확실합니다.")
    elif s_rate >= 0.9 and l_rate <= 0.1:
        out("   [FAIL] (A) MTU 블랙홀입니다. 작은 요청은 되는데 큰 응답만 막힙니다.")
        out("          조치: 양쪽 라우터의 MTU 를 1400 으로 낮추고 재측정. 안 되면 1360.")
        out("                http://192.168.39.1:8090  (admin / uned123456)")
        out("                로봇 라우터와 서버 라우터 둘 다 바꿔야 합니다.")
        if best:
            out("          측정된 경로 MTU 는 %d 입니다. 이보다 낮게 잡으세요." % (best + 28))
        out("          영향: 온라인 판정과 배차는 되지만, 맵 동기화와 장비정보가 실패합니다.")
    elif s_rate < 0.9:
        out("   [FAIL] (C) ping 은 되는데 TCP 가 막힙니다.")
        out("          ping 은 라우터가 답하는 것이라 포워딩과 무관합니다. 여기가 그 차이입니다.")
        out("          조치: 로봇 라우터 포워딩 WAN 8090 -> 192.168.39.100:8090 (TCP)")
        out("                포트 번호는 반드시 8090 그대로. 코드에 하드코딩되어 있습니다.")
    else:
        out("   [OK]   (D) 네트워크는 정상입니다. 작은 것도 큰 것도 통과했습니다.")
        out("          원인은 네트워크 밖입니다. robots.ip_address / 온라인 캐시 /")
        out("          job_points 매핑을 보세요 (현장점검 명령어 가이드 STEP 5~7).")

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtu_check_%s.txt" % stamp)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(_lines))
        out()
        out("   결과 저장: %s" % path)
    except Exception as e:
        out("   결과 저장 실패: %s" % e)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단됨")
    print()
    try:
        input("엔터를 누르면 닫습니다 ")
    except EOFError:
        pass
