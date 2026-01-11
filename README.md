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

   - Windows: `python-mpv` needs `libmpv-2.dll`. This repo does not commit the DLL because it's a third-party binary (licensing/security/updates), but you shouldn't have to hunt for it.
     - Install mpv (recommended): `winget install --id=mpv.mpv -e` (or `scoop install mpv`)
     - Then run: `powershell -ExecutionPolicy Bypass -File scripts\get-libmpv.ps1 -Destination .`
     - You can also put `libmpv-2.dll` in your system PATH or next to `src/main.py`.

## Building Executable
### Linux
Run `./build_linux.sh`

### Windows
1. `pip install pyinstaller`
2. `pyinstaller --name "SleepyShows" --windowed --noconsole src/main.py`
3. Copy `libmpv-2.dll` into the `dist/SleepyShows/` folder. (If you installed mpv, you can run `powershell -ExecutionPolicy Bypass -File scripts\get-libmpv.ps1 -Destination dist\SleepyShows`)
