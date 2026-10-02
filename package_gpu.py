"""Split the frozen CUDA build into the GPU pack's release assets (#18; CI runs this on Windows).
  python package_gpu.py dist-gpu/FlowStudio 1.2.0 gpu-out
Writes to gpu-out: FlowStudio-gpu-core-<version>-<n>.zip (the app), FlowStudio-gpu-libs-<id>-<n>.zip
(the NVIDIA libraries from torch/lib, flat) and gpu-pack.json (every zip's name, size, SHA-256).
The libs id hashes the libraries' contents, so the app downloads them again only when they change.
"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import gpu_pack

NVIDIA = ("cublas", "cudart", "cudnn", "cufft", "curand", "cusolver", "cusparse",
          "nvjitlink", "nvrtc", "nvtoolsext", "cupti", "nvfatbin", "nvblas")
# GitHub Releases rejects assets over 2 GiB. Grouped by uncompressed size, so every zip is under it.
MAX_ZIP = 1_900_000_000


def _zips(files, root, stem, out, flat=False):
    """Pack files into as few zips under MAX_ZIP as fit; [{name, size, sha256}]."""
    groups, size = [[]], 0
    for f in files:
        n = f.stat().st_size
        if groups[-1] and size + n > MAX_ZIP:
            groups.append([])
            size = 0
        groups[-1].append(f)
        size += n
    assets = []
    for i, group in enumerate(groups, 1):
        z = out / f"{stem}-{i}.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in group:
                zf.write(f, f.name if flat else f.relative_to(root).as_posix())
        assets.append({"name": z.name, "size": z.stat().st_size, "sha256": gpu_pack._sha256(z)})
        print(f"  {z.name}: {len(group)} files, {z.stat().st_size / 1e6:,.0f} MB")
    return assets


def main(dist, version, out):
    dist, out = Path(dist), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    torch_lib = dist / "_internal" / "torch" / "lib"
    libs = sorted(f for f in torch_lib.iterdir() if f.name.lower().startswith(NVIDIA))
    if not any(f.name.lower().startswith("cublas") for f in libs):
        sys.exit(f"no CUDA libraries in {torch_lib}: is this the CUDA build?")
    core = sorted(f for f in dist.rglob("*") if f.is_file() and f not in libs)
    h = hashlib.sha256()
    for f in libs:
        h.update(f"{f.name}:{gpu_pack._sha256(f)}\n".encode())
    libs_id = h.hexdigest()
    manifest = {"version": version, "libs_id": libs_id,
                "core": _zips(core, dist, f"FlowStudio-gpu-core-{version}", out),
                "libs": _zips(libs, torch_lib, f"FlowStudio-gpu-libs-{libs_id[:12]}", out, flat=True)}
    (out / gpu_pack.MANIFEST).write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:4])
