# Build Flow Studio into a standalone Windows folder via PyInstaller.
#   .\build.ps1           # windowed build (production; runs with no console)
#   .\build.ps1 -Debug    # console build (shows startup errors — use when diagnosing)
#
# Output: <drive>:\FlowStudio-build\dist\FlowStudio\FlowStudio.exe  (~1.1 GB, onedir)
# The build dir is kept on the same drive as this repo — PyInstaller's --specpath
# cannot compute a relative path across drives (ValueError: path is on mount ...).
#
# This exact flag set is verified: the frozen exe loads torch + kokoro + misaki +
# spaCy (en_core_web_sm) and generates audio. The --collect-all list exists because
# torch bundles fine on its own, but the NLP stack ships data files PyInstaller does
# not auto-detect (spaCy model + its metadata, misaki/language_tags JSON, espeak data).
param([switch]$Debug)

$ErrorActionPreference = "Stop"
$root  = $PSScriptRoot
$py    = Join-Path $root "venv\Scripts\python.exe"
$drive = Split-Path $root -Qualifier                 # e.g. "D:"
$out   = "$drive\FlowStudio-build"
$mode  = if ($Debug) { "--console" } else { "--windowed" }

& $py -m PyInstaller (Join-Path $root "flow_studio.py") `
  --name FlowStudio --onedir $mode --icon (Join-Path $root "flow.ico") --noconfirm `
  --distpath "$out\dist" --workpath "$out\build" --specpath $out `
  --collect-all torch --collect-all kokoro --collect-all misaki `
  --collect-all en_core_web_sm --collect-all spacy --collect-all thinc `
  --collect-all faster_whisper --collect-all ctranslate2 `
  --collect-all sounddevice --collect-all soundfile --collect-all av `
  --collect-all onnxruntime --collect-all webview --collect-all espeakng_loader `
  --collect-all phonemizer --collect-all num2words --collect-all language_tags `
  --copy-metadata en_core_web_sm --copy-metadata spacy --copy-metadata torch --copy-metadata numpy

if ($LASTEXITCODE -eq 0) {
    Write-Host "`nBuilt: $out\dist\FlowStudio\FlowStudio.exe" -ForegroundColor Green
}
