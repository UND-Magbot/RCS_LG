# -*- coding: utf-8 -*-
"""
RCS LG - 통신사/LG 요청 3종 연결성 테스트 (2026-08-28)

요청받은 항목
    1) ping 10.115.244.12        로봇 라우터 WAN 까지 IP 도달
    2) tcping 10.115.244.12 8090 그 포트로 TCP 연결이 맺어지는지
    3) ping 8.8.8.8              외부 인터넷 경로

tcping 은 윈도우 기본 명령이 아니고 현장은 인터넷이 없어 설치도 못 합니다.
여기서는 같은 일(대상 포트로 TCP 연결을 맺고 걸린 시간을 잼)을 파이썬 소켓으로 합니다.
설치 필요 없고 관리자 권한도 필요 없습니다.

    python carrier_test.py
    python carrier_test.py --ip 10.115.244.12 --port 8090 --count 20

결과는 carrier_test_<날짜시각>.txt 로 저장됩니다. 그 파일을 그대로 전달하면 됩니다.

주의 - 이 3종은 전부 "닿느냐"만 봅니다. "얼마나 빠르냐"는 못 잽니다.
      우리 문제는 속도이므로, 반드시 mtu_check.py 의 처리량 수치를 같이 전달하세요.
"""

import argparse
import datetime
import os
import re
import socket
import statistics
import subprocess
import sys
import time

_lines = []


def out(s=""):
    """콘솔 인코딩(cp949 등)에 없는 문자가 섞여도 죽지 않게 한다."""
    try:
        print(s)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(s.encode(enc, errors="replace").decode(enc, errors="replace"))
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


