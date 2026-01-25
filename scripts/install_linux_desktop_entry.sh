#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

dist_exe="$repo_root/dist/SleepyShows/SleepyShows"
icon_src="$repo_root/assets/sleepy-ico.png"

desktop_dir="$HOME/.local/share/applications"
icon_dir="$HOME/.local/share/icons/hicolor/256x256/apps"

if [[ ! -f "$dist_exe" ]]; then
  echo "Missing built executable: $dist_exe" >&2
  echo "Run ./build_linux.sh first, then re-run this script." >&2
  exit 1
fi

if [[ ! -f "$icon_src" ]]; then
  echo "Missing icon file: $icon_src" >&2
  exit 1
fi

mkdir -p "$desktop_dir" "$icon_dir"

install -m 0644 "$icon_src" "$icon_dir/sleepyshows.png"

cat > "$desktop_dir/sleepyshows.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=SleepyShows
Comment=Video player with sleep timer
Exec=$dist_exe
Icon=sleepyshows
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=SleepyShows
EOF

echo "Installed desktop entry: $desktop_dir/sleepyshows.desktop"
echo "Installed icon:         $icon_dir/sleepyshows.png"
echo "If it doesn't appear immediately, log out/in or run: update-desktop-database $desktop_dir (if available)."
