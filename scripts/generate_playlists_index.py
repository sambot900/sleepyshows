import json
import os
import sys


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    playlists_dir = os.path.join(root, 'playlists')

    if len(sys.argv) >= 2:
        playlists_dir = sys.argv[1]

    if not os.path.isdir(playlists_dir):
        print(f"Not a directory: {playlists_dir}")
        return 2

    files = []
    for name in os.listdir(playlists_dir):
        if name.lower().endswith('.json'):
            files.append(name)

    files.sort(key=lambda s: s.lower())
    out = {
        'playlists': files,
    }

    index_path = os.path.join(playlists_dir, 'index.json')
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {index_path} ({len(files)} playlists)")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
