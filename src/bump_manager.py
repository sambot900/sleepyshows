import os
import random
import re
import shlex

class BumpManager:
    def __init__(self):
        # List of bump script dicts.
        # Each script is shaped like: {'cards': [...], 'duration': int, 'music': 'any'|'<filename>'}
        # Card dicts are shaped like: {'type': 'text'|'pause', 'text'?: str, 'duration': int}
        self.bump_scripts = []
        # Music files are tracked as dicts: {'path': str, 'duration_s': float|None}
        # Duration is parsed from the filename's last space-delimited token.
        self.music_files = []

        # Base folder for bump images. Scripts reference images by filename only.
        self.bump_images_dir = None

        # Base folder for bump audio FX. Scripts reference sounds by filename only.
        self.bump_audio_fx_dir = None

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

        # Recent history to reduce near-repeats when queues are rebuilt.
        # Rule: the 8 most recently used items cannot appear in the first 8 slots
        # of a newly rebuilt queue (best-effort).
        self._recent_spread_n = 8
        self._recent_script_indices = []   # list[int]
        self._recent_music_basenames = []  # list[str] lower

        # Short bump rule: if bump duration is <= 15s, prefer music tracks <= 15s
        # (but still long enough for the bump) instead of using longer tracks.
        self._short_bump_s = 15.0

        # Lazy indices for case-insensitive file resolution in user-selected folders.
        self._images_index_dir = None
        self._images_index = None
        self._fx_index_dir = None
        self._fx_index = None

        self._audio_exts = ('.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.opus', '.webm', '.mp4')
        self._image_exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif')

        # Bump duration normalization:
        # - If total bump duration is in [29s, 35s], proportionally reduce only auto-timed cards
        #   so that total duration becomes exactly 29s.
        # - Do not adjust outro timing.
        # - Do not adjust cards that specify duration via <card ...>.
        # - If total duration is > 35s, do not play the bump at all (skip it).
        self._bump_target_ms = 29_000
        self._bump_max_ms = 35_000

    def _normalize_bump_script_duration(self, script: dict) -> bool:
        """Normalize a parsed bump script to the target duration.

        Returns True if the script should be kept/playable, False if it should be rejected.
        """
        if not isinstance(script, dict):
            return False
        cards = script.get('cards')
        if not isinstance(cards, list) or not cards:
            return False

        try:
            total_ms = int(script.get('duration', 0) or 0)
        except Exception:
            total_ms = 0

        if total_ms <= 0:
            return False
        if total_ms > int(self._bump_max_ms):
            return False

        target_ms = int(self._bump_target_ms)
        if total_ms < target_ms:
            # Leave short bumps alone.
            return True
        if total_ms == target_ms:
            return True

        # Only normalize in the [target, max] window.
        if total_ms > int(self._bump_max_ms):
            return False

        fixed_total = 0
        adjustable = []  # (idx, duration_ms)
        for i, c in enumerate(cards):
            try:
                d = int(c.get('duration', 0) or 0)
            except Exception:
                d = 0

            # Treat outro, pauses, and explicitly-timed cards as fixed.
            mode = str(c.get('_duration_mode', 'auto') or 'auto').lower()
            if mode in {'fixed', 'explicit'}:
                fixed_total += d
            else:
                adjustable.append((i, d))

        # If fixed content alone already consumes the entire target, we can't shrink to target.
        if fixed_total >= target_ms:
            # Allow exact match if there is nothing adjustable.
            if fixed_total == target_ms and not adjustable:
                script['duration'] = fixed_total
                return True
            return False

        adjustable_total = sum(d for _, d in adjustable)
        if adjustable_total <= 0:
            # Nothing we can adjust.
            return False

        desired_adjustable_total = target_ms - fixed_total
        if desired_adjustable_total <= 0:
            return False

        # Scale adjustable cards down proportionally so the TOTAL becomes exactly target_ms.
        scale = float(desired_adjustable_total) / float(adjustable_total)
        if scale <= 0.0:
            return False

        # Integer rounding: distribute leftover ms by largest fractional part.
        scaled = []  # (idx, base_ms, frac)
        base_sum = 0
        for idx, d in adjustable:
            v = float(d) * scale
            base = int(v)
            if base < 1:
                base = 1
            frac = v - float(int(v))
            scaled.append((idx, base, frac))
            base_sum += base

        remainder = int(desired_adjustable_total) - int(base_sum)
        if remainder != 0:
            # Add/subtract 1ms adjustments while preserving proportionality as much as possible.
            # For adding, give ms to largest fractional parts; for subtracting, take from smallest.
            if remainder > 0:
                scaled.sort(key=lambda t: t[2], reverse=True)
                for j in range(min(remainder, len(scaled))):
                    idx, base, frac = scaled[j]
                    scaled[j] = (idx, base + 1, frac)
            else:
                take = -remainder
                scaled.sort(key=lambda t: t[2])
                for j in range(min(take, len(scaled))):
                    idx, base, frac = scaled[j]
                    if base > 1:
                        scaled[j] = (idx, base - 1, frac)

        # Apply new durations.
        new_total = fixed_total
        for idx, base, _ in scaled:
            cards[idx]['duration'] = int(base)
            new_total += int(base)

        script['duration'] = int(new_total)
        # If rounding made us miss by a tiny amount, do a final nudge on the first adjustable card.
        drift = int(target_ms) - int(new_total)
        if drift != 0 and scaled:
            first_idx = scaled[0][0]
            try:
                cur = int(cards[first_idx].get('duration', 0) or 0)
            except Exception:
                cur = 0
            cur = max(1, cur + int(drift))
            cards[first_idx]['duration'] = int(cur)
            script['duration'] = int(new_total + drift)

        return int(script.get('duration', 0) or 0) == int(target_ms)

    def _build_file_index(self, folder: str):
        """Return {lower_basename: full_path} for files under folder (recursive)."""
        folder = str(folder or '').strip()
        if not folder or not os.path.isdir(folder):
            return {}
        out = {}
        try:
            for root, _, files in os.walk(folder):
                for f in files:
                    try:
                        out[str(f).lower()] = os.path.join(root, f)
                    except Exception:
                        continue
        except Exception:
            return {}
        return out

    def _get_images_index(self):
        img_dir = str(getattr(self, 'bump_images_dir', None) or '').strip()
        if not img_dir or not os.path.isdir(img_dir):
            self._images_index_dir = None
            self._images_index = None
            return None
        if self._images_index is None or self._images_index_dir != img_dir:
            self._images_index_dir = img_dir
            self._images_index = self._build_file_index(img_dir)
        return self._images_index or {}

    def _get_fx_index(self):
        fx_dir = str(getattr(self, 'bump_audio_fx_dir', None) or '').strip()
        if not fx_dir or not os.path.isdir(fx_dir):
            self._fx_index_dir = None
            self._fx_index = None
            return None
        if self._fx_index is None or self._fx_index_dir != fx_dir:
            self._fx_index_dir = fx_dir
            self._fx_index = self._build_file_index(fx_dir)
        return self._fx_index or {}

    def _find_case_insensitive(self, folder: str, filename: str):
        """Find a file under folder by case-insensitive basename match (recursive)."""
        folder = str(folder or '').strip()
        name = os.path.basename(str(filename or '').strip())
        if not folder or not os.path.isdir(folder) or not name:
            return None
        name_l = name.lower()
        try:
            for root, _, files in os.walk(folder):
                for f in files:
                    try:
                        if str(f).lower() == name_l:
                            return os.path.join(root, f)
                    except Exception:
                        continue
        except Exception:
            return None
        return None

    def _rebuild_script_queue(self):
        self._script_queue = self._build_queue_with_recent_exclusion(
            items=list(range(len(self.bump_scripts))),
            recent=list(self._recent_script_indices),
            n=int(self._recent_spread_n),
        )

    def _rebuild_music_queue(self):
        self._music_queue = self._build_queue_with_recent_exclusion(
            items=list(range(len(self.music_files))),
            recent=list(self._recent_music_basenames),
            n=int(self._recent_spread_n),
            key_fn=self._music_queue_key,
        )

    def _music_queue_key(self, idx):
        try:
            entry = self.music_files[int(idx)]
            p = entry.get('path') if isinstance(entry, dict) else entry
            return os.path.basename(str(p or '')).lower()
        except Exception:
            return ''

    def _build_queue_with_recent_exclusion(self, *, items, recent, n: int, key_fn=None):
        """Build a FIFO queue with a recent-spacing constraint.

        Best-effort rule: the N most recently used items cannot appear in the
        first N slots of a newly rebuilt queue.
        """
        q = [x for x in list(items or []) if x is not None]
        if len(q) <= 1:
            return q

        random.shuffle(q)

        try:
            n = int(n)
        except Exception:
            n = 0
        if n <= 0:
            return q

        def _key(x):
            try:
                return key_fn(x) if callable(key_fn) else x
            except Exception:
                return x

        recent_keys = []
        for r in list(recent or []):
            try:
                recent_keys.append(str(r).lower() if isinstance(r, str) else r)
            except Exception:
                recent_keys.append(r)

        recent_set = set(recent_keys[-n:])
        if not recent_set:
            return q

        # Best-effort: push all non-recent items as early as possible.
        # If there are at least N non-recent items, then the first N slots will
        # contain no recent items.
        non_recent = []
        recent_items = []
        for x in q:
            if _key(x) in recent_set:
                recent_items.append(x)
            else:
                non_recent.append(x)

        return non_recent + recent_items

    def _note_recent_script(self, script_idx: int):
        try:
            self._recent_script_indices.append(int(script_idx))
            self._recent_script_indices = self._recent_script_indices[-int(self._recent_spread_n):]
        except Exception:
            pass

    def _note_recent_music_path(self, path: str):
        try:
            name = os.path.basename(str(path or '')).lower()
            if not name:
                return
            self._recent_music_basenames.append(name)
            self._recent_music_basenames = self._recent_music_basenames[-int(self._recent_spread_n):]
        except Exception:
            pass

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
                try:
                    base_dir = os.path.dirname(str(filepath))
                except Exception:
                    base_dir = None
                self._parse_single_bump(body, header, base_dir=base_dir)
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
        # - <outro="[sleepy shows]" audio>
        # - <outro='[sleepy shows]'>
        # - <outro=[sleepy shows]>
        try:
            s = str(outro_tag).strip()
        except Exception:
            return '[sleepy shows]'

        # Prefer an explicitly quoted value anywhere in the tag.
        m = re.search(r'"([^"]*)"|\'([^\']*)\'', s)
        if m:
            value = (m.group(1) or m.group(2) or '').strip()
            return value or '[sleepy shows]'

        # Fallback: take anything after the tag name (and optional '=') up to '>'.
        try:
            inner = re.sub(r'^\s*<\s*outro\b', '', s, flags=re.IGNORECASE)
            inner = re.sub(r'>\s*$', '', inner)
            inner = inner.strip()
            if inner.startswith('='):
                inner = inner[1:].strip()
            # Remove trailing standalone 'audio' arg if present.
            inner = re.sub(r'\s+audio\s*$', '', inner, flags=re.IGNORECASE).strip()
            return inner or '[sleepy shows]'
        except Exception:
            return '[sleepy shows]'

    def _parse_outro_audio_flag(self, outro_tag):
        """Return True if the <outro ...> tag includes an 'audio' argument.

        Example:
          <outro="[sleepy shows]" audio>
        """
        if not outro_tag:
            return False
        try:
            s = str(outro_tag)
            # Remove quoted segments so a quoted word "audio" doesn't trigger the flag.
            s = re.sub(r'"[^"]*"|\'[^\']*\'', '', s)
            return re.search(r'\baudio\b', s, flags=re.IGNORECASE) is not None
        except Exception:
            return False

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

    def _parse_card_duration_spec(self, card_tag):
        """Parse optional duration override from a <card ...> tag.

        Supported:
          - <card>            -> None (use standard timing)
          - <card 500ms>      -> ('abs', 500)
          - <card +500ms>     -> ('delta', +500)
          - <card -500ms>     -> ('delta', -500)

                We accept optional whitespace and a unit suffix:
                    - ms (milliseconds)
                    - s  (seconds)

                If no unit is provided, milliseconds are assumed.
        """
        if not card_tag:
            return None
        s = str(card_tag).strip()
        if not s:
            return None

        # Quickly reject non-card tags.
        if not re.match(r'^<\s*card\b', s, flags=re.IGNORECASE):
            return None

        # Extract the inner payload (between 'card' and '>').
        m = re.match(r'^<\s*card\b\s*([^>]*)>\s*$', s, flags=re.IGNORECASE)
        if not m:
            return None

        payload = (m.group(1) or '').strip()
        if not payload:
            return None

        # Expect something like "+500ms", "500ms", "5s" (allow spaces).
        m2 = re.match(r'^([+-]?)\s*(\d+(?:\.\d+)?)\s*(ms|s)?\s*$', payload, flags=re.IGNORECASE)
        if not m2:
            return None

        sign = (m2.group(1) or '').strip()
        num_s = (m2.group(2) or '').strip()
        unit = (m2.group(3) or '').strip().lower()
        try:
            value = float(num_s)
        except Exception:
            return None

        if value < 0:
            value = abs(value)

        # Default unit: ms
        if unit == 's':
            ms = int(round(value * 1000.0))
        else:
            ms = int(round(value))

        if sign == '+':
            return ('delta', ms)
        if sign == '-':
            return ('delta', -ms)
        return ('abs', ms)

    def _expand_whitespace_tags(self, text):
        r"""Expand explicit whitespace tags in bump scripts.

        - <\s> => space
        - <\t> => tab
        - <\n> => newline
        """
        if text is None:
            return ''
        s = str(text)
        # Important: these tags include a literal backslash in the script.
        s = s.replace('<\\s>', ' ')
        s = s.replace('<\\t>', '\t')
        s = s.replace('<\\n>', '\n')
        return s

    def _clamp_card_duration_ms(self, ms):
        try:
            ms = int(ms)
        except Exception:
            ms = int(self._min_card_ms)

        if ms < int(self._min_card_ms):
            ms = int(self._min_card_ms)
        if ms > int(self._max_card_ms):
            ms = int(self._max_card_ms)
        return int(ms)

    def _resolve_bump_image_path(self, filename, base_dir=None):
        name = str(filename or '').strip().strip('"\'')
        if not name:
            return ''

        img_dir = str(getattr(self, 'bump_images_dir', None) or '').strip()
        if img_dir:
            candidate = os.path.normpath(os.path.join(img_dir, name))
            if os.path.exists(candidate):
                return candidate

            # Case-insensitive fallback for Linux/macOS.
            try:
                idx = self._get_images_index()
                if isinstance(idx, dict):
                    hit = idx.get(os.path.basename(name).lower())
                    if hit and os.path.exists(hit):
                        return os.path.normpath(hit)
            except Exception:
                pass

            # Refresh-safe fallback: walk the folder if the cached index is stale
            # or if the file was added after the app started.
            try:
                hit = self._find_case_insensitive(img_dir, name)
                if hit and os.path.exists(hit):
                    return os.path.normpath(hit)
            except Exception:
                pass

        if base_dir:
            candidate = os.path.normpath(os.path.join(str(base_dir), name))
            if os.path.exists(candidate):
                return candidate

            try:
                hit = self._find_case_insensitive(str(base_dir), name)
                if hit and os.path.exists(hit):
                    return os.path.normpath(hit)
            except Exception:
                pass

        return os.path.normpath(name)

    def _resolve_bump_sound_path(self, filename, base_dir=None):
        name = str(filename or '').strip().strip('"\'')
        if not name:
            return ''

        base_name = os.path.basename(name)
        root, ext = os.path.splitext(base_name)
        # Allow extensionless filenames in scripts: <sound long-beep interrupt>
        candidates = [name]
        if not ext:
            candidates = [root + e for e in self._audio_exts]

        fx_dir = str(getattr(self, 'bump_audio_fx_dir', None) or '').strip()
        if fx_dir:
            for cand in candidates:
                candidate = os.path.normpath(os.path.join(fx_dir, cand))
                if os.path.exists(candidate):
                    return candidate

            # Case-insensitive fallback.
            try:
                idx = self._get_fx_index()
                if isinstance(idx, dict):
                    for cand in candidates:
                        hit = idx.get(os.path.basename(cand).lower())
                        if hit and os.path.exists(hit):
                            return os.path.normpath(hit)
            except Exception:
                pass

            # Refresh-safe fallback: walk the folder if needed.
            try:
                for cand in candidates:
                    hit = self._find_case_insensitive(fx_dir, cand)
                    if hit and os.path.exists(hit):
                        return os.path.normpath(hit)
            except Exception:
                pass

        if base_dir:
            for cand in candidates:
                candidate = os.path.normpath(os.path.join(str(base_dir), cand))
                if os.path.exists(candidate):
                    return candidate

            try:
                for cand in candidates:
                    hit = self._find_case_insensitive(str(base_dir), cand)
                    if hit and os.path.exists(hit):
                        return os.path.normpath(hit)
            except Exception:
                pass

        return os.path.normpath(name)

    def _parse_sound_tag(self, sound_tag, *, base_dir=None):
        """Parse a <sound ...> tag.

        Supported (order flexible):
          - <sound file.wav>
          - <sound file.wav add>
          - <sound file.wav interrupt>
                    - <sound file.wav cut>
          - <sound file.wav duration>
          - <sound file.wav card>
          - <sound file.wav 500ms>
          - <sound file.wav 5s>
        Defaults:
          - mix: add
          - play_for: card
        """
        if not sound_tag:
            return None

        m = re.match(r'^<\s*sound\b\s*([^>]*)>\s*$', str(sound_tag).strip(), flags=re.IGNORECASE)
        if not m:
            return None

        raw = (m.group(1) or '').strip()
        if not raw:
            return None

        try:
            tokens = shlex.split(raw)
        except Exception:
            tokens = [t for t in re.split(r'\s+', raw) if t]

        filename = None
        mix = 'add'
        play_for = 'card'  # 'card' | 'duration' | 'ms'
        ms = None

        for t in tokens:
            tl = str(t).strip().lower()
            if not tl:
                continue

            if tl == 'add':
                mix = 'add'
                continue
            if tl == 'interrupt':
                mix = 'interrupt'
                continue
            if tl == 'cut':
                mix = 'cut'
                continue

            if tl == 'duration':
                play_for = 'duration'
                ms = None
                continue
            if tl == 'card':
                play_for = 'card'
                ms = None
                continue

            tm = re.match(r'^(\d+(?:\.\d+)?)\s*(ms|s)$', tl)
            if tm:
                try:
                    v = float(tm.group(1))
                    unit = tm.group(2)
                    ms = int(round(v * 1000.0)) if unit == 's' else int(round(v))
                    if ms < 0:
                        ms = abs(ms)
                    play_for = 'ms'
                except Exception:
                    pass
                continue

            if filename is None:
                filename = str(t).strip()

        if not filename:
            return None

        resolved = self._resolve_bump_sound_path(filename, base_dir=base_dir)
        info = {
            'filename': str(filename),
            'path': str(resolved),
            'mix': str(mix),
            'play_for': str(play_for),
        }
        if play_for == 'ms' and ms is not None:
            info['ms'] = int(ms)

        return info

    def _parse_img_tag(self, img_tag, *, base_dir=None, full_card_text=None):
        """Parse an <img ...> tag.

        Supported:
          - <img filename.png>
          - <img filename.png lines>
          - <img filename.png char>
          - <img filename.png 20%>
        """
        if not img_tag:
            return None

        m = re.match(r'^<\s*img\b\s*([^>]*)>\s*$', str(img_tag).strip(), flags=re.IGNORECASE)
        if not m:
            return None

        raw = (m.group(1) or '').strip()
        if not raw:
            return None

        try:
            tokens = shlex.split(raw)
        except Exception:
            tokens = [t for t in re.split(r'\s+', raw) if t]

        filename = None
        mode = 'default'
        percent = None

        for t in tokens:
            tl = str(t).strip().lower()
            if not tl:
                continue
            if tl == 'lines':
                mode = 'lines'
                continue
            if tl == 'char':
                mode = 'char'
                continue
            pm = re.match(r'^(\d+(?:\.\d+)?)%$', tl)
            if pm:
                mode = 'percent'
                try:
                    percent = float(pm.group(1))
                except Exception:
                    percent = None
                continue
            if filename is None:
                filename = str(t).strip()

        if not filename:
            return None

        resolved = self._resolve_bump_image_path(filename, base_dir=base_dir)
        info = {
            'filename': str(filename),
            'path': str(resolved),
            'mode': str(mode),
        }
        if percent is not None:
            info['percent'] = float(percent)

        if mode == 'lines':
            try:
                cleaned = str(full_card_text or '')
                cleaned = re.sub(r'<\s*img\b[^>]*>', '', cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r'<\s*sound\b[^>]*>', '', cleaned, flags=re.IGNORECASE)
                lines = [ln for ln in cleaned.splitlines() if ln.strip()]
                info['lines_count'] = int(len(lines))
            except Exception:
                info['lines_count'] = 0

        return info

    def _parse_single_bump(self, content, bump_header=None, base_dir=None):
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
        
        tokens = re.split(r'(<(?:card\b[^>]*|outro\b[^>]*|pause\b[^>]*)>)', content, flags=re.IGNORECASE)
        
        current_card_text = []
        current_card_duration_spec = None
        in_outro = False
        
        def finalize_card():
            if current_card_text:
                text = "\n".join(current_card_text).strip()
                if text:
                    text = self._expand_whitespace_tags(text)
                    # Duration is based on character count (comprehension score).
                    # Do not include <img ...> or <sound ...> markup in the timing model.
                    timing_text = re.sub(r'<\s*(?:img|sound)\b[^>]*>', '', text, flags=re.IGNORECASE)
                    standard_duration = self._card_duration_ms_for_text(timing_text)
                    duration = standard_duration

                    spec = current_card_duration_spec
                    duration_mode = 'auto'
                    if spec and isinstance(spec, tuple) and len(spec) == 2:
                        mode, val = spec
                        try:
                            if mode == 'abs':
                                duration = int(val)
                            elif mode == 'delta':
                                duration = int(standard_duration) + int(val)
                        except Exception:
                            duration = standard_duration
                        duration_mode = 'explicit'

                    duration = self._clamp_card_duration_ms(duration)

                    # Optional: sound FX tags.
                    sound_m = re.search(r'<\s*sound\b[^>]*>', text, flags=re.IGNORECASE)
                    sound_info = None
                    if sound_m:
                        try:
                            sound_info = self._parse_sound_tag(sound_m.group(0), base_dir=base_dir)
                        except Exception:
                            sound_info = None

                    def _strip_sound_markup(s):
                        try:
                            return re.sub(r'<\s*sound\b[^>]*>', '', str(s or ''), flags=re.IGNORECASE)
                        except Exception:
                            return str(s or '')

                    # Optional: image cards.
                    img_m = re.search(r'<\s*img\b[^>]*>', text, flags=re.IGNORECASE)
                    if img_m:
                        img_tag = img_m.group(0)
                        img_info = self._parse_img_tag(img_tag, base_dir=base_dir, full_card_text=text)
                        if img_info and img_info.get('path'):
                            before = _strip_sound_markup((text[:img_m.start()] or '')).rstrip()
                            after = _strip_sound_markup((text[img_m.end():] or '')).lstrip()

                            if str(img_info.get('mode')) == 'char':
                                template = _strip_sound_markup((text[:img_m.start()] or '')) + '[[IMG]]' + _strip_sound_markup((text[img_m.end():] or ''))
                                card_obj = {
                                    'type': 'img_char',
                                    'template': template,
                                    'image': img_info,
                                    'duration': duration,
                                    '_duration_mode': duration_mode,
                                }
                            else:
                                def _count_lines(s):
                                    try:
                                        return len([ln for ln in str(s or '').splitlines() if ln.strip()])
                                    except Exception:
                                        return 0

                                card_obj = {
                                    'type': 'img',
                                    'text_before': before.strip(),
                                    'text_after': after.strip(),
                                    'image': img_info,
                                    'before_lines': _count_lines(before),
                                    'after_lines': _count_lines(after),
                                    'duration': duration,
                                    '_duration_mode': duration_mode,
                                }

                            if sound_info:
                                card_obj['sound'] = sound_info

                            script['cards'].append(card_obj)

                            script['duration'] += duration
                            current_card_text.clear()
                            return

                    display_text = _strip_sound_markup(text).strip()

                    card_obj = {
                        'type': 'text',
                        'text': display_text,
                        'duration': duration,
                        '_duration_mode': duration_mode,
                    }

                    if sound_info:
                        card_obj['sound'] = sound_info

                    script['cards'].append(card_obj)
                    script['duration'] += duration
                current_card_text.clear()

        for token in tokens:
            token_clean = token.strip()
            if not token_clean:
                continue
                
            token_lower = token_clean.lower()
            
            if token_lower.startswith('<card'):
                finalize_card()
                in_outro = False
                current_card_duration_spec = self._parse_card_duration_spec(token_clean)
            elif token_lower.startswith('<outro'):
                finalize_card()
                # Outro tag: show specified text briefly at the end.
                text = self._parse_outro_text(token_clean)
                outro_audio = self._parse_outro_audio_flag(token_clean)
                duration = 800
                script['cards'].append({
                    'type': 'text',
                    'text': text,
                    'duration': duration,
                    '_duration_mode': 'fixed',
                    'outro_audio': bool(outro_audio),
                })
                script['duration'] += duration
                in_outro = True
                current_card_duration_spec = None
            elif token_lower.startswith('<pause'):
                finalize_card()
                ms = self._parse_pause_ms(token_clean)
                
                script['cards'].append({
                    'type': 'pause',
                    'duration': ms,
                    '_duration_mode': 'fixed',
                })
                script['duration'] += ms
                current_card_duration_spec = None
            else:
                # Content text
                # clean up tags if split left partials? No, re.split with groups keeps the delimiter.
                # Just text.
                # Append strictly if not just empty space.
                # NOTE: card bodies may legitimately begin with markup like <img ...> or <sound ...>.
                if token.strip():
                    current_card_text.append(token.strip())
        
        finalize_card()

        if script['cards']:
            if self._normalize_bump_script_duration(script):
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

    def _is_music_entry_eligible(self, entry, min_duration_s, allow_xmas=False, max_duration_s=None):
        try:
            dur_s = entry.get('duration_s', None)
            # If we can't infer duration from the filename, we normally can't
            # guarantee it will cover the bump. But we still want bumps to be
            # playable (music can end early and the bump continues silently).
            if dur_s is None:
                return float(min_duration_s) <= 0.0
            if float(dur_s) < float(min_duration_s):
                return False
            if max_duration_s is not None and float(dur_s) > float(max_duration_s):
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

    def _pick_music_from_queue(self, min_duration_s, allow_xmas=False, max_duration_s=None):
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

            if self._is_music_entry_eligible(entry, min_duration_s=min_duration_s, allow_xmas=allow_xmas, max_duration_s=max_duration_s):
                # Consume it (do not re-append) to maximize spacing before repeats.
                p = str(entry.get('path'))
                self._note_recent_music_path(p)
                return p

            # Not eligible for this script; rotate it to the back for future bumps.
            self._music_queue.append(idx)

        return None

    def _pick_any_music_from_queue(self, allow_xmas=False):
        """Pick the next music track from the shuffle-bag with no duration constraint."""
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

            p = str(entry.get('path') or '')
            if not p:
                # Rotate empty entries.
                self._music_queue.append(idx)
                continue

            if not allow_xmas:
                try:
                    name = os.path.basename(p).lower()
                    if name.startswith('xmas'):
                        self._music_queue.append(idx)
                        continue
                except Exception:
                    self._music_queue.append(idx)
                    continue

            # Consume it (do not re-append) to maximize spacing.
            self._note_recent_music_path(p)
            return p

        return None

    def _pick_music_for_script(self, script, min_duration_s):
        music_pref = str(script.get('music') or 'any').strip()
        if music_pref and music_pref.lower() != 'any':
            # Explicit filename: treat as required.
            entry = self._find_music_by_basename(music_pref.lower())
            if not entry:
                return None
            # If it exists but is "too short" per filename heuristics, still allow it.
            # Music can end early; the bump should still run.
            p = str(entry.get('path'))
            self._note_recent_music_path(p)
            return p

        # Default: use queue-based selection, skipping xmas.
        try:
            if float(min_duration_s) <= float(self._short_bump_s):
                picked = self._pick_music_from_queue(min_duration_s=min_duration_s, allow_xmas=False, max_duration_s=float(self._short_bump_s))
                if picked:
                    return picked
        except Exception:
            pass

        picked = self._pick_music_from_queue(min_duration_s=min_duration_s, allow_xmas=False)
        if picked:
            return picked

        # Fallback: allow any track (even if "too short" / duration unknown).
        return self._pick_any_music_from_queue(allow_xmas=False)

    def _pick_music_for_script_strict(self, script, min_duration_s):
        """Pick music for a script, but require it to be long enough.

        Returns a path or None.
        """
        music_pref = str(script.get('music') or 'any').strip()
        if music_pref and music_pref.lower() != 'any':
            entry = self._find_music_by_basename(music_pref.lower())
            if not entry:
                return None
            if not self._is_music_entry_eligible(entry, min_duration_s=min_duration_s, allow_xmas=True):
                return None
            p = str(entry.get('path'))
            self._note_recent_music_path(p)
            return p

        # Prefer <=15s tracks for <=15s bumps.
        try:
            if float(min_duration_s) <= float(self._short_bump_s):
                picked = self._pick_music_from_queue(min_duration_s=min_duration_s, allow_xmas=False, max_duration_s=float(self._short_bump_s))
                if picked:
                    return picked
        except Exception:
            pass

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
        if not self.bump_scripts:
            return None

        if not self._script_queue:
            self._rebuild_script_queue()
        if not self._script_queue:
            return None

        # Prefer a script that has a long-enough music track (when possible).
        # This prevents "short music" when there are valid pairings available.
        attempts = len(self._script_queue)
        chosen = None
        chosen_audio = None

        if self.music_files:
            # Pass 1 (strict): require a long-enough track.
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

                try:
                    audio_path = self._pick_music_for_script_strict(script, min_duration_s=bump_s)
                except Exception:
                    audio_path = None

                if audio_path:
                    chosen = (script_idx, script)
                    chosen_audio = str(audio_path)
                    break

                # Not playable strictly; rotate it to the back.
                self._script_queue.append(script_idx)

        # Pass 2 (best-effort): pick the next script, allow any/short music (or silence).
        if chosen is None:
            for _ in range(attempts):
                script_idx = self._script_queue.pop(0)
                try:
                    script = self.bump_scripts[script_idx]
                except Exception:
                    continue

                chosen = (script_idx, script)
                chosen_audio = None
                if self.music_files:
                    try:
                        bump_ms = int(script.get('duration', 0) or 0)
                    except Exception:
                        bump_ms = 0
                    bump_s = bump_ms / 1000.0
                    try:
                        chosen_audio = self._pick_music_for_script(script, min_duration_s=bump_s)
                    except Exception:
                        chosen_audio = None
                break

        if chosen is None:
            return None

        script_idx, script = chosen
        # Consume this script (do not re-append) to maximize spacing.
        self._note_recent_script(script_idx)
        return {
            'type': 'bump',
            'script': script,
            'audio': (str(chosen_audio) if chosen_audio else None)
        }

