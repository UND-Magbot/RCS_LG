# 현장 작업 도구 (bat 전용 브랜치)

RCS LG 현장 작업용 실행 파일만 담은 브랜치입니다.
소스 코드는 없습니다. **clone 이 가볍습니다.**

---

## 서버PC 에서 받기

```
git clone --depth 1 -b field-tools https://github.com/UND-Magbot/RCS_LG.git C:\Temp\field-tools
```

받으면 `C:\Temp\field-tools\현장작업\` 안에 전부 있습니다.

인터넷 브라우저만 되면 GitHub 웹에서 **Code → Download ZIP** 으로 받아도 됩니다.

---

## 쓰는 순서

`현장작업\읽어보기.txt` 를 먼저 보세요. **전부 더블클릭입니다.**

| 파일 | |
|---|---|
| `config.bat` | **로봇 IP 등 공통 설정. 여기만 고치면 됩니다** |
| `A_사용폴더_git올리기.bat` | 사용폴더를 git 저장소로 (한 번만) |
| `0_서버PC동기화.bat` | 급할 때 파일만 복사 |
| `1_망측정.bat` | ping/tracert ★ **현장에서만 잴 수 있습니다** |
| `2_HTTP측정.bat` | ping 막힌 망일 때 |
| `3_사전점검.bat` | 30초 ★ **주행 전 필수** |
| `4_주행기록.bat` | 본 측정. 멈칫마다 스페이스바 |
| `5_부하모니터.bat` | 브라우저 모니터 |
| `6_로그모으기.bat` | 결과 zip |
| `9_상태확인.bat` | git · 백엔드 · 로봇 · 맵영역 |

---

## 이 브랜치에 없는 것 — 따로 받아야 합니다

bat 만 담았으므로 아래 파일이 있어야 동작하는 것들이 있습니다.

| bat | 필요한 것 | 받는 법 |
|---|---|---|
| `5_부하모니터.bat` | `tools/robot_monitor.html` | 아래 참조 |
| `3_사전점검.bat`<br>`4_주행기록.bat` | `scripts/drive_log.py`<br>`BackEnd/venv` | 사용폴더에 이미 있음 |

사용폴더가 git 저장소가 된 뒤에 가져오는 법:

```
git fetch origin
git checkout origin/feature/lg_luke -- tools/robot_monitor.html
git checkout origin/feature/lg_luke -- scripts/load_test.py
```

`fetch` 는 파일을 건드리지 않습니다. `checkout` 에 `--` 뒤로 경로를 주면 **그 파일만** 가져옵니다.

---

## 🔴 덮으면 안 되는 파일

```
BackEnd/default_area.json        현재 사용 맵 번호
BackEnd/static/robot_speed.json  로봇별 속도
BackEnd/static/poi_labels.json   화면 한글 이름
BackEnd/config.env               DB 접속정보
BackEnd/static/maps/             맵 이미지
```

> 2026-09-22 — `default_area.json` 하나가 안 맞아서 로봇이 빈 자리에서
> 랙을 찾다 `rack_retry_count_exceeded`(502) 를 **8분간 반복**했습니다.

`A_사용폴더_git올리기.bat` 이 `skip-worktree` 로 자동 처리합니다.

---

## 현장 제약

- 현장 = 생산라인 내부. **방진복**을 입어야 들어갑니다
- 현장 안은 **인터넷이 없습니다.** push/pull 불가 → 안에서는 커밋만, 나와서 push
- **현장 망은 현장에서만 측정됩니다.** 서버PC 를 빼오면 못 잽니다
- 서버PC 를 빼면 **현장 운영이 멈춥니다.** 원복 항목을 미리 메모하세요

---

## 지금까지 밝혀진 것 (멈칫)

멈칫은 **"가다가"가 아니라 "출발할 때"** 생깁니다.

```
경유지 주행 시작 → 매번 3~4초 정지
통신 왕복 6~8회 × 446ms = 2.7~3.6초
실측 출발 지연 3.2초   → 계산이 맞아떨어집니다
```

**배제된 것** (다시 의심하지 마세요)
장애물 · 경로이탈 · 위치추정 · 구동계 · 안전존 · 서버PC 성능

**아직 모르는 것**
- 10초 넘는 공백 34건 — 통신만으로는 설명이 안 됩니다
- 446ms 가 망 탓인지 로봇 탓인지 → `1_망측정.bat` 으로 갈립니다

| ping 평균 | 결론 |
|---|---|
| 200~500 ms | **망이 원인.** 라우터 통합. 코드 수정 없음 |
| 1~20 ms | **로봇 REST 가 느린 것.** 왕복 횟수를 줄여야 함 |
