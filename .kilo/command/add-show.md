---
description: Add a show card + playlist to Sleepy Shows
argument-hint: "<Show Name> [shuffle_mode] [year] [icon_filename]"
---

Add a show to Sleepy Shows. Use $ARGUMENTS for the full argument string.

## Steps

### 1. Derive parameters from $1 (show name, required) and optional $2-$4

- `show_name` = $1 (keep exact casing)
- `shuffle_mode` = $2 or default: `"standard"` (options: `"off"`, `"standard"`, `"season"`)
- `year` = $3 or default: current year
- `icon` = $4 or default: `$1.lower().replace(" ", "").replace("'", "").replace(".", "") + "-icon.png"`

### 2. Add entry to `SHOW_CATALOG` in `src/main.py` (line ~100)

Insert before the `# Movies` comment, following the existing alignment pattern:

```python
    {"name": "<show_name>",     "icon": "<icon>", "type": "show", "shuffle_mode": "<shuffle_mode>", "year": <year>},
```

The existing catalog uses column alignment. Match the format of the existing "Trailer Park Boys" line (the last show before `# Movies`).

### 3. Add show patterns to BOTH auto-detect functions

Use `replaceAll=true` when editing since the two functions (`auto_detect_show_folders` ~line 2480 and `auto_detect_show_folders_web` ~line 2873) have identical `# Movies` boundaries.

Insert before the `# Movies` comment in each function. The target `oldString` must include the `Trailer Park Boys` block + the following `# Movies` comment to ensure uniqueness for replaceAll:

```
        ("Trailer Park Boys", [
            os.path.join('Shows', 'Trailer Park Boys', 'Episodes'),
            os.path.join('Shows', 'Trailer Park Boys'),
            os.path.join('Trailer Park Boys', 'Episodes'),
            os.path.join('Trailer Park Boys'),
            # Common abbreviation fallback
            os.path.join('Shows', 'TPB', 'Episodes'),
            os.path.join('Shows', 'TPB'),
            os.path.join('TPB', 'Episodes'),
            os.path.join('TPB'),
        ]),
        # Movies
        ("Birdman (2014)", [
```

Replace with:

```
        ("Trailer Park Boys", [
            os.path.join('Shows', 'Trailer Park Boys', 'Episodes'),
            os.path.join('Shows', 'Trailer Park Boys'),
            os.path.join('Trailer Park Boys', 'Episodes'),
            os.path.join('Trailer Park Boys'),
            # Common abbreviation fallback
            os.path.join('Shows', 'TPB', 'Episodes'),
            os.path.join('Shows', 'TPB'),
            os.path.join('TPB', 'Episodes'),
            os.path.join('TPB'),
        ]),
        ("<show_name>", [
            os.path.join('Shows', '<show_name>', 'Episodes'),
            os.path.join('Shows', '<show_name>'),
            os.path.join('<show_name>', 'Episodes'),
            os.path.join('<show_name>'),
        ]),
        # Movies
        ("Birdman (2014)", [
```

### 4. Create playlist JSON stub at `playlists/<show_name>.json`

```json
{
  "playlist": [],
  "shuffle_default": <true if shuffle_mode != "off" else false>,
  "shuffle_mode": "<shuffle_mode>",
  "auto_generated": false,
  "source_folder": "",
  "frequency_settings": {
    "episode_offsets": {},
    "season_offsets": {},
    "episode_factors": {},
    "season_factors": {}
  }
}
```

### 5. Verify

```bash
python3 -c "compile(open('src/main.py').read(), 'src/main.py', 'exec')" && echo "Syntax OK"
python3 -c "import json; json.load(open('playlists/<show_name>.json'))" && echo "Valid JSON"
```

### 6. Rebuild

```bash
bash build_linux.sh
```

## Notes

- The `show_name` must match the folder name on disk exactly (case-sensitive).
- Episodes go in one of: `<media>/Shows/<name>/Episodes/`, `<media>/Shows/<name>/`, `<media>/<name>/Episodes/`, `<media>/<name>/`
- The icon must exist at `assets/<icon>` — ask the user if unsure.
- Auto-detect on next launch will populate the playlist with episodes.
- Save-spot/resume is handled automatically by the app's built-in resume system (`~/.config/SleepyShows/resume_state.json`).
- `shuffle_mode: "off"` = chronological, `shuffle_mode: "standard"` = exposure-weighted shuffle, `shuffle_mode: "season"` = random season order.
- The project file is `src/main.py` (~14.7k lines). The `playlists/` directory is gitignored.
