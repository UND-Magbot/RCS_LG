# RCS_LG 맵핑 재작성 + 테스트 절차

> 작성일: 2026-08-13
> 대상: 서버 PC 세팅 완료 후, **맵을 새로 따고 랙 인식·배차 테스트를 재개**하는 절차
> 선행 문서: `RCS_LG_서버PC_구축가이드.md` (서버 PC 구축이 먼저입니다)

---

## 0. 이 문서 사용법

### Claude에게 시킬 경우

```
이 문서(RCS_LG_맵핑_및_테스트_절차.md)대로 진행해줘.
STEP 순서대로 하나씩 하고, 각 단계의 [확인] 명령을 실제로 실행해서
결과를 보여준 다음 넘어가줘.
```

**Claude에게 주는 지시 (이 문단은 Claude가 읽을 것):**
- `[확인]` 명령은 **실제로 실행**해서 결과를 보고할 것. 실행 없이 "완료"라고 하지 말 것
- **STEP 1(물리 조치)은 사람이 해야 한다.** 완료됐는지 먼저 물어볼 것
- 맵핑·POI 등록은 **관제 화면(브라우저)에서 사람이 하는 작업**이다. Claude는 결과 검증을 맡는다
- 로봇을 움직이는 명령은 **사람이 로봇을 볼 수 있는 상태인지 확인**하고 실행할 것
- 실패하면 다음으로 넘어가지 말고 §7 문제 해결을 먼저 볼 것

### 왜 맵을 다시 따는가 (배경)

2026-08-13에 아래 문제가 확인됐습니다.

```
lidar_matched: False           라이다는 정상 회전(스캔 데이터 수신 중)
lidar_matching_score: 0.21     그런데 지도와 맞는 지점이 하나도 없음
good_constraint_count: 0
alerts: []                     하드웨어 경고 없음
```

**센서 고장이 아니라 "로봇이 자기 위치를 잘못 알고 있는 상태"** 입니다.
원인으로 지목된 것: **충전소(C1)가 앵커로 고정돼 있지 않아 물리적으로 밀림.**
충전소는 위치재조정의 **기준점**이라, 여기가 움직이면 이후 모든 좌표가 그만큼 어긋납니다.

그 결과 오늘 이런 일이 있었습니다.
- 랙이 R1 제자리에 있는데 로봇이 R1 정중앙까지 가서도 **랙 검출 0**
- 랙을 든 채 이동해 **랙이 두 번 끌려감**
- 세트가 연속 실패로 중단

---

## STEP 1 — 물리 조치 ★ 맵핑 전에 반드시

**이걸 안 하고 맵을 따면, 그 맵도 며칠 뒤 똑같이 어긋납니다.**

| # | 조치 | 이유 |
|---|---|---|
| 1 | **충전소 C1 을 바닥에 앵커 고정** | 위치재조정의 기준점. 가장 중요 |
| 2 | **랙 자리 R1 에 스토퍼·가이드 설치** | 랙 자세가 인식 성패를 가름(2026-08-13 확인) |
| 3 | **맵핑 구역의 랙·대차·이동 가능 물체 전부 치우기** | 아래 경고 참조 |
| 4 | 바닥에 랙 다리 위치 마킹(테이프) | 이후 밀림을 눈으로 확인 가능 |

> ⚠️ **3번을 빠뜨리면 안 됩니다.** 2026-08-11에 R1에 랙을 둔 채 맵핑해서
> **랙 다리 4개가 지도에 영구 장애물로 기록**됐고, 로봇이 그 자리에서 출발할 때
> `calculation_failed`(경로 계산 실패)로 움직이지 못했습니다. 랙을 치우고 다시 따서 해결했습니다.
>
> 운영 중에 랙이 그 자리에 있는 건 문제없습니다(라이다가 실시간 장애물로 처리).
> **지도에 박히는 게 문제입니다.**

### [확인]
- 충전소를 손으로 밀어봐서 안 움직이는지
- 맵핑 구역에 랙·대차가 없는지 (눈으로)

---

