#
#  agent-bridge-mcp - start/stop/status for the bridge on this machine
#
#  Windows PowerShell 5.1: no '&&', no ternary, no null-coalescing.
#
#    tools\bridge.ps1 start
#    tools\bridge.ps1 status
#    tools\bridge.ps1 stop
#    tools\bridge.ps1 restart
#    tools\bridge.ps1 firewall     (needs an elevated shell)

[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'status', 'restart', 'firewall')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$Root    = Split-Path -Parent $PSScriptRoot
$Python  = Join-Path $Root '.venv\Scripts\python.exe'
$PidFile = Join-Path $Root 'server.pid'
$OutLog  = Join-Path $Root 'server.log'
$ErrLog  = Join-Path $Root 'server.err'

$cfg  = Get-Content (Join-Path $Root 'config.json') -Raw | ConvertFrom-Json
$Port = $cfg.port

function Get-Listeners {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $conns) { return @() }
    return @($conns.OwningProcess | Select-Object -Unique)
}

function Stop-Bridge {
    # Kill by PORT OWNER, not by the pid file. The venv's python.exe is a
    # trampoline that re-execs the base interpreter, so the process we launched
    # is the PARENT of the one holding the socket - stopping the launched PID
    # alone leaves the port held and the next start fails on bind.
    $targets = @(Get-Listeners)
    if (Test-Path $PidFile) {
        $recorded = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($recorded -match '^\s*(\d+)\s*$') { $targets += [int]$Matches[1] }
    }
    # Drop nulls and zeroes BEFORE counting. A $null that survives into the list
    # makes Count non-zero while every kill silently no-ops, which reads exactly
    # like "stop ran and did nothing" - the failure this script is meant to avoid.
    $targets = @($targets | Where-Object { $_ } | ForEach-Object { [int]$_ } |
                 Where-Object { $_ -gt 0 } | Select-Object -Unique)
    if ($targets.Count -eq 0) { "nothing listening on $Port"; return }

    foreach ($t in $targets) {
        "stopping PID $t"
        Stop-Process -Id $t -Force -ErrorAction SilentlyContinue
    }
    # Poll until the socket is actually released. A fixed sleep here is the bug
    # that makes 'restart' intermittently fail with "already listening": a
    # process that has been signalled still holds the port for a moment, and
    # Start-Bridge then sees a listener and declines to start.
    for ($i = 0; $i -lt 25; $i++) {
        if ((Get-Listeners).Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    }
    if ((Get-Listeners).Count -gt 0) { "WARNING: $Port is still held after 5s" }
    if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
}

function Start-Bridge {
    if ((Get-Listeners).Count -gt 0) {
        "already listening on $Port - use restart"
        return
    }
    $p = Start-Process -FilePath $Python -ArgumentList '-m', 'agent_bridge' `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
    $p.Id | Out-File -Encoding ascii $PidFile

    # Poll health rather than sleeping a fixed guess - a bind failure shows up
    # immediately and there is no reason to wait the full timeout for it.
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 400
        if ($p.HasExited) {
            "FAILED to start. Last lines of ${ErrLog}:"
            Get-Content $ErrLog -Tail 12
            return
        }
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 2
            # Record the process actually holding the socket, which is not
            # necessarily the one we launched (see Stop-Bridge).
            $owner = Get-Listeners | Select-Object -First 1
            if ($owner) { $owner | Out-File -Encoding ascii $PidFile }
            "started PID $($p.Id) (listener $owner) - $($r.Content)"
            return
        } catch { }
    }
    "started PID $($p.Id) but /api/health did not answer; check $ErrLog"
}

function Get-Status {
    $owners = Get-Listeners
    if ($owners.Count -eq 0) { "bridge is DOWN (nothing on $Port)"; return }
    "listening on $Port, PID(s): $($owners -join ', ')"
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 3
        $r.Content
    } catch {
        "port is held but /api/health failed: $($_.Exception.Message)"
    }
}

function Add-FirewallRule {
    $admin = ([Security.Principal.WindowsPrincipal] `
              [Security.Principal.WindowsIdentity]::GetCurrent()
             ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { "run this from an ELEVATED PowerShell"; return }

    $existing = Get-NetFirewallRule -DisplayName 'agent-bridge-mcp' -ErrorAction SilentlyContinue
    if ($null -ne $existing) { $existing | Remove-NetFirewallRule }

    # LocalSubnet, not Any. This bridge runs commands and reads source; it has no
    # business being reachable from beyond the LAN even with a good token.
    New-NetFirewallRule -DisplayName 'agent-bridge-mcp' -Direction Inbound `
        -Action Allow -Protocol TCP -LocalPort $Port `
        -RemoteAddress LocalSubnet -Profile Private, Domain | Out-Null
    "opened TCP $Port to the local subnet"
}

switch ($Action) {
    'start'    { Start-Bridge }
    'stop'     { Stop-Bridge }
    'status'   { Get-Status }
    'restart'  { Stop-Bridge; Start-Bridge }
    'firewall' { Add-FirewallRule }
}
