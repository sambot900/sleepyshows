import os
import random
import re

class BumpManager:
    def __init__(self):
        # List of bump script dicts.
        # Each script is shaped like: {'cards': [...], 'duration': int, 'music': 'any'|'<filename>'}
        # Card dicts are shaped like: {'type': 'text'|'pause', 'text'?: str, 'duration': int}
        self.bump_scripts = []
        # Music files are tracked as dicts: {'path': str, 'duration_s': float|None}
        # Duration is parsed from the filename's last space-delimited token.
        self.music_files = []

        # Card timing model: duration is derived from character count.
        # Tuned so that:
        # - "that takes faces" ~ 1.2s
        # - "by resizing... facial features" ~ 3.0s
        self._ms_per_char = 41
        # Extra time applied only to the character-derived portion of the model.
        # (Requested: +15% more time in the character-duration logic.)
        self._ms_per_char_scale = 1.15
        self._base_card_ms = 550
        # Bonus time for short, single-line cards to improve readability.
        # Applied after the main scaling so the bonus is a true +800ms.
        self._one_line_bonus_ms = 800
        self._min_card_ms = 900
        self._max_card_ms = 6000
        # Overall timing multiplier for bump card readability.
        # Previously +20%; this bumps it an additional +5% (1.2 * 1.05 = 1.26).
        self._duration_scale = 1.26

        # Shuffle-bag queues to avoid repeats and spread items out.
        self._script_queue = []  # list[int] indices into bump_scripts
        self._music_queue = []   # list[int] indices into music_files

    def _rebuild_script_queue(self):
        self._script_queue = list(range(len(self.bump_scripts)))
        random.shuffle(self._script_queue)

    def _rebuild_music_queue(self):
        self._music_queue = list(range(len(self.music_files)))
        random.shuffle(self._music_queue)

    def _normalize_card_text(self, text):
        # Make whitespace consistent so char counting is stable.
        return re.sub(r'\s+', ' ', str(text or '')).strip()

    def _is_single_line_card(self, text):
        # Treat as single-line if there's 0-1 non-empty lines.
        raw = str(text or '').strip()
        if not raw:
            return True
        non_empty_lines = [ln for ln in raw.splitlines() if ln.strip()]
        return len(non_empty_lines) <= 1

    def _card_duration_ms_for_text(self, text):
        is_single_line = self._is_single_line_card(text)
        t = self._normalize_card_text(text)
        chars = len(t)
        ms = (self._base_card_ms + (chars * self._ms_per_char * float(self._ms_per_char_scale))) * float(self._duration_scale)
        if is_single_line:
            ms += int(self._one_line_bonus_ms)
        ms = int(ms)
        if ms < self._min_card_ms:
            ms = self._min_card_ms
        if ms > self._max_card_ms:
            ms = self._max_card_ms
        return ms

    def _duration_from_music_filename(self, path):
        """Extract duration (seconds) from the filename's last token.

        Rule: "each music filename has the duration as the last token (delimited by spaces)".
        Examples:
          - "Cool Track 29.mp3" -> 29
          - "Cool Track 29s.mp3" -> 29
          - "Cool Track 29.5.mp3" -> 29.5
        """
        base = os.path.splitext(os.path.basename(str(path)))[0]
        tokens = [t for t in base.split(' ') if t]
        if not tokens:
            return None
        last = tokens[-1]
        m = re.search(r'(\d+(?:\.\d+)?)', last)
        if not m:
            return None
        try:
            v = float(m.group(1))
            if v <= 0:
                return None
            return v
        except Exception:
            return None
        
    def load_bumps(self, folder_path):
        self.bump_scripts = []
        if not os.path.exists(folder_path):
            return

        for root, _, files in os.walk(folder_path):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                # Many users store scripts without an extension (e.g. "script1").
                if ext in {'.txt', '.text', ''}:
                    self._parse_bump_file(os.path.join(root, file))

            self._rebuild_script_queue()
                    
    def _parse_bump_file(self, filepath):
        try:
            # Bump script files may be authored in various encodings.
            # Prefer UTF-8 (with BOM support), but fall back gracefully.
            try:
                with open(filepath, 'r', encoding='utf-8-sig', errors='strict') as f:
                    content = f.read()
            except UnicodeDecodeError:
                with open(filepath, 'r', encoding='latin-1', errors='replace') as f:
                    content = f.read()

            # Split into individual bumps by finding <bump ...> tags.
            # Example: <bump music=any>
            bump_tags = list(re.finditer(r'<bump\b[^>]*>', content, flags=re.IGNORECASE))
            if not bump_tags:
                return

            for i, m in enumerate(bump_tags):
                header = m.group(0)
                body_start = m.end()
                body_end = bump_tags[i + 1].start() if (i + 1) < len(bump_tags) else len(content)
                body = content[body_start:body_end]
                self._parse_single_bump(body, header)
        except Exception as e:
            print(f"Error parsing {filepath}: {e}")

    def _parse_bump_music_pref(self, bump_header):
        if not bump_header:
            return 'any'

        # Supports:
        # - music=any
        # - music=myfile.mp3
        # - music="my file.mp3"
        m = re.search(
            r'music\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            bump_header,
            flags=re.IGNORECASE,
        )
        if not m:
            return 'any'

        value = (m.group(1) or m.group(2) or m.group(3) or '').strip()
        return value or 'any'

    def _parse_outro_text(self, outro_tag):
        if not outro_tag:
            return '[sleepy shows]'

        # Supports:
        # - <outro>
        # - <outro="[sleepy shows]">
        # - <outro='[sleepy shows]'>
        # - <outro=[sleepy shows]>
        m = re.search(
            r'<outro\b\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^>]*))>',
            outro_tag,
            flags=re.IGNORECASE,
        )
        if not m:
            return '[sleepy shows]'
        value = (m.group(1) or m.group(2) or m.group(3) or '').strip()
        return value or '[sleepy shows]'

    def _parse_pause_ms(self, pause_tag):
        # Supports:
        # - <pause>
        # - <pause=1200>
        # - <pause=1200ms>
        if not pause_tag:
            return 1200
        m = re.search(r'(\d+)', pause_tag)
        if not m:
            return 1200
        try:
            return int(m.group(1))
        except Exception:
            return 1200

    def _parse_single_bump(self, content, bump_header=None):
        script = {
            'cards': [],
            'duration': 0,
            'music': self._parse_bump_music_pref(bump_header)
        }
        
        # Split by tags but keep delimiters to process order
        # Regex to find <card>, <outro>, <pause...>
        # Simplification: We can iterate linewise or split by tags.
        # User format: <card>\nText...
        
        # Let's use a tokenizing approach
        # Tokens: <card>, <outro>, <pause=X>, <pause>
        
        tokens = re.split(r'(<(?:card|outro(?:=[^>]*)?|pause(?:=[^>]*)?)>)', content, flags=re.IGNORECASE)
        
        current_card_text = []
        in_outro = False
        
        def finalize_card():
            if current_card_text:
                text = "\n".join(current_card_text).strip()
                if text:
                    # Duration is based on character count (comprehension score).
                    duration = self._card_duration_ms_for_text(text)
                    
                    script['cards'].append({
                        'type': 'text',
                        'text': text,
                        'duration': duration
                    })
                    script['duration'] += duration
                current_card_text.clear()

        for token in tokens:
            token_clean = token.strip()
            if not token_clean:
                continue
                
            token_lower = token_clean.lower()
            
            if token_lower == '<card>':
                finalize_card()
                in_outro = False
            elif token_lower.startswith('<outro'):
                finalize_card()
                # Outro tag: show specified text briefly at the end.
                text = self._parse_outro_text(token_clean)
                duration = 800
                script['cards'].append({
                    'type': 'text',
                    'text': text,
                    'duration': duration
                })
                script['duration'] += duration
                in_outro = True
            elif token_lower.startswith('<pause'):
                finalize_card()
                ms = self._parse_pause_ms(token_clean)
                
                script['cards'].append({
                    'type': 'pause',
                    'duration': ms
                })
                script['duration'] += ms
            else:
                # Content text
                # clean up tags if split left partials? No, re.split with groups keeps the delimiter.
                # Just text.
                if token_clean.startswith('<'):
                     # Unknown tag or malformed, skip or treat as text? 
                     # Treat as text for robustness
                     pass
                else:
                    # Append strictly if not just empty space
                    if token.strip():
                        current_card_text.append(token.strip())
        
        finalize_card()
        
        if script['cards']:
            self.bump_scripts.append(script)

    def scan_music(self, folder_path):
        self.music_files = []
        if not os.path.exists(folder_path):
            return

        audio_exts = {'.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.opus', '.webm', '.mp4'}
        for root, _, files in os.walk(folder_path):
            for f in files:
                if os.path.splitext(f)[1].lower() in audio_exts:
                    full_path = os.path.join(root, f)
                    self.music_files.append({
                        'path': full_path,
                        'duration_s': self._duration_from_music_filename(full_path)
                    })

        self._rebuild_music_queue()

    def _iter_music_entries(self):
        # Backward compatibility: allow either dict entries or raw paths.
        for entry in self.music_files:
            if isinstance(entry, dict):
                p = entry.get('path')
                d = entry.get('duration_s', None)
                yield {'path': str(p), 'duration_s': d}
            else:
                p = str(entry)
                yield {'path': p, 'duration_s': self._duration_from_music_filename(p)}

    def _find_music_by_basename(self, basename_lower):
        for entry in self._iter_music_entries():
            p = entry.get('path')
            if not p:
                continue
            if os.path.basename(str(p)).lower() == basename_lower:
                return entry
        return None

    def _is_music_entry_eligible(self, entry, min_duration_s, allow_xmas=False):
        try:
            dur_s = entry.get('duration_s', None)
            if dur_s is None:
                return False
            if float(dur_s) < float(min_duration_s):
                return False
        except Exception:
            return False

        if not allow_xmas:
            try:
                name = os.path.basename(str(entry.get('path', ''))).lower()
                if name.startswith('xmas'):
                    return False
            except Exception:
                return False

        return True

    def _pick_music_from_queue(self, min_duration_s, allow_xmas=False):
        """Pick the next music track from the shuffle-bag that meets criteria.

        To keep spacing fair, we iterate through the queue once, rotating
        ineligible items to the back without consuming them.
        """
        if not self.music_files:
            return None
        if not self._music_queue:
            self._rebuild_music_queue()
        if not self._music_queue:
            return None

        attempts = len(self._music_queue)
        for _ in range(attempts):
            idx = self._music_queue.pop(0)
            entry = self.music_files[idx]
            if not isinstance(entry, dict):
                entry = {'path': str(entry), 'duration_s': self._duration_from_music_filename(str(entry))}

            if self._is_music_entry_eligible(entry, min_duration_s=min_duration_s, allow_xmas=allow_xmas):
                # Consume it (do not re-append) to maximize spacing before repeats.
                return str(entry.get('path'))

            # Not eligible for this script; rotate it to the back for future bumps.
            self._music_queue.append(idx)

        return None

    def _pick_music_for_script(self, script, min_duration_s):
        music_pref = str(script.get('music') or 'any').strip()
        if music_pref and music_pref.lower() != 'any':
            # Explicit filename: treat as required.
            entry = self._find_music_by_basename(music_pref.lower())
            if not entry:
                return None
            if not self._is_music_entry_eligible(entry, min_duration_s=min_duration_s, allow_xmas=True):
                return None
            return str(entry.get('path'))

        # Default: use queue-based selection, skipping xmas.
        return self._pick_music_from_queue(min_duration_s=min_duration_s, allow_xmas=False)

    def _music_candidates(self, music_pref, min_duration_s):
        pref = str(music_pref or 'any').strip()
        pref_lower = pref.lower()

        candidates = []
        for entry in self._iter_music_entries():
            path = entry.get('path')
            dur_s = entry.get('duration_s', None)
            if not path:
                continue

            # Must have a usable duration and be long enough for the bump.
            try:
                if dur_s is None or float(dur_s) < float(min_duration_s):
                    continue
            except Exception:
                continue

            name = os.path.basename(str(path)).lower()

            # Respect explicit music selection.
            if pref_lower != 'any':
                if name != pref_lower:
                    continue
            else:
                # Default rule: avoid xmas tracks.
                if name.startswith('xmas'):
                    continue

            candidates.append({'path': str(path), 'duration_s': float(dur_s)})

        return candidates

    def get_random_bump(self):
        """
        Returns {'script': dict, 'audio': str} or None
        """
        if not self.bump_scripts or not self.music_files:
            return None

        if not self._script_queue:
            self._rebuild_script_queue()
        if not self._script_queue:
            return None

        # Walk the script queue once to find a playable bump.
        attempts = len(self._script_queue)
        for _ in range(attempts):
            script_idx = self._script_queue.pop(0)
            try:
                script = self.bump_scripts[script_idx]
            except Exception:
                continue

            try:
                bump_ms = int(script.get('duration', 0) or 0)
            except Exception:
                bump_ms = 0
            bump_s = bump_ms / 1000.0

            audio_path = self._pick_music_for_script(script, min_duration_s=bump_s)
            if audio_path:
                # Consume this script (do not re-append) to maximize spacing.
                return {
                    'type': 'bump',
                    'script': script,
                    'audio': str(audio_path)
                }

            # Not playable right now; rotate it to the back so it stays spread out.
            self._script_queue.append(script_idx)

        # No script had a long-enough music candidate.
        return None

