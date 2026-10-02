#!/usr/bin/env bash
# Build the Flow Studio .dmg end-to-end on an Apple-silicon Mac (CI runs this on macos-14).
#   ./package.sh            # → FlowStudio.dmg
#   ./package.sh 1.2.0      # version for Info.plist
# Needs: venv/ with requirements-mac.lock + pyinstaller installed (see the release workflow).
#
# Signing is ad-hoc (every nested dylib and the .app; Apple silicon refuses unsigned code).
# Public release: sign with a Developer ID and notarize instead, i.e. replace the codesign line with
#   codesign --force --deep --options runtime --timestamp --sign "Developer ID Application: <name> (<team>)" "$app"
# then, after building the .dmg:
#   xcrun notarytool submit FlowStudio.dmg --apple-id <id> --team-id <team> --password <app-specific> --wait
#   xcrun stapler staple FlowStudio.dmg
# The hardened runtime (--options runtime) will likely need entitlements for torch/ctranslate2:
# com.apple.security.cs.allow-unsigned-executable-memory, ...disable-library-validation, and
# com.apple.security.device.audio-input for the mic.
set -euo pipefail
cd "$(dirname "$0")"
py=venv/bin/python
app="dist/Flow Studio.app"

# 1. Freeze the app (onedir + .app bundle with the Info.plist from FlowStudio.spec).
FLOW_VERSION="${1:-1.0.0}" "$py" -m PyInstaller FlowStudio.spec --noconfirm --distpath dist --workpath build

# 2. Bundle the default models as a Hugging Face cache in Contents/Resources/models
#    (flow_studio.py points HF_HUB_CACHE at models/hub and loads them in place): Kokoro-82M + its voices, faster-whisper small.
#    The cache's snapshot symlinks stay: codesign seals them and the .dmg keeps them.
export HF_HOME="$app/Contents/Resources/models"
"$py" -c "
import faster_whisper
from huggingface_hub import snapshot_download
faster_whisper.download_model('small')
snapshot_download('hexgrad/Kokoro-82M', allow_patterns=['config.json', 'kokoro-v1_0.pth', 'voices/*'])
"
rm -rf "$HF_HOME/xet"   # download-only chunk cache
unset HF_HOME

# 3. Re-sign: the models changed the bundle after PyInstaller signed it.
codesign --force --deep --sign - "$app"
codesign --verify --deep --strict --verbose=2 "$app"

# 4. Wrap it in a .dmg with the usual drag-to-Applications link.
stage=build/dmg
rm -rf "$stage" FlowStudio.dmg
mkdir -p "$stage"
ditto "$app" "$stage/Flow Studio.app"   # keeps symlinks + signatures
ln -s /Applications "$stage/Applications"
hdiutil create -volname "Flow Studio" -srcfolder "$stage" -format ULFO -ov FlowStudio.dmg
echo "Built: FlowStudio.dmg ($(du -m FlowStudio.dmg | cut -f1) MB)"
