$ErrorActionPreference = 'Stop'

$scriptPath = $MyInvocation.MyCommand.Path
$appDir = Split-Path -Parent $scriptPath
$repoRoot = Split-Path -Parent $appDir
$localVenvPython = Join-Path $appDir '.venv\Scripts\python.exe'
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
# Prefer the app-local venv (db-testing-tool dedicated) over the repo-root one
$pyExe = if (Test-Path $localVenvPython) {
    $localVenvPython
} elseif (Test-Path $venvPython) {
    $venvPython
} else {
    'python'
}

$mainPort = 8550
$debugPort = 8551
$appUrl = "http://127.0.0.1:$mainPort/"
$debugUrl = "http://127.0.0.1:$debugPort/"
$actionCache = Join-Path $repoRoot 'debug_action_cache.log'
$reloadDir = Join-Path $appDir 'app'
$logDir = Join-Path $repoRoot 'logs'
$mainStdOut = Join-Path $logDir 'db-testing-tool-main.log'
$mainStdErr = Join-Path $logDir 'db-testing-tool-main.err.log'
$debugStdOut = Join-Path $logDir 'db-testing-tool-debug.log'
$debugStdErr = Join-Path $logDir 'db-testing-tool-debug.err.log'

$currentProcessId = $PID
$parentProcessId = $null
try {
    $selfProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $currentProcessId" -ErrorAction Stop
    if ($selfProcess.ParentProcessId -and $selfProcess.ParentProcessId -gt 0) {
        $parentProcessId = [int]$selfProcess.ParentProcessId
    }
} catch {}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Write-ActionCache([string]$msg) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    Add-Content -Path $actionCache -Value "[$stamp] RESTART :: $msg"
}

function Test-AppUp([string]$url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Stop-ToolProcesses {
    try {
        # Nuclear kill: stop ALL python processes to prevent zombie sockets
        Get-Process -Name python, pythonw -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1

        # --- Aggressive kill: find ALL python/uvicorn processes related to our app ---
        $processIds = @()

        # 1. Any process whose command line references our app
        try {
            $processIds += Get-CimInstance Win32_Process | Where-Object {
                $_.ProcessId -ne $currentProcessId -and
                $_.ProcessId -ne $parentProcessId -and
                $_.CommandLine -and (
                    ($_.CommandLine -match 'app\.main:app') -or
                    ($_.CommandLine -match 'db-testing-tool') -or
                    ($_.CommandLine -match '--port\s+855[01]')
                )
            } | Select-Object -ExpandProperty ProcessId
        } catch {}

        # 2. Anything listening on our ports
        try {
            $processIds += Get-NetTCPConnection -LocalPort $mainPort, $debugPort -State Listen -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess
        } catch {}

        # Normalize process id list to integers only.
        $processIds = @(
            $processIds |
            Where-Object { $_ -ne $null } |
            ForEach-Object {
                try { [int]$_ } catch { $null }
            } |
            Where-Object {
                $_ -gt 4 -and
                $_ -ne $currentProcessId -and
                $_ -ne $parentProcessId
            } |
            Sort-Object -Unique
        )

        # 3. Parent processes of the above (uvicorn reloader spawns children)
        $parentIds = @()
        foreach ($procId in $processIds) {
            try {
                $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction Stop
                if ($proc.ParentProcessId -and $proc.ParentProcessId -gt 4) {
                    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.ParentProcessId)" -ErrorAction Stop
                    if ($parent.CommandLine -and ($parent.CommandLine -match 'python|uvicorn')) {
                        $parentIds += [int]$proc.ParentProcessId
                    }
                }
            } catch {}
        }
        $processIds += $parentIds
        $processIds = $processIds | Sort-Object -Unique

        # 4. Kill child processes first (WMI tree walk)
        foreach ($procId in $processIds) {
            try {
                Get-CimInstance Win32_Process -Filter "ParentProcessId = $procId" -ErrorAction Stop |
                    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            } catch {}
        }

        # 5. Kill the main processes
        foreach ($processId in $processIds) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }

        # 6. Final safety: if ports are still occupied, use taskkill via netstat
        Start-Sleep -Milliseconds 500
        foreach ($port in @($mainPort, $debugPort)) {
            try {
                $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
                foreach ($l in $listeners) {
                    if ($l.OwningProcess -gt 4) {
                        taskkill /F /PID $l.OwningProcess 2>$null
                    }
                }
            } catch {}
        }
    } catch {
        Write-ActionCache "Stop-ToolProcesses warning: $($_.Exception.Message)"
    }
}

function Start-UvicornInstance([int]$port, [string]$label, [string]$stdoutLog, [string]$stderrLog, [bool]$useReload) {
    $uvArgs = @('-u', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', [string]$port, '--log-level', 'debug', '--access-log')
    if ($useReload) {
        $uvArgs += @('--reload', '--reload-dir', $reloadDir)
    }

    Write-ActionCache "Starting $label server on port $port. Logs: $stdoutLog ; $stderrLog"
    Start-Process -FilePath $pyExe `
        -ArgumentList $uvArgs `
        -WorkingDirectory $appDir `
        -WindowStyle Minimized `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog
}

Write-ActionCache 'Stopping DB Testing Tool servers on ports 8550 and 8551.'
Stop-ToolProcesses
Start-Sleep -Seconds 1

Start-UvicornInstance -port $mainPort -label 'main' -stdoutLog $mainStdOut -stderrLog $mainStdErr -useReload $false
Start-UvicornInstance -port $debugPort -label 'debug' -stdoutLog $debugStdOut -stderrLog $debugStdErr -useReload $false

$started = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 750
    if ((Test-AppUp $appUrl) -and (Test-AppUp $debugUrl)) {
        $started = $true
        break
    }
}

if ($started) {
    Write-ActionCache "Restart health checks passed. Main=$appUrl Debug=$debugUrl"
    Start-Process $appUrl
    exit 0
}

Write-ActionCache "Restart health check failed. MainUp=$(Test-AppUp $appUrl) DebugUp=$(Test-AppUp $debugUrl)"
Write-Host "Failed to restart DB Testing Tool on $appUrl"
Write-Host "Debug sidecar expected on $debugUrl"
exit 1
