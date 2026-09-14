<#
  RCS LG 현장 점검 — 로봇 통신 3종 확인 (인터넷 불필요, Windows 내장 curl 사용)

  사용법
    사무실 S300 :  .\check_robot.ps1
    현장 로봇   :  .\check_robot.ps1 10.115.244.12
#>
param([string]$Ip = "192.168.30.101")

$curl = "$env:SystemRoot\System32\curl.exe"
$result = [ordered]@{}

function Head($t) { Write-Host ""; Write-Host ("===== " + $t + " =====") -ForegroundColor Cyan }
function OK($t)   { Write-Host ("   " + $t) -ForegroundColor Green }
function NG($t)   { Write-Host ("   " + $t) -ForegroundColor Red }

Write-Host ""
Write-Host ("점검 대상 로봇 : " + $Ip + ":8090") -ForegroundColor Yellow

# ── 0. 내 PC 네트워크 ── 고정 IP 가 실제로 적용됐는지까지 본다
Head "0. 내 PC 네트워크"
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -notlike "*Loopback*" } |
    ForEach-Object {
        Write-Host ("   {0,-18} {1,-16} {2}" -f $_.InterfaceAlias, $_.IPAddress, $_.PrefixOrigin)
    }
Write-Host "   * PrefixOrigin 이 Manual 이어야 고정 IP 가 적용된 것 (Dhcp 면 미적용)" -ForegroundColor DarkGray

# ── 1. 섀시 응답 ──
Head "1. chassis/status  (로봇이 살아있나)"
try {
    $r1 = Invoke-RestMethod -Uri ("http://{0}:8090/chassis/status" -f $Ip) -TimeoutSec 8 -ErrorAction Stop
    Write-Host ("   control_mode       : " + $r1.control_mode)
    Write-Host ("   emergency_stop     : " + $r1.emergency_stop_pressed)
    OK "PASS"
    $result["1. chassis/status"] = "PASS"
} catch {
    NG ("FAIL — " + $_.Exception.Message)
    $result["1. chassis/status"] = "FAIL"
}

# ── 2. 장비 정보 ── DB 등록에 쓰이는 SN/모델을 여기서 읽어온다
Head "2. device/info  (등록에 쓰는 SN / 모델 / 잭)"
try {
    $r2 = Invoke-RestMethod -Uri ("http://{0}:8090/device/info" -f $Ip) -TimeoutSec 10 -ErrorAction Stop
    Write-Host ("   SN                 : " + $r2.device.sn)
    Write-Host ("   model              : " + $r2.device.model)
    Write-Host ("   supportsJack       : " + $r2.caps.supportsJack)
    OK "PASS"
    $result["2. device/info"] = "PASS"
} catch {
    NG ("FAIL — " + $_.Exception.Message)
    $result["2. device/info"] = "FAIL"
}

# ── 3. WebSocket ── 관제 화면의 온라인 판정이 이걸 본다
Head "3. WebSocket  (관제 온라인 판정)"
$wsArgs = @(
    "-s", "-i", "-N", "--max-time", "6",
    "-H", "Connection: Upgrade",
    "-H", "Upgrade: websocket",
    "-H", "Sec-WebSocket-Version: 13",
    "-H", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
    ("http://{0}:8090/ws/v2/topics" -f $Ip)
)
$out  = & $curl $wsArgs
$head = ($out | Select-Object -First 1)

if ($head -match "101") {
    Write-Host ("   " + $head)
    OK "PASS"
    $result["3. websocket"] = "PASS"

    # 덤 — 위치추정 상태까지 확인 (맵 동기화 후 lidar_matched 가 true 여야 정상)
    $slam = ($out | Select-String -Pattern "/slam/state" | Select-Object -First 1)
    if ($slam) {
        $t = $slam.ToString()
        if ($t -match '"state":\s*"([^"]+)"')            { Write-Host ("   slam state         : " + $Matches[1]) }
        if ($t -match '"lidar_matched":\s*(true|false)') { Write-Host ("   lidar_matched      : " + $Matches[1]) }
    }
} else {
    if ([string]::IsNullOrWhiteSpace($head)) { NG "FAIL — 무응답 (포트 차단 / 포워딩 누락 의심)" }
    else                                     { NG ("FAIL — " + $head) }
    $result["3. websocket"] = "FAIL"
}

# ── 요약 ──
Head "결과 요약"
foreach ($k in $result.Keys) {
    if ($result[$k] -eq "PASS") { OK ("{0,-20} {1}" -f $k, $result[$k]) }
    else                        { NG ("{0,-20} {1}" -f $k, $result[$k]) }
}

$fail = ($result.Values | Where-Object { $_ -eq "FAIL" }).Count
Write-Host ""
if ($fail -eq 0) {
    Write-Host " 3개 모두 통과 — 로봇 등록 / 관제 온라인 조건 충족" -ForegroundColor Green
} else {
    Write-Host " 실패 항목 있음 — 1번 실패면 경로/포워딩, 3번만 실패면 WS 구간" -ForegroundColor Red
}
Write-Host ""