## STEP 2 — 맵핑 실행

로봇이 온라인이고 서버 PC와 같은 대역인지 먼저 봅니다.

```powershell
cd C:\RCS_LG
.\BackEnd\venv\Scripts\python.exe scripts\robot_check.py
```

관제 화면에서:

```
맵 관리 → 사업장·영역 선택 → [맵핑] → 로봇 연결 확인 → 시작
   → 로봇을 구역 전체에 주행시켜 지도 생성
   → 완료 후 [저장]
```

> ⚠️ **"저장" 과 "동기화"는 다릅니다.** 저장은 서버 DB에만 들어갑니다.
> 로봇에 반영하려면 STEP 4의 동기화를 반드시 해야 합니다.
> (2026-08-04에 이걸 몰라 헤맨 기록이 있습니다)

### [확인]
```powershell
.\BackEnd\venv\Scripts\python.exe -c "import os,sys; os.environ.setdefault('DB_NAME','rcs_lg_db'); os.environ.setdefault('DB_HOST','127.0.0.1'); os.environ.setdefault('DB_USER','root'); os.environ.setdefault('DB_PASSWORD','1234'); sys.path.insert(0,'BackEnd'); from app.database import SessionLocal; from app.models.map import RobotMap; db=SessionLocal(); [print(f'map id={m.id} area={m.area_id} active={m.is_active}') for m in db.query(RobotMap).order_by(RobotMap.id.desc()).limit(5).all()]; db.close()"
```
새 맵이 `is_active=True` 로 맨 위에 있어야 합니다.

---

## STEP 3 — POI 등록 ★ 랙 사이즈 주의

등록할 POI 7개:

| 이름 | 타입 | 비고 |
|---|---|---|
| `C1` | charging | 충전소 |
| `C1-1` | waypoint | 충전소 사전 접근 지점 (2단계 도킹용) |
| `R1` | **standby** | 랙 위치. **랙 사이즈를 `LG2` 로 반드시 변경** |
| `J1` `J2` `J3` `J4` | jack | 작업 위치 |

> ⚠️ **랙 사이즈 기본값이 `S600` 입니다.**
> `standby` 타입을 고르면 사이즈가 자동으로 S600 으로 붙습니다
> (`POIEditPopup.tsx` — `poi.rackSize ?? "S600"`).
> **명시적으로 LG2 로 바꾸지 않으면** 로봇에 엉뚱한 규격이 전송되고,
> 규격이 2개가 되면 펌웨어가 오매칭해서 정렬이 실패합니다.
>
> 2026-08-13 오전에 실수로 만든 `R2`(S600)가 이 문제를 일으켰습니다.

**불필요한 standby POI를 만들지 마세요.** 랙 위치는 `R1` 하나만 있으면 됩니다.

### [확인]
```powershell
.\BackEnd\venv\Scripts\python.exe -c "import os,sys; os.environ.setdefault('DB_NAME','rcs_lg_db'); os.environ.setdefault('DB_HOST','127.0.0.1'); os.environ.setdefault('DB_USER','root'); os.environ.setdefault('DB_PASSWORD','1234'); sys.path.insert(0,'BackEnd'); from app.database import SessionLocal; from app.models.map import MapPOI, RobotMap; db=SessionLocal(); m=db.query(RobotMap).filter(RobotMap.is_active==True).order_by(RobotMap.id.desc()).first(); [print(f'  id={p.id:<6} {p.name:<7} {p.poi_type:<10} rack_size={p.rack_size}') for p in db.query(MapPOI).filter(MapPOI.map_id==m.id, MapPOI.is_active==True).order_by(MapPOI.id).all()]; db.close()"
```

**확인 포인트**
- `R1` 이 `standby` / `rack_size=LG2` 인지
- `standby` 타입 POI가 **R1 하나뿐**인지 (다른 게 있으면 규격이 2개 전송됨)
- J1~J4 가 `jack` 인지

---

## STEP 4 — 로봇의 충전소·대기장소 재지정 ★ 놓치기 쉬움