def ping_run(ip, count, wait_ms=2000):
    """ping 을 count 회. (성공수, 응답시간리스트) 를 돌려준다."""
    ok_n, times = 0, []
    for i in range(1, count + 1):
        try:
            p = subprocess.run(["ping", "-n", "1", "-w", str(wait_ms), ip],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            text = _decode(p.stdout)
        except Exception:
            text = ""
        ok = "TTL=" in text.upper()
        if ok:
            ok_n += 1
            m = re.search(r"(?:time|시간)[=<]\s*(\d+)\s*ms", text, re.IGNORECASE)
            times.append(int(m.group(1)) if m else 0)
        sys.stdout.write("." if ok else "X")
        sys.stdout.flush()
        if i % 20 == 0:
            sys.stdout.write("\n")
    if count % 20 != 0:
        sys.stdout.write("\n")
    return ok_n, times


def tcp_run(ip, port, count, timeout=5.0):
    """tcping 대체 - 대상 포트로 TCP 연결을 맺고 걸린 시간(ms)을 잰다."""
    ok_n, times, errs = 0, [], {}
    for i in range(1, count + 1):
        t0 = time.time()
        s = None
        try:
            s = socket.create_connection((ip, port), timeout=timeout)
            ms = int((time.time() - t0) * 1000)
            ok_n += 1
            times.append(ms)
            mark = "."
        except Exception as e:
            errs[type(e).__name__] = str(e)
            mark = "X"
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        sys.stdout.write(mark)
        sys.stdout.flush()
        if i % 20 == 0:
            sys.stdout.write("\n")
        time.sleep(0.3)
    if count % 20 != 0:
        sys.stdout.write("\n")
    return ok_n, times, errs


def stat_line(ok_n, count, times, unit="ms"):
    loss = 100.0 * (count - ok_n) / count if count else 0
    out("   성공 %d/%d   손실률 %.0f%%" % (ok_n, count, loss))
    if times:
        j = statistics.pstdev(times) if len(times) > 1 else 0.0
        out("   시간  최소 %d%s / 평균 %d%s / 최대 %d%s / 흔들림 %.0f%s"
            % (min(times), unit, int(statistics.mean(times)), unit,
               max(times), unit, j, unit))
    return loss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="10.115.244.12", help="로봇 라우터 WAN")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--internet", default="8.8.8.8", help="외부 인터넷 확인 대상")
    ap.add_argument("--count", type=int, default=20)
    a = ap.parse_args()

    out()
    out("###########################################################")
    out("  RCS LG 연결성 테스트 (통신사 요청 3종)")
    out("  실행 시각 : %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    out("###########################################################")

    # 지금 어느 망에 붙어 있는지를 같이 남겨야 결과 해석이 가능하다
    head("0. 실행 PC 네트워크")
    try:
        p = subprocess.run(["ipconfig"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        txt = _decode(p.stdout)
        for line in txt.split("\n"):
            if re.search(r"IPv4|어댑터|adapter|게이트웨이|Gateway", line, re.IGNORECASE):
                line = line.rstrip()
                if line.strip():
                    out("   " + line.strip())
    except Exception as e:
        out("   ipconfig 실패: %s" % e)

    # 1) ping 로봇 라우터
    head("1. ping %s  (%d회)" % (a.ip, a.count))
    ok1, t1 = ping_run(a.ip, a.count)
    _lines.append("   " + ("." * ok1) + ("X" * (a.count - ok1)) + "   (. 성공 / X 실패)")
    loss1 = stat_line(ok1, a.count, t1)

    # 2) tcping 대체
    head("2. TCP %s:%d  (%d회)  [tcping 상당]" % (a.ip, a.port, a.count))
    out("   tcping 은 윈도우 기본 명령이 아니라, 같은 동작을 파이썬 소켓으로 수행합니다.")
    out("   (대상 포트로 TCP 연결을 맺고 걸린 시간을 측정)")
    ok2, t2, errs2 = tcp_run(a.ip, a.port, a.count)
    _lines.append("   " + ("." * ok2) + ("X" * (a.count - ok2)) + "   (. 성공 / X 실패)")
    loss2 = stat_line(ok2, a.count, t2)
    if errs2:
        for k, v in errs2.items():
            out("   실패 사유: %s - %s" % (k, v))

    # 3) 외부 인터넷
    head("3. ping %s  (외부 인터넷, %d회)" % (a.internet, min(a.count, 10)))
    n3 = min(a.count, 10)
    ok3, t3 = ping_run(a.internet, n3)
    _lines.append("   " + ("." * ok3) + ("X" * (n3 - ok3)) + "   (. 성공 / X 실패)")
    loss3 = stat_line(ok3, n3, t3)
    out("   ※ M2M 전용 APN 은 외부 인터넷이 막혀 있는 것이 정상일 수 있습니다.")
    out("      여기가 실패해도 그 자체로 고장은 아닙니다.")

    # 요약
    head("요약")
    out("   1) ping %-16s 손실 %3.0f%%   %s" % (a.ip, loss1, "정상" if loss1 <= 5 else "이상"))
    out("   2) TCP  %s:%-9d 손실 %3.0f%%   %s" % (a.ip, a.port, loss2, "정상" if loss2 <= 5 else "이상"))
    out("   3) ping %-16s 손실 %3.0f%%   %s" % (a.internet, loss3,
                                                "열림" if loss3 <= 50 else "막힘(전용망이면 정상일 수 있음)"))
    out()
    out("   [중요] 위 3종은 전부 '닿느냐(연결성)'만 봅니다. '얼마나 빠르냐'는 못 잽니다.")
    out("          우리 문제는 속도입니다. 아래 실측을 반드시 같이 전달하세요.")
    out("            - 6537바이트 응답 수신에 37.5초 소요 = 약 174 B/s (약 1.4 kbps)")
    out("            - 같은 시각 신호세기 -63 dBm (정상 기준 -71 보다 양호)")
    out("            - 경로 MTU 1500 정상, ping 손실 0%")
    out("          즉 전파와 경로는 정상인데 처리량만 극단적으로 낮습니다.")
    out("          최신 수치는 mtu_check.py 를 돌려 그 결과 파일을 같이 첨부하세요.")

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carrier_test_%s.txt" % stamp)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(_lines))
        out()
        out("   결과 저장: %s" % path)
        out("   이 파일을 그대로 전달하시면 됩니다.")
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
