@echo off
chcp 949 > nul
title [0] 개발분 받기 (현장 설정은 안 건드림)
setlocal EnableDelayedExpansion

rem ============================================================
rem  개발 PC 에서 push 한 로직을 서버PC 사용폴더로 가져온다.
rem
rem  ★ 브랜치를 바꾸지 않는다. 경로를 지정해 그 파일들만 가져온다.
rem    개발 저장소와 사용폴더 저장소는 히스토리가 다르다(git init 으로 따로 만듦).
rem    git pull(merge) 은 "unrelated histories" 로 거부되고,
rem    브랜치 전환은 현장 설정을 개발 PC 값으로 덮는다.
rem    checkout 에 경로를 주면 히스토리와 무관하게 그 파일만 바뀐다.
rem
rem  ※ 인터넷이 필요하다. 현장 안에서는 안 된다 - 탈의실에서 실행할 것.
rem ============================================================

set BRANCH=feature/route-only-drive
set TARGETS=BackEnd/app scripts/clock_sync.py scripts/run_probe.py scripts/fetch_bags.py scripts/field_session.py scripts/drive_log.py scripts/field_console.py .gitignore 현장작업
set CFG=%~dp0사용폴더경로.txt

set USEDIR=
if exist "%CFG%" set /p USEDIR=<"%CFG%"

echo.
echo ============================================================
echo  [0] 개발분 받기
echo ============================================================
echo.
echo   브랜치    : %BRANCH%
echo   가져올 것 : BackEndpp  +  scripts 6개  +  .gitignore  +  현장작업
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
echo  [1/5] 지금 상태 (되돌릴 때 필요)
echo ------------------------------------------------------------
for /f "delims=" %%i in ('git rev-parse --short HEAD') do set BEFORE=%%i
for /f "delims=" %%i in ('git branch --show-current') do set CURBR=%%i
echo   브랜치 : !CURBR!
echo   커밋   : !BEFORE!
echo.
echo   되돌리려면 :  git checkout !BEFORE! -- %TARGETS%

echo.
echo ------------------------------------------------------------
echo  [2/5] 받아오기 (인터넷 필요)
echo ------------------------------------------------------------
git fetch origin %BRANCH%
if errorlevel 1 goto :fetch_fail
echo   완료

echo.
echo ------------------------------------------------------------
echo  [3/5] 무엇이 바뀌는지 미리보기
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
echo  [4/5] 적용
echo ------------------------------------------------------------
git checkout FETCH_HEAD -- %TARGETS%
if errorlevel 1 goto :checkout_fail
echo   완료

echo.
echo ------------------------------------------------------------
echo  [5/5] 장소별 설정을 git 추적에서 빼기
echo ------------------------------------------------------------
rem  맵 번호/로봇 속도/주행 설정은 장소마다 값이 달라야 한다.
rem  .gitignore 에 넣어도 이미 추적 중이면 무시되지 않으므로 여기서 뺀다.
rem  --cached 라서 인덱스에서만 빠지고 로컬 파일은 그대로 남는다.
git rm --cached -q BackEnd/default_area.json BackEnd/static/robot_speed.json BackEnd/static/system_settings.json > nul 2>&1
if errorlevel 1 (
  echo   이미 빠져 있습니다 - 넘어갑니다.
) else (
  echo   완료 - 이제 브랜치를 바꿔도 이 셋은 안 덮입니다.
)
echo.
echo   파일이 그대로 있는지 확인
if exist "BackEnd\default_area.json" (echo     [OK] default_area.json) else (echo     [주의] default_area.json 이 없습니다)
if exist "BackEnd\static\robot_speed.json" (echo     [OK] robot_speed.json) else (echo     [주의] robot_speed.json 이 없습니다)
if exist "BackEnd\static\system_settings.json" (echo     [OK] system_settings.json) else (echo     [주의] system_settings.json 이 없습니다)

echo.
echo ============================================================
echo  검증 - 현장 설정 값이 그대로인지
echo ============================================================
set CFGCHK=%TEMP%\cfgchk_%RANDOM%.txt
git status --short -- BackEnd/default_area.json BackEnd/static/robot_speed.json BackEnd/static/system_settings.json > "!CFGCHK!" 2>nul
set SZ=1
for %%A in ("!CFGCHK!") do set SZ=%%~zA
if "!SZ!"=="0" (
  echo   [OK] 값이 바뀌지 않았습니다.
) else (
  echo   아래는 추적 해제 표시일 수 있습니다. D 로 시작하면 정상입니다.
  type "!CFGCHK!"
)
del "!CFGCHK!" > nul 2>&1

echo.
echo ------------------------------------------------------------
echo  다음에 할 일
echo ------------------------------------------------------------
echo   1) 백엔드 재시작
echo   2) 기동 로그에 Application startup complete 확인
echo   3) 기록으로 남기려면
echo        git add -A     그리고     git commit -m "chore: 개발분 반영"
echo.
popd
goto :done

:no_backend
echo.
echo   [중단] !USEDIR!\BackEnd 가 없습니다. 경로를 확인하세요.
del "%CFG%" > nul 2>&1
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
