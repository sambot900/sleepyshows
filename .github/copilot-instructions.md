# Sleepy Shows — Copilot Instructions

## Code Quality

- Write extensible, maintainable, readable code. No band-aid fixes. If a fix doesn't fit the existing architecture, refactor the architecture.
- Prefer the simplest correct solution. Do not over-engineer or add abstractions for hypothetical future needs.
- After completing an implementation, review it for: redundancy, compatibility with existing structures, reliability edge cases, and performance.
- Never duplicate logic that already exists elsewhere in the codebase — find and reuse it.

## Development Practice

- Consider user experience at every decision — loading states, error states, empty states, and fallback behavior matter.
- Avoid blocking the main Qt event loop. Long-running work (file scanning, exposure score I/O) must be deferred or throttled.
- New implementations must reuse existing patterns (exposure scoring, shuffle-bag queues, signal/slot wiring) before introducing new ones.
- Use `rg` (ripgrep) instead of `grep` for all terminal searches.

## Cross-Platform Compatibility

- All code must work on both **Windows 11** and **Linux**. Primary testing happens on Windows; request Linux testing explicitly when needed.
- Use `os.path` (`join`, `basename`, `exists`, etc.) for all path construction — never hardcode `/` or `\\` separators.
- Use platform-aware config directories (`%APPDATA%\SleepyShows\` on Windows, `~/.config/SleepyShows/` on Linux) via the existing `_get_user_config_dir()` helper.
- File I/O must specify `encoding='utf-8'` explicitly; Windows defaults to the system codepage otherwise.
- Guard platform-specific code (e.g., DLL loading, `ctypes` calls, `caffeinate`) behind `sys.platform` / `platform.system()` checks.
- Qt/PySide6 abstractions handle most UI differences, but watch for: native window embedding (`wid`), DPI scaling, and font rendering differences.
- When a feature touches mpv, path resolution, or keep-awake, verify the approach works on both platforms before merging.

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3 |
| UI Framework | PySide6 (Qt6) |
| Media Backend | mpv via python-mpv |
| Audio Metadata | mutagen |
| Packaging | PyInstaller |
| Keep-Awake | Platform-native (Windows ctypes, macOS caffeinate, Linux DBus/systemd-inhibit) |
| Configuration | JSON files in platform-specific config dirs |

## Project Structure

```
src/
  main.py               — QMainWindow app: player, browser, playlist editor, settings,
                           sleep timer, bump card rendering, gradient UI, custom widgets
  player_backend.py     — MpvPlayer (embedded video) + MpvAudioPlayer (sound effects/bump music)
  playlist_manager.py   — Episode library, shuffle modes, exposure scoring, playback history, queuing
  bump_manager.py       — Bump script parsing, duration estimation, music matching, queue management
  keep_awake.py         — Cross-platform idle/sleep inhibitor
  ui_styles.py          — Dark-theme QSS stylesheet
  test_path.py          — Asset path resolver (frozen vs source)
  services/
    playlist_io.py      — Playlist JSON loader with URL rejection
    web_mode_paths.py   — Network filesystem path conversion

assets/                 — Icons, logos, images, crickets.mp3, HelveticaNeue font
bumps/                  — User-populated bump script .txt files
playlists/              — Show JSON playlists + exposure_scores.json
drivers/                — Pre-built mpv binary for Windows
libs/                   — Bundled libxcb-cursor.so for Linux
packaging/              — sleepyshows.desktop (Linux desktop entry)
scripts/                — Build helpers, debug tools, asset generators
```

## Key Classes

- `SleepyShowsWindow` — Main application window; owns tabs (Player, Browser, Editor, Settings), sleep timer, playback controls
- `MpvPlayer` — Embeds mpv as a native window inside a Qt widget; signals: `positionChanged`, `durationChanged`, `playbackFinished`, `errorOccurred`, `playbackPaused`, `mouseMoved`, `fullscreenRequested`, `escapePressed`
- `MpvAudioPlayer` — Audio-only mpv instance (vo=null) for sound effects and bump music
- `PlaylistManager` — Episode library scanning, shuffle modes (off/standard/season), exposure-based randomization, playback history, queue management
- `BumpManager` — Parses bump scripts, estimates card durations, matches music by exposure + eligibility, manages shuffle-bag queues for scripts/music/video/outro
- `KeepAwakeInhibitor` — Platform-specific idle/sleep prevention while media plays

## Custom Widgets (main.py)

- `BumpImageView` — Renders bump card images with viewport-aware scaling
- `GradientBackgroundWidget` — 6-color base gradient with 3 overlaid radial blobs
- `TriStrokeButton` / `TriStrokeToolButton` — Buttons with gradient stroke outlines
- `ToggleSwitch` — Pill-track toggle with sliding thumb
- `Spinner` — Animated loading spinner
- `StartupLoadingScreen` — Splash with progress bar

## Playlist Format

JSON files in `playlists/`, one per show:
```json
{
  "playlist": [
    { "type": "video", "path": "/absolute/path/to/S01E01 Title.mkv" },
    ...
  ]
}
```
Episodes sorted naturally (S01E01, S01E02, … S01E10, … S02E01). Paths are absolute; in web mode they're resolved relative to a configured network mount root.

## Exposure Scoring System

Prevents repetition across episodes, bumps, interludes, scripts, and music:

- **Per-play deltas:** First 3 plays → +100, next 3 → +50, next 3 → +25, … (diminishing returns)
- **Sleep timer integration:** Deltas diminish when sleep timer is ON; constant when OFF
- **Per-episode/season overrides:** `episode_exposure_offsets`, `season_exposure_offsets` (additive), `episode_exposure_factors`, `season_exposure_factors` (multiplicative)
- **Persistent:** `exposure_scores.json` with throttled disk writes (1.5s minimum between saves)
- **Shuffle-bag queues:** Avoid repeats — recent 8 items excluded from first 8 queue slots; auto-rebuild on dependency change

## Shuffle Modes

| Mode | Behavior |
|---|---|
| off | Chronological playback |
| standard | Random episode from entire library |
| season | Pick random season, then random episode within it |

## Bump Card System

TV-style interstitial cards between episodes. Bumps are defined by `.txt` script files parsed per the grammar in `docs/bump syntax.txt`.

**Script components:** text cards, pauses, images (`<img>`), sound effects (`<sound>`), outro cards (`<outro>`)

**Duration model:** `ms_per_char × char_count + base_card_ms + one_line_bonus`, clamped to [900ms, 6000ms], scaled by `_duration_scale = 1.26`

**Music matching:**
1. Eligibility: `T_estimated ≤ T_music × (1 + ε)` where ε = 0.20
2. Separate fixed (explicit durations) vs scalable (auto-timed) card time
3. Distribute reduction across scalable cards by power-normalized weights (α = 1.0) with exponential soft clamp (k = 4.0)
4. Short bump rule: scripts ≤ ~23s estimated can compress to 15s music clips (ε = 0.533)

**Exposure tracked per:** script, music file, video, outro sound — each with its own shuffle-bag queue

## Playback Architecture

1. User selects episode (or shuffle picks one)
2. Optional bump card plays first (script + music + optional video/outro)
3. Episode plays via `MpvPlayer` embedded in Qt widget
4. On end-of-file: apply exposure scoring, check skip penalty, advance queue
5. Playback history supports previous/next navigation across episodes and bumps

## Sleep Timer

- User configurable duration
- Visual countdown in UI
- Affects exposure delta scaling (diminishing deltas while active)
- On timeout: stops playback, releases keep-awake inhibitor
- Resume state saved for next session

## Keep-Awake

| Platform | Mechanism |
|---|---|
| Windows | `SetThreadExecutionState` (ES_CONTINUOUS \| ES_SYSTEM_REQUIRED \| ES_DISPLAY_REQUIRED) |
| macOS | `caffeinate -dimsu -w {PID}` |
| Linux | DBus `org.freedesktop.ScreenSaver.Inhibit`, fallback to `systemd-inhibit` |

## Visual Design

- Dark theme by default; all colors from `ui_styles.py` QSS (background #2b2b2b, text #e0e0e0)
- Modern "chunky" gradient aesthetic: 6-color base gradient, 3 radial blob overlays, shared anchor point
- Hard color stop pairs (0.14/0.141) for sharp gradient transitions
- HSL color space functions for theme customization
- HelveticaNeue-CondensedBlack font bundled for bump card rendering

## Modes

- **Portable mode:** External drive detected by label (default "T7"); auto-discovers shows from drive
- **Web mode:** Network shares (SMB/UNC) mounted locally; playlist paths resolved relative to configured root

## Configuration

Per-user JSON settings stored at platform-specific paths:

| Platform | Path |
|---|---|
| Linux | `~/.config/SleepyShows/` |
| Windows | `%APPDATA%\SleepyShows\` |
| macOS | `~/Library/Application Support/SleepyShows/` |

## Build & Run

- **Run:** `./run.sh` or `python src/main.py` (with `LD_LIBRARY_PATH` including `libs/` on Linux)
- **Build Linux:** `./build_linux.sh` (creates venv, installs deps, runs PyInstaller via `SleepyShows.spec`)
- **Build Windows:** `powershell -ExecutionPolicy Bypass -File scripts\windows_build.ps1 -Clean`
- **Linux runtime deps:** libmpv2, libxcb-cursor0 (bundled in `libs/`)
- **Windows:** mpv DLL from `drivers/` archive; PerMonitorV2 DPI awareness via `SleepyShows.manifest`