# Point every OS's data dir at a throwaway folder before any app module is imported,
# so tests never touch real history, pronunciations or settings.
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="flowstudio_selftest_")
for var in ("LOCALAPPDATA", "XDG_DATA_HOME", "HOME"):
    os.environ[var] = _tmp
