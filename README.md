# Sleepy Shows

A robust video player with sleep timer functionality, episode browser, and playlist management.

## Features
- Sleep Timer
- Play/Pause/Previous/Next controls
- Episode browser by season
- Playlist creation (source folders, ordering, culling, shuffling)
- Interstitial media injection

## Usage
1. Install dependencies: `pip install -r requirements.txt`
2. Run the application: `python src/main.py`
3. Ensure you have the required system libraries installed.
   - **mpv**: `sudo apt install libmpv2`
   - **Qt Requirements**: `sudo apt install libxcb-cursor0` (Required for play button/window to appear on some Linux distributions)

   - Windows: `python-mpv` needs `libmpv-2.dll`. The build script will build the app and copy the DLL into `dist/` automatically (it will install `mpv.net` via `winget` if needed).
     - Prereq: install 7-Zip (so `7z.exe` is available): `winget install --id 7zip.7zip -e`
     - Navigate to the project directory first, then run: `powershell -ExecutionPolicy Bypass -File scripts\windows_build.ps1`

## Building Executable
### Linux
Run `./build_linux.sh`

### Windows
Navigate to the project directory, then run:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows_build.ps1
```
