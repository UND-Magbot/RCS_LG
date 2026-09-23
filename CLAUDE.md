# RCS LG 프로젝트

> 이 폴더는 **LG 프로젝트**입니다. 바탕화면의 `RCS_Basic_VESA_ver-feature-backend_noah`(VESA/삼성웰스토리)와
> **같은 코드베이스에서 갈라진 별개 프로젝트**입니다. 아래 §2 를 먼저 읽으세요.

## 언어
- 모든 답변은 한국어로 작성 (코드 주석도 한국어)

## 용어 — 지킬 것
- 주행 중 갑자기 속도가 떨어졌다 회복하는 현상 = **비이상적 정지(급감속)**
  - **"멈칫" · "멈칫거림" · "stutter" 를 쓰지 않는다.** 고객사 보고에 나가는 용어다
  - 처음 한 번만 `비이상적 정지(급감속)` 로 풀고, 이후에는 `비이상적 정지`
  - 보고서·문서·커밋 메시지·화면 문구 전부 동일
  - 파일명/변수명의 `stutter` 는 그대로 두되(동작에 영향), 사람이 읽는 문장에는 안 쓴다

---

## 1. 프로젝트 개요

- RCS (Robot Control System) **LG 버전** — Basic → VESA → LG 로 분기. **인터랙티브 배차** 운영 방식
- 로봇: AutoXing **longjack**(펌웨어 2.12.28-pi64) 1대가 **LG 대차(랙)** 를 잭업해 경유지를 순회
- 백엔드: FastAPI (Python 3.11) + MariaDB — 포트 **8002**
- 프론트엔드: Next.js 16 + TypeScript + Three.js (3D 모니터링) — 개발 포트 **3000**
  - 배포 시 **포트 통합** 예정: `BUILD_STATIC=1` 정적 빌드를 백엔드가 서빙 → 8002 하나 (코드 반영 완료, **실빌드 검증 미완**)
- 태블릿: Android WebView 셸 (Kotlin) + 백엔드 서빙 HTML (**콘솔용 / 로봇부착용 2종**)
- DB: **`rcs_lg_db`** (로컬 127.0.0.1, `run_local.ps1`)
- 로컬 REST API만 사용 (CSP/클라우드 미사용). **LTE(M2M) 망 운영 대응** — 유동 IP·지연 보정
- GitHub: https://github.com/UND-Magbot/RCS_Basic_VESA_ver
  - 작업 브랜치 **`feature/luke-lg`** — 원작자 브랜치 `feature/backend_noah` 의 커밋 `6bb5b5c` 에서 분기
  - ⚠️ **`feature/backend_noah` 에 직접 푸시하지 말 것** (원작자 Choigyuhwi 가 계속 수정 중)
  - ⚠️ `feature/luke-lg` 는 **아직 원격에 푸시되지 않음** → 다른 PC로 옮길 땐 폴더 복사만 가능

