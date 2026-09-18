# start_bg.ps1 - start Flask backend + Vite frontend in hidden background windows.
#
# ASCII ONLY, on purpose: start.bat runs this with Windows PowerShell 5.1, which
# decodes a .ps1 file without a BOM using the system ANSI codepage. Non-ASCII
# comments then get corrupted and can break parsing (it already happened once).
# The Chinese explanation of this script lives in scripts/README.md.
#
# Flow:
#   1. kill leftovers from an earlier run (logs\*.pid, then the listening ports)
#   2. start backend (backend\venv python) + frontend (node vite), both hidden
#   3. wait until the frontend port accepts connections, then open the browser
#   4. spawn a hidden watchdog (this same script, -WatchMinutes/-WatchPids) that
#      stops both processes when the TTL expires -> no stop script needed
#
# Usage:  start.bat [hours]      hours = 0 disables auto-stop (default 4)
param(
    [double]$TtlHours = 4,
    [int]$WatchMinutes = 0,     # internal: watchdog mode, spawned by this script
    [string]$WatchPids = ''     # internal: pids to watch, comma separated
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# ---------- watchdog mode (spawned by the start flow, not for manual use) ----
if ($WatchMinutes -gt 0 -and $WatchPids) {
    $watch = @($WatchPids -split ',' | Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ })
    $deadline = (Get-Date).AddMinutes($WatchMinutes)
    while ((Get-Date) -lt $deadline) {
        $alive = @($watch | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { exit 0 }        # already stopped: nothing to do
        Start-Sleep -Seconds 30
    }
    foreach ($p in $watch) {
        if (Get-Process -Id $p -ErrorAction SilentlyContinue) {
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($name in 'backend', 'vue') {
        $pidFile = Join-Path $logDir "$name.pid"
        if (-not (Test-Path $pidFile)) { continue }
        $value = (Get-Content $pidFile | Select-Object -First 1).Trim()
        if ($value -match '^\d+$' -and $watch -contains [int]$value) {
            Remove-Item $pidFile -ErrorAction SilentlyContinue
        }
    }
    Add-Content -Path (Join-Path $logDir 'watchdog.log') -Value (
        "{0:yyyy-MM-dd HH:mm} auto-stop after {1} min (pids {2})" -f (Get-Date), $WatchMinutes, ($watch -join ',')
    )
    exit 0
}

# ---------- helpers ----------------------------------------------------------
function Get-FlaskPort {
    $port = 8000
    $configPath = Join-Path $projectRoot 'config.yaml'
    if (Test-Path $configPath) {
        $inFlask = $false
        foreach ($line in Get-Content $configPath) {
            if ($line -match '^flask:\s*$') { $inFlask = $true; continue }
            if ($inFlask) {
                if ($line -match '^\S') { break }               # left the flask block
                if ($line -match 'port:\s*(\d+)') { $port = [int]$Matches[1]; break }
            }
        }
    }
    return $port
}

function Stop-IfRunning {
    param([int]$TargetId, [string]$Label)
    if (Get-Process -Id $TargetId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $TargetId -Force -ErrorAction SilentlyContinue
        Write-Host ("Stopped {0} (pid {1})" -f $Label, $TargetId)
    }
}

# ---------- 1. leftovers from an earlier run ---------------------------------
# Running start.bat again therefore means "restart": the old pair is stopped
# first, and its watchdog notices the pids are gone and exits on its own.
foreach ($name in 'backend', 'vue') {
    $pidFile = Join-Path $logDir "$name.pid"
    if (-not (Test-Path $pidFile)) { continue }
    $oldPid = (Get-Content $pidFile | Select-Object -First 1).Trim()
    if ($oldPid -match '^\d+$') { Stop-IfRunning -TargetId ([int]$oldPid) -Label "old $name" }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

Start-Sleep -Milliseconds 500
$flaskPort = Get-FlaskPort
foreach ($port in $flaskPort, 5173) {
    $conns = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    foreach ($conn in $conns) { Stop-IfRunning -TargetId $conn.OwningProcess -Label "port $port" }
}

# Note: backend\venv\Scripts\python.exe is only a launcher that re-executes the
# real server with the system python, so logs\backend.pid holds the launcher while
# the port is held by its child. Killing either one takes the other down within
# about 0.5s (measured on this machine), and the two steps above cover both cases:
# pid file missing, or the recorded pid being the launcher.

# ---------- 2. backend -------------------------------------------------------
# Prefer backend/venv, fall back to the system python.
$backendPython = Join-Path $projectRoot 'backend\venv\Scripts\python.exe'
if (-not (Test-Path $backendPython)) { $backendPython = 'python' }

# The entry path must be absolute: -WorkingDirectory is backend\, so a relative
# path resolves to backend\scripts\start_backend.py (does not exist).
$backend = Start-Process -FilePath $backendPython `
    -ArgumentList (Join-Path $projectRoot 'scripts\start_backend.py') `
    -WorkingDirectory (Join-Path $projectRoot 'backend') `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'flask.log') `
    -RedirectStandardError (Join-Path $logDir 'flask.err.log') `
    -PassThru
Set-Content -Path (Join-Path $logDir 'backend.pid') -Value $backend.Id

# ---------- 3. frontend ------------------------------------------------------
# node runs vite directly: npm's shim breaks under Git Bash.
$vue = Start-Process -FilePath 'node' `
    -ArgumentList 'node_modules\vite\bin\vite.js', '--port', '5173', '--strictPort' `
    -WorkingDirectory (Join-Path $projectRoot 'frontend') `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'vue.log') `
    -RedirectStandardError (Join-Path $logDir 'vue.err.log') `
    -PassThru
Set-Content -Path (Join-Path $logDir 'vue.pid') -Value $vue.Id

Write-Host ("Backend  pid {0}  log: logs\flask.log" -f $backend.Id)
Write-Host ("Frontend pid {0}  log: logs\vue.log" -f $vue.Id)

# ---------- 4. open the browser once the frontend answers (max 90s) ----------
$frontUrl = 'http://localhost:5173'
$ready = $false
for ($i = 0; $i -lt 90 -and -not $ready; $i++) {
    try {
        $probe = New-Object System.Net.Sockets.TcpClient
        $probe.Connect('127.0.0.1', 5173)
        $probe.Close()
        $ready = $true
    } catch {
        Start-Sleep -Seconds 1
    }
}
if ($ready) {
    Start-Process $frontUrl
    Write-Host "Opened $frontUrl"
} else {
    Write-Host "Frontend not listening after 90s; check logs\vue.err.log"
}

# ---------- 5. watchdog: stop both processes when the TTL expires ------------
# Same interpreter that runs this script also runs the watchdog.
if ($TtlHours -gt 0) {
    $ttlMinutes = [int][Math]::Ceiling($TtlHours * 60)
    $hostExe = (Get-Process -Id $PID).Path
    $watchdog = Start-Process -FilePath $hostExe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
                        '-WatchMinutes', $ttlMinutes,
                        '-WatchPids', "$($backend.Id),$($vue.Id)") `
        -WindowStyle Hidden -PassThru
    Write-Host ("Auto-stop at {0:HH:mm} (in {1} min, watchdog pid {2})" -f (Get-Date).AddMinutes($ttlMinutes), $ttlMinutes, $watchdog.Id)
} else {
    Write-Host "Auto-stop disabled (hours=0); kill python/node to stop"
}
