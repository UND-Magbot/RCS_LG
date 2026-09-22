@echo off
chcp 949 > nul
title [A2] 사용폴더를 git 저장소로 - 기준점 커밋
setlocal EnableDelayedExpansion

rem ============================================================
rem  A1 에서 M 목록을 확인한 뒤에 실행한다.
rem  .gitignore 정리 -^> skip-worktree -^> 새 브랜치 -^> 커밋 -^> push
rem ============================================================

set CFG=%~dp0사용폴더경로.txt
if not exist "%CFG%" (
  echo  [중단] 사용폴더경로.txt 가 없습니다. A1 을 먼저 실행하세요.
  pause & exit /b 1
)
set /p USEDIR=<"%CFG%"

if not exist "!USEDIR!\.git" (
  echo  [중단] 사용폴더에 .git 이 없습니다. A1 을 먼저 실행하세요.
  pause & exit /b 1
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i
set BRANCH=field/baseline-%TODAY%

echo.
echo ============================================================
echo  [A2] 기준점 커밋
echo ============================================================
echo.
echo  사용폴더 : !USEDIR!
echo  새 브랜치: %BRANCH%
echo.
echo  A1 에서 [M] 목록을 확인하셨습니까?
echo  소스 코드 수정분이 있었다면 그 내용을 파악한 뒤에 진행하세요.
echo.
set /p YN=확인했습니다. 진행할까요? [y/N]:
if /i not "!YN!"=="y" (
  echo  중단합니다.
  pause & exit /b 0
)

pushd "!USEDIR!"

rem ── 1) .gitignore ────────────────────────────────────────
echo.
echo ------------------------------------------------------------
echo  [1/5] .gitignore 보완
echo ------------------------------------------------------------
(
echo.
echo # ── git 전환 시 추가 ^(%TODAY%^) ──
echo BackEnd/venv/
echo BackEnd/venv_old/
echo frontend/node_modules/
echo frontend/.next/
echo frontend/.next_old_asus/
echo frontend/out/
echo BackEnd/web/
echo __pycache__/
echo *.pyc
echo BackEnd/config.env
echo _db/
echo *.sql
echo BackEnd/static/maps/
echo BackEnd/_logs/
echo _logs/
echo _backup/
echo *.bak
echo *.bak_*
echo TabletApp/.gradle/
echo TabletApp/app/build/
echo TabletApp/build/
echo TabletApp/local.properties
echo 로그수집_*.zip
echo 현장작업/사용폴더경로.txt
) >> .gitignore
echo   완료

rem ── 2) 이미 추적 중인 것 해제 ────────────────────────────
echo.
echo ------------------------------------------------------------
echo  [2/5] 이미 추적 중인 맵 파일 해제  ^(--cached: 파일은 남습니다^)
echo ------------------------------------------------------------
for /f %%c in ('git ls-files BackEnd/static/maps ^| find /c /v ""') do set NMAPS=%%c
echo   추적 중인 맵 파일: %NMAPS% 개
if not "%NMAPS%"=="0" (
  git rm --cached -r -q BackEnd/static/maps
  echo   추적 해제 완료 ^(디스크 파일은 그대로^)
)
for /f %%c in ('dir /b /s "BackEnd\static\maps" 2^>nul ^| find /c /v ""') do set NDISK=%%c
echo   디스크에 남은 맵 파일: %NDISK% 개   ^<- 0 이면 안 됩니다

rem ── 3) PC별 설정 무시 ────────────────────────────────────
echo.
echo ------------------------------------------------------------
echo  [3/5] PC별 설정 파일 변경 무시 ^(skip-worktree^)
echo ------------------------------------------------------------
if exist "BackEnd\default_area.json" git update-index --skip-worktree BackEnd/default_area.json 2>nul && echo   default_area.json
if exist "BackEnd\static\poi_labels.json" git update-index --skip-worktree BackEnd/static/poi_labels.json 2>nul && echo   poi_labels.json
if exist "BackEnd\static\robot_speed.json" git update-index --skip-worktree BackEnd/static/robot_speed.json 2>nul && echo   robot_speed.json
echo.
echo   ^(PC 마다 한 번씩 해야 합니다. push 로 전파되지 않습니다^)

rem ── 4) 브랜치 + add ──────────────────────────────────────
echo.
echo ------------------------------------------------------------
echo  [4/5] 새 브랜치 + 스테이징
echo ------------------------------------------------------------
git checkout -b %BRANCH%
if errorlevel 1 (
  echo   [중단] 브랜치 생성 실패. 이미 있는 이름일 수 있습니다.
  popd & pause & exit /b 1
)
git add -A

for /f %%c in ('git status --short ^| find /c /v ""') do set NSTAGED=%%c
echo.
echo   스테이징된 항목: %NSTAGED% 개
echo.
echo ------------------------------------------------------------
echo  ★ 마지막 브레이크 - 목록을 눈으로 보세요
echo ------------------------------------------------------------
git status --short | more
echo.
echo ------------------------------------------------------------
echo   venv / node_modules 가 보입니까?
echo     보인다 -^> 커밋하지 마세요. n 을 누르면 되돌립니다.
echo     안 보인다 -^> y
echo ------------------------------------------------------------
set /p YN2=커밋할까요? [y/N]:
if /i not "!YN2!"=="y" (
  git reset -q
  git checkout -q -
  git branch -D %BRANCH% 2>nul
  echo.
  echo   되돌렸습니다. 파일은 안 건드렸습니다.
  popd & pause & exit /b 0
)

rem ── 5) 커밋 + push ───────────────────────────────────────
echo.
echo ------------------------------------------------------------
echo  [5/5] 커밋
echo ------------------------------------------------------------
git commit -q -m "chore: 현장 서버 사용폴더 현재 상태 기준점 (%TODAY%)" -m "- 사용폴더를 git 저장소로 전환 (업로드폴더의 .git 을 옮김)" -m "- 맵 이미지/로그/venv 추적 해제" -m "- default_area.json 등 PC별 설정은 skip-worktree"
git log --oneline -1
echo.

echo  인터넷이 되면 push 합니다. 안 되면 나중에 해도 됩니다.
set /p YN3=지금 push 할까요? [y/N]:
if /i "!YN3!"=="y" (
  git push -u origin %BRANCH%
  echo.
  git branch -vv | findstr baseline
) else (
  echo.
  echo   나중에: git push -u origin %BRANCH%
)

popd
echo.
echo ============================================================
echo  완료. 이제 사용폴더가 git 저장소입니다.
echo.
echo   롤백 지점  : %BRANCH%
echo   새 코드 받기: git pull origin feature/lg_luke
echo.
echo  업로드폴더는 바로 지우지 말고 1~2주 두세요.
echo  이름만 바꿔두는 것을 권합니다: 업로드폴더_폐기_%TODAY%
echo ============================================================
echo.
pause
