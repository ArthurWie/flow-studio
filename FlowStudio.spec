# PyInstaller spec shared by every OS (one runner per OS; PyInstaller can't cross-compile).
#   pyinstaller FlowStudio.spec --noconfirm              # windowed (production)
#   set FLOW_CONSOLE=1 & pyinstaller FlowStudio.spec     # console build, shows startup errors
# Output: dist/FlowStudio/  (onedir; package.ps1 adds models/ and wraps it with Inno Setup)
#         macOS also: dist/Flow Studio.app  (package.sh adds models, signs ad-hoc, wraps it in a .dmg)
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
# The version the app reports (gpu_pack fetches the GPU pack of the same version).
os.makedirs("build", exist_ok=True)
with open("build/version.txt", "w") as f:
    f.write(os.environ.get("FLOW_VERSION", "0.0.0"))
datas, binaries, hidden = [("flow.ico", "."), ("flow.icns", "."), ("build/version.txt", ".")], [], []
for pkg in PACKAGES:
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
for pkg in ("en_core_web_sm", "spacy", "torch", "numpy"):
    datas += copy_metadata(pkg)


def _runtime(entry):
    """Drop build-time files collect_all drags in (torch's static .lib, C++ headers, debug symbols)."""
    src = entry[0].replace("\\", "/")
    return not (src.endswith((".lib", ".pdb", ".h", ".hpp", ".cuh")) or "/include/" in src)


datas, binaries = list(filter(_runtime, datas)), list(filter(_runtime, binaries))

a = Analysis(["flow_studio.py"], datas=datas, binaries=binaries, hiddenimports=hidden)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="FlowStudio",
          console=bool(os.environ.get("FLOW_CONSOLE")),
          icon="flow.ico" if sys.platform == "win32" else None)
coll = COLLECT(exe, a.binaries, a.datas, name="FlowStudio")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Flow Studio.app", icon="flow.icns",
                 # keep it stable: macOS ties the mic/Accessibility grants to it
                 bundle_identifier="io.github.arthurwie.flowstudio",
                 version=os.environ.get("FLOW_VERSION", "0.0.0"),
                 info_plist={
                     "NSMicrophoneUsageDescription":
                         "Flow Studio listens to your microphone while dictation is on, and turns your speech into text on this Mac.",
                     "LSMinimumSystemVersion": "14.0",   # torch 2.13's macOS wheel floor
                     "NSHighResolutionCapable": True,
                 })
