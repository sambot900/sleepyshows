#!/usr/bin/env python3

import argparse
import json
import os
import random
import sys


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _infer_paths():
    settings_path = os.path.expanduser("~/.config/SleepyShows/settings.json")
    settings = _read_json(settings_path) or {}

    scripts_dir = None
    music_dir = None
    outro_dir = None

    # Best-effort: use the common TV Vibe folder layout when present.
    try:
        tv_vibe_root = "/media/tyler/T7/Sleepy Shows Data/TV Vibe"
        if os.path.isdir(tv_vibe_root):
            if os.path.isdir(os.path.join(tv_vibe_root, "scripts")):
                scripts_dir = os.path.join(tv_vibe_root, "scripts")
            if os.path.isdir(os.path.join(tv_vibe_root, "music")):
                music_dir = os.path.join(tv_vibe_root, "music")
            if os.path.isdir(os.path.join(tv_vibe_root, "outro sounds")):
                outro_dir = os.path.join(tv_vibe_root, "outro sounds")
    except Exception:
        pass

    # If settings contains paths, prefer them when they exist.
    # (Settings currently stores images/audio fx; scripts/music dirs are typically auto-detected.)
    return {
        "settings_path": settings_path,
        "scripts_dir": scripts_dir,
        "music_dir": music_dir,
        "outro_dir": outro_dir,
        "exposure_path": os.path.expanduser("~/.config/SleepyShows/playlists/exposure_scores.json"),
    }


def main():
    parser = argparse.ArgumentParser(description="Debug bump queue composition (script -> music mapping).")
    parser.add_argument("--scripts", dest="scripts_dir", default=None, help="Bump scripts folder")
    parser.add_argument("--music", dest="music_dir", default=None, help="Bump music folder")
    parser.add_argument("--outro", dest="outro_dir", default=None, help="Outro sounds folder")
    parser.add_argument(
        "--exposure",
        dest="exposure_path",
        default=None,
        help="Exposure JSON path (~/.config/SleepyShows/playlists/exposure_scores.json)",
    )
    parser.add_argument("--seed", dest="seed", type=int, default=12345, help="Random seed")
    parser.add_argument("--max", dest="max_items", type=int, default=24, help="Max queue items to print")
    parser.add_argument(
        "--probe-durations",
        dest="probe_durations",
        action="store_true",
        default=True,
        help="Probe real audio durations (recommended; default)",
    )
    parser.add_argument(
        "--no-probe-durations",
        dest="probe_durations",
        action="store_false",
        help="Do not probe audio durations; rely on filename heuristics",
    )
    args = parser.parse_args()

    inferred = _infer_paths()

    scripts_dir = args.scripts_dir or inferred.get("scripts_dir")
    music_dir = args.music_dir or inferred.get("music_dir")
    outro_dir = args.outro_dir or inferred.get("outro_dir")
    exposure_path = args.exposure_path or inferred.get("exposure_path")

    print("settings:", inferred.get("settings_path"))
    print("scripts_dir:", scripts_dir)
    print("music_dir:", music_dir)
    print("outro_dir:", outro_dir)
    print("exposure_path:", exposure_path)
    print()

    if not scripts_dir or not os.path.isdir(scripts_dir):
        print("ERROR: scripts_dir not found")
        return 2
    if not music_dir or not os.path.isdir(music_dir):
        print("ERROR: music_dir not found")
        return 2

    # Ensure repo src is importable.
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    sys.path.insert(0, repo_root)

    from src.bump_manager import BumpManager  # noqa

    random.seed(args.seed)

    bm = BumpManager()

    # Load persisted exposure state if available.
    state = _read_json(exposure_path) or {}
    try:
        # Current app writes bump component exposure under 'bump_components'.
        # Keep a fallback to the older 'bump' key for compatibility.
        bump_state = (state or {}).get("bump_components", None)
        if bump_state is None:
            bump_state = (state or {}).get("bump", {})
        if isinstance(bump_state, dict):
            bm.set_exposure_state(bump_state)
    except Exception:
        pass

    bm.load_bumps(scripts_dir)
    bm.scan_music(music_dir, probe_durations=bool(args.probe_durations))

    if outro_dir and os.path.isdir(outro_dir):
        try:
            out_paths = []
            for name in os.listdir(outro_dir):
                p = os.path.join(outro_dir, name)
                if os.path.isfile(p):
                    out_paths.append(p)
            bm.set_outro_sounds(out_paths)
        except Exception:
            pass

    bm._rebuild_bump_queue()  # intentionally calling the internal queue builder

    q = list(getattr(bm, "_bump_queue", []) or [])
    if not q:
        print("Queue is empty.")
        return 0

    limit = min(int(args.max_items), len(q))

    # Print mapping + detect duplicates.
    seen_basenames = {}
    dup_basenames = {}

    print(f"Queue items: {len(q)} (showing first {limit})")
    print("---")

    for i, item in enumerate(q[:limit]):
        script = (item or {}).get("script") if isinstance(item, dict) else None
        audio = (item or {}).get("audio") if isinstance(item, dict) else None

        sk = None
        try:
            # Prefer stable key if present.
            if isinstance(script, dict):
                sk = script.get("_script_key")
        except Exception:
            sk = None

        dur_ms = None
        try:
            if isinstance(script, dict):
                dur_ms = int(script.get("duration", 0) or 0)
        except Exception:
            dur_ms = None

        audio_base = os.path.basename(str(audio or ""))
        if audio_base:
            if audio_base in seen_basenames:
                dup_basenames.setdefault(audio_base, []).append(i)
            else:
                seen_basenames[audio_base] = i

        print(f"{i:02d}  script={sk or '<mem>'}  est_ms={dur_ms}  music={audio_base}")

    print("---")

    if not dup_basenames:
        print("No duplicate music basenames detected in the printed segment.")
    else:
        print("Duplicate music basenames in the printed segment:")
        for bn, idxs in sorted(dup_basenames.items(), key=lambda t: (-len(t[1]), t[0])):
            first = seen_basenames.get(bn)
            all_idxs = [first] + list(idxs) if first is not None else list(idxs)
            all_idxs = [x for x in all_idxs if x is not None]
            all_idxs.sort()
            print(f"- {bn}: positions {all_idxs}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
