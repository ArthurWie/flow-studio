# Build the Flow Studio installer end-to-end (CI runs this on windows-latest).
#   .\package.ps1                    # windowed build → FlowStudioSetup.exe
#   .\package.ps1 -Debug             # console build (shows startup errors — use when diagnosing)
#   .\package.ps1 -Version 1.2.0
# Needs: venv\ with requirements-win.lock + pyinstaller installed (see the release workflow),
# and Inno Setup 6 (ISCC.exe on PATH or at a default location).
param([switch]$Debug, [string]$Version = "1.0.0")

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py   = Join-Path $root "venv\Scripts\python.exe"
$dist = Join-Path $root "dist\FlowStudio"
Set-Location $root

# 1. Freeze the app (onedir). No console unless -Debug.
if ($Debug) { $env:FLOW_CONSOLE = "1" } else { Remove-Item Env:FLOW_CONSOLE -ErrorAction Ignore }
$env:FLOW_VERSION = $Version   # FlowStudio.spec bakes it into version.txt for the updater
& $py -m PyInstaller FlowStudio.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 2. Bundle the default models as a Hugging Face cache next to the exe (flow_studio.py
#    points HF_HUB_CACHE at models\hub and loads them in place): Kokoro-82M + voices, whisper small.
$env:HF_HOME = Join-Path $dist "models"
& $py -c @"
import faster_whisper
from huggingface_hub import snapshot_download
faster_whisper.download_model('small')
snapshot_download('hexgrad/Kokoro-82M', allow_patterns=['config.json', 'kokoro-v1_0.pth', 'voices/*'])
"@
if ($LASTEXITCODE -ne 0) { throw "model download failed" }
Remove-Item (Join-Path $env:HF_HOME "xet") -Recurse -Force -ErrorAction Ignore   # download-only chunk cache
# The cache's snapshots\ files are symlinks into blobs\, and Inno follows symlinks, so
# each model would ship twice (plus hub 1.x's shared hub\blobs\). Loading only reads
# snapshots\ (huggingface_hub returns the snapshot file before it looks at blobs):
# turn the links into real files, then drop the blob stores.
$hub = Join-Path $env:HF_HOME "hub"
foreach ($link in @(Get-ChildItem $hub -Recurse -File | Where-Object LinkType)) {
  [IO.File]::Copy($link.FullName, "$($link.FullName).real")   # reads through the link
  Remove-Item $link.FullName
  Rename-Item "$($link.FullName).real" $link.Name
}
@(Get-ChildItem $hub -Recurse -Directory -Filter blobs) | Remove-Item -Recurse -Force
Remove-Item Env:HF_HOME

# 3. WebView2 Evergreen bootstrapper; the installer runs it only when the runtime is missing.
$wv2 = Join-Path $root "build\MicrosoftEdgeWebview2Setup.exe"
if (-not (Test-Path $wv2)) {
  Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $wv2
}

# 4. Compile the installer with Inno Setup
$isccCandidates = @(
  (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
  "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { $iscc = "ISCC.exe" }
& $iscc "/DAppVer=$Version" (Join-Path $root "FlowStudio.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$setup = Get-Item (Join-Path $root "FlowStudioSetup.exe")
Write-Host ("`nBuilt: {0} ({1:N0} MB)" -f $setup.FullName, ($setup.Length / 1MB)) -ForegroundColor Green
