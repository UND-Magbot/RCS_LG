# 작업 디렉터리 고정 — app.main 임포트와 _logs 상대경로가 여기 기준이다
Set-Location $PSScriptRoot

$env:DB_NAME="rcs_lg_db"
$env:DB_HOST="127.0.0.1"      # 로컬
$env:DB_USER="root"
$env:DB_PASSWORD="1234"       # 로컬 MariaDB root 비번 (구 unde5466 는 원격 서버용)

# 로그 폴더 (없으면 uvicorn 이 시작 시 에러남)
if (-not (Test-Path "$PSScriptRoot\_logs")) { New-Item -ItemType Directory "$PSScriptRoot\_logs" | Out-Null }

# --log-config : 화면 출력 유지 + _logs\backend.log 에 동시 기록(10MB x 5 자동순환).
#                uvicorn 리로더 부모까지 적용돼 "Reloading" 메시지도 파일에 남는다.
& "$PSScriptRoot\venv\Scripts\python.exe" -m uvicorn app.main:app --reload --reload-exclude "_logs/*" --host 0.0.0.0 --port 8002 --log-config "$PSScriptRoot\log_config.json"
