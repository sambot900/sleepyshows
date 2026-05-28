# Adding a New Show

Three changes in `src/main.py`, plus the icon file.

## 1. SHOW_CATALOG (line ~82)

Add an entry to the list:

```python
{"name": "Show Name", "icon": "show-icon.png", "type": "show", "shuffle_mode": "standard", "year": 2025},
```

- `name` — must match the folder name on disk exactly (case-sensitive)
- `icon` — filename in `assets/`
- `type` — `"show"` or `"movie"`
- `shuffle_mode` — `"standard"`, `"off"`, or `"season"`
- `year` — release year

## 2. show_patterns lists (TWO locations)

Both lists are identical. Add the show to both:

### a) `auto_detect_show_folders` (~line 2011)
### b) `auto_detect_show_folders_web` (~line 2348)

Add after the last show entry, before `# Movies`:

```python
("Show Name", [
    os.path.join('Shows', 'Show Name', 'Episodes'),
    os.path.join('Shows', 'Show Name'),
    os.path.join('Show Name', 'Episodes'),
    os.path.join('Show Name'),
]),
```

Use `replaceAll=True` when editing since both lists are identical and need the same change.

## 3. Icon file

Place the icon in `assets/show-icon.png`.

## Folder layout on disk

Episodes go in one of these patterns (checked in order):

```
<media_dir>/Shows/Show Name/Episodes/
<media_dir>/Shows/Show Name/
<media_dir>/Show Name/Episodes/
<media_dir>/Show Name/
```

## Rebuild

```bash
bash build_linux.sh
```

The splash screen on next launch will auto-detect the folder and generate a playlist `.json`.
