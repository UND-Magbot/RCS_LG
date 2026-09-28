@echo off
chcp 949 > nul
title [9] 반영 확인
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

echo.
echo ============================================================
echo  개발분이 제대로 들어왔는지 확인  (읽기만 합니다)
echo ============================================================
echo.

set NG=0

call :chk "BackEnd\app\services\jack_service.py"      "def wait_jack_settled"      "잭 완료 확인"
call :chk "BackEnd\app\services\jack_service.py"      "JACK_SETTLE_TIMEOUT"        "잭 대기 상한 25초"
call :chk "BackEnd\app\services\jack_service.py"      "def decelerate_to_stop"     "종료 시 감속 정지"
call :chk "BackEnd\app\services\jack_service.py"      "def drive_straight"         "랙에서 직선 이탈"
call :chk "BackEnd\app\services\jack_service.py"      "STRAIGHT_SPEED"             "이탈 속도 0.15"
call :chk "BackEnd\app\services\dispatch_service.py"  "def _escape_after_unload"   "강제종료 후 랙 이탈"
call :chk "BackEnd\app\services\dispatch_service.py"  "UNLOAD_ESCAPE_M"            "이탈 거리 1.2m"
call :chk "BackEnd\app\services\dispatch_service.py"  "def _load_any_charging_poi" "충전소 폴백 조회"
call :chk "BackEnd\app\services\scheduler.py"         "_move_via_waypoints"        "복귀가 경유지 사용"
call :chk "BackEnd\app\services\waypoint_route.py"    "coords: list[str] = [f"     "좌표열에 출발점 포함"
call :chk "BackEnd\app\services\boot_recovery.py"     "jack_service.is_laden(ip)"  "적재 플래그 보호"
call :chk "BackEnd\app\routers\robot.py"              "jack_service.jack_up"       "원격 잭이 서비스 경유"
call :chk "scripts\clock_sync.py"                     "def measure"                "시계 차이 측정 도구"
call :chk "scripts\drive_log.py"                      "def _clock_offset"          "마크에 로봇시각 환산"
call :chk "scripts\run_probe.py"                      "def log_clock"              "주행기록에 시계 기록"
call :chk "scripts\fetch_bags.py"                     "clock_sync"                 "bag 받을 때 시계 기록"

echo.
echo ------------------------------------------------------------
echo  종료 순서  (stop 줄번호가 cancel 보다 작아야 정상)
echo ------------------------------------------------------------
echo   stop_robot_job :
for /f "tokens=1 delims=:" %%L in ('findstr /n /c:"jack_service.stop_robot_job" "BackEnd\app\services\dispatch_service.py"') do echo        %%L
echo   cancel_move    :
for /f "tokens=1 delims=:" %%L in ('findstr /n /c:"jack_service.cancel_current_move" "BackEnd\app\services\dispatch_service.py"') do echo        %%L

echo.
echo ------------------------------------------------------------
echo  현장 설정  (바뀌면 안 되는 것)
echo ------------------------------------------------------------
echo   default_area.json :
type "BackEnd\default_area.json"
echo.
echo   robot_speed.json :
type "BackEnd\static\robot_speed.json"

echo.
echo ============================================================
if "!NG!"=="0" (
  echo   결과 : 전부 들어왔습니다.
) else (
  echo   결과 : !NG! 건이 없습니다. 위에서 [없음] 을 확인하세요.
)
echo ============================================================
echo.
pause
exit /b

:chk
findstr /c:%2 %1 > nul 2>&1
if errorlevel 1 (
  echo   [없음]  %~3
  set /a NG+=1
) else (
  echo   [OK]    %~3
)
exit /b
