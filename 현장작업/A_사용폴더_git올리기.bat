@echo off
chcp 949 > nul
title [A] 사용폴더를 새 브랜치로 바로 올리기
setlocal EnableDelayedExpansion

rem ============================================================
rem  사용폴더(실가동) 자체를 git 저장소로 만들고
rem  새 브랜치로 바로 push 한다. 업로드폴더를 끼우지 않는다.
rem
rem  ★ fetch / checkout 을 하지 않는다.
rem    그래서 로컬 파일이 덮일 일이 없다.
rem    (문서가 위험하다고 한 것은 git init 이 아니라
rem     그 뒤의 checkout origin/... 이다)
rem
rem  이 bat 은 어디서 실행해도 된다. 사용폴더 경로를 물어본다.
rem ============================================================

set CFG=%~dp0사용폴더경로.txt
set USEDIR=
if exist "%CFG%" set /p USEDIR=<"%CFG%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set BRANCH=field/baseline-%TODAY%
set REPO=https://github.com/UND-Magbot/RCS_LG.git

echo.
echo ============================================================
echo  [A] 사용폴더를 새 브랜치로 올리기
echo ============================================================
echo.
echo   저장소  : %REPO%
echo   브랜치  : %BRANCH%
echo.
if not "%USEDIR%"=="" (
  echo   사용폴더: %USEDIR%
  set /p YN=이 경로가 맞습니까? [Y/n]:
  if /i "!YN!"=="n" set USEDIR=
)
if "%USEDIR%"=="" (
  echo.
  echo   사용폴더 = 실제로 백엔드가 돌아가는 폴더
  set /p USEDIR=사용폴더 전체 경로:
)

rem 따옴표를 붙여 붙여넣는 경우가 많다. 제거한다
set USEDIR=!USEDIR:"=!
rem 끝에 역슬래시가 있으면 뗀다
if "!USEDIR:~-1!"=="\" set USEDIR=!USEDIR:~0,-1!

if not exist "!USEDIR!\BackEnd" (
  echo.
  echo   [중단] !USEDIR!\BackEnd 가 없습니다. 경로를 확인하세요.
  del "%CFG%" 2>nul
  pause & exit /b 1
)
echo !USEDIR!>"%CFG%"

if exist "!USEDIR!\.git" (
  echo.
  echo   [중단] 사용폴더에 이미 .git 이 있습니다.
  pushd "!USEDIR!"
  git log --oneline -3
  git branch --show-current
  popd
  echo.
  echo   이미 저장소입니다. 이 bat 은 처음 한 번만 씁니다.
  pause & exit /b 1
)

rem ── 1) 백업 ──────────────────────────────────────────────
set BKZIP=%USERPROFILE%\Desktop\사용폴더백업_%TS%.zip
echo.
echo ------------------------------------------------------------
echo  [1/6] 백업  (안전장치. 몇 분 걸립니다)
echo ------------------------------------------------------------
echo   %BKZIP%
set BKTMP=%TEMP%\usebk_%TS%
robocopy "!USEDIR!" "%BKTMP%" /E /XD venv venv_old node_modules .next .next_old_asus __pycache__ _logs /NFL /NDL /NJH /NJS /nc /ns /np >nul
powershell -NoProfile -Command "Compress-Archive -Path '%BKTMP%\*' -DestinationPath '%BKZIP%' -Force"
rmdir /S /Q "%BKTMP%" 2>nul
if not exist "%BKZIP%" (
  echo.
  echo   [중단] 백업 실패. 여기서 멈춥니다.
  pause & exit /b 1
)
powershell -NoProfile -Command "$f=Get-Item '%BKZIP%'; Write-Host ('   완료: ' + [math]::Round($f.Length/1MB,1) + ' MB')"

pushd "!USEDIR!"

rem ── 2) .gitignore 먼저 ───────────────────────────────────
echo.
echo ------------------------------------------------------------
echo  [2/6] .gitignore  ^(add 전에 해야 venv 가 안 딸려옵니다^)
echo ------------------------------------------------------------
(
echo # ── 의존성 · 빌드 산출물 ──
echo BackEnd/venv/
echo BackEnd/venv_old/
echo frontend/node_modules/
echo frontend/.next/
echo frontend/.next_old_asus/
echo frontend/out/
echo BackEnd/web/
echo __pycache__/
echo *.pyc
echo.
echo # ── 서버별 설정 ──
echo BackEnd/config.env
echo frontend/.env.local
echo.
echo # ── 운영 데이터 ──
echo _db/
echo *.sql
echo.
echo # ── 맵핑 산출물 ^(DB엔 경로만 저장^) ──
echo BackEnd/static/maps/
echo.
echo # ── 로그 ──
echo BackEnd/_logs/
echo _logs/
echo 로그수집_*.zip
echo.
echo # ── 백업본 ──
echo _backup/
echo *.bak
echo *.bak_*
echo.
echo # ── 안드로이드 빌드 ──
echo TabletApp/.gradle/
echo TabletApp/app/build/
echo TabletApp/build/
echo TabletApp/local.properties
echo.
echo # ── 현장작업 산출물 ──
echo 현장작업/사용폴더경로.txt
echo 현장작업/차이_*.txt
echo 현장작업/현장수정분_*.patch
) > .gitignore
echo   완료

