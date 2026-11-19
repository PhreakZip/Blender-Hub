<#
.SYNOPSIS
  Blender Hub installer for Windows (wrapper around install.py).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeDesktop -Test
  powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeCode
  powershell -ExecutionPolicy Bypass -File install.ps1 -Check
  powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeDesktop -Uninstall
#>
param(
  [switch]$ClaudeDesktop,
  [switch]$ClaudeCode,
  [switch]$Cursor,
  [switch]$Print,
  [switch]$Check,
  [switch]$Test,
  [switch]$Uninstall,
  [switch]$Yes,
  [switch]$Background,
  [string]$Blender,
  [string]$Config,
  [string]$Name = "blender-hub"
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Python 3.10+: prefer the py launcher, then python on PATH.
$python = $null
foreach ($cand in @(@("py", "-3"), @("python"))) {
  $exe = Get-Command $cand[0] -ErrorAction SilentlyContinue
  if (-not $exe) { continue }
  $pyArgs = @()
  if ($cand.Length -gt 1) { $pyArgs = $cand[1..($cand.Length - 1)] }
  # Windows PowerShell 5.1 turns a native program's stderr into an error under "Stop"
  # (e.g. the Microsoft Store "python" alias): probe with "Continue" and catch everything.
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & $exe.Source @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null | Select-Object -First 1
    $ok = $false
    if ($out -match '^\d+\.\d+$') { $ok = ([version]$out -ge [version]"3.10") }
    if ($ok) { $python = @($exe.Source) + $pyArgs }
  } catch {
    $python = $null
  } finally {
    $ErrorActionPreference = $prev
  }
  if ($python) { break }
}
if (-not $python) {
  Write-Host "[FAIL] Python 3.10+ not found. Install it from https://www.python.org/downloads/ (check 'Add to PATH')."
  exit 1
}

$a = @((Join-Path $here "install.py"))
if ($ClaudeDesktop) { $a += "--claude-desktop" }
if ($ClaudeCode)    { $a += "--claude-code" }
if ($Cursor)        { $a += "--cursor" }
if ($Print)         { $a += "--print" }
if ($Check)         { $a += "--check" }
if ($Test)          { $a += "--test" }
if ($Uninstall)     { $a += "--uninstall" }
if ($Yes)           { $a += "--yes" }
if ($Background)    { $a += "--background" }
if ($Blender)       { $a += @("--blender", $Blender) }
if ($Config)        { $a += @("--config", $Config) }
$a += @("--name", $Name)

$pyExe = $python[0]
$pyPre = @()
if ($python.Length -gt 1) { $pyPre = $python[1..($python.Length - 1)] }
& $pyExe @pyPre @a
exit $LASTEXITCODE
