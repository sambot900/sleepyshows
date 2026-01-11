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

   - Windows: `python-mpv` needs `libmpv-2.dll`. This repo includes an mpv build archive in `drivers/` and a script that builds the app and copies the DLL into `dist/` automatically.
     - One command: `powershell -ExecutionPolicy Bypass -File scripts\windows_build.ps1`

## Building Executable
### Linux
Run `./build_linux.sh`

### Windows
Run: `powershell -ExecutionPolicy Bypass -File scripts\windows_build.ps1`
