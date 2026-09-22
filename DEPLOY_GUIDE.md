# RCS_LG 서버 구축 안내

> 이 패키지는 RCS_LG 시스템을 새 서버에 구축하기 위한 최소 구성입니다.
> 가상환경(venv), node_modules, 빌드 산출물(.next)은 **의도적으로 제외**되어 있으며
> 서버에서 새로 생성합니다.

---

## 1. 패키지 구성

```
RCS_LG_deploy/
├── BackEnd/
│   ├── app/                     백엔드 소스 (FastAPI)
│   ├── static/maps/             ★ 맵핑 산출물 59개 (반드시 필요)
│   ├── static/audio/            음성 파일
│   ├── Dockerfile
│   ├── requirements_linux.txt   ★ 서버 설치는 이 파일만 사용
│   ├── config.env.example       설정 템플릿
│   ├── default_area.json        기본 area 설정 (area_id=22)
│   └── run_local.ps1            (윈도우 직접 실행 시 참고)
├── frontend/                    관제 화면 소스 (Next.js)
├── _db/
│   └── undrcs_원본_260811.sql   ★ DB 덤프 (반드시 복원 필요)
├── docker-compose.yml
├── .env.example
└── 배포_안내.md                 (이 문서)
```

---

## 2. ⚠️ 먼저 알아야 할 주의사항 4가지

### ① 패키지 설치는 `requirements_linux.txt` 만 사용

원본 저장소에는 requirements 파일이 3개 있으나, **서버 구축에는 `requirements_linux.txt` 하나만**
사용합니다. (이 패키지에는 그것만 포함했습니다.)

나머지 두 개는 개발 PC 전체를 freeze 한 것이라 torch, PyQt5, pyrealsense2 등
이 시스템이 사용하지 않는 대용량 패키지가 포함되어 있습니다.

### ② `.env` 의 DB 이름을 반드시 `rcs_lg_db` 로

`docker-compose.yml` 과 `.env.example` 의 기본값이 다른 프로젝트(VESA) 값으로 되어 있습니다.
반드시 `.env` 에서 덮어써야 합니다.

```
DB_NAME=rcs_lg_db          ← 기본값(rcs_basic_db / rcs_db)이 아님에 주의
```

### ③ 포트 8002는 VESA 버전과 동일

**같은 서버에 VESA와 LG를 동시에 띄울 수 없습니다.** 별도 서버를 쓰거나 포트를 변경하세요.

### ④ `NEXT_PUBLIC_API_URL` 은 빌드 시점에 코드에 박힙니다

나중에 서버 IP가 바뀌면 환경변수만 고쳐서는 반영되지 않습니다.
프론트엔드를 `--no-cache` 로 **재빌드**해야 합니다.

---

## 3. 구축 절차 (Docker — 리눅스 서버)

### 3-1. `.env` 생성

패키지 최상위에 `.env` 파일을 만듭니다.

```bash
cat > .env << 'EOF'
SERVER_IP=서버IP
DB_PORT=3306
DB_USER=root
DB_PASSWORD=비밀번호
DB_NAME=rcs_lg_db
NEXT_PUBLIC_API_URL=http://서버IP:8002
EOF
```

### 3-2. DB 생성 및 덤프 복원

```bash
mysql -u root -p -e "CREATE DATABASE rcs_lg_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
mysql -u root -p rcs_lg_db < _db/undrcs_원본_260811.sql
```

### 3-3. 맵 파일 확인

```bash
ls BackEnd/static/maps | wc -l      # 59 가 나와야 정상
```

> 이 파일들이 없으면 맵 동기화 시
> **"서버에 저장된 매핑 데이터가 없습니다"(400)** 오류가 발생하여 로봇에 맵을 내려보낼 수 없습니다.

### 3-4. 기동

```bash
docker-compose up -d --build
```

### 3-5. 정상 동작 확인

```bash
curl http://localhost:8002/ping      # {"message":"pong"}
curl http://localhost:8002/health    # database: ok 확인
```

---

## 4. 접속 주소

| 대상 | 주소 |
|---|---|
| 관제 웹 | `http://서버IP:3002` |
| 백엔드 API | `http://서버IP:8002` |
| API 문서 (Swagger) | `http://서버IP:8002/docs` |
| 배차 콘솔 | `http://서버IP:8002/api/dispatch/console` |
| 로봇 부착 태블릿 | `http://서버IP:8002/api/dispatch/robot-tablet/{robot_id}` |

---

## 5. 구축 후 반드시 확인할 것

| # | 확인 항목 | 이유 |
|---|---|---|
| 1 | `robots` 테이블의 `ip_address` | 덤프에 들어있는 IP는 기존 환경 값이므로 현장 로봇 IP로 변경 필요 |
| 2 | `robots.charging_id` / `standby_id` | NULL이면 작업 종료 시 충전소 복귀가 동작하지 않음 |
| 3 | 맵 [메인 적용] 상태 | `default_area.json` 기준 area_id=22. 콘솔 POI 목록이 이 area 기준으로 표시됨 |
| 4 | 서버 → 로봇 통신 | `curl http://로봇IP:8090/chassis/current-map` 응답 확인 |
| 5 | 방화벽 8002 / 3302 | 콘솔·태블릿이 접속해야 하므로 개방 필요 (3002, 8002) |

---

## 6. (참고) 윈도우 서버에서 직접 실행하는 경우

Docker를 사용하지 않을 때의 절차입니다.

1. Python 3.11 설치
2. `cd BackEnd && python -m venv venv`
3. `venv\Scripts\pip install -r requirements_linux.txt`
4. `config.env.example` → `config.env` 로 복사 후 DB 정보 입력
5. `run_local.ps1` 을 참고해 실행 (DB 접속 정보는 실제 서버 값으로 수정)
6. 프론트는 `BUILD_STATIC=1 npm run build` 후 생성되는 `out/` 을 `BackEnd/web/` 으로 복사하면
   백엔드가 8002 포트에서 관제 화면까지 함께 서빙합니다.
   (`config.env` 의 `WEB_DIR` 주석 참고)

---

## 7. 시스템 구조를 이해해야 할 경우

원본 저장소의 `_시스템구조_정리.md` 문서에 프론트/백엔드/DB/로봇 간 통신 방식,
배차 상태 머신, 운영 루틴이 정리되어 있습니다. 필요 시 별도 요청 바랍니다.