rem ── 3) git init + 원격 ───────────────────────────────────
echo.
echo ------------------------------------------------------------
echo  [3/6] 저장소 생성 + 원격 연결
echo ------------------------------------------------------------
git init -q
if errorlevel 1 (
  echo   [중단] git init 실패. git 이 설치돼 있는지 확인하세요.
  popd & pause & exit /b 1
)
git remote add origin %REPO%
git remote -v
echo.
echo   ★ fetch / checkout 은 하지 않습니다. 로컬 파일을 건드리지 않습니다.

rem ── 4) 브랜치 + add ──────────────────────────────────────
echo.
echo ------------------------------------------------------------
echo  [4/6] 브랜치 %BRANCH% + 스테이징
echo ------------------------------------------------------------
git checkout -q -b %BRANCH%
echo   스테이징 중... ^(파일이 많으면 1~2분^)
git add -A

for /f %%c in ('git diff --cached --name-only ^| find /c /v ""') do set NFILES=%%c
echo.
echo   올라갈 파일 수: %NFILES% 개

rem ── 5) 마지막 확인 ───────────────────────────────────────
echo.
echo ============================================================
echo  [5/6] ★ 마지막 브레이크 - 눈으로 확인하세요
echo ============================================================
echo.
echo --- 폴더별 파일 수 (상위 15개) ---
git diff --cached --name-only > "%TEMP%\staged_%TS%.txt"
powershell -NoProfile -Command "Get-Content '%TEMP%\staged_%TS%.txt' | ForEach-Object { ($_ -split '/')[0] } | Group-Object | Sort-Object Count -Descending | Select-Object -First 15 | ForEach-Object { '{0,8}  {1}' -f $_.Count, $_.Name }"
echo.
echo --- 용량이 큰 파일 (상위 10개) ---
powershell -NoProfile -Command "Get-Content '%TEMP%\staged_%TS%.txt' | Where-Object { Test-Path $_ } | ForEach-Object { Get-Item $_ } | Sort-Object Length -Descending | Select-Object -First 10 | ForEach-Object { '{0,8} MB  {1}' -f [math]::Round($_.Length/1MB,1), $_.Name }"
echo.
echo ------------------------------------------------------------
echo   확인할 것
echo     venv / node_modules / maps 가 보입니까?  -^> 보이면 n
echo     파일 수가 수천 개입니까?                 -^> 그러면 n
echo     보통 수백 개면 정상입니다.
echo ------------------------------------------------------------
set /p YN2=커밋할까요? [y/N]:
if /i not "!YN2!"=="y" (
  git reset -q
  echo.
  echo   되돌렸습니다. .git 을 지우려면:  rmdir /S /Q "!USEDIR!\.git"
  popd & pause & exit /b 0
)

rem ── 6) 커밋 + push ───────────────────────────────────────
echo.
echo ------------------------------------------------------------
echo  [6/6] 커밋
echo ------------------------------------------------------------
git commit -q -m "chore: 현장 서버 사용폴더 현재 상태 기준점 (%TODAY%)" -m "실가동 폴더를 그대로 git 저장소로 만들어 올린 것이다." -m "업로드폴더 방식을 폐기한다. fetch/checkout 을 하지 않아 로컬 파일은 그대로다."
git log --oneline -1

rem PC별로 달라야 하는 설정은 앞으로 변경을 무시한다
git update-index --skip-worktree BackEnd/default_area.json 2>nul
git update-index --skip-worktree BackEnd/static/poi_labels.json 2>nul
git update-index --skip-worktree BackEnd/static/robot_speed.json 2>nul
echo.
echo   PC별 설정 파일 변경 무시 적용 ^(default_area / poi_labels / robot_speed^)

echo.
set /p YN3=지금 push 할까요? (인터넷 필요) [y/N]:
if /i "!YN3!"=="y" (
  echo.
  git push -u origin %BRANCH%
  if errorlevel 1 (
    echo.
    echo   push 실패. GitHub 로그인 창이 떴는지 확인하세요.
    echo   나중에 다시: git push -u origin %BRANCH%
  ) else (
    echo.
    git branch -vv | findstr baseline
  )
) else (
  echo.
  echo   나중에:  cd /d "!USEDIR!"  ^&^&  git push -u origin %BRANCH%
)

popd
echo.
echo ============================================================
echo  완료. 사용폴더가 git 저장소가 됐습니다.
echo.
echo   롤백 지점 : %BRANCH%
echo   백업      : %BKZIP%
echo.
echo  [앞으로 개발PC 코드를 받는 법]
echo    통째 merge 는 하지 마세요. 이력이 별개라 충돌이 큽니다.
echo    필요한 파일만 골라 가져옵니다:
echo.
echo      git fetch origin
echo      git checkout origin/feature/lg_luke -- scripts/load_test.py
echo      git checkout origin/feature/lg_luke -- tools/robot_monitor.html
echo.
echo    ^(fetch 는 파일을 안 건드립니다. checkout 에 -- 뒤로 경로를 주면
echo     그 파일만 가져옵니다^)
echo.
echo  업로드폴더는 바로 지우지 말고 1~2주 두세요.
echo ============================================================
echo.
pause
