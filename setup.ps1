# setup.ps1 — one-command setup for the Autodesk Connector on Windows.
#
# Run from the Contech-AI folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# It checks Python, creates a private environment (.venv), installs the libraries,
# creates .env from the template, checks the sign-in port, and prints the exact
# Claude Desktop config block for this folder. Safe to run again.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "   OK: $text" -ForegroundColor Green }
function Warn($text) { Write-Host "   WARNING: $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "   PROBLEM: $text" -ForegroundColor Red; exit 1 }

# 1. Python ---------------------------------------------------------------
Step "Checking Python (3.10 or newer)"
$python = $null
foreach ($candidate in @(@("py", "-3"), @("python"))) {
    $exe = $candidate[0]
    $extra = @($candidate | Select-Object -Skip 1)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        try {
            $version = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version -match '^\d+\.\d+$') { $python = $candidate; break }
        } catch { }
    }
}
if (-not $python) {
    Fail ("Python not found. Install Python 3.10+ from https://www.python.org/downloads/ " +
          "(tick 'Add python.exe to PATH'), then open a new PowerShell and run this again. " +
          "If Windows opens the Microsoft Store instead, turn off the python.exe alias in " +
          "Settings > Apps > Advanced app settings > App execution aliases.")
}
$major, $minor = $version.Split('.') | ForEach-Object { [int]$_ }
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) { Fail "Python $version found; 3.10 or newer is needed." }
Ok "Python $version"

# 2. Private environment + libraries --------------------------------------
Step "Creating the private Python environment (.venv)"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $exe = $python[0]; $extra = @($python | Select-Object -Skip 1)
    & $exe @extra -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create .venv." }
}
Ok ".venv ready"

Step "Installing libraries (httpx, python-dotenv, fastmcp, mcp)"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Library install failed — check your internet connection and run again." }
Ok "libraries installed"

# 3. Settings file ----------------------------------------------------------
Step "Settings (.env)"
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item ".env.example" $envFile
    Ok "created .env from the template"
} else {
    Ok ".env already exists (left as is)"
}
if (Select-String -Path $envFile -Pattern '^[A-Z_]+=.*<' -Quiet) {
    Warn ".env still has <...> placeholders. Opening it in Notepad — fill in your client ID, secret and project ID, then save."
    Start-Process notepad $envFile -Wait
}

# 4. Sign-in port -----------------------------------------------------------
Step "Checking the sign-in port (5002)"
$listener = Get-NetTCPConnection -LocalPort 5002 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    Warn ("Port 5002 is in use by '" + $proc.ProcessName + "' (PID " + $listener.OwningProcess + "). " +
          "Close it before signing in, or the Autodesk sign-in can't finish.")
} else {
    Ok "port 5002 is free"
}

# 5. Claude Desktop config block --------------------------------------------
Step "Your Claude Desktop config block"
$pyJson  = $venvPython -replace '\\', '\\'
$appJson = (Join-Path $PSScriptRoot "acc_mcp.py") -replace '\\', '\\'
$block = @"
    "autodesk-connector": {
      "command": "$pyJson",
      "args": ["$appJson"]
    }
"@
Write-Host $block -ForegroundColor White

Write-Host "`nNext steps:" -ForegroundColor Cyan
Write-Host "  1. Sign in once:      .\.venv\Scripts\python acc_mcp.py --test"
Write-Host "  2. Quit Claude:       Stop-Process -Name claude -Force"
Write-Host "  3. Add the block above inside `"mcpServers`" in:"
Write-Host "        $env:APPDATA\Claude\claude_desktop_config.json"
Write-Host "  4. Start Claude Desktop -> Settings -> Developer -> autodesk-connector should say 'running'"
Write-Host ""
