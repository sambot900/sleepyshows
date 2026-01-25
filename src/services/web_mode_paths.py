import os
import re


_SLEEPY_DATA_MARKER = 'Sleepy Shows Data'


def web_data_root_for_files_root(files_root: str) -> str:
    """Return a directory that should behave like '.../Sleepy Shows Data' for a given files_root.

    Accepts either:
    - a direct path to the 'Sleepy Shows Data' folder
    - a parent folder that contains a 'Sleepy Shows Data' child
    - otherwise returns the given root as-is (best-effort)
    """
    try:
        base = str(files_root or '').strip().strip('"').strip("'")
    except Exception:
        base = ''

    if not base:
        return ''

    try:
        tail = os.path.basename(os.path.normpath(base))
        if tail.lower() == _SLEEPY_DATA_MARKER.lower():
            return base
    except Exception:
        pass

    try:
        candidate = os.path.join(base, _SLEEPY_DATA_MARKER)
        if os.path.isdir(candidate):
            return candidate
    except Exception:
        pass

    return base


def path_to_web_files_path(path: str, web_files_root: str) -> str:
    """Best-effort conversion from a playlist path to an on-filesystem path under web_files_root.

    Rules:
    - Relative paths are treated as relative to the data root.
    - If the path contains 'Sleepy Shows Data', strip everything up to that marker
      and re-root under web_files_root.
    - URLs are not supported; if the path looks like a URL, it is treated as an opaque
      string and will fall back to basename.
    """
    files_root = str(web_files_root or '').strip()
    if not files_root:
        return str(path or '')

    data_root = web_data_root_for_files_root(files_root)
    if not data_root:
        return str(path or '')

    p = str(path or '')
    rel = ''

    try:
        if p and not os.path.isabs(p) and not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', p):
            rel = p.lstrip('/\\')
    except Exception:
        pass

    try:
        if _SLEEPY_DATA_MARKER in p:
            rel = p.split(_SLEEPY_DATA_MARKER, 1)[1].lstrip('/\\')
    except Exception:
        pass

    if not rel:
        try:
            rel = os.path.basename(p)
        except Exception:
            rel = p

    rel = rel.replace('\\', os.sep).replace('/', os.sep)
    try:
        return os.path.normpath(os.path.join(data_root, rel))
    except Exception:
        return os.path.join(data_root, rel)


def resolve_video_play_target(path: str, playback_mode: str, web_files_root: str) -> str:
    """Return the string mpv should play for an episode/interstitial."""
    p = str(path or '')
    try:
        mode = str(playback_mode or 'portable').strip().lower()
    except Exception:
        mode = 'portable'

    if mode != 'web':
        return p

    files_root = str(web_files_root or '').strip()
    if files_root:
        return path_to_web_files_path(p, files_root)

    return p
