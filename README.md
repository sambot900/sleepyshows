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

   - Windows: Download `libmpv-2.dll` (from https://sourceforge.net/projects/mpv-player-windows/files/libmpv/) and place it in the system path or next to `main.py`.

## Building Executable
### Linux
Run `./build_linux.sh`

### Windows
1. `pip install pyinstaller`
2. `pyinstaller --name "SleepyShows" --windowed --noconsole src/main.py`
3. Copy `libmpv-2.dll` into the `dist/SleepyShows/` folder.
