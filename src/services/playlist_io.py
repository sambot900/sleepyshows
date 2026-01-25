import json
import os
import re
from dataclasses import dataclass


_URL_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*://')


def is_url(value: str) -> bool:
    try:
        return bool(_URL_RE.match(str(value or '')))
    except Exception:
        return False


def reject_url_source(value: str) -> None:
    if is_url(value):
        raise RuntimeError('Loading playlists from URLs is not supported. Use local playlist files.')


@dataclass(frozen=True)
class PlaylistLoadResult:
    source_path: str
    data: dict


def load_playlist_json(source_path: str) -> PlaylistLoadResult:
    """Load a playlist JSON file from disk.

    This project intentionally does NOT load playlists from URLs.
    """
    src = str(source_path or '')
    if not src:
        raise RuntimeError('No playlist file provided.')

    reject_url_source(src)

    if not os.path.exists(src):
        raise FileNotFoundError(src)

    with open(src, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise RuntimeError('Invalid playlist file format (expected JSON object).')

    return PlaylistLoadResult(source_path=src, data=data)
