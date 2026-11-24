<#
.SYNOPSIS
  Finishes a local dev.factory setup: installs the Claude Code layer into one or more
  profiles, puts the `factory` shim on the user PATH, and checks the result.

.DESCRIPTION
  Run it with Claude Code / the Claude app CLOSED: install.py renders the agents' tool lists
  from the MCP servers configured for each profile, and a running app can change that config.
  The MCP config is only READ (server names); this script and install.py never edit it.

  Windows PowerShell 5.1 compatible. Changes only:
    - each <profile> (skills/factory, agents/factory-*, hooks, settings.json, CLAUDE.md block, bin/)
    - the USER Path variable (appends <first profile>\bin once)
  Backups are written by install.py to <profile>\backups\factory-<timestamp>\.

.EXAMPLE
  .\scripts\install-profiles.ps1 -Profiles "$HOME\.claude", "$HOME\.claude-work" -Workspace acme

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-profiles.ps1 -Profiles "$HOME\.claude,$HOME\.claude-work" -Workspace acme
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  # One or more profile dirs. With `powershell -File`, pass them as ONE string separated by ',' or ';'.
  [string[]] $Profiles = @("$HOME\.claude"),
  [string] $Workspace = "",
  [switch] $RemoveHarness,
  [switch] $SkipPath,
  [switch] $Force
)

$ErrorActionPreference = "Stop"
# $PSScriptRoot is not available in param defaults on Windows PowerShell 5.1: resolve paths here.
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path $scriptDir -Parent
# `-File` passes "a,b" as one string: split on ',' / ';' and trim stray quotes or spaces.
$Profiles = @($Profiles | ForEach-Object { $_ -split '[,;]' } | ForEach-Object { $_.Trim().Trim('"').Trim("'") } | Where-Object { $_ })

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "OK   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "WARN $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "FAIL $text" -ForegroundColor Red; exit 1 }

Step "Pre-checks"
$running = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(claude|Claude)$' }
if ($running -and -not $Force) {
  Fail "Claude is running (PID $($running.Id -join ', ')). Close it and run again (or pass -Force at your own risk)."
}
foreach ($tool in @("python", "node", "git")) {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "$tool not found on PATH" }
}
Ok "python, node and git found; Claude is not running"

Step "Install dev.factory into profiles"
Push-Location $repo
try {
  foreach ($p in $Profiles) {
    $full = [Environment]::ExpandEnvironmentVariables($p)
    if (-not (Test-Path $full)) { Warn "profile not found, skipped: $full"; continue }
    $installArgs = @("-m", "install", "--profile", $full, "--yes")
    if ($RemoveHarness) { $installArgs += "--remove-harness" }
    Write-Host "-- python $($installArgs -join ' ')"
    & python @installArgs
    if ($LASTEXITCODE -ne 0) { Fail "install failed for $full (exit $LASTEXITCODE)" }
    Ok "installed into $full"
  }
} finally { Pop-Location }

$bin = Join-Path ([Environment]::ExpandEnvironmentVariables($Profiles[0])) "bin"
if (-not $SkipPath) {
  Step "User PATH"
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $parts = @()
  if ($userPath) { $parts = $userPath.Split(';') | Where-Object { $_ -ne "" } }
  if ($parts -contains $bin) {
    Ok "$bin already on the user PATH"
  } else {
    $newPath = (($parts + $bin) -join ';')
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Ok "added $bin to the user PATH (open a new terminal to use it)"
  }
  $env:Path = "$env:Path;$bin"
}

Step "Checks"
$shim = Join-Path $bin "factory.cmd"
if (-not (Test-Path $shim)) { Fail "shim missing: $shim" }
& $shim --version
if ($LASTEXITCODE -ne 0) { Fail "factory --version failed" }
Ok "factory CLI runs"

foreach ($p in $Profiles) {
  $full = [Environment]::ExpandEnvironmentVariables($p)
  if (-not (Test-Path $full)) { continue }
  $agent = Join-Path $full "agents\factory-scout.md"
  if (Test-Path $agent) {
    $tools = (Select-String -Path $agent -Pattern '^tools:' | Select-Object -First 1).Line
    Ok "scout $tools"
  }
  $dev =@(Get-ChildItem (Join-Path $full "skills") -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "dev-*" }).Count
  if ($dev -gt 0) { Warn "$dev dev-* skills still in $full\skills (run with -RemoveHarness)" } else { Ok "no dev-* skills in $full" }
}

if ($Workspace) {
  & $shim workspace check $Workspace
  if ($LASTEXITCODE -ne 0) { Warn "workspace $Workspace has gaps (see above)" } else { Ok "workspace $Workspace valid" }
}

Write-Host "`nDone. Open Claude again and start a NEW session so it loads the factory skill, agents and hooks." -ForegroundColor Cyan
