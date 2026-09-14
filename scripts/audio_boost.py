"""안내 음원을 '더 크게 들리도록' 가공한다 — 압축(compression) + 리미팅.

    python scripts/audio_boost.py <입력파일> [-o 출력.wav] [--target -12]

왜 단순 볼륨 올리기가 아닌가
  LG 음원은 **피크가 이미 0.855(헤드룸 1.4 dB)** 라 그냥 키우면 1.4 dB 밖에 못 얻는다.
  사람이 느끼는 크기는 피크가 아니라 **RMS(평균 에너지)** 를 따라간다. 그래서
  큰 부분을 눌러(compression) 작은 부분을 끌어올린 뒤 전체를 키운다.

  안내방송용 기준은 대략 RMS -12 ~ -14 dBFS. 원본은 -18 dBFS 였다.

출력 형식은 확장자로 정한다. **로봇은 WAV URL 을 재생하지 않는다**(2026-09-09 S300 실기 —
startPlayAudio 가 code 0 으로 접수되지만 소리가 안 남). 그래서 **.mp3 로 내보내야 한다.**
인코딩은 pip 패키지 `lameenc`(LAME 내장) 로 하므로 외부 프로그램 설치가 없고,
**서버 노트북에는 아무것도 설치할 필요가 없다** — 완성된 mp3 만 복사하면 된다.
"""
import argparse
import os
import sys

import numpy as np
import soundfile as sf

ATTACK_MS, RELEASE_MS = 5.0, 120.0   # 엔벨로프 추종 — 말소리에 맞춘 값
THRESHOLD = 0.25                     # 이 위를 누른다 (피크 1.0 기준)
RATIO = 4.0                          # 4:1
CEILING = 0.97                       # 최종 피크 상한


def _envelope(x: np.ndarray, sr: int) -> np.ndarray:
    """피크 추종 엔벨로프. 붙을 땐 빠르게, 놓을 땐 느리게."""
    a_att = np.exp(-1.0 / (sr * ATTACK_MS / 1000.0))
    a_rel = np.exp(-1.0 / (sr * RELEASE_MS / 1000.0))
    env = np.empty_like(x)
    e = 0.0
    for i, v in enumerate(x):
        a = a_att if v > e else a_rel
        e = a * e + (1.0 - a) * v
        env[i] = e
    return env


def boost(x: np.ndarray, sr: int, target_dbfs: float) -> np.ndarray:
    x = x - x.mean(axis=0, keepdims=True)          # DC 제거
    peak = np.abs(x).max()
    if peak > 0:
        x = x / peak                                # 먼저 피크를 꽉 채운다

    mono = np.abs(x).max(axis=1)                    # 채널 공통 게인 (이미지 안 틀어지게)
    env = _envelope(mono, sr)
    gain = np.ones_like(env)
    over = env > THRESHOLD
    gain[over] = (THRESHOLD / env[over]) ** (1.0 - 1.0 / RATIO)
    y = x * gain[:, None]

    rms = np.sqrt((y.astype(np.float64) ** 2).mean())
    if rms > 0:                                     # 목표 RMS 까지 메이크업 게인
        y = y * (10.0 ** (target_dbfs / 20.0) / rms)

    y = np.tanh(y / CEILING) * CEILING              # 부드러운 리미팅 (딱딱한 클리핑 방지)
    p = np.abs(y).max()
    if p > CEILING:
        y = y * (CEILING / p)
    return y.astype(np.float32)


def write_audio(path: str, y: np.ndarray, sr: int) -> None:
    """확장자에 맞춰 저장. mp3 는 lameenc(LAME 내장 pip 패키지)로 인코딩한다."""
    if not path.lower().endswith(".mp3"):
        sf.write(path, y, sr, subtype="PCM_16")
        return
    import lameenc
    pcm = np.clip(y, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    ch = pcm.shape[1]
    enc = lameenc.Encoder()
    enc.set_in_sample_rate(sr)
    enc.set_channels(ch)
    enc.set_bit_rate(64 if ch == 1 else 128)
    enc.set_quality(2)               # 2 = 높은 품질 (0 이 최고, 느림)
    data = enc.encode(pcm.tobytes()) + enc.flush()
    with open(path, "wb") as f:
        f.write(bytes(data))

def stats(x: np.ndarray) -> str:
    m = float(np.abs(x).max())
    r = float(np.sqrt((x.astype(np.float64) ** 2).mean()))
    return f"최대 {m:5.3f}  RMS {20*np.log10(r+1e-12):6.1f} dBFS"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--out")
    ap.add_argument("--target", type=float, default=-12.0, help="목표 RMS (dBFS)")
    a = ap.parse_args()

    x, sr = sf.read(a.src, dtype="float32", always_2d=True)
    y = boost(x, sr, a.target)

    out = a.out or (os.path.splitext(a.src)[0] + "_loud.mp3")
    write_audio(out, y, sr)
    print(f"  원본  {os.path.basename(a.src):<22} {stats(x)}")
    print(f"  결과  {os.path.basename(out):<22} {stats(y)}")
    gain = 20*np.log10(np.sqrt((y.astype(np.float64)**2).mean()) /
                       (np.sqrt((x.astype(np.float64)**2).mean()) + 1e-12))
    print(f"  → 체감 음량 약 +{gain:.1f} dB   ({out}, {os.path.getsize(out)//1024} KB)")


if __name__ == "__main__":
    main()
