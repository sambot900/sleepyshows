#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

dist_dir="$repo_root/dist/SleepyShows"
dist_exe="$dist_dir/SleepyShows"
icon_src="$repo_root/assets/sleepy-ico.png"

desktop_dir="$HOME/.local/share/applications"
hicolor_base="$HOME/.local/share/icons/hicolor"
pixmaps_dir="$HOME/.local/share/pixmaps"
bin_dir="$HOME/.local/bin"

if [[ ! -f "$dist_exe" ]]; then
  echo "Missing built executable: $dist_exe" >&2
  echo "Run ./build_linux.sh first, then re-run this script." >&2
  exit 1
fi

if [[ ! -f "$icon_src" ]]; then
  echo "Missing icon file: $icon_src" >&2
  exit 1
fi

mkdir -p "$desktop_dir" "$bin_dir"

# Create a launcher wrapper in ~/.local/bin (avoids space-in-path issues in .desktop Exec).
cat > "$bin_dir/sleepyshows" <<LAUNCHER
#!/usr/bin/env bash
exec "$dist_exe" "\$@"
LAUNCHER
chmod +x "$bin_dir/sleepyshows"
echo "Installed launcher: $bin_dir/sleepyshows"

# Install 256x256 icon (native size).
icon_dir_256="$hicolor_base/256x256/apps"
mkdir -p "$icon_dir_256"
install -m 0644 "$icon_src" "$icon_dir_256/sleepyshows.png"
echo "Installed icon: $icon_dir_256/sleepyshows.png"

# Generate and install smaller icon sizes for desktop environments that need them.
for size in 128 64 48; do
  icon_dir_sized="$hicolor_base/${size}x${size}/apps"
  mkdir -p "$icon_dir_sized"
  if command -v convert &>/dev/null; then
    convert "$icon_src" -resize "${size}x${size}" "$icon_dir_sized/sleepyshows.png"
    echo "Installed icon: $icon_dir_sized/sleepyshows.png"
  elif command -v magick &>/dev/null; then
    magick "$icon_src" -resize "${size}x${size}" "$icon_dir_sized/sleepyshows.png"
    echo "Installed icon: $icon_dir_sized/sleepyshows.png"
  fi
done

# Pixmaps fallback (some older DEs and launchers look here).
mkdir -p "$pixmaps_dir"
install -m 0644 "$icon_src" "$pixmaps_dir/sleepyshows.png"

cat > "$desktop_dir/sleepyshows.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=SleepyShows
Comment=Video player with sleep timer
Exec=$bin_dir/sleepyshows
Icon=$icon_dir_256/sleepyshows.png
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=SleepyShows
EOF

chmod 644 "$desktop_dir/sleepyshows.desktop"

echo "Installed desktop entry: $desktop_dir/sleepyshows.desktop"

# Update desktop database so the entry appears in application menus immediately.
if command -v update-desktop-database &>/dev/null; then
  update-desktop-database "$desktop_dir" 2>/dev/null || true
fi

# Update icon cache so the icon is picked up without a logout.
if command -v gtk-update-icon-cache &>/dev/null; then
  gtk-update-icon-cache -f -t "$hicolor_base" 2>/dev/null || true
fi

echo "Done. SleepyShows should now appear in your application launcher."
