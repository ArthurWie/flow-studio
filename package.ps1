# Build the Flow Studio installer end-to-end.
#   .\package.ps1
# Requires: Inno Setup 6 (ISCC.exe on PATH or at the default location).
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py   = Join-Path $root "venv\Scripts\python.exe"
$stage = Join-Path $root "stage"
$boot = "D:\FlowStudio-build\boot"

# 1. Build bootstrap.exe (stdlib-only, small, windowed for production)
& $py -m PyInstaller (Join-Path $root "bootstrap.py") --onefile --noconsole `
  --name bootstrap --icon (Join-Path $root "flow.ico") --noconfirm `
  --distpath "$boot\dist" --workpath "$boot\build" --specpath $boot
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 2. Fetch latest uv.exe if not already present
$uv = Join-Path $root "uv.exe"
if (-not (Test-Path $uv)) {
  $uvUrl = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
  $tmp = Join-Path $env:TEMP "uv.zip"
  Invoke-WebRequest -Uri $uvUrl -OutFile $tmp
  Expand-Archive -Path $tmp -DestinationPath $env:TEMP -Force
  Copy-Item (Join-Path $env:TEMP "uv.exe") $uv -Force
}

# 3. Stage everything the installer ships
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null
Copy-Item (Join-Path $root "flow_studio.py") $stage
Copy-Item (Join-Path $root "app.py") $stage
Copy-Item (Join-Path $root "flow.py") $stage
Copy-Item (Join-Path $root "os_win.py") $stage
Copy-Item (Join-Path $root "paths.py") $stage
Copy-Item (Join-Path $root "flow.ico") $stage
Copy-Item (Join-Path $root "requirements.txt") $stage
Copy-Item "$boot\dist\bootstrap.exe" $stage
Copy-Item $uv $stage

# 4. Compile the installer with Inno Setup
$isccCandidates = @(
  (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
  "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { $iscc = "ISCC.exe" }
& $iscc (Join-Path $root "FlowStudio.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Write-Host "`nBuilt: $(Join-Path $root 'FlowStudioSetup.exe')" -ForegroundColor Green
