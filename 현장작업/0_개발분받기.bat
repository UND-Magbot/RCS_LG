@echo off
chcp 949 > nul
title [0] 개발분 받기 (현장 설정은 안 건드림)
setlocal EnableDelayedExpansion

rem ============================================================
rem  개발 PC 에서 push 한 로직을 서버PC 사용폴더로 가져온다.
rem
rem  ★ 브랜치를 바꾸지 않는다. 경로를 지정해 그 파일들만 가져온다.
rem    - 사용폴더 저장소(field/...)와 개발 저장소는 히스토리가 다르다.
rem      git pull(merge) 은 "unrelated histories" 로 거부된다.
rem      checkout 은 히스토리와 무관하게 그 경로의 파일만 꺼낸다.
rem    - 현장 설정은 전부 BackEnd\app 밖이라 손대지 않는다.
rem        BackEnd\default_area.json           어느 맵을 쓸지
rem        BackEnd\static\robot_speed.json     로봇별 속도
rem        BackEnd\static\system_settings.json 주행/음성 설정
rem        BackEnd\static\maps\                맵 이미지
rem        BackEnd\config.env                  DB 접속정보
rem        BackEnd\run_server.ps1              서버 실행 스크립트
rem
rem  ※ 인터넷이 필요하다. 현장 안에서는 안 된다 - 탈의실에서 실행할 것.
rem ============================================================

set BRANCH=feature/route-only-drive
set TARGETS=BackEnd/app scripts/clock_sync.py scripts/run_probe.py scripts/fetch_bags.py
set CFG=%~dp0사용폴더경로.txt

set USEDIR=
if exist "%CFG%" set /p USEDIR=<"%CFG%"

echo.
echo ============================================================
echo  [0] 개발분 받기
echo ============================================================
echo.
echo   브랜치    : %BRANCH%
echo   가져올 것 : BackEnd\app  +  scripts 3개
echo.
if not "%USEDIR%"=="" (
  echo   사용폴더  : %USEDIR%
  set /p YN=이 경로가 맞습니까? [Y/n]:
  if /i "!YN!"=="n" set USEDIR=
)
if "!USEDIR!"=="" (
  echo.
  echo   사용폴더 = 실제로 백엔드가 돌아가는 폴더
  set /p USEDIR=사용폴더 전체 경로:
)
set USEDIR=!USEDIR:"=!
if "!USEDIR:~-1!"=="\" set USEDIR=!USEDIR:~0,-1!

if not exist "!USEDIR!\BackEnd" goto :no_backend
if not exist "!USEDIR!\.git" goto :no_git
echo !USEDIR!>"%CFG%"

pushd "!USEDIR!"

echo.
echo ------------------------------------------------------------
echo  [1/4] 지금 상태 (되돌릴 때 필요)
echo ------------------------------------------------------------
for /f "delims=" %%i in ('git rev-parse --short HEAD') do set BEFORE=%%i
for /f "delims=" %%i in ('git branch --show-current') do set CURBR=%%i
echo   브랜치 : !CURBR!
echo   커밋   : !BEFORE!
echo.
echo   되돌리려면 :  git checkout !BEFORE! -- %TARGETS%

echo.
echo ------------------------------------------------------------
echo  [2/4] 받아오기 (인터넷 필요)
echo ------------------------------------------------------------
git fetch origin %BRANCH%
if errorlevel 1 goto :fetch_fail
echo   완료

echo.
echo ------------------------------------------------------------
echo  [3/4] 무엇이 바뀌는지 미리보기
echo ------------------------------------------------------------
git diff --stat HEAD FETCH_HEAD -- %TARGETS%
echo.
echo   위 목록만 바뀝니다. 설정 파일이 목록에 없어야 정상입니다.
echo.
set GO=
set /p GO=적용할까요? [y/N]:
if /i not "!GO!"=="y" goto :cancelled

echo.
echo ------------------------------------------------------------
echo  [4/4] 적용
echo ------------------------------------------------------------
git checkout FETCH_HEAD -- %TARGETS%
if errorlevel 1 goto :checkout_fail
echo   완료

echo.
echo ============================================================
echo  검증 - 현장 설정이 그대로인지 확인
echo ============================================================
set CFGCHK=%TEMP%\cfgchk_%RANDOM%.txt
git status --short -- BackEnd/default_area.json BackEnd/static/robot_speed.json BackEnd/static/system_settings.json > "!CFGCHK!" 2>nul
set SZ=1
for %%A in ("!CFGCHK!") do set SZ=%%~zA
if "!SZ!"=="0" (
  echo   [OK] 설정 3종 그대로 - 아무것도 바뀌지 않았습니다.
) else (
  echo   [경고] 설정이 바뀌었습니다:
  type "!CFGCHK!"
  echo.
  echo   되돌리려면:
  echo     git checkout -- BackEnd/default_area.json BackEnd/static/robot_speed.json BackEnd/static/system_settings.json
)
del "!CFGCHK!" 2>nul

echo.
echo ------------------------------------------------------------
echo  다음에 할 일
echo ------------------------------------------------------------
echo   1) 백엔드 재시작
echo   2) 기동 로그에 "Application startup complete" 확인
echo   3) 기록으로 남기려면 :
echo        git add -A  그리고  git commit -m "chore: 개발분 반영"
echo.
popd
goto :done

:no_backend
echo.
echo   [중단] !USEDIR!\BackEnd 가 없습니다. 경로를 확인하세요.
del "%CFG%" 2>nul
goto :done

:no_git
echo.
echo   [중단] 사용폴더가 git 저장소가 아닙니다.
echo          A_사용폴더_git올리기.bat 을 먼저 실행하세요.
goto :done

:fetch_fail
echo.
echo   [중단] fetch 실패. 인터넷 연결을 확인하세요.
echo          현장 안에서는 안 됩니다 - 탈의실에서 실행하세요.
popd
goto :done

:checkout_fail
echo   [실패] checkout 이 안 됐습니다.
popd
goto :done

:cancelled
echo   취소했습니다. 아무것도 바뀌지 않았습니다.
popd
goto :done

:done
echo.
pause
