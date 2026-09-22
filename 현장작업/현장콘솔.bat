@echo off
chcp 949 > nul
title 현장 점검 콘솔
cd /d "%~dp0.."

echo.
echo ============================================================
echo  현장 점검 콘솔
echo ============================================================
echo.
echo  브라우저가 자동으로 열립니다.
echo  폰/태블릿에서도 볼 수 있습니다 - 잠시 뒤 아래에 주소가 뜹니다.
echo.
echo  ** 이 검은 창을 닫지 마세요. 닫으면 화면도 멈춥니다 **
echo     종료할 때는 이 창에서 Ctrl+C
echo.

if not exist "BackEnd\venv\Scripts\python.exe" (
  echo  [오류] BackEnd\venv\Scripts\python.exe 가 없습니다.
  echo         가상환경 경로를 확인하세요.
  pause
  exit /b 1
)
if not exist "scripts\field_console.py" (
  echo  [오류] scripts\field_console.py 가 없습니다.
  echo.
  echo         받는 법:
  echo           git fetch origin
  echo           git checkout origin/feature/lg_luke -- scripts/field_console.py
  pause
  exit /b 1
)

"BackEnd\venv\Scripts\python.exe" scripts\field_console.py
echo.
echo  콘솔이 종료됐습니다.
pause
