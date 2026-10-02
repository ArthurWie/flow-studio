# PyInstaller spec shared by every OS (one runner per OS; PyInstaller can't cross-compile).
#   pyinstaller FlowStudio.spec --noconfirm              # windowed (production)
#   set FLOW_CONSOLE=1 & pyinstaller FlowStudio.spec     # console build, shows startup errors
# Output: dist/FlowStudio/  (onedir; package.ps1 adds models/ and wraps it with Inno Setup)
#
# The --collect-all list is the flag set verified with the old build.ps1 (the frozen exe
# loads torch + kokoro + misaki + spaCy en_core_web_sm and generates audio) plus pymupdf.
# torch bundles fine on its own; the NLP stack ships data files PyInstaller doesn't detect.
import os
import sys

from PyInstaller.utils.hooks import collect_all, copy_metadata

PACKAGES = ["torch", "kokoro", "misaki", "en_core_web_sm", "spacy", "thinc",
            "faster_whisper", "ctranslate2", "sounddevice", "soundfile", "av",
            "onnxruntime", "webview", "espeakng_loader", "phonemizer", "num2words",
            "language_tags", "pymupdf", "trafilatura", "justext"]
datas, binaries, hidden = [("flow.ico", ".")], [], []
for pkg in PACKAGES:
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
for pkg in ("en_core_web_sm", "spacy", "torch", "numpy"):
    datas += copy_metadata(pkg)

a = Analysis(["flow_studio.py"], datas=datas, binaries=binaries, hiddenimports=hidden)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="FlowStudio",
          console=bool(os.environ.get("FLOW_CONSOLE")),
          icon="flow.ico" if sys.platform == "win32" else None)
coll = COLLECT(exe, a.binaries, a.datas, name="FlowStudio")
