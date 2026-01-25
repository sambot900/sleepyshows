import os
import sys
import platform
import random
import re
import json
import time

# Extensions used for:
# - show folder auto-detection ("does this folder contain videos?")
# - episode scanning when building auto-playlists
# Keep this reasonably broad so libraries in common containers (e.g. .m4v/.webm)
# don't silently fail auto-config.
VIDEO_EXTENSIONS = {
    '.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.flv',
    '.webm', '.mpg', '.mpeg', '.ts', '.m2ts',
}

from bump_manager import BumpManager


def get_local_playlists_dir() -> str:
    """Return the directory where playlists + exposure scores are stored.

    - Source/dev runs: keep using the project-local `playlists/` folder.
    - Frozen builds: use a writable per-user app data folder.
    """
    try:
        if getattr(sys, 'frozen', False):
            home = os.path.expanduser('~')
            if platform.system().lower().startswith('win'):
                base = os.getenv('APPDATA') or os.path.join(home, 'AppData', 'Roaming')
                root = os.path.join(base, 'SleepyShows')
            elif platform.system().lower() == 'darwin':
                root = os.path.join(home, 'Library', 'Application Support', 'SleepyShows')
            else:
                xdg = os.getenv('XDG_CONFIG_HOME')
                root = os.path.join(xdg if xdg else os.path.join(home, '.config'), 'SleepyShows')
            folder = os.path.join(root, 'playlists')
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            folder = os.path.join(base_dir, 'playlists')

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass
        return folder
    except Exception:
        return os.path.join(os.getcwd(), 'playlists')

def natural_sort_key(s):
    """
    Splits string into a list of integers and text chunks.
    's1e2' -> ['s', 1, 'e', 2]
    's1e10' -> ['s', 1, 'e', 10]
    """
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split(r'(\d+)', s)]

