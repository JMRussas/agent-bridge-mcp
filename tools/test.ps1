# Run the Windows suite through uv, including the ripgrep integration tests.
[CmdletBinding()]
param(
    [string]$Group = '',
    [string]$Name = '',
    [switch]$CollectOnly
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$OriginalPath = $env:PATH
if (($env:PATHEXT -split ';') -notcontains '.EXE') {
    throw 'PATHEXT must include .EXE so PowerShell waits for uv and captures its exit code.'
}
$Uv = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $Uv) {
    throw 'Install uv and put uv.exe on PATH before running the suite.'
}

try {
    if (-not (Get-Command rg.exe -ErrorAction SilentlyContinue)) {
        $Extensions = Join-Path $env:USERPROFILE '.vscode\extensions'
        $Candidates = @(Get-ChildItem $Extensions -Directory -Filter 'openai.chatgpt-*' `
            -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
        foreach ($Extension in $Candidates) {
            $Bin = Join-Path $Extension.FullName 'bin\windows-x86_64'
            if (Test-Path (Join-Path $Bin 'rg.exe')) {
                $env:PATH = "$Bin;$env:PATH"
                break
            }
        }
    }
    if (-not (Get-Command rg.exe -ErrorAction SilentlyContinue)) {
        throw 'Install ripgrep and put rg.exe on PATH before running the full suite.'
    }
    Push-Location $Root
    try {
        $PytestArgs = @('-q', '-ra')
        if ($Group) { $PytestArgs += @('-m', $Group) }
        if ($Name) { $PytestArgs += @('-k', $Name) }
        if ($CollectOnly) { $PytestArgs += '--collect-only' }
        & $Uv.Source run --locked --extra dev python -m pytest @PytestArgs
        $Result = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    $env:PATH = $OriginalPath
}
exit $Result