### 운영 UI (현행)
- **관제 메인화면(Next.js `/monitoring`)** — 3D 모니터링 + 로봇별 원격제어(방향/잭/충전소복귀/시스템재시작/**작업 강제 종료**)
- **콘솔(`/api/dispatch/console`)** — 범용 단말 1대로 모든 POI 호출/예약/경유지출발/종료 + 우측 로봇 사이드바 + 카드별 [직접 제어]
- **위치별 태블릿(`/api/dispatch/tablet/poi/{poi_id}`)** — **배송 모드의 현장 단말.**
  R(랙 보관)에는 [호출], J(작업지점)에는 [확인 — 랙을 치웠습니다]. 현장 4대(R1·R2·J1·J2).
  APK `poi` flavor 로도 열림(2026-08-24 신설). 앱에 넣는 값은 **서버 주소 + POI ID** 두 칸뿐
- **로봇 부착 태블릿(`/api/dispatch/robot-tablet/{robot_id}`)** — 경유지 진행용 [확인]/[작업 종료].
  배송 모드에서는 확인 단계가 없어 **비상 종료용**으로만 남는다

### 운영 흐름 (인터랙티브 배차 — 핵심)

```
[충전소] 로봇 idle
  ↓ [호출] 콘솔에서 POI 타일 클릭 → 가용 로봇 1대 자동 선정(배터리 1순위)
[픽업 1회] standby → align_with_rack → jack_up
  ↓ [이동] 호출한 POI 도착 (잭 유지) → awaiting_next
[경유지] 콘솔에서 경유지 여러 개 등록 + [출발]
  ↓ 각 경유지 도착 → awaiting_confirm → 로봇 태블릿 [확인] → 다음 경유지
[종료] 마지막 [확인] 또는 [작업 종료] → standby 복귀 → jack_down → 충전소 도킹
```

**핵심 단순화**: `align_with_rack` 1회 · `jack_up` 1회 · `jack_down` 1회 (잭 모터 부하 최소화).
경유지를 아무리 많이 돌아도 **잭은 든 채로** 다닌다.

**상태 머신** (`dispatch_sessions.status`):
`starting` → `picking_up` → `moving` → `awaiting_next` ↔ (`moving` → `awaiting_confirm`) → `returning` → `completed`/`failed`
→ 상세는 [docs/01_시스템구조.md](docs/01_시스템구조.md) §10

### 운영 흐름 ② 배송 모드 (2026-08-24 · PoC 기본 모드)

```
[충전소] ─(R 에서 호출)→ 그 R 에서 적재 → 짝 J 로 이동 → 하차 → 전진 이탈 → J 점유 표시
          → 대기 예약 있으면 그 J 의 R 로 이어서 / 없으면 충전소 복귀
```

**호출은 R, 확인은 J.** 현장은 R 과 J 가 **100m 이상 떨어져 서로 다른 구역**에 있다.

| 구역 | POI | 그 구역 작업자 |
|---|---|---|
| A | **R1 + J2** | R1 에서 호출 → J1 로 배송 / J2 에서 확인 |
| B | **J1 + R2** | R2 에서 호출 → J2 로 배송 / J1 에서 확인 |

- J 옆 사람은 100m 밖 R 에 랙이 있는지 볼 수 없다 → **호출은 랙이 눈앞에 있는 R 에서** (LG 요구)
- R 옆 사람은 100m 밖 J 가 비었는지 모른다 → **R 화면이 짝 J 의 점유 상태를 보여주고, 랙이 남아 있으면 호출 버튼을 안 그린다**
- 랙 감지 센서가 없고 **라이다 판정도 불가(실측)** → J 의 [확인]이 **유일한** 점유 해제 수단
- **로봇 부착 태블릿 확인 없음. 랙 반납 단계 없음.**

호출된 POI 에 `job_points` 매핑이 있을 때만 이 모드로 진입하고,
**매핑이 없으면 위 인터랙티브 모드로 그대로 동작한다** (UI 도 종전 화면이 뜬다 → **롤백 = 매핑 삭제**).

> 배차 코드는 안 건드렸다. R 로 들어온 호출은 `poi_call()` 입구에서 **매핑된 J 로 치환**한다.

**태블릿 APK 3종** (`RCS_LG/APK/`, 패키지 id 가 달라 한 기기에 동시 설치 가능)
`console`(관리자콘솔) · `robot`(로봇태블릿) · **`poi`(위치태블릿, 2026-08-24 신설)**
빌드는 `TabletApp/APK빌드.bat`. ⚠️ **JDK 17 필요** — JRE 1.8 은 AGP 가 안 붙고,
JDK 25 는 Gradle 8.7 이 못 읽는다(`Unsupported class file major version 69`).

→ 상세는 [docs/08_공정시나리오_변경.md](docs/08_공정시나리오_변경.md)

**⚠️ 로봇을 새로 등록하면 반드시 충전소·대기장소를 지정할 것.** `register-by-ip` 는 이 두 값을
채우지 않고, NULL 이면 **마지막 복귀만 조용히 안 된다** (2026-08-24 실기 1차가 이걸로 멈춤).

### 운영 흐름 ③ 주행 방식 (2026-09-07~10 개편)

로봇에게 경로 탐색을 맡기지 않는다. 좁아진 통로에서 경로를 못 찾고 서는 일이 있었다
(2026-09-07 실측 — alert `1007`, 직선상 최소 여유 0.212 m < 로봇 외접원 0.519 m).

```
[1단계] 경유지 체인 ─────→ 마지막 경유지   along_given_route · detour_tolerance = 0
                                          지정한 길로만. 장애물을 만나면 우회 없이 정지
[2단계] 마지막 경유지 ───→ 진입점          standard  ★ 여기부터 로봇 자율 + 회피
[3단계] 진입점 ──────────→ 작업지점        align_with_rack / to_unload_point
                                          로봇이 스스로 자세를 잡고 진입
```

- **경유지**는 맵 POI 이름 `W1`, `W2` … (`^W\d+$`). **로봇에 동기화하지 않고** 백엔드가 DB 에서 직접 읽는다
- **진입점**은 `<작업지점>-1` 규약 — `C1-1`(충전소, 기존) · `R1-1`/`R2-1`/`J1-1`/`J2-1`(2026-09-09 신설).
  **안 찍으면 그 작업지점은 종전 동작 그대로** → 롤백 = POI 삭제
- **안전거리**: Yellow 3.0 m 서행 / Red 1.0 m 정지. **앞끝 기준**, 좌우는 **중심 기준** 밴드 반폭.
  작업지점 반경 2 m 이내는 판정 해제(로봇 자체 회피가 담당). 좌우·후방은 서버가 보지 않는다

→ 상세는 [docs/11_경유지주행.md](docs/11_경유지주행.md) · [docs/10_충전도킹과_안전설정.md](docs/10_충전도킹과_안전설정.md) §7

---

## 2. 🚫 VESA 프로젝트와 합치지 말 것

같은 AutoXing 로봇을 쓰지만 **고객사가 다른 별개 프로젝트**입니다.

| | **이 폴더 (LG)** | `RCS_Basic_VESA_ver-feature-backend_noah` (VESA) |
|---|---|---|
| 고객사 | **LG** | **VESA(삼성웰스토리)** |
| git 브랜치 | **`feature/luke-lg`** | `feature/luke` |
| 로봇 | **longjack** (펌웨어 2.12.28-pi64) | crawler_s300_op5 (2.12.21-opi64) |
| 랙 | S300/S600 + **LG / LG2** | S300 / S600 |
| DB | **`rcs_lg_db`** | `rcs_vesa_db` |
| 망 | **LTE(M2M)** — 유동 IP·지연 보정 | 로컬 Wi-Fi |
| 태블릿 | **콘솔용 / 로봇부착용 2종** | 1종 |
| 작업일지 | "RCS LG 작업일지" | "RCS Basic VESA 작업일지" |

**코드를 그대로 옮기면 깨집니다.** 로봇 모델·랙 치수·DB 이름·네트워크 전제가 전부 다릅니다.
설계 방식이 참고할 만하면 **읽고 나서 상대 프로젝트에 맞게 새로 작성**하세요.

> 실제 사례(2026-08-10): VESA 쪽에서 호출 대기열을 만들 때, 이 프로젝트가 대기열을
> DB(`dispatch_reservations`)에 저장하는 방식이 더 낫다고 판단해 **그 방식만** 차용했습니다.
> 코드는 한 줄도 복사하지 않고 VESA 구조에 맞게 새로 작성했습니다.

### 이 폴더에만 있는 것 (VESA 쪽에 없음)
- `BackEnd/app/constants/rack_specs.py` 의 **LG / LG2 랙 스펙** (2026-07~08 실측)
- 프론트 POI 편집의 랙 사이즈 선택지 `LG` / `LG2`
- LTE 지연 대응(맵 재선택 3회 재시도), 부팅 후 자동 복구, 작업 강제 종료
- DB 기반 호출 대기열(`dispatch_reservations`) · 경유지(`dispatch_waypoints`)
- 테스트 스크립트 5종(`scripts/`) + 배차 동시성 테스트(`tests/test_dispatch_lg.py`)

### VESA 쪽에만 있는 것 (이 폴더에 없음)
- `미해결이슈.md`, `현장세팅_체크리스트.md`
- `failure_guide.py` (태블릿 실패 안내 문구 단일 소스)
- 배정 락(`_assign_lock`, 로봇/POI 이중배정 차단) — LG는 다른 방식으로 해결([docs/06](docs/06_테스트현황과_이슈.md) 결함 B)
- 렉 반납 `to_unload_point`, `calculation_failed` 조기 중단

---

## 2-1. 🔴 현장 서버PC 구조 — 폴더가 둘이다

**묻지 말고 이 전제로 시작할 것.** 매번 확인하면 시간이 낭비된다.

```
[서버PC]
  ├─ 사용폴더      실제로 백엔드가 돌아가는 것.   git 연결 ❌
  └─ 업로드폴더    git 연결 ⭕ (origin/feature/lg_luke)
                   필요한 파일만 손으로 복사해 넣던 곳
```

**동기화 = 업로드폴더에서 `git pull` → 사용폴더로 복사.**
복사는 손으로 하지 말고 `현장작업\0_서버PC동기화.bat` 을 쓴다
(사용폴더 경로를 한 번 입력하면 `사용폴더경로.txt` 에 기억한다).

> 사용폴더 자체를 git 저장소로 전환하는 계획이 있다(약 1시간).
> 옵시디언 `_보관/merge/` 참조. **급할 때는 하지 말 것.**

### 현장 물리 제약 — 이게 작업 설계를 지배한다

| | |
|---|---|
| 현장 = 생산라인 내부 | **방진복**을 입어야 들어간다 |
| 현장 안 | **인터넷 없음.** git push/pull 불가, 검색 불가 |
| 들어가는 비용 | 한 번 들어가면 **안에서 최대한 끝내야 한다** |
| 현장 수정분 회수 | 안에서는 **커밋만** 하고 나와서 push |

**나오기 전에 반드시 재야 하는 것** — 현장 망은 현장에서만 측정된다.
`현장작업\1_망측정.bat` (ping/tracert/ipconfig 자동). 서버PC 를 빼오면 못 잰다.

### 서버PC 를 빼올 때

서버PC 를 빼면 **현장 운영이 멈춘다.** 원복 항목(IP·랜선·전원)을 미리 메모할 것.

### 덮으면 안 되는 파일 — git 추적 중이지만 서버마다 값이 다르다

| 파일 | 내용 |
|---|---|
| `BackEnd/default_area.json` | **현재 사용 맵 번호.** 덮으면 로봇이 엉뚱한 좌표로 간다 |
| `BackEnd/static/robot_speed.json` | 로봇별 속도(IP 기준) |
| `BackEnd/config.env` | DB 접속정보 (`.gitignore`) |

> 2026-09-22 실제 사고 — 활성 맵이 36 인데 콘솔이 map35 의 POI 를 호출해
> 로봇이 빈 자리에서 랙을 찾다 `rack_retry_count_exceeded`(502) 를 8분 반복.
> 원인은 `default_area.json` 하나였다.

---

## 3. 로컬 실행

```powershell
# 백엔드 — DB rcs_lg_db(127.0.0.1), 0.0.0.0:8002
cd C:\Users\ASUS\Desktop\RCS_LG\BackEnd
.\run_local.ps1

# 프론트 — http://localhost:3000
cd C:\Users\ASUS\Desktop\RCS_LG\frontend
npm run dev
```

- 기동 성공 신호: `Application startup complete` + `[startup] 속도 설정: <SN> → 1.2 m/s`
  - `속도 설정 실패` = **로봇에 못 닿는 것**(망/IP 확인 → [docs/02_네트워크.md](docs/02_네트워크.md))
- `WinError 10013` = 포트 8002 이미 사용 중. 점유 프로세스 종료 후 재실행
- API 문서 `http://localhost:8002/docs` · 헬스체크 `/ping`, `/health`

### 의존성 ⚠️ `requirements.txt` 를 쓰지 말 것
UTF-16 인코딩 + 개발 PC 전체 `pip freeze`(torch·PyQt5·pyrealsense2 등 약 3GB)라 그대로 설치하면 안 됩니다.
실제 필요한 것은 **9개**뿐:
```
fastapi uvicorn[standard] SQLAlchemy PyMySQL
python-jose[cryptography] passlib bcrypt==4.0.1
requests websocket-client apscheduler
```
- `apscheduler` 는 두 requirements 파일 **어디에도 없음** (반드시 별도 설치)
- `bcrypt` 는 **4.0.1 고정** — 4.1+ 는 passlib 1.7.4 와 충돌(`__about__` AttributeError → 로그인 깨짐)

### DB
- 원본 덤프: `_db/undrcs_원본_260811.sql` (DROP+CREATE 포함 → 그대로 import 하면 초기화)
- 21 테이블 / FK 26개 / 약 2.8MB
- 서버 PC 이관 절차는 [docs/03_서버PC구축.md](docs/03_서버PC구축.md) STEP 3

---

## 4. 디렉토리 구조

```
RCS_LG/
  BackEnd/
    app/
      routers/      auth, user, robot, map, alarm_log, activity_log, backup, log,
                    jack_test, task, settings, dispatch ★
      models/       SQLAlchemy (+ dispatch.py — sessions/reservations/waypoints/tablet_slots)
      crud/         DB CRUD (+ dispatch.py)
      schemas/      Pydantic (+ dispatch.py)
      services/     dispatch_service(배차 워커) ★, jack_service, scheduler, zone_guard,
                    zone_lock, poi_lock, geometry, deadlock_monitor(비활성),
                    boot_recovery(부팅 후 자동 복구), rack_detect_service,
                    waypoint_route(경유지 경로 생성) ★, safety_zone(Yellow/Red) ★,
                    map_image(맵 벽 판정), robot_voice(9000 채널 음성), thread_utils
      constants/    robot_types(work_mode), rack_specs(S300/S600/LG/LG2)
      templates/    dispatch_console.html ★, dispatch_robot_tablet.html ★,
                    tablet.html·dispatch_tablet.html(레거시)
      static/       ★ 맵 이미지 약 43MB — .gitignore 라 git 에 없음. 빠뜨리면 맵이 안 뜸
    run_local.ps1
  frontend/
    app/monitoring/  3D 모니터링 (Three.js)
    app/map/         맵 관리    app/robots/  로봇 관리
    app/tasks/·routes/·schedules/  레거시 자동 모드용 (LG 미사용)
  TabletApp/         Android WebView 셸 (MainActivity.kt)
  scripts/           테스트·진단 스크립트 17종 → docs/04, docs/09, docs/10
                     run_probe(주행 정지 원인 기록) ★ · costmap_watch(장애물 실시간)
                     check_waypoints(경유지 배치 점검) · audio_boost(음원 증폭)
                     (2026-09-04 wall_probe·make_corridor·robot_snapshot / 2026-09-09~10 위 4종 추가)
  tests/             test_dispatch_lg.py (배차 동시성 모의 테스트)
  _logs/             스크립트 실행 로그(JSONL) + run_probe 기록(run_<시각>/)
  BackEnd/_logs/     backend.log — 서버 로그 파일(2026-09-10 신설, 10MB×3 순환)
  _db/               DB 덤프·스냅샷 (.gitignore)
  docs/              상세 문서 11종 → §6
  tools/             현장 점검 배치·스크립트 (check_robot.bat 등, 인터넷 불필요)
  APK/               태블릿 앱 3종 (console / robot / poi)
```

---

## 5. 개발 규칙

- **코드 수정 전 계획 먼저** 보여주고 승인받을 것 (단순 오타·1~2줄 제외)
- 수정 후 **문법 체크** 실행. 안전 관련 파라미터가 바뀌면 기존값과 비교해서 보고
- **추측과 사실을 섞지 말 것** — 확인한 건 [측정], 해석은 [추정]으로 구분
- CSS `@import` 사용 금지 (Turbopack 호환 문제 — `globals.css` 에 인라인)
- POI world 좌표: DB에 `world_x`, `world_y` 저장 (pixel 좌표와 별도)
- 맵 동기화 시 overlay 에 **Shelves Point(`type=34`)** 포함 필요 — 없으면 `align_with_rack` 자체가 동작 안 함
- **랙 위치(standby) POI 는 `rack_size` 를 반드시 확인** — 기본값이 `S600` 이라 그냥 만들면 엉뚱한 규격이 전송됨
- **가상벽은 overlay LineString 에 `lineType:"2"` 로 보낼 것** — `properties.type` 이 아니다(로봇 파서가
  LineString 에선 type 을 안 읽음). `_is_firewall_feature()` 로 판정한다(2026-09-04)
- **가상벽·POI 만 바꿨으면 [POI 동기화] 를 쓸 것** — [맵 동기화] 는 SLAM 맵 전체를 교체해 로봇을 60~90초
  재시작시킨다. 재시작 후 overlay 복구는 자동이지만 불필요하게 오래 걸린다
- **랙 규격(`rack.specs`) 을 바꾸면 `frontend/lib/constants/footprint.ts` 도 같이 고칠 것** —
  `BackEnd/app/constants/rack_specs.py` 와 값이 어긋나면 맵 편집기 마커 크기가 실물과 달라진다
- **경유지 체인은 `번호 순서`로만 이어진다** — `waypoint_route.py` 의 docstring 은 '번호는 이름일 뿐,
  자동 연결 그래프'라고 되어 있으나 **구현(`_shortest`)은 번호순 인접만 잇고 벽 검사를 안 한다**
  (2026-09-08 현장 요구로 되돌림). 번호 순서 = 통로 순서여야 하고, 중간 삽입 시 뒤 번호를 전부 밀어야 한다
- **작업지점 둘이 같은 경유지를 공유하면 그 사이 이동은 경유지를 안 쓴다** — `plan()` 의 직행 규칙에
  거리 제한도 벽 검사도 없다. 작업지점마다 **전용 경유지**를 두는 것이 안전하다
- **안전거리 밴드 폭을 더 줄이지 말 것** — 하한이 자기 반폭이라 이미 랙 크기와 같다.
  더 줄이면 랙 모서리가 부딪칠 장애물이 사각지대가 된다(2026-09-07 현장에서 +0.25 를 더했다가 철회)
- **`/tracked_pose` 에는 `speed` 필드가 없다**(AutoXing 문서 명시). 속도는 연속 포즈의 **위치 차분**으로 구한다
- **오디오는 8090 REST 가 아니라 `ws://로봇:9000`** — 장치 볼륨(`setVoice`)은 **mode 마다 별개 값**이다
  (mode 1 상위기 / 2 섀시). 로봇은 **WAV URL 을 재생하지 않는다**(mp3 만)
- 커밋은 기능 단위로, 되돌릴 수 있게 자주

```bash
git status                 # 브랜치가 feature/luke-lg 인지 확인
git add -A && git commit -m "..."
git push -u origin feature/luke-lg
```

### .gitignore 에 등록된 것 (커밋되면 안 되는 것)
- `_db/`, `*.sql` — DB 덤프(운영 데이터)
- `BackEnd/static/maps/` — 맵핑 산출물(38~43MB, DB엔 경로만 저장)
- `BackEnd/venv/`, `frontend/node_modules/`, `frontend/.env.local`

---

## 6. 문서 지도

| 문서 | 언제 보나 |
|---|---|
| [docs/01_시스템구조.md](docs/01_시스템구조.md) | **프론트/백엔드/DB/로봇이 어떻게 맞물리나.** 통신 방식·용어·운영 루틴·상태머신·API 목록·DB 표·튜닝값 |
| [docs/02_네트워크.md](docs/02_네트워크.md) | 로봇에 접속이 안 될 때. IP/포트/APN/포트포워딩 개념, 사무실↔현장 구성, 로봇 WiFi·고정IP 런북 |
| [docs/03_서버PC구축.md](docs/03_서버PC구축.md) | 현장 서버 PC를 새로 만들 때. MariaDB→Python→Node→방화벽→자동시작 + 검증 체크리스트 |
| [docs/04_현장절차_맵핑과테스트.md](docs/04_현장절차_맵핑과테스트.md) | 맵을 새로 따고 테스트를 재개할 때. STEP 1~7 + 스크립트 5종 사용법 + rb-admin 수동 확인 |
| [docs/05_랙인식.md](docs/05_랙인식.md) | `align_with_rack` 이 실패할 때. rack.specs 원리·실측 튜닝·각도 보정 조사 결론·접지 체인 |
| [docs/06_테스트현황과_이슈.md](docs/06_테스트현황과_이슈.md) | **지금 어디까지 됐고 뭐가 남았나.** 진행 현황·알려진 버그 A~F·미해결 이슈·남은 작업 |
| [docs/07_LGIT현장설치_LTE.md](docs/07_LGIT현장설치_LTE.md) | 현장 LTE(pLTE) 설치·라우터·포트포워딩. 라우터 매핑·ping 방향성·유선 전환 이력 |
| [docs/08_공정시나리오_변경.md](docs/08_공정시나리오_변경.md) | **현재 공정 시나리오(R→J 배송)의 단일 소스.** 확정 사양·구현·제약·남은 작업 |
| [docs/09_주행음성안내.md](docs/09_주행음성안내.md) | **주행 중 음성 안내(LGIT 요청2).** 9000 채널·통신 구조·내장음성 59종·LG음원 23종·콘솔 UI·L150 이식 체크리스트 |
| [docs/10_충전도킹과_안전설정.md](docs/10_충전도킹과_안전설정.md) | **도킹 실패가 잦을 때 / 가상벽이 안 먹힐 때 / 안전거리 관련.** 도킹 상수 분리·dock_log.py·**가상벽 실증(§8, lineType 규격)**·**동기화 결함 2건+맵/POI 동기화 분리(§9)**·**랙규격+L150 통로폭(§10)**·Yellow/Red Zone 사양(§7)·2026-09-04 로봇 설정 적용 이력(§11) |
| [docs/11_경유지주행.md](docs/11_경유지주행.md) | **경유지 주행의 단일 소스.** 왜 로봇 탐색을 안 쓰는지·체인 규칙·진입점 규약·접근점 생략 |
| `RCS_LG_패치_<날짜>.zip` 안의 `설명.txt` | **현장 서버 이관용 패치 설명서.** zip 마다 동봉. 적용 절차·변경 내용·검증 상태·롤백 |
| `현장자료/*.html` (현장 PC) | 오프라인 세팅 가이드·통신 구조도. **이 폴더에는 없다** — 현장 이관 zip 에만 동봉했다 |

### 현장 이관 묶음

USB 하나로 들고 간다. **zip 마다 `설명.txt` 가 들어 있고, 그게 그 패치의 설명서다.**

```
RCS_LG_패치_20260909_2.zip   주행 로직 · 오디오 · 안내 음원 24종 · 점검 스크립트
RCS_LG_패치_20260910.zip     백엔드 파일 로그 · 주행 진단 기록기
```

> ⚠️ **현장은 M2M 전용망이라 인터넷이 없다.** 검색·다운로드로 해결할 수 없으므로
> 필요한 자료는 전부 zip 에 넣어 둔다. HTML 은 외부 리소스 0개라 파일만 있으면 열린다.
> `default_area.json` 은 **일부러 제외** — 개발 PC 값이라 덮어쓰면 현장 맵이 바뀐다.
> `system_settings.json` 도 제외 — 서버마다 값이 다르다.

| 그 밖에 | |
|---|---|
| `.claude/daily/<날짜>.md` | **일자별 상세 기록.** 프로젝트 폴더 밖(사용자 홈)에 있어 **git 에 없다** — 인수 시 따로 챙길 것 |
| `tools/check_robot.bat` | 현장에서 로봇 통신 3종(TCP) 점검. 인터넷 불필요 |
| `.claude/agents/` | 영역별 전담 에이전트 4종 (backend / robot / frontend / tablet) |

---

## 7. 업무일지 양식

> ⚠️ 예전에는 `작업일지.md` 를 프로젝트 폴더에 두었으나 **지금은 없다.**
> 기록은 `C:\Users\<사용자>\.claude\daily\<날짜>.md` 에 남긴다 — **프로젝트 폴더 밖이라 git 에 안 들어간다.**
> 인수인계 시 이 폴더를 따로 챙길 것.

최신 날짜를 위쪽에 추가.

```
Noah 일일 업무 보고 <YYYY.MM.DD>

1. RCS LG 개발 (부제)

    소제목
     - 작업 내용 1
     - 작업 내용 2
```

- 코드는 넣지 말고 **그 코드가 무슨 역할을 했는지 / 어떤 테스트를 했고 / 결과가 어땠는지** 중심으로
- 큰 틀로 나누되, 큰 틀 안에서 세부 항목을 적을 것

---

## 8. 인수인계 — 처음 받았을 때

### 읽는 순서

```
1. 이 파일 §1(개요) · §2(VESA 와 섞지 말 것)     무엇인지 · 무엇과 헷갈리면 안 되는지
2. docs/01_시스템구조.md                         프론트·백엔드·DB·로봇이 어떻게 맞물리나
3. docs/06_테스트현황과_이슈.md                   지금 어디까지 됐고 뭐가 남았나
4. docs/08_공정시나리오_변경.md                   현재 공정(R->J 배송)의 단일 소스
5. docs/11_경유지주행.md · docs/10 §7             주행 방식과 안전거리
6. §3(로컬 실행) 으로 직접 띄워 보기
```

막히면 증상별로 — 로봇 접속 안 됨 `docs/02` · 랙 정렬 실패 `docs/05` ·
도킹/가상벽/안전거리 `docs/10` · 현장 LTE `docs/07` · 음성 `docs/09`

### ⚠️ 폴더를 통째로 복사해야 한다

**`feature/luke-lg` 는 원격에 푸시되지 않았고, git 에 안 들어가는 파일이 많다.**
git clone 으로는 프로젝트가 돌아가지 않는다.

| git 에 없는 것 | 왜 |
|---|---|
| `BackEnd/static/maps/` (약 43 MB) | `.gitignore`. **빠뜨리면 맵이 아예 안 뜬다** |
| `_db/`, `*.sql` | DB 덤프(운영 데이터) — 맵·POI·로봇 등록 정보가 여기 있다 |
| `BackEnd/venv/`, `frontend/node_modules/` | 의존성 — 받는 쪽에서 새로 설치 |
| `.claude/daily/` | 사용자 홈에 있음. 일자별 상세 기록 |
| 미커밋 작업분 | 2026-08-12 이후 작업이 커밋되지 않은 상태였다(2026-09-11 확인) |

**인수 전 체크리스트**

```
[ ] git status 로 미커밋 변경 확인 -> 커밋
[ ] BackEnd/static/maps/ 포함해서 폴더 통째 복사
[ ] DB 덤프(_db/) 함께 전달
[ ] .claude/daily/ 와 .claude/agents/ 전달
[ ] 받는 PC 에서 §3 대로 띄워 보고 콘솔(/api/dispatch/console) 접속 확인
```

### 현재 상태 요약 (2026-09-11)

- **구현 완료** — 경유지 주행 · 진입점 규약 · 안전거리(Yellow/Red) · 배송 모드 · 음성 안내 · 부팅 복구
- **실기 미검증** — `to_unload_point` 하차(배차 경로에서 처음 쓰는 동작) · 진단 기록기 실로봇 동작
- **미해결** — 랙 적재 주행 시 멈칫거림(원인 규명 중) · 진입점 도착 후 불필요한 회전
- 상세는 `docs/06_테스트현황과_이슈.md` 와 `.claude/daily/2026-09-0*.md`

---

## 변경 이력

| 날짜 | 내용 |
|---|---|
| 2026-08-14 | VESA 원본에서 이어받은 기재값을 LG 실제값으로 정정(브랜치·DB·포트·테이블·실행법). `_출처_및_주의.md` 를 흡수하고, 상세 내용은 `docs/` 6종으로 분리 |
| 2026-09-11 | **인수인계 대비 최신화.** ①깨진 참조 정정(`작업일지.md`·`RCS_LG_패치_20260824.*`·`현장자료/` — 전부 없는 파일이었음) ②2026-09-07~10 작업 반영(경유지 주행·진입점 규약·안전거리·진단 도구) ③§4 디렉토리 실제와 대조(services 5개 추가·scripts 11->17종) ④§5 에 실기에서 얻은 함정 6건 추가 ⑤**§8 인수인계 신설** |