맵을 새로 따면 POI id 가 전부 바뀝니다. 그런데 **로봇 레코드는 옛 POI id 를 그대로 들고 있습니다.**
이걸 안 고치면 복귀할 때 **없어진 좌표로 갑니다.**

```
관제 → 로봇 관리 → 해당 로봇 → 충전소 / 대기장소를 새 POI 로 재지정
```

### [확인]
```powershell
.\BackEnd\venv\Scripts\python.exe -c "import os,sys; os.environ.setdefault('DB_NAME','rcs_lg_db'); os.environ.setdefault('DB_HOST','127.0.0.1'); os.environ.setdefault('DB_USER','root'); os.environ.setdefault('DB_PASSWORD','1234'); sys.path.insert(0,'BackEnd'); from app.database import SessionLocal; from app.models.robot import Robot; from app.models.map import MapPOI; db=SessionLocal(); [print(f'{r.name} area={r.area_id} charging={r.charging_id}->{(db.query(MapPOI).filter(MapPOI.id==r.charging_id).first() or type(\"x\",(),{\"name\":\"없음\",\"map_id\":\"-\"})).name} standby={r.standby_id}->{(db.query(MapPOI).filter(MapPOI.id==r.standby_id).first() or type(\"x\",(),{\"name\":\"없음\",\"map_id\":\"-\"})).name}') for r in db.query(Robot).filter(Robot.is_active==True).all()]; db.close()"
```

`charging` → `C1`, `standby` → `R1` 이고 **둘 다 새 맵 소속**이어야 합니다.
로봇의 `area_id` 도 새 영역이어야 합니다.

---

## STEP 5 — 로봇에 동기화

```
맵 관리 → 동기화 → 대상 로봇 선택 → 실행
```

동기화가 하는 일:
1. 맵 데이터 전송 + 로봇의 현재 맵으로 설정
2. overlay 전송 (충전소·가상벽 + **Shelves Point**)
3. **`rack.specs` 전송** — 맵의 POI 들이 가진 `rack_size` 종류만큼만

> **Shelves Point(`type=34`, `subtype=rack`)** 가 overlay 에 있어야 `align_with_rack` 이 동작합니다.
> 동기화가 자동으로 넣어줍니다.

### [확인]
```powershell
.\BackEnd\venv\Scripts\python.exe scripts\robot_check.py
```

**반드시 확인할 것**
- `위치추정 [정상] lidar_matched=True` ← ★ **이게 False면 다음 단계로 가지 마세요**
- `랙 위치 R1 ... rack_size=LG2`

그리고 로봇에 들어간 규격 확인:
```powershell
.\BackEnd\venv\Scripts\python.exe -c "import sys; sys.argv=['x']; sys.path.insert(0,'scripts'); import rack_bench as m; [print(' ', {k:s.get(k) for k in ('width','depth','leg_size','foot_radius','margin')}) for s in m.Robot(m.load_robot_ip()).get_rack_specs()]"
```
**LG2 (0.64 × 0.545) 하나만** 나와야 합니다. 2개 이상이면 STEP 3으로 돌아가세요.

---

## STEP 6 — 위치재조정 후 최종 점검

```
1) 로봇을 충전소(C1)에 도킹  (원격제어 → 충전소 복귀)
2) 관제 → 로봇 관리 → 위치재조정   (또는 POST /api/map/relocalize)
3) 확인
```

```powershell
.\BackEnd\venv\Scripts\python.exe scripts\robot_check.py
```

| 항목 | 정상값 |
|---|---|
| `lidar_matched` | **True** |
| `lidar_matching_score` | 높을수록 좋음 (2026-08-13 문제 상황은 0.21~0.37) |
| 섀시 | 비상정지 False, 제어모드 auto |
| 잭 | progress 0.0 (내려감) |

> ⚠️ **위치재조정은 반드시 충전소에 도킹한 상태에서** 하세요.
> `relocalize_robot_to_dock()` 이 **충전소 위치를 정답으로 박아 넣기** 때문에,
> 엉뚱한 데서 하면 그 자리를 기준으로 잡아 더 나빠집니다.