class PlaylistManager:
    def __init__(self):
        self.source_folders = [] # List of added source paths
        self.library_structure = {} # { "SourcePath": { "GroupName": [items] } }
        self.interstitial_folder = ""
        self.episodes = [] # Flat List of all available episodes
        self.interstitials = []
        self.current_playlist = [] # List of config objects
        self.current_index = -1
        self.bump_manager = BumpManager()
        
        # Shuffle modes:
        # - off: chronological playback
        # - standard: random episodes
        # - season: random season, then random episodes within that season
        self.shuffle_mode = 'off'

        # Hidden queue of upcoming episode indices (indices into current_playlist)
        self.play_queue = []

        # History of episode indices that have been started (for avoiding immediate repeats)
        self.episode_history = []

        # Playback history across all playable items (episodes + injections).
        # Used for "Previous" navigation so shuffle doesn't jump unpredictably.
        self.playback_history = []
        self.playback_history_pos = -1

        # Special-case sequencing overrides (e.g., multipart episodes).
        self._forced_next_episode_index = None

        # Exposure score tracking
        # - Episodes: {normalized_path: float}
        # - Session counters affect how much score is applied per play.
        self.episode_exposure_scores = {}

        # Per-playlist exposure controls (loaded from the playlist JSON).
        # Offsets are additive: effective exposure score is base + offsets.
        # Factors multiply per-play deltas and also influence queue selection via projected delta.
        self.episode_exposure_offsets = {}      # {norm_path: float}
        self.season_exposure_offsets = {}       # {season_bucket_key: float}
        self.episode_exposure_factors = {}      # {norm_path: float}
        self.season_exposure_factors = {}       # {season_bucket_key: float}

        # Raw settings snapshot used for saving back to playlist JSON.
        self._playlist_frequency_settings = {}

        # Session exposure counters (separate for episodes vs bumps).
        # First 3 plays => +100, next 3 => +50, next 3 => +25, ... per kind.
        self._session_episode_plays = 0
        self._session_bump_plays = 0

        # Exposure scaling rule depends on whether the sleep timer is enabled.
        # - Sleep timer ON: episode deltas diminish every 3 episode plays.
        # - Sleep timer OFF: episodes always get +100 base points per play.
        self._sleep_timer_active_for_exposure = False

        # Persistence (global across playlists).
        self.playlists_dir = get_local_playlists_dir()
        self._exposure_scores_path = os.path.join(self.playlists_dir, 'exposure_scores.json')
        self._exposure_last_save_monotonic = 0.0
        self._exposure_dirty = False
        self._load_exposure_scores()



    def reset_playback_state(self):
        self.play_queue = []
        self.episode_history = []
        self.playback_history = []
        self.playback_history_pos = -1
        self._forced_next_episode_index = None

        # New viewing session: reset exposure scaling counters.
        self._session_episode_plays = 0
        self._session_bump_plays = 0

    def set_sleep_timer_active_for_exposure(self, active: bool):
        """Update whether episode exposure deltas should diminish this session."""
        try:
            new_val = bool(active)
        except Exception:
            new_val = False

        prev = bool(getattr(self, '_sleep_timer_active_for_exposure', False))
        self._sleep_timer_active_for_exposure = new_val

        # If the behavior toggles, restart the episode-delta tiering so
        # "first 3 plays" semantics apply only while sleep-timer mode is active.
        if new_val != prev:
            self._session_episode_plays = 0

    def _norm_path_key(self, path: str) -> str:
        try:
            p = str(path or '')
        except Exception:
            p = ''
        if not p:
            return ''
        try:
            return os.path.normcase(os.path.normpath(p))
        except Exception:
            return p

    def _ensure_exposure_store_dir(self):
        try:
            folder = os.path.dirname(str(self._exposure_scores_path or ''))
        except Exception:
            folder = ''
        if folder:
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception:
                pass

    def _load_exposure_scores(self):
        """Load persisted exposure scores (best-effort)."""
        path = str(getattr(self, '_exposure_scores_path', '') or '')
        if not path:
            return
        try:
            if not os.path.exists(path):
                return
        except Exception:
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return

        eps = data.get('episodes', None)
        if isinstance(eps, dict):
            cleaned = {}
            for k, v in eps.items():
                try:
                    kk = self._norm_path_key(str(k))
                    vv = float(v)
                except Exception:
                    continue
                if kk:
                    cleaned[kk] = vv
            self.episode_exposure_scores = cleaned

        # Frequency settings (offsets/factors) are now persisted with playlist JSON only.

        bump_state = data.get('bump_components', None)
        if isinstance(bump_state, dict):
            try:
                self.bump_manager.set_exposure_state(bump_state)
            except Exception:
                # Backward-compatible fallback.
                try:
                    self.bump_manager.script_exposure_scores = dict(bump_state.get('scripts') or {})
                    self.bump_manager.music_exposure_scores = dict(bump_state.get('music') or {})
                    self.bump_manager.outro_exposure_scores = dict(bump_state.get('outro') or {})
                except Exception:
                    pass

    def _save_exposure_scores(self, *, force: bool = False):
        """Persist exposure scores to disk (best-effort, throttled)."""
        if not getattr(self, '_exposure_dirty', False) and not force:
            return

        now = time.monotonic()
        try:
            last = float(getattr(self, '_exposure_last_save_monotonic', 0.0) or 0.0)
        except Exception:
            last = 0.0

        # Avoid spamming disk writes during rapid bump cards.
        if not force and (now - last) < 1.5:
            return

        path = str(getattr(self, '_exposure_scores_path', '') or '')
        if not path:
            return
        self._ensure_exposure_store_dir()

        try:
            bump_state = self.bump_manager.get_exposure_state()
        except Exception:
            bump_state = {
                'scripts': dict(getattr(self.bump_manager, 'script_exposure_scores', {}) or {}),
                'music': dict(getattr(self.bump_manager, 'music_exposure_scores', {}) or {}),
                'outro': dict(getattr(self.bump_manager, 'outro_exposure_scores', {}) or {}),
            }

        payload = {
            'episodes': dict(self.episode_exposure_scores or {}),
            'bump_components': dict(bump_state or {}),
        }

        tmp = path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, path)
            self._exposure_last_save_monotonic = float(now)
            self._exposure_dirty = False
        except Exception:
            # Best-effort cleanup.
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _exposure_delta_for_next_play(self, kind: str) -> float:
        """Return the score increment for the next play of a given kind.

        First 3 plays of that kind => +100.
        Plays 4-6 => +50.
        Plays 7-9 => +25, etc.
        """
        kind_l = str(kind or '').strip().lower()
        if kind_l == 'bump':
            try:
                n = int(self._session_bump_plays)
            except Exception:
                n = 0
        else:
            # Sleep timer OFF: keep episode deltas constant during the session.
            try:
                if not bool(getattr(self, '_sleep_timer_active_for_exposure', False)):
                    return 100.0
            except Exception:
                return 100.0
            try:
                n = int(self._session_episode_plays)
            except Exception:
                n = 0

        tier = max(0, int(n) // 3)
        try:
            delta = 100.0 / (2.0 ** float(tier))
        except Exception:
            delta = 100.0
        if delta < 1.0:
            delta = 1.0
        return float(delta)

    def apply_episode_skip_penalty(self, index, points: float = 1.0):
        """Apply a small negative adjustment for skipped/cut-off episodes.

        The deduction is applied to the *base* points, before user factors.
        That means we subtract (points * factor) from the stored exposure score.
        """
        try:
            idx = int(index)
        except Exception:
            return 0.0

        if idx < 0 or idx >= len(self.current_playlist):
            return 0.0
        if not self.is_episode_item(self.current_playlist[idx]):
            return 0.0

        try:
            p = self._episode_path_for_index(idx)
        except Exception:
            p = ''
        key = self._norm_path_key(p)
        if not key:
            return 0.0

        try:
            pts = float(points)
        except Exception:
            pts = 1.0
        pts = abs(pts)
        if pts <= 0:
            return 0.0

        try:
            factor = float(self._effective_episode_factor(p))
        except Exception:
            factor = 1.0

        delta = -float(pts) * float(factor)
        try:
            self.episode_exposure_scores[key] = float(self.episode_exposure_scores.get(key, 0.0) or 0.0) + float(delta)
        except Exception:
            self.episode_exposure_scores[key] = float(delta)

        self._exposure_dirty = True
        self._save_exposure_scores()
        return float(delta)

    def _season_bucket_key_from_path(self, path: str) -> str:
        """Return a stable-ish season bucket key for season-level overrides."""
        try:
            p = str(path or '')
        except Exception:
            p = ''
        if not p:
            return ''

        parts = [x for x in re.split(r'[\\/]+', p) if x]
        season_num = self._season_key_from_path(p)

        show_name = ''
        try:
            # Heuristic: use the folder right before the season folder if present.
            season_re = re.compile(r'(?:season|s)[ _-]?\d{1,2}', flags=re.IGNORECASE)
            season_idx = None
            for i, part in enumerate(parts):
                if season_re.search(str(part)):
                    season_idx = i
                    break
            if season_idx is not None and season_idx > 0:
                show_name = str(parts[season_idx - 1])
            elif len(parts) >= 2:
                show_name = str(parts[-2])
        except Exception:
            show_name = ''

        show_name = show_name.strip()
        if show_name:
            return f"{show_name}|season:{int(season_num)}"
        return f"season:{int(season_num)}"

    def _season_bucket_keys_from_path(self, path: str) -> list[str]:
        """Return candidate season keys (for backward/forward compatibility)."""
        keys = []
        try:
            k = self._season_bucket_key_from_path(path)
            if k:
                keys.append(str(k))
        except Exception:
            pass
        try:
            n = int(self._season_key_from_path(path) or 0)
        except Exception:
            n = 0
        if n:
            k2 = f"season:{n}"
            if k2 not in keys:
                keys.append(k2)
        return keys

    def _effective_episode_offset(self, path: str) -> float:
        key = self._norm_path_key(path)
        off = 0.0
        for season_key in self._season_bucket_keys_from_path(path):
            try:
                off = float(off) + float(self.season_exposure_offsets.get(season_key, 0.0) or 0.0)
            except Exception:
                pass
        try:
            off = float(off) + float(self.episode_exposure_offsets.get(key, 0.0) or 0.0)
        except Exception:
            pass
        if off < 0.0:
            off = 0.0
        return float(off)

    def _effective_episode_factor(self, path: str) -> float:
        key = self._norm_path_key(path)
        factor = None
        try:
            if key in self.episode_exposure_factors:
                factor = float(self.episode_exposure_factors.get(key, 1.0) or 1.0)
        except Exception:
            factor = None
        if factor is None:
            for season_key in self._season_bucket_keys_from_path(path):
                try:
                    if season_key in self.season_exposure_factors:
                        factor = float(self.season_exposure_factors.get(season_key, 1.0) or 1.0)
                        break
                except Exception:
                    factor = None
        if factor is None:
            factor = 1.0
        if factor <= 0.0:
            factor = 1.0
        return float(factor)

    def set_episode_exposure_offset(self, path: str, value: float):
        key = self._norm_path_key(path)
        if not key:
            return
        try:
            v = float(value)
        except Exception:
            v = 0.0
        v = float(max(0.0, v))
        if v > 0.0:
            self.episode_exposure_offsets[key] = v
        else:
            self.episode_exposure_offsets.pop(key, None)
        self._exposure_dirty = True
        self._save_exposure_scores()

    def set_episode_exposure_factor(self, path: str, value: float):
        key = self._norm_path_key(path)
        if not key:
            return
        try:
            v = float(value)
        except Exception:
            v = 1.0
        if v <= 0.0:
            v = 1.0
        self.episode_exposure_factors[key] = float(v)
        self._exposure_dirty = True
        self._save_exposure_scores()

    def set_season_exposure_offset(self, season_key: str, value: float):
        k = str(season_key or '').strip()
        if not k:
            return
        try:
            v = float(value)
        except Exception:
            v = 0.0
        v = float(max(0.0, v))
        if v > 0.0:
            self.season_exposure_offsets[k] = v
        else:
            self.season_exposure_offsets.pop(k, None)
        self._exposure_dirty = True
        self._save_exposure_scores()

    def set_season_exposure_factor(self, season_key: str, value: float):
        k = str(season_key or '').strip()
        if not k:
            return
        try:
            v = float(value)
        except Exception:
            v = 1.0
        if v <= 0.0:
            v = 1.0
        self.season_exposure_factors[k] = float(v)
        self._exposure_dirty = True
        self._save_exposure_scores()

    def apply_frequency_settings(self, *,
                                episode_offsets: dict | None = None,
                                season_offsets: dict | None = None,
                                episode_factors: dict | None = None,
                                season_factors: dict | None = None):
        """Replace per-playlist frequency settings in bulk (offsets + factors)."""
        if isinstance(episode_offsets, dict):
            cleaned = {}
            for k, v in episode_offsets.items():
                try:
                    kk = self._norm_path_key(str(k))
                    vv = float(v)
                except Exception:
                    continue
                if kk and vv > 0.0:
                    cleaned[kk] = float(vv)
            self.episode_exposure_offsets = cleaned

        if isinstance(season_offsets, dict):
            cleaned = {}
            for k, v in season_offsets.items():
                try:
                    kk = str(k).strip()
                    vv = float(v)
                except Exception:
                    continue
                if kk and vv > 0.0:
                    cleaned[kk] = float(vv)
            self.season_exposure_offsets = cleaned

        if isinstance(episode_factors, dict):
            cleaned = {}
            for k, v in episode_factors.items():
                try:
                    kk = self._norm_path_key(str(k))
                    vv = float(v)
                except Exception:
                    continue
                if kk and vv > 0.0 and abs(vv - 1.0) > 1e-9:
                    cleaned[kk] = float(vv)
            self.episode_exposure_factors = cleaned

        if isinstance(season_factors, dict):
            cleaned = {}
            for k, v in season_factors.items():
                try:
                    kk = str(k).strip()
                    vv = float(v)
                except Exception:
                    continue
                if kk and vv > 0.0 and abs(vv - 1.0) > 1e-9:
                    cleaned[kk] = float(vv)
            self.season_exposure_factors = cleaned

        self._playlist_frequency_settings = self.get_frequency_settings_for_save()

    def set_frequency_settings_from_playlist_data(self, data: dict | None):
        """Load per-playlist frequency settings from playlist JSON data."""
        if not isinstance(data, dict):
            data = {}

        # New preferred field.
        fs = data.get('frequency_settings', None)
        if not isinstance(fs, dict):
            fs = {}

        # Backward-compat: accept older spellings.
        if not fs:
            legacy = data.get('exposure_overrides', None)
            if isinstance(legacy, dict):
                fs = dict(legacy)

        episode_offsets = fs.get('episode_offsets', fs.get('episode_min_exposure', None))
        season_offsets = fs.get('season_offsets', fs.get('season_min_exposure', None))
        episode_factors = fs.get('episode_factors', fs.get('episode_exposure_factors', None))
        season_factors = fs.get('season_factors', fs.get('season_exposure_factors', None))

        self.apply_frequency_settings(
            episode_offsets=episode_offsets if isinstance(episode_offsets, dict) else {},
            season_offsets=season_offsets if isinstance(season_offsets, dict) else {},
            episode_factors=episode_factors if isinstance(episode_factors, dict) else {},
            season_factors=season_factors if isinstance(season_factors, dict) else {},
        )

    def get_frequency_settings_for_save(self) -> dict:
        return {
            'episode_offsets': dict(self.episode_exposure_offsets or {}),
            'season_offsets': dict(self.season_exposure_offsets or {}),
            'episode_factors': dict(self.episode_exposure_factors or {}),
            'season_factors': dict(self.season_exposure_factors or {}),
        }

    def clear_frequency_settings(self):
        self.episode_exposure_offsets = {}
        self.season_exposure_offsets = {}
        self.episode_exposure_factors = {}
        self.season_exposure_factors = {}
        self._playlist_frequency_settings = {}

    def clear_episode_exposure_scores_for_paths(self, paths):
        removed = 0
        for p in list(paths or []):
            key = self._norm_path_key(p)
            if not key:
                continue
            if key in self.episode_exposure_scores:
                try:
                    del self.episode_exposure_scores[key]
                    removed += 1
                except Exception:
                    pass
        if removed:
            self._exposure_dirty = True
            self._save_exposure_scores(force=True)
        return int(removed)

    def clear_episode_exposure_scores_all(self):
        try:
            self.episode_exposure_scores = {}
        except Exception:
            pass
        self._exposure_dirty = True
        self._save_exposure_scores(force=True)

    def note_bump_played(self, bump_item: dict):
        """Apply exposure to bump components based on session visibility."""
        delta = self._exposure_delta_for_next_play('bump')
        self._session_bump_plays += 1
        try:
            self.bump_manager.apply_bump_exposure(bump_item, delta=delta)
        except Exception:
            pass

        self._exposure_dirty = True
        self._save_exposure_scores()

    def is_episode_item(self, item):
        if isinstance(item, dict):
            # Only regular video episodes count as "episodes" for shuffle.
            return item.get('type', 'video') == 'video'
        # Legacy string paths are treated as episodes.
        return True

    def _episode_indices(self):
        return [i for i, item in enumerate(self.current_playlist) if self.is_episode_item(item)]

    def _episode_path_for_index(self, idx):
        if idx < 0 or idx >= len(self.current_playlist):
            return ''
        item = self.current_playlist[idx]
        if isinstance(item, dict):
            return item.get('path', '')
        return str(item)

    def _season_key_from_path(self, path):
        if not path:
            return 0

        # Try to extract season number from the path (folder name or filename).
        # Supports: "Season 1", "season_02", "S3", "s04", etc.
        parts = re.split(r'[\\/]+', path)
        for part in parts:
            m = re.search(r'(?:season|s)[ _-]?(\d{1,2})', part, flags=re.IGNORECASE)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    return 0
        return 0

    def _chronological_episode_indices(self):
        # Best-effort: sort by full path using natural sort.
        eps = self._episode_indices()
        return sorted(eps, key=lambda i: natural_sort_key(self._episode_path_for_index(i)))

    def _is_koth_playlist(self):
        # Heuristic: if any episode path includes "King of the Hill" (common folder name),
        # treat this as a KOTH playlist.
        for idx in self._episode_indices()[:30]:
            p = (self._episode_path_for_index(idx) or '').lower()
            if 'king of the hill' in p or 'koth' in p:
                return True
        return False

    def _is_part1_episode(self, path):
        if not path:
            return False
        base = os.path.splitext(os.path.basename(str(path)))[0]
        return re.search(r'\(1\)\s*$', base.strip()) is not None

    def _next_chronological_episode_index_after(self, episode_index):
        ordered = self._chronological_episode_indices()
        if not ordered or episode_index not in ordered:
            return -1
        pos = ordered.index(episode_index)
        if pos + 1 >= len(ordered):
            return -1
        return ordered[pos + 1]

    def mark_episode_started(self, index, *, sleep_timer_on: bool | None = None):
        # Track episode history for avoidance rules.
        if index is None or index < 0:
            return
        if index >= len(self.current_playlist):
            return
        if not self.is_episode_item(self.current_playlist[index]):
            return

        # Keep exposure behavior in sync with the UI sleep timer.
        if sleep_timer_on is not None:
            try:
                self.set_sleep_timer_active_for_exposure(bool(sleep_timer_on))
            except Exception:
                pass

        # Exposure scoring: apply per-session visibility weight to the episode.
        try:
            p = self._episode_path_for_index(index)
        except Exception:
            p = ''
        key = self._norm_path_key(p)
        if key:
            delta = self._exposure_delta_for_next_play('episode')

            # Only advance diminishing tiers while sleep-timer exposure mode is active.
            try:
                if bool(getattr(self, '_sleep_timer_active_for_exposure', False)):
                    self._session_episode_plays += 1
            except Exception:
                pass

            # Apply user factor per episode/season.
            try:
                delta = float(delta) * float(self._effective_episode_factor(p))
            except Exception:
                pass

            try:
                self.episode_exposure_scores[key] = float(self.episode_exposure_scores.get(key, 0.0) or 0.0) + float(delta)
            except Exception:
                self.episode_exposure_scores[key] = float(delta)

            self._exposure_dirty = True
            self._save_exposure_scores()

        self.episode_history.append(index)
        # Keep history bounded.
        if len(self.episode_history) > 50:
            self.episode_history = self.episode_history[-50:]

    def record_playback_index(self, index):
        if index is None or index < 0:
            return
        if index >= len(self.current_playlist):
            return

        # If user had navigated back, discard the "future".
        if 0 <= self.playback_history_pos < len(self.playback_history) - 1:
            self.playback_history = self.playback_history[:self.playback_history_pos + 1]

        if not self.playback_history or self.playback_history[-1] != index:
            self.playback_history.append(index)
        self.playback_history_pos = len(self.playback_history) - 1

        # Cap history size
        if len(self.playback_history) > 200:
            extra = len(self.playback_history) - 200
            self.playback_history = self.playback_history[extra:]
            self.playback_history_pos = max(-1, self.playback_history_pos - extra)

    def step_back_in_history(self):
        if not self.playback_history:
            return -1
        if self.playback_history_pos <= 0:
            self.playback_history_pos = 0
            return self.playback_history[0]
        self.playback_history_pos -= 1
        return self.playback_history[self.playback_history_pos]

    def step_forward_in_history(self):
        if not self.playback_history:
            return -1
        if self.playback_history_pos >= len(self.playback_history) - 1:
            self.playback_history_pos = len(self.playback_history) - 1
            return -1
        self.playback_history_pos += 1
        return self.playback_history[self.playback_history_pos]

    def _recent_episode_indices(self, current_index=None, count=2):
        recent = []

        # Include the current episode index first.
        if current_index is None:
            current_index = self.current_index
        if current_index is not None and current_index >= 0 and current_index < len(self.current_playlist):
            if self.is_episode_item(self.current_playlist[current_index]):
                recent.append(current_index)

        # Then include prior watched episodes.
        for idx in reversed(self.episode_history):
            if idx not in recent:
                recent.append(idx)
            if len(recent) >= count:
                break

        return recent[:count]

    def _apply_avoid_recent_rule(self, queue, current_index=None):
        # If the next-up items match the last two watched episodes (including current),
        # push them to the end to avoid immediate repeats.
        recent = self._recent_episode_indices(current_index=current_index, count=2)
        for r in recent:
            if r in queue[:2]:
                queue.remove(r)
                queue.append(r)
        return queue

    def rebuild_queue(self, current_index=None):
        if current_index is None:
            current_index = self.current_index

        episode_idxs = self._episode_indices()
        if not episode_idxs:
            self.play_queue = []
            return

        def _ep_score(i: int) -> float:
            try:
                path = self._episode_path_for_index(i)
                key = self._norm_path_key(path)
            except Exception:
                path = ''
                key = ''
            try:
                base = float(self.episode_exposure_scores.get(key, 0.0) or 0.0)
            except Exception:
                base = 0.0

            # Additive offsets (episode + season).
            try:
                off = float(self._effective_episode_offset(path))
            except Exception:
                off = 0.0

            # Factor should influence queue selection by considering the imminent score change
            # that would be applied if this item is played next.
            try:
                factor = float(self._effective_episode_factor(path))
            except Exception:
                factor = 1.0
            try:
                projected = float(self._exposure_delta_for_next_play('episode')) * float(factor)
            except Exception:
                projected = 0.0

            return float(base + off + projected)

        def _order_by_exposure(indices):
            # Stable-ish: group by score, shuffle within same-score buckets.
            buckets = {}
            for i in list(indices or []):
                try:
                    s = round(float(_ep_score(int(i))), 6)
                except Exception:
                    s = 0.0
                buckets.setdefault(s, []).append(int(i))
            out = []
            for s in sorted(buckets.keys()):
                b = buckets[s]
                random.shuffle(b)
                out.extend(b)
            return out

        # Build a full cycle order (excluding the current episode from the upcoming queue).
        if self.shuffle_mode == 'season':
            season_map = {}
            for idx in episode_idxs:
                season = self._season_key_from_path(self._episode_path_for_index(idx))
                season_map.setdefault(season, []).append(idx)

            # Order seasons by their least-exposed episode (ties randomized).
            season_keys = list(season_map.keys())
            season_scores = []
            for s in season_keys:
                eps = season_map.get(s) or []
                try:
                    sc = min([_ep_score(i) for i in eps]) if eps else 0.0
                except Exception:
                    sc = 0.0
                season_scores.append((round(float(sc), 6), s))

            # Sort by exposure, randomize ties.
            season_scores.sort(key=lambda t: t[0])
            ordered_seasons = []
            j = 0
            while j < len(season_scores):
                k = j
                while k < len(season_scores) and season_scores[k][0] == season_scores[j][0]:
                    k += 1
                chunk = [s for (_sc, s) in season_scores[j:k]]
                random.shuffle(chunk)
                ordered_seasons.extend(chunk)
                j = k

            order = []
            for season in ordered_seasons:
                eps = season_map[season][:]
                order.extend(_order_by_exposure(eps))

        elif self.shuffle_mode == 'standard':
            order = _order_by_exposure(episode_idxs)

        else:
            # off
            ordered = self._chronological_episode_indices()
            if current_index is not None and current_index in ordered:
                pos = ordered.index(current_index)
                # Upcoming begins after current, wraps around.
                order = ordered[pos+1:] + ordered[:pos]
            else:
                order = ordered

        # Remove current episode from upcoming order if present.
        if current_index is not None and current_index in order:
            order = [i for i in order if i != current_index]

        # Exposure-based queueing makes recent-avoidance unnecessary; keep the queue deterministic.
        self.play_queue = list(order)

    def set_shuffle_mode(self, mode, current_index=None, rebuild=True):
        # Backward-compat: bool True means standard shuffle, False means off.
        if isinstance(mode, bool):
            mode = 'standard' if mode else 'off'

        if mode not in ('off', 'standard', 'season'):
            mode = 'off'

        self.shuffle_mode = mode
        if rebuild:
            self.rebuild_queue(current_index=current_index)

    def get_next_index(self):
        """Calculates the next index based on the hidden queue and current mode."""
        if not self.current_playlist:
            return -1

        # Always honor sequential playback for non-episode items.
        if 0 <= self.current_index < len(self.current_playlist):
            cur_item = self.current_playlist[self.current_index]
            if not self.is_episode_item(cur_item):
                nxt = self.current_index + 1
                if nxt < len(self.current_playlist) and not self.is_episode_item(self.current_playlist[nxt]):
                    return nxt

                # If we have a forced next episode pending (e.g., multipart), honor it
                # once we're done with any injection(s).
                if self._forced_next_episode_index is not None:
                    forced = int(self._forced_next_episode_index)
                    self._forced_next_episode_index = None
                    if forced in self.play_queue:
                        self.play_queue = [i for i in self.play_queue if i != forced]
                    if 0 <= forced < len(self.current_playlist):
                        return forced

                return nxt if nxt < len(self.current_playlist) else -1

            # If the next item is an injection (interstitial/bump), play it next.
            nxt = self.current_index + 1
            if nxt < len(self.current_playlist) and not self.is_episode_item(self.current_playlist[nxt]):
                # If this was a multipart part-1 episode, remember the forced next episode
                # so that after the injection we still continue to part 2.
                if self.shuffle_mode in ('standard', 'season'):
                    cur_path = self._episode_path_for_index(self.current_index)
                    if self._is_koth_playlist() and self._is_part1_episode(cur_path):
                        forced = self._next_chronological_episode_index_after(self.current_index)
                        if forced != -1:
                            self._forced_next_episode_index = forced
                return nxt

            # Multipart KOTH rule: in shuffle mode, if episode ends with "(1)", force
            # the next chronological episode once, then resume shuffling.
            if self.shuffle_mode in ('standard', 'season') and self._forced_next_episode_index is None:
                cur_path = self._episode_path_for_index(self.current_index)
                if self._is_koth_playlist() and self._is_part1_episode(cur_path):
                    forced = self._next_chronological_episode_index_after(self.current_index)
                    if forced != -1 and forced != self.current_index:
                        if forced in self.play_queue:
                            self.play_queue = [i for i in self.play_queue if i != forced]
                        return forced

        # If we have a pending forced next episode, honor it now.
        if self._forced_next_episode_index is not None:
            forced = int(self._forced_next_episode_index)
            self._forced_next_episode_index = None
            if forced in self.play_queue:
                self.play_queue = [i for i in self.play_queue if i != forced]
            if 0 <= forced < len(self.current_playlist):
                return forced

        # We are moving to the next episode.
        if not self.play_queue:
            self.rebuild_queue(current_index=self.current_index)
        if not self.play_queue:
            return -1

        return self.play_queue.pop(0)

    def get_prev_index(self):
        # Allow shuffle history? For now basic.
        if self.current_index - 1 >= 0:
            return self.current_index - 1
        return 0 # Restart 0

    def add_source(self, folder_path):
        """Adds a source folder and scans it, appending to library."""
        if not folder_path or not os.path.exists(folder_path):
            return self.library_structure
            
        if folder_path in self.source_folders:
             return self.library_structure # Already added
             
        self.source_folders.append(folder_path)
        
        # Scan this specific folder
        structure = {}

        # If the selected folder is a generic leaf like "Episodes", include its parent
        # so multiple shows don't end up with the same source label.
        base = os.path.basename(folder_path)
        parent = os.path.basename(os.path.dirname(folder_path))
        if base.lower() in ('episodes', 'episodesl') and parent:
            source_name = f"{parent}/{base}"
        else:
            source_name = base
        
        for root, dirs, files in os.walk(folder_path):
            # Sort directories and files using natural sort
            dirs.sort(key=natural_sort_key)
            files.sort(key=natural_sort_key)
            
            relative_path = os.path.relpath(root, folder_path)
            # Group Name: "SeriesName/Season 1"
            if relative_path == ".":
                group_name = "Root"
            else:
                group_name = relative_path
            
            video_files = [f for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS]
            
            if video_files:
                structure[group_name] = []
                for f in video_files:
                    full_path = os.path.join(root, f)
                    item = {
                        "name": f,
                        "path": full_path,
                        "group": group_name,
                        "source": source_name
                    }
                    structure[group_name].append(item)
                    self.episodes.append(item) # Add to flat list

        self.library_structure[folder_path] = structure
        return self.library_structure

    def clear_library(self):
        self.source_folders = []
        self.library_structure = {}
        self.episodes = []

    def scan_interstitials(self, folder_path):
        self.interstitial_folder = folder_path
        self.interstitials = []
        if not folder_path or not os.path.exists(folder_path):
            return 
        
        for root, _, files in os.walk(folder_path):
            for f in files:
                if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                    self.interstitials.append(os.path.join(root, f))
                    
    def scan_bumps(self, scripts_folder, music_folder):
        self.bump_manager.load_bumps(scripts_folder)
        self.bump_manager.scan_music(music_folder)

        # If bump scanning seeded any initial exposure values, persist them now.
        try:
            if (
                bool(getattr(self.bump_manager, '_music_exposure_seeded_last_changed', False))
                or bool(getattr(self.bump_manager, '_script_exposure_seeded_last_changed', False))
            ):
                self._exposure_dirty = True
                self._save_exposure_scores(force=True)
        except Exception:
            pass

    def generate_playlist(self, selected_episodes=None, shuffle=False, inject_interstitials=False, inject_bumps=False):
        """
        Generates a playback queue.
        selected_episodes: list of episode dicts or paths. If None, use all scanned.
        """
        if selected_episodes is None:
            # Use all scanned
            pool = [e['path'] for e in self.episodes]
        else:
            pool = selected_episodes
            
        # Note: We used to shuffle here. Now we don't, because we want runtime shuffle.
        # if shuffle:
        #    random.shuffle(pool)

        if shuffle:
            # We don't shuffle the list physically anymore.
            pass
        
        final_list = []
        
        # Bumps inject between episodes.
        # Interstitials inject between episodes.
        # "bumps dont play between interstitials and episodes... only between episodes."
        # This implies we shouldn't have Ep -> Bump -> Int.
        # But if we have both, we need to order them.
        # Let's interpret "only between episodes" as "Bump belongs to an episode gap primarily".
        # If we have both enabled: Ep1 -> Bump -> Ep2 -> Int -> Ep3 ... ?
        # Or Ep1 -> Bump -> Int -> Ep2 ... which VIOLATES the rule.
        # "You only use one bump script between episodes."
        # Maybe:
        # If Interstitials AND Bumps:
        # Ep1 -> Bump -> Ep2 -> Int -> Ep3 -> Bump -> Ep4 ... (Alternate?)
        # Or just append logic blindly but we need to respect the rule.
        # "bumps dont play between interstitials and episodes" might mean don't do [Int][Bump] or [Bump][Int].
        # So we can't play both in the same gap.
        # Decision: Randomly choose EITHER a Bump OR an Interstitial for the gap?
        # Or prioritize one?
        # A common Adult Swim flow: Show End -> Bump -> Commercials (Interstitials) -> Bump -> Show Start. This violates the "don't play between int and ep" potentially if interpreted strictly.
        # User said: "There should be an option to have two different types of media: regular episodes of a playlist and also additional media to inject in a shuffle-fashion between episodes."
        # User defined Bumps later.
        
        # Simpler interpretation:
        # If Bumps are ON and Interstitials are ON:
        # For each gap, start with Episode.
        # Then decide what to put after.
        # Maybe: Ep -> (Bump OR Interstitial) -> Ep...
        # Let's randomize the injection type if both are enabled.
        
        # Wait, usually bumps are short. Interstitials (commercials) are longer.
        # "You only use one bump script between episodes"
        # Let's do:
        # For each episode:
        #   Add Episode.
        #   If not last:
        #       If Bumps: Add Bump?
        #       If Interstitials: Add Interstitial?
        #       If Both: Add Bump OR Interstitial? Or Ep -> Bump -> Int -> Ep is technically "Bump is between Ep and Int"?
        #       "bumps dont play between interstitials and episodes" -> Avoid [Int, Bump] or [Bump, Int].
        #       So in a gap, ONLY ONE of them can exist.
        #       Decision: 50/50 chance if both enabled? Or let's just cycle.
        
        for i, ep_path in enumerate(pool):
            # Add Episode
            final_list.append({'type': 'video', 'path': ep_path})
            
            if i < len(pool) - 1: # Gap exists
                # Determine injection
                candidates = []
                if inject_interstitials and self.interstitials:
                    candidates.append('int')
                if inject_bumps and self.bump_manager.music_files and self.bump_manager.bump_scripts:
                    candidates.append('bump')
                    
                if candidates:
                    choice = random.choice(candidates)
                    if choice == 'int':
                        inte = random.choice(self.interstitials)
                        final_list.append({'type': 'interstitial', 'path': inte})
                    elif choice == 'bump':
                        bump_obj = self.bump_manager.get_next_bump()
                        if bump_obj:
                            final_list.append(bump_obj)
                            
        self.current_playlist = final_list
        self.current_index = -1

        # Regenerate queue for the new playlist.
        self.rebuild_queue(current_index=self.current_index)
        return final_list

    def get_next(self):
        if self.current_index + 1 < len(self.current_playlist):
            self.current_index += 1
            return self.current_playlist[self.current_index]
        return None

    def get_previous(self):
        if self.current_index - 1 >= 0:
            self.current_index -= 1
            return self.current_playlist[self.current_index]
        return None

    def list_saved_playlists(self):
        """Returns list of .json files in playlists/ directory"""
        folder = str(getattr(self, 'playlists_dir', '') or '')
        if not folder:
            folder = get_local_playlists_dir()
            self.playlists_dir = folder
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass
        try:
            out = []
            for f in os.listdir(folder):
                name = str(f)
                low = name.lower()
                if not low.endswith('.json'):
                    continue
                # Internal state file; not a user playlist.
                if low == 'exposure_scores.json':
                    continue
                out.append(name)
            return out
        except Exception:
            return []
    
    def has_next(self):
         return self.current_index + 1 < len(self.current_playlist)

    def has_previous(self):
        return self.current_index > 0
