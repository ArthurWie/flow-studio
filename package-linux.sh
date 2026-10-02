#!/usr/bin/env bash
# Build the Flow Studio Linux tarball end-to-end (CI runs this on ubuntu-latest).
#   ./package-linux.sh            # → flow-studio-linux-x86_64.tar.gz
#   ./package-linux.sh 1.2.0      # version the app reports
# Needs: venv/ with requirements-linux.lock + pyinstaller installed (see release-linux.yml).
# Users unpack it and run flow-studio/install-linux.sh.
set -euo pipefail
cd "$(dirname "$0")"
py=venv/bin/python
dist=dist/FlowStudio

# 1. Freeze the app (onedir).
FLOW_VERSION="${1:-1.0.0}" "$py" -m PyInstaller FlowStudio.spec --noconfirm --distpath dist --workpath build

# 2. Bundle the default models as a Hugging Face cache next to the exe (flow_studio.py
#    points HF_HUB_CACHE at models/hub and loads them in place): Kokoro-82M + its voices, faster-whisper small.
#    The cache's snapshot symlinks stay: tar keeps them.
export HF_HOME="$dist/models"
"$py" -c "
import faster_whisper
from huggingface_hub import snapshot_download
faster_whisper.download_model('small')
snapshot_download('hexgrad/Kokoro-82M', allow_patterns=['config.json', 'kokoro-v1_0.pth', 'voices/*'])
"
rm -rf "$HF_HOME/xet"   # download-only chunk cache
unset HF_HOME

# 3. The launcher icon, the install script and the glibc it needs.
"$py" -c "from PIL import Image; Image.open('flow.ico').save('$dist/flow.png')"
cp install-linux.sh "$dist/"
getconf GNU_LIBC_VERSION | cut -d' ' -f2 > "$dist/glibc.txt"   # bundled host libs need at least this

# 4. Tarball with a flow-studio/ top folder.
tar -C dist --transform 's,^FlowStudio,flow-studio,' -czf flow-studio-linux-x86_64.tar.gz FlowStudio
echo "Built: flow-studio-linux-x86_64.tar.gz ($(du -m flow-studio-linux-x86_64.tar.gz | cut -f1) MB)"