---

## STEP 7 — 테스트 재개

### 순서를 지켜야 합니다

```
1) rack_bench.py     랙 인식만        ← 이게 안정적이어야 아래가 의미 있음
2) auto_cycle.py     고정 경로 사이클  ← 랙 반납 누적 오차 측정
3) random_cycle.py   랜덤 사이클      ← 예약 이어받기 등 안 밟던 경로
```

**건너뛰면 안 됩니다.** 실패했을 때 "랜덤 조합 때문인지 / 기본 흐름이 원래 안 되는지" 구분이 안 됩니다.

> `rack_bench` 는 **백엔드 없이도** 동작합니다(로봇에 직접 접속).
> `auto_cycle`·`random_cycle` 은 **백엔드가 떠 있어야** 합니다.

---

### 7-1. 랙 인식 (`rack_bench.py`)

```powershell
.\BackEnd\venv\Scripts\python.exe scripts\rack_bench.py --cond B --trials 20 --retry
```

**조작**
| 키 | 동작 |
|---|---|
| `Enter` | 자동 반복 시작 (20회 → 멈춤 → 랙 자세 바꾸고 입력 → 다음 세트) |
| `Space` | 수동 진입/반납 토글 |
| `d` | 랙 치수 측정 |
| `s` / `2` | 규격 조회 / LG2 주입 |
| `x` / `q` | 이동 취소 / 종료+결과 |

**세트 시작마다 랙 자세를 타이핑합니다.** 예: `정면`, `왼쪽으로 조금 돌림`, `오른쪽으로 많이 돌림`

> 자세를 안 적으면 나중에 **어느 자세가 문제였는지 알 수 없습니다.**
> 2026-08-12에 4시간·120회를 돌리고도 50%가 나온 세트의 자세를 몰라 재현을 못 했습니다.

**허용 각도를 재려면** — 랙 다리 간격이 0.64m 이므로:

| 한쪽 다리만 옮기는 거리 | 회전 각도 |
|---|---|
| 2.2 cm | 약 2° |
| 5.6 cm | 약 5° |
| 11 cm | 약 10° |
| 17 cm | 약 15° |

세트를 `0도` → `왼쪽 5도` → `오른쪽 5도` → `왼쪽 10도` → `오른쪽 10도` 순으로.
**작은 각도부터** 하세요. 큰 각도부터 하면 전부 실패해서 아무 기준도 안 남습니다.

**결과 읽는 법**
```
세트  랙 자세          1차  2차  3차  4차  최종실패  1차성공률   최종성공률
1    0도               19   1   0   0      0      95%        100%
2    왼쪽 5도           17   2   1   0      0      85%        100%
3    왼쪽 10도           9   4   3   0      4      45%         80%
```
- **1차 성공률** = 인식이 얼마나 깨끗한가 (자세의 좋고 나쁨)
- **최종 성공률** = 실운영에서 실제로 성공하는 비율 ← 현장에서 중요한 숫자
- 뒤 단계(3·4차)에 몰리면 지금은 되지만 여유가 없다는 뜻

**최종 실패한 것은 "랙을 봤는가"까지 나옵니다**
```
세트3 [왼쪽 10도] 4차까지 시도 — 검출 0/77회        ← 랙을 아예 못 봄
세트3 [왼쪽 10도] 4차까지 시도 — 검출 11/84, 0.71x0.58  ← 봤는데 치수가 큼(체인 등 간섭)
```
이 둘은 원인이 다릅니다.

---

### 7-2. 고정 경로 사이클 (`auto_cycle.py`)

```powershell
# 흐름 확인 (로봇 안 움직임)
.\BackEnd\venv\Scripts\python.exe scripts\auto_cycle.py --dry-run

# 1사이클 — 옆에서 지켜보며, Ctrl+C 로 비상정지 실동작도 확인
.\BackEnd\venv\Scripts\python.exe scripts\auto_cycle.py --rounds 1

# 5~10 사이클 — 랙 반납 누적 오차
.\BackEnd\venv\Scripts\python.exe scripts\auto_cycle.py --rounds 10
```

