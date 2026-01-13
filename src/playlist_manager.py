import os
import random
import re

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv'}

from bump_manager import BumpManager

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

    def reset_playback_state(self):
        self.play_queue = []
        self.episode_history = []
        self.playback_history = []
        self.playback_history_pos = -1

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

    def mark_episode_started(self, index):
        # Track episode history for avoidance rules.
        if index is None or index < 0:
            return
        if index >= len(self.current_playlist):
            return
        if not self.is_episode_item(self.current_playlist[index]):
            return

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

        # Build a full cycle order (excluding the current episode from the upcoming queue).
        if self.shuffle_mode == 'season':
            season_map = {}
            for idx in episode_idxs:
                season = self._season_key_from_path(self._episode_path_for_index(idx))
                season_map.setdefault(season, []).append(idx)

            seasons = list(season_map.keys())
            random.shuffle(seasons)

            order = []
            for season in seasons:
                eps = season_map[season][:]
                random.shuffle(eps)
                order.extend(eps)

        elif self.shuffle_mode == 'standard':
            order = episode_idxs[:]
            random.shuffle(order)

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

        self.play_queue = self._apply_avoid_recent_rule(order, current_index=current_index)

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
                return nxt if nxt < len(self.current_playlist) else -1

            # If the next item is an injection (interstitial/bump), play it next.
            nxt = self.current_index + 1
            if nxt < len(self.current_playlist) and not self.is_episode_item(self.current_playlist[nxt]):
                return nxt

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
                        bump_obj = self.bump_manager.get_random_bump()
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
        folder = "playlists"
        if not os.path.exists(folder):
            os.makedirs(folder)
        return [f for f in os.listdir(folder) if f.lower().endswith(".json")]
    
    def has_next(self):
         return self.current_index + 1 < len(self.current_playlist)

    def has_previous(self):
        return self.current_index > 0
