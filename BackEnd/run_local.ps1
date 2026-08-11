$env:DB_NAME="rcs_lg_db"
$env:DB_HOST="127.0.0.1"      # 로컬
$env:DB_USER="root"
$env:DB_PASSWORD="1234"       # 로컬 MariaDB root 비번 (구 unde5466 는 원격 서버용)
& "$PSScriptRoot\venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8002
