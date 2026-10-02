#!/usr/bin/env bash
# Install Flow Studio for this user (no root needed): run it from the unpacked tarball.
#   tar xzf flow-studio-linux-x86_64.tar.gz && ./flow-studio/install-linux.sh
# Puts the app in ~/.local/share/flow-studio/app (the folder above it holds your history,
# settings and models, so reinstalling keeps them), adds the `flow-studio` command to
# ~/.local/bin and a launcher to the app menu, then names any missing system libraries
# with the apt / dnf command that installs them.
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
dest=$data/flow-studio/app
bin=$HOME/.local/bin

# ── system libraries ────────────────────────────────────────────────────────
# The window (Qt WebEngine, bundled) links a few desktop libraries the distro provides;
# dictation loads PortAudio. Ask the dynamic linker what's missing, with the bundle's libs on the path.
qt=$src/_internal/PySide6/Qt
out=$(LD_LIBRARY_PATH="$src/_internal:$qt/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ldd "$src"/_internal/libpython3*.so* \
  "$qt/plugins/platforms/libqxcb.so" "$qt/lib/libQt6WebEngineCore.so.6" "$qt/libexec/QtWebEngineProcess" 2>&1 || true)
# PyInstaller bundles libraries from the build machine, so its glibc is the floor (glibc.txt, package-linux.sh).
need=$(cat "$src/glibc.txt") have=$(getconf GNU_LIBC_VERSION | cut -d' ' -f2)
if [ "$(printf '%s\n' "$need" "$have" | sort -V | head -1)" != "$need" ]; then
  echo "This Linux is too old for Flow Studio: it needs glibc $need or newer (you have $have; Ubuntu 24.04 and Fedora 40 are new enough)."
  exit 1
fi

# Copy next to the old version, then swap: a failed copy leaves the installed one working.
mkdir -p "$data/flow-studio" "$bin" "$data/applications"
if [ "$src" != "$dest" ]; then
  rm -rf "$dest.new" "$dest.old"
  cp -a "$src" "$dest.new"
  if [ -e "$dest" ]; then mv "$dest" "$dest.old"; fi
  mv "$dest.new" "$dest"
  rm -rf "$dest.old"
fi
ln -sfn "$dest/FlowStudio" "$bin/flow-studio"
cat > "$data/applications/flow-studio.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Flow Studio
Comment=Local dictation and text to speech
Exec="$dest/FlowStudio"
Icon=$dest/flow.png
Terminal=false
Categories=Utility;Audio;
StartupWMClass=FlowStudio
EOF
command -v update-desktop-database >/dev/null && update-desktop-database -q "$data/applications" || true
echo "Flow Studio is installed: open it from the app menu, or run flow-studio."
case ":$PATH:" in *":$bin:"*) ;; *) echo "Note: $bin isn't on your PATH, so the command is $bin/flow-studio." ;; esac

# ── what to install ───────────────────────────────────────────────────────
missing=$(awk '/=> not found/ {print $1}' <<<"$out" | sort -u)
PATH=$PATH:/sbin:/usr/sbin ldconfig -p | grep 'libportaudio\.so\.2 ' >/dev/null || missing="libportaudio.so.2 $missing"
missing=$(echo $missing)
[ -z "$missing" ] && exit 0

apt_pkg() {
  # Debian naming: libxcb-cursor.so.0 → libxcb-cursor0, libxkbcommon-x11.so.0 → libxkbcommon-x11-0,
  # libnss3.so → libnss3; plus the t64 rename where the distro has one (Ubuntu 24.04: libasound2t64).
  # ponytail: naming heuristic, a soname whose package breaks the pattern needs a table
  local n=${1%%.so*} v=${1#*.so} p
  v=${v#.}; v=${v%%.*}; n=${n,,}
  [[ $n =~ [0-9]$ && -n $v ]] && n=$n-
  p=$n$v
  if apt-cache show "${p}t64" >/dev/null 2>&1; then p=${p}t64; fi
  echo "$p"
}
echo
echo "Missing system libraries: $missing"
echo "Dictation needs PortAudio (libportaudio); the app window needs the others."
echo "Install them with:"
if command -v apt-get >/dev/null; then
  echo "  sudo apt install $(for l in $missing; do apt_pkg "$l"; done | xargs)"
elif command -v dnf >/dev/null; then
  echo "  sudo dnf install$(for l in $missing; do printf " '%s()(64bit)'" "$l"; done)"
else
  echo "  your package manager's packages that provide these files"
fi
