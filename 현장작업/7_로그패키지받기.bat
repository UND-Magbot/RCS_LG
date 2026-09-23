@echo off
chcp 949 > nul
title [7] 제조사 제출용 로그(bag) 받기
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
call "%~dp0config.bat"

echo.
echo ============================================================
echo  [7] 제조사 제출용 로그(bag) 받기
echo ============================================================
echo.
echo  로봇은 주행 로그를 10분 단위로 **자동 녹화**하고 있습니다.
echo  화면의 파란 버튼을 누르지 않아도 이미 저장돼 있습니다.
echo.
echo  용량이 차면 오래된 것부터 지워지므로 필요한 시간대를 빨리 받아두세요.
echo.
echo  로봇: %ROBOT_IP%
echo ------------------------------------------------------------
echo.

if not exist "BackEnd\venv\Scripts\python.exe" (
  echo  [오류] BackEnd\venv\Scripts\python.exe 가 없습니다.
  pause & exit /b 1
)
if not exist "scripts\fetch_bags.py" (
  echo  [오류] scripts\fetch_bags.py 가 없습니다.
  echo         git pull 로 받으세요.
  pause & exit /b 1
)

echo  먼저 어떤 로그가 있는지 봅니다...
echo.
"BackEnd\venv\Scripts\python.exe" scripts\fetch_bags.py --ip %ROBOT_IP%
if errorlevel 1 (
  echo.
  echo  목록을 못 받았습니다. 로봇과 같은 망에 있는지 확인하세요.
  pause & exit /b 1
)

echo.
echo ------------------------------------------------------------
echo  받을 시간대를 정하세요. 비이상적 정지가 난 시각 앞뒤로 넉넉히 잡으면 됩니다.
echo  (10분 단위 파일이라 한 파일이 약 25~45 MB 입니다)
echo ------------------------------------------------------------
set DAY=
set FRM=
set TOO=
set /p DAY=날짜 (YYYY-MM-DD):
set /p FRM=시작 시각 (HH:MM):
set /p TOO=끝 시각   (HH:MM):

if "!DAY!"=="" (
  echo  날짜를 안 넣어서 취소합니다.
  pause & exit /b 0
)

echo.
"BackEnd\venv\Scripts\python.exe" scripts\fetch_bags.py --ip %ROBOT_IP% --date !DAY! --from !FRM! --to !TOO!

echo.
echo ============================================================
echo  받은 파일은  _logs\bags\  에 있습니다.
echo  이 파일을 제조사(AutoXing)에 전달하세요.
echo.
echo  중간에 끊겼으면 이 창을 다시 실행하세요 ? 이어서 받습니다.
echo ============================================================
echo.
pause
