"""Where Flow Studio keeps writable data (history, pronunciations, models, settings). Stdlib only."""
import os
import sys
from pathlib import Path


def data_dir():
    """%LOCALAPPDATA%\\FlowStudio on Windows (unchanged, so existing data stays found),
    ~/Library/Application Support/FlowStudio on macOS, $XDG_DATA_HOME/flow-studio elsewhere."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FlowStudio"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FlowStudio"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "flow-studio"
