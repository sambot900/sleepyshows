# Sleepy Shows

A robust video player with sleep timer functionality, episode browser, and playlist management.

## Features
- Sleep Timer
- Auto-Show: switch to a chosen show after a configurable amount of playback
- Trim: per-show beginning and ending skips, in seconds (Windows and Linux)
- Play/Pause/Previous/Next controls
- Episode browser by season
- Playlist creation (source folders, ordering, culling, shuffling)
- Interlude media injection
- Portable mode (local files) or Web mode (LAN mounts)

Configure Auto-Show in Settings. The player toggle changes it for the current night;
it returns to the saved preference at the auto sleep time, or midnight when auto
sleep is disabled. Pauses don't count toward the delay, and switching shows keeps
the remaining sleep timer and nightly shutoff schedule.

In **Settings → Trim**, choose a show and enter how many seconds to skip at the
beginning and end of each episode. Each show saves its own values; 0 disables
either skip. You can edit several shows before pressing Save. Changes apply when
an episode starts. Chronological playback keeps your resume position beyond the
intro, and an episode that reaches its trimmed ending advances normally. Movies,
bumps, and interludes are excluded.

## Usage
1. Install dependencies: `pip install -r requirements.txt`
2. Run the application: `python src/main.py`
3. Ensure you have the required system libraries installed.
   - **mpv**: `sudo apt install libmpv2`
   - **Qt Requirements**: `sudo apt install libxcb-cursor0` (Required for play button/window to appear on some Linux distributions)

   - Windows: `python-mpv` needs `libmpv-2.dll`. The build script will build the app and copy the DLL into `dist/` automatically (it will install `mpv.net` via `winget` if needed).
     - Prereq: install 7-Zip (so `7z.exe` is available): `winget install --id 7zip.7zip -e`
     - Navigate to the project directory first, then run: `powershell -ExecutionPolicy Bypass -File scripts\windows_build.ps1`

## Testing

On Windows, run `py -3 -m unittest discover -s tests -v` for playback, exposure,
nightly timers, Auto-Show, and Qt settings/control regressions. Run
`py -3 tests/smoke_mpv.py -v` to also test real playback with generated media,
including saved mpv resume positions, rapid file changes, and Unicode paths.
Trim checks also cover per-show persistence, movie/interlude exclusion, resume
behavior and real mpv playback ending early and advancing to the next episode.
The playback check requires `libmpv-2.dll` next to Python or in the project root.
Both commands use temporary media/settings. On Linux, use `python` in place of
`py -3`.

## Portable vs Web Mode

The Settings page has a toggle:
- **Portable mode**: plays local media files from your computer. **Not currently functional.**
- **Web mode** (default): plays media from a network filesystem root (SMB/UNC mounted as a local folder).

Startup behavior:
- If the configured external drive label (default: `T7`) is mounted, the app starts in **Portable** mode and runs auto-config.
- If the drive is not mounted, the app starts in **Web** mode.

Note: Portable mode still needs the paths in your playlists to exist on disk. If your playlists point at a mount point (e.g. `/mnt/shows/...`) and that share/drive is not mounted, playback will fail.

Configuration is stored in the per-user settings file:
- Linux: `~/.config/SleepyShows/settings.json`
- Windows: `%APPDATA%\\SleepyShows\\settings.json`
- macOS: `~/Library/Application Support/SleepyShows/settings.json`

Keys:
- `playback_mode`: `"portable"` or `"web"`
- `web_files_root` (recommended): filesystem root for Web mode (mounted SMB share / Windows drive letter / UNC path). Web mode resolves playlist paths under this root.

Example:
```json
{
  "playback_mode": "web",
  "web_files_root": "/mnt/shows/Sleepy Shows Data"
}
```

## Web Mode Notes

Web mode is filesystem-based:
- Mount `//10.0.0.210/shows` to a stable local path (e.g. `/mnt/shows`), then set `web_files_root` to `/mnt/shows/Sleepy Shows Data`.
- Playlist paths can be absolute (portable paths will be remapped) or relative under `Sleepy Shows Data/` (recommended).

## Building Executable
### Linux
Run ./build_linux.sh

### Linux (Start/Menu Entry)
After building, install a per-user desktop launcher + icon:
```bash
bash scripts/install_linux_desktop_entry.sh
```

### Windows
Navigate to the project directory, then run:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows_build.ps1
```
Notes:
- The Windows executable icon is embedded via PyInstaller using `assets/sleepy-ico.ico`.