흐름: `호출(J1) → 랙 픽업 → J1 도착 → 경유지 J2,J3,J4 → 각 도착마다 자동 확인 → 복귀`

**이 테스트의 목적은 "돌아가는지"가 아니라 "랙 반납이 밀리는지"입니다.**

```
사이클  픽업+이동   →J2   →J3   →J4   복귀   합계
1           28      45    38    41    112    264
2           31      44    39    40    115    269
3           71      46    38    42    118    315   ← 픽업이 길어짐 = 랙이 밀리는 중
----------------------------------------------------
픽업 소요 추세: 28초 → 71초  (증가 ↑)
⚠️ 랙 반납 위치가 밀리고 있을 가능성
```

**픽업 시간이 우상향하면** 몇 사이클마다 사람이 랙을 맞춰줘야 하는지가 정해집니다.

> 근본 원인 후보: 복귀 시 랙 반납이 `standard` 이동입니다
> ([dispatch_service.py](file:///C:/RCS_LG/BackEnd/app/services/dispatch_service.py) `_return_to_standby_and_park`).
> `standard` 는 **로봇**을 POI에 세우는 것이라 **랙**은 그만큼 어긋난 자리에 놓입니다.
> `to_unload_point` 로 바꾸는 개선이 검토 중입니다(미적용).

---

### 7-3. 랜덤 사이클 (`random_cycle.py`)

```powershell
.\BackEnd\venv\Scripts\python.exe scripts\random_cycle.py --dry-run
.\BackEnd\venv\Scripts\python.exe scripts\random_cycle.py --seed 42 --cycles 3
.\BackEnd\venv\Scripts\python.exe scripts\random_cycle.py --seed 42 --cycles 20
```

```
1. 호출 POI 랜덤 (J1~J4)
2. 픽업 → 도착
3. 랜덤 분기① — 콘솔의 [경유지 등록] vs [종료]
4. 각 도착 → 랜덤 대기 → [확인]  (낮은 확률로 도중 [종료])
5. 랜덤 분기② — 마지막 확인 전에 확률로 다른 POI 예약
     예약 있음 → 자동 이어받기 → 3번으로 (루프)
     예약 없음 → 복귀
```

> **`--seed` 를 반드시 고정하세요.** 문제가 났을 때 같은 순서를 재현하지 못하면 원인 분석이 불가능합니다.

**불변식을 매번 검사합니다** — 위반하면 백엔드 결함 후보입니다.
- 보낸 경유지 순서 == 실제 방문 순서
- `is_last` 표시가 실제와 일치

**종료 사유를 구분해서 기록합니다.**
```
3회  랜덤: 경유지 없이 종료
2회  예약 없음 → 자동 복귀
1회  예약 있었으나 이어받기 안 됨    ← 원인 확인 필요(배터리/점유/결함)
```

---

## 8. 테스트 중 안전

세 스크립트 모두 아래가 들어 있습니다.

| 상황 | 동작 |
|---|---|
| **비상정지(E-STOP)** | **실패로 세지 않고 대기.** 해제하면 자동으로 이어짐 (최대 5분) |
| **통신 두절** | 대기 후 재개. 성공률 집계에서 제외 |
| **이동 명령 전** | **잭 상태를 로봇에 직접 확인** → 올라가 있으면 먼저 내림 |
| `x` 키 | 현재 이동 취소 |
| `Ctrl+C` | 이동 취소 → 세션 정리 → 스냅샷 저장 → 종료 |

> ⚠️ **잭 판정 기준은 `progress` 입니다** (`weight` 아님).
> ```
> 랙 적재:   progress 1.0, weight 65
> 내려놓음:  progress 0.0, weight  0
> 잭만 올림: progress 1.0, weight  0~26   ← weight 로는 구분 불가
> ```
> 2026-08-13에 weight 로 판정했다가 **랙을 든 채 후진해 랙을 끌고 간 사고**가 두 번 있었습니다.

**[확인] 버튼을 시간으로 대체하는 것은 테스트 전용입니다.** 현장 운영에 쓰지 마세요.

---

## 9. 문제 해결

### 9-1. `lidar_matched=False` 가 안 없어진다
```
1) 로봇을 충전소에 도킹했는지 확인 (도킹 안 하고 재조정하면 더 나빠짐)
2) 위치재조정 재실행
3) 그래도 False면 → 로봇 재부팅 (원격제어 → 시스템 재시작, 약 90초)
     부팅 후 boot_recovery 가 충전소 기준으로 자동 재조정
4) 재부팅해도 False면 → 하드웨어 점검
```
하드웨어 점검용 서비스(로봇에 존재 확인됨):
```
/services/imu/recalibrate            IMU 재보정 (평평한 곳에 정지 상태로)
/services/imu/calibrate_gyro_scale   자이로 스케일
/services/baseboard/power_on_lidar   라이다 전원 재인가
/services/reset_usb_devices          USB 리셋
/services/monitor_recheck_errors     에러 재점검
```
경고 확인: `/alerts` 토픽이 비어 있으면(`[]`) 하드웨어 경고는 없는 것입니다.

### 9-2. 랙 검출이 0
- **로봇이 랙 아래에 있어야 검출됩니다.** 앞에서 1.6m 떨어져 바라보면 안 잡힙니다(실측 확인)
- 랙이 실제로 R1 자리에 있는지 눈으로 확인
- `lidar_matched` 가 False면 로봇이 엉뚱한 데 있는 것 → 9-1

### 9-3. 정렬이 `fail_reason=506` 으로 실패
`jack is in up state` — 잭이 올라가 있어 이동이 거부된 것입니다. **랙 인식 실패가 아닙니다.**
스크립트가 자동으로 잭을 내리고 이어가지만, 반복되면 수동으로 잭 다운 후 재시작하세요.

### 9-4. 정렬이 `fail_reason=501` 로 실패
`rack_detection_error` — **진짜 랙 인식 실패**입니다. 로그의 "랙 관측"을 보세요.
- `검출 0` → 랙을 아예 못 봄 (위치·자세 문제)
- `검출 있음 + 치수가 큼` → 체인 등 간섭물을 다리로 오인

### 9-5. 로봇에 규격이 2개 이상 들어감
맵에 `standby` POI 가 2개 이상이거나 `rack_size` 가 서로 다른 것입니다.
STEP 3 [확인]으로 POI 를 점검하고, 불필요한 standby 를 비활성화한 뒤 다시 동기화하세요.

---

## 10. 참고 — 2026-08-13까지 확인된 것

| 항목 | 결과 |
|---|---|
| LG2 규격 `0.64 × 0.545` | **정확함.** 검출 성공 시 소수점까지 일치, 흔들림 0 |
| 정면 자세 | **100%** (20/20, 16/16) |
| 왼쪽으로 튼 자세 | 실패 발생 — 실패 시 **랙 검출 0** (치수 불일치가 아님) |
| 접지 체인 | 제거 불가(접지용). **수직 구간이 라이다 평면을 반드시 통과** → 기둥에 밀착 고정 권장 |
| 랙 검출 범위 | **로봇이 랙 아래에 있어야만** 잡힘. 사전 측정 불가 |
| `/detected_rack` | map 좌표계로 랙 위치·각도 제공 (측정 안정성 0.6cm / 0.5도) |
| 하드웨어 | 라이다 정상 회전, `/alerts` 비어 있음, IMU `icm42688` |

### 미해결 / 검토 중

- 랙 반납을 `to_unload_point` 로 변경 (누적 오차 근본 해결) — 미적용
- `align_with_rack` 회전 허용 파라미터 — `rack.specs` 에 없음. AutoXing 문의 필요
- 랙이 몇 도까지 틀어져도 되는지 — **STEP 7-1 로 측정 예정**

---

## 변경 이력

| 날짜 | 내용 |
|---|---|
| 2026-08-13 | 최초 작성 |
