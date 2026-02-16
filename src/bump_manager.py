import os
import random
import re
import shlex
import math
import time
import subprocess
import json

try:
    from mutagen import File as _mutagen_file
except Exception:
    _mutagen_file = None


class BumpManager:
    def __init__(self):
        # List of bump script dicts.
        # Each script is shaped like: {'cards': [...], 'duration': int, 'music': 'any'|'<filename>'}
        # Card dicts are shaped like: {'type': 'text'|'pause', 'text'?: str, 'duration': int}
        self.bump_scripts = []
        # Music files are tracked as dicts:
        #   {'path': str, 'duration_ms': int|None, 'duration_s': float|None}
        # Duration is determined exactly via mutagen when possible.
        self.music_files = []

        # Optional: outro sounds that may be selected when a bump script includes
        # an <outro ... audio> card.
        # Tracked as a list[str] of file paths.
        self.outro_sounds = []

        # Base folder for bump images. Scripts reference images by filename only.
        self.bump_images_dir = None

        # Base folder for bump audio FX. Scripts reference sounds by filename only.
        self.bump_audio_fx_dir = None

        # Base folder for bump video assets. Scripts reference videos by filename only.
        self.bump_videos_dir = None

        # Optional cache of exact video durations (ms) keyed by normalized absolute path.
        # Filled by main.py during startup probing.
        self.video_durations_ms = {}

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

        # New duration estimator control variables (see docs/bump duration strategy.txt).
        # Applied to the output of the readability model.
        self._duration_estimate_scale = 1.0
        # α (alpha): 0 => equalized, 1 => proportional-to-duration, >1 => exaggerated
        self._duration_normalization_exponent = 1.0
        # ε (epsilon): global overage tolerance for music matching
        self._music_overage_tolerance = 0.20

        # Separate tolerance for the short-bump (15s) compression heuristic.
        # This controls which scripts are considered eligible to be compressed
        # into a 15s clip.
        #
        # Example: 15s target with 0.533 tolerance => allow estimated up to ~23s
        # to be treated as short-clip eligible.
        # 23s max accepted estimate for a 15s target => (23/15)-1
        self._short_bump_overage_tolerance = (23.0 / 15.0) - 1.0
        # k: soft clamp strength for reduction saturation
        self._duration_soft_clamp_k = 4.0
        # Minimum allowed fraction of original auto-timed duration.
        self._min_scalable_fraction = 0.40

        # Shuffle-bag queues to avoid repeats and spread items out.
        self._script_queue = []  # list[int] indices into bump_scripts
        self._music_queue = []   # list[int] indices into music_files

        # Unified bump queue (complete bumps: script + chosen music + chosen outro).
        self._bump_queue = []  # list[dict]
        # 0 means "auto" (build as many complete bumps as feasible).
        self._bump_queue_size = 0

        # Exposure score tracking for bump components.
        # Complete bumps are transient; persistent scores live on components.
        # - scripts: {script_key: float}
        # - music:   {normalized_path: float}
        # - videos:  {normalized_path: float}
        # - outro:   {normalized_path: float}
        self.script_exposure_scores = {}
        self.music_exposure_scores = {}
        self.video_exposure_scores = {}
        self.outro_exposure_scores = {}

        # Install-time exposure seeds for certain bump music tracks.
        # Applied only when a track is first seen AND has no existing score.
        # Keyed by basename-without-extension, lowercased.
        self._seed_music_basenames = {
            'vibe1', 'vibe2', 'vibe3', 'vibe4',
            'chill1', 'chill2', 'chill3', 'chill4',
        }
        self._music_exposure_seeded_last_changed = False

        # Install-time exposure seeds for certain bump scripts.
        # Rule: scripts that cannot be compressed to fit within a 15s music clip
        # start with exposure score 1 (only if they have no existing score).
        self._script_exposure_seeded_last_changed = False

        # Recent history to reduce near-repeats when queues are rebuilt.
        # Rule: the 8 most recently used items cannot appear in the first 8 slots
        # of a newly rebuilt queue (best-effort).
        self._recent_spread_n = 8
        self._recent_script_indices = []   # list[int]
        self._recent_music_basenames = []  # list[str] lower

        # Video spacing (for bump videos).
        self._recent_video_basenames = []  # list[str] lower

        # Outro spacing (if outro_sounds is populated).
        self._outro_queue = []  # list[int] indices into outro_sounds
        self._recent_outro_basenames = []  # list[str] lower

        # Short bump rule: if bump duration is <= 15s, prefer music tracks <= 15s
        # (but still long enough for the bump) instead of using longer tracks.
        self._short_bump_s = 15.0

        # Hard UX rule: if there exist scripts that can fit a 15s clip, do not
        # allow non-15s-fit scripts to appear at the very start of a rebuilt
        # bump queue.
        # This is intentionally NOT based on exposure scores.
        self._early_short_only_slots = 4

        # Lazy indices for case-insensitive file resolution in user-selected folders.
        self._images_index_dir = None
        self._images_index = None
        self._fx_index_dir = None
        self._fx_index = None

        self._audio_exts = ('.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.opus', '.webm', '.mp4')
        self._image_exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif')
        self._video_exts = ('.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v')

        # Bump target cap for music fit. Even if music is longer than this, scripts
        # will not be stretched to fill it.
        self._bump_target_ms = 29_000
        # Kept for backward compatibility; the new strategy uses eligibility rules
        # rather than rejecting long scripts by a hard cap.
        self._bump_max_ms = 35_000

    def set_outro_sounds(self, paths):
        """Set the available outro sounds.

        `paths` should be an iterable of file paths.
        """
        out = []
        for p in list(paths or []):
            try:
                s = str(p or '').strip()
            except Exception:
                s = ''
            if s:
                out.append(s)
        self.outro_sounds = out
        # Clear transient queue so the next bump queue rebuild reselects.
        self._outro_queue = []
        self._bump_queue = []

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

    def seed_initial_music_exposure_scores(self, *, initial_score: float = 1.0) -> bool:
        """Seed initial exposure scores for selected bump music tracks.

        This is intentionally idempotent:
        - It only adds a score if the track is present in `music_files` AND the score key
          doesn't already exist.
        - It never overwrites existing values, so scores evolve naturally over time.
        """
        try:
            init = float(initial_score)
        except Exception:
            init = 1.0
        if init <= 0:
            init = 1.0

        changed = False
        targets = set(getattr(self, '_seed_music_basenames', set()) or set())
        if not targets:
            self._music_exposure_seeded_last_changed = False
            return False

        for entry in list(getattr(self, 'music_files', []) or []):
            p = ''
            try:
                if isinstance(entry, dict):
                    p = str(entry.get('path') or '').strip()
                else:
                    p = str(entry or '').strip()
            except Exception:
                p = ''
            if not p:
                continue

            try:
                base = os.path.splitext(os.path.basename(p))[0].strip().lower()
            except Exception:
                base = ''
            if not base or base not in targets:
                continue

            k = self._norm_path_key(p)
            if not k:
                continue

            if k in self.music_exposure_scores:
                continue

            try:
                self.music_exposure_scores[k] = float(init)
            except Exception:
                self.music_exposure_scores[k] = 1.0
            changed = True

        self._music_exposure_seeded_last_changed = bool(changed)
        return bool(changed)

    def seed_initial_script_exposure_scores(self, *, initial_score: float = 1.0, target_s: float | None = None) -> bool:
        """Seed initial exposure scores for scripts that cannot fit within a short clip.

        A script is considered a candidate for short-clip compression if it is eligible
        for a music duration of `target_s` (default: the 15s short bump rule).

        This is intentionally idempotent:
        - It only adds a score if the script is present in `bump_scripts` AND the score key
          doesn't already exist.
        - It never overwrites existing values.
        """
        try:
            init = float(initial_score)
        except Exception:
            init = 1.0
        if init <= 0:
            init = 1.0

        try:
            ts = float(target_s) if target_s is not None else float(self._short_bump_s)
        except Exception:
            ts = float(self._short_bump_s)
        if ts <= 0.0:
            ts = float(self._short_bump_s)

        target_ms = int(round(ts * 1000.0))
        changed = False

        for script in list(getattr(self, 'bump_scripts', []) or []):
            if not isinstance(script, dict):
                continue

            try:
                timing = script.get('_timing', None)
            except Exception:
                timing = None
            if not isinstance(timing, dict):
                try:
                    timing = self._analyze_script_timing(script)
                    script['_timing'] = timing
                except Exception:
                    timing = None

            if isinstance(timing, dict):
                try:
                    short_eps = float(getattr(self, '_short_bump_overage_tolerance', 0.533) or 0.533)
                except Exception:
                    short_eps = 0.533
                if self._can_fit_short_clip(timing, target_ms=int(target_ms), overage_tolerance=float(short_eps)):
                    # Short-clip candidate; do not seed.
                    continue

            k = self._script_key(script)
            if not k or k in self.script_exposure_scores:
                continue

            try:
                self.script_exposure_scores[k] = float(init)
            except Exception:
                self.script_exposure_scores[k] = 1.0
            changed = True

        self._script_exposure_seeded_last_changed = bool(changed)
        return bool(changed)

    def _script_key(self, script: dict) -> str:
        if not isinstance(script, dict):
            return ''
        try:
            k = script.get('_script_key', None)
        except Exception:
            k = None
        if k:
            return str(k)
        # Fallback: stable-ish key for in-memory scripts.
        return f"mem:{id(script)}"

    def apply_bump_exposure(self, bump_item: dict, *, delta: float = 100.0):
        """Apply exposure score to the bump components used by this bump."""
        if not isinstance(bump_item, dict):
            return
        try:
            d = float(delta)
        except Exception:
            d = 100.0

        script = bump_item.get('script')
        if isinstance(script, dict):
            k = self._script_key(script)
            if k:
                try:
                    self.script_exposure_scores[k] = float(self.script_exposure_scores.get(k, 0.0) or 0.0) + float(d)
                except Exception:
                    self.script_exposure_scores[k] = float(d)

        audio = bump_item.get('audio')
        if audio:
            mk = self._norm_path_key(str(audio))
            if mk:
                try:
                    self.music_exposure_scores[mk] = float(self.music_exposure_scores.get(mk, 0.0) or 0.0) + float(d)
                except Exception:
                    self.music_exposure_scores[mk] = float(d)

        video = bump_item.get('video')
        if video:
            vk = self._norm_path_key(str(video))
            if vk:
                try:
                    self.video_exposure_scores[vk] = float(self.video_exposure_scores.get(vk, 0.0) or 0.0) + float(d)
                except Exception:
                    self.video_exposure_scores[vk] = float(d)

        outro = bump_item.get('outro_audio_path')
        if outro:
            ok = self._norm_path_key(str(outro))
            if ok:
                try:
                    self.outro_exposure_scores[ok] = float(self.outro_exposure_scores.get(ok, 0.0) or 0.0) + float(d)
                except Exception:
                    self.outro_exposure_scores[ok] = float(d)

    def get_exposure_state(self) -> dict:
        """Return a JSON-serializable snapshot of bump component exposure scores."""
        try:
            return {
                'scripts': dict(self.script_exposure_scores or {}),
                'music': dict(self.music_exposure_scores or {}),
                'videos': dict(self.video_exposure_scores or {}),
                'outro': dict(self.outro_exposure_scores or {}),
            }
        except Exception:
            return {'scripts': {}, 'music': {}, 'videos': {}, 'outro': {}}

    def set_exposure_state(self, state: dict):
        """Restore exposure scores from a persisted snapshot (best-effort)."""
        if not isinstance(state, dict):
            return

        def _clean_map(m, norm_paths: bool = False):
            if not isinstance(m, dict):
                return {}
            out = {}
            for k, v in m.items():
                try:
                    kk = str(k)
                except Exception:
                    continue
                if norm_paths:
                    kk = self._norm_path_key(kk)
                try:
                    vv = float(v)
                except Exception:
                    continue
                if kk:
                    out[kk] = vv
            return out

        try:
            self.script_exposure_scores = _clean_map(state.get('scripts'), norm_paths=False)
        except Exception:
            self.script_exposure_scores = {}

        try:
            self.music_exposure_scores = _clean_map(state.get('music'), norm_paths=True)
        except Exception:
            self.music_exposure_scores = {}

        try:
            self.video_exposure_scores = _clean_map(state.get('videos'), norm_paths=True)
        except Exception:
            self.video_exposure_scores = {}

        try:
            self.outro_exposure_scores = _clean_map(state.get('outro'), norm_paths=True)
        except Exception:
            self.outro_exposure_scores = {}

    def _rebuild_outro_queue(self):
        self._outro_queue = self._build_queue_with_recent_exclusion(
            items=list(range(len(self.outro_sounds))),
            recent=list(self._recent_outro_basenames),
            n=int(self._recent_spread_n),
            key_fn=self._outro_queue_key,
        )

    def _outro_queue_key(self, idx):
        try:
            p = str(self.outro_sounds[int(idx)] or '')
            return os.path.basename(p).lower()
        except Exception:
            return ''

    def _note_recent_outro_path(self, path: str):
        try:
            name = os.path.basename(str(path or '')).lower()
            if not name:
                return
            self._recent_outro_basenames.append(name)
            self._recent_outro_basenames = self._recent_outro_basenames[-int(self._recent_spread_n):]
        except Exception:
            pass

    def _pick_outro_sound_from_queue(self):
        if not self.outro_sounds:
            return None
        if not self._outro_queue:
            self._rebuild_outro_queue()
        if not self._outro_queue:
            return None

        attempts = len(self._outro_queue)
        for _ in range(attempts):
            idx = self._outro_queue.pop(0)
            try:
                p = str(self.outro_sounds[int(idx)] or '')
            except Exception:
                p = ''
            if not p:
                continue
            self._note_recent_outro_path(p)
            return p
        return None

    def _script_needs_outro_audio(self, script: dict) -> bool:
        try:
            cards = script.get('cards') if isinstance(script, dict) else None
            if not isinstance(cards, list):
                return False
            for c in cards:
                if isinstance(c, dict) and bool(c.get('outro_audio', False)):
                    return True
        except Exception:
            return False
        return False

    def _analyze_script_timing(self, script: dict) -> dict:
        """Compute timing properties for a parsed script template.

        The template contains per-card base durations and duration modes. This
        analysis produces fixed/scalable aggregates used for music matching.
        """
        cards = script.get('cards') if isinstance(script, dict) else None
        if not isinstance(cards, list) or not cards:
            return {
                'fixed_ms': 0,
                'scalable_orig_ms': 0,
                'estimated_ms': 0,
                'min_possible_ms': 0,
                'scalable_cards': [],
            }

        fixed_ms = 0
        scalable_orig_ms = 0
        min_possible_ms = 0
        scalable_cards = []  # [{'idx': int, 't': float, 't_min': float, 'delta_ms': int, 'mode': str}]

        min_frac = float(self._min_scalable_fraction)

        for i, c in enumerate(cards):
            if not isinstance(c, dict):
                continue

            mode = str(c.get('_duration_mode', 'auto') or 'auto').lower()
            base_ms = c.get('_base_duration_ms', None)
            delta_ms = c.get('_delta_ms', 0) or 0

            # Pause and explicit fixed cards are fixed.
            if mode == 'fixed':
                try:
                    fixed_ms += int(c.get('duration', 0) or 0)
                except Exception:
                    fixed_ms += 0
                continue

            # For abs duration override, treat the whole card as fixed.
            if mode == 'abs':
                try:
                    fixed_ms += int(c.get('duration', 0) or 0)
                except Exception:
                    fixed_ms += 0
                continue

            # For delta and auto, the base portion is scalable.
            try:
                t = float(base_ms) if base_ms is not None else float(c.get('duration', 0) or 0)
            except Exception:
                t = float(c.get('duration', 0) or 0)
            if t < 0.0:
                t = 0.0
            t_min = t * min_frac
            r_delta = 0
            try:
                r_delta = int(delta_ms)
            except Exception:
                r_delta = 0

            # Deltas are fixed-time adjustments.
            fixed_ms += int(r_delta)
            scalable_orig_ms += int(round(t))
            min_possible_ms += int(round(t_min))
            scalable_cards.append({
                'idx': int(i),
                't': float(t),
                't_min': float(t_min),
                'delta_ms': int(r_delta),
                'mode': mode,
            })

        estimated_ms = int(fixed_ms) + int(scalable_orig_ms)
        min_possible_total_ms = int(fixed_ms) + int(min_possible_ms)

        return {
            'fixed_ms': int(fixed_ms),
            'scalable_orig_ms': int(scalable_orig_ms),
            'estimated_ms': int(estimated_ms),
            'min_possible_ms': int(min_possible_total_ms),
            'scalable_cards': scalable_cards,
        }

    def _script_can_fit_any_track(self, timing: dict) -> bool:
        """Return True if this script could possibly fit under the target cap."""
        try:
            fixed_ms = int(timing.get('fixed_ms', 0) or 0)
            min_possible_ms = int(timing.get('min_possible_ms', 0) or 0)
        except Exception:
            return False
        # Target is capped at 29s, so if even the minimum possible exceeds that,
        # the script can never fit any track.
        return fixed_ms <= int(self._bump_target_ms) and min_possible_ms <= int(self._bump_target_ms)

    def _is_music_eligible_for_script(self, timing: dict, music_duration_ms: int, *, overage_tolerance: float | None = None) -> bool:
        try:
            T_music = int(music_duration_ms)
        except Exception:
            return False
        if T_music <= 0:
            return False

        try:
            T_estimated = int(timing.get('estimated_ms', 0) or 0)
            min_possible = int(timing.get('min_possible_ms', 0) or 0)
        except Exception:
            return False

        try:
            eps = float(overage_tolerance) if overage_tolerance is not None else float(self._music_overage_tolerance)
        except Exception:
            eps = float(self._music_overage_tolerance)
        if float(T_estimated) > float(T_music) * (1.0 + eps):
            return False

        T_target = min(int(T_music), int(self._bump_target_ms))
        if int(min_possible) > int(T_target):
            return False

        return True

    def _can_fit_short_clip(self, timing: dict, *, target_ms: int, overage_tolerance: float) -> bool:
        """Return True if a script can be treated as 'short-clip compressible'.

        This is stricter than `_is_music_eligible_for_script`:
        - It enforces the short overage cap (e.g. <= ~23s estimated for a 15s target).
        - It requires the fitter to actually succeed at producing a <= target solution.
        """
        try:
            T_target = int(target_ms)
        except Exception:
            return False
        if T_target <= 0:
            return False

        try:
            T_estimated = int(timing.get('estimated_ms', 0) or 0)
            min_possible = int(timing.get('min_possible_ms', 0) or 0)
        except Exception:
            return False

        try:
            eps = float(overage_tolerance)
        except Exception:
            eps = float(getattr(self, '_short_bump_overage_tolerance', 0.533) or 0.533)

        # Respect the user's short-compression acceptance window (e.g. up to 23s).
        try:
            max_est_ms = int(round(float(T_target) * (1.0 + float(eps))))
        except Exception:
            max_est_ms = int(T_target)
        if int(T_estimated) > int(max_est_ms):
            return False

        # Must be feasible even at minimum scalable fraction.
        if int(min_possible) > int(min(int(T_target), int(self._bump_target_ms))):
            return False

        try:
            return self._fit_scalable_durations(timing, music_duration_ms=int(T_target)) is not None
        except Exception:
            return False

    def _fit_scalable_durations(self, timing: dict, music_duration_ms: int):
        """Return {card_index: fitted_base_ms} for scalable cards, or None if impossible."""
        try:
            T_music = int(music_duration_ms)
        except Exception:
            return None
        if T_music <= 0:
            return None

        T_target = min(int(T_music), int(self._bump_target_ms))
        fixed_ms = int(timing.get('fixed_ms', 0) or 0)
        scalable = list(timing.get('scalable_cards') or [])

        scalable_target = int(T_target) - int(fixed_ms)
        if scalable_target < 0:
            return None

        orig = {}
        cur = {}
        t_min = {}
        active = []
        for item in scalable:
            try:
                idx = int(item.get('idx'))
            except Exception:
                continue
            try:
                t = float(item.get('t', 0.0) or 0.0)
            except Exception:
                t = 0.0
            try:
                mn = float(item.get('t_min', 0.0) or 0.0)
            except Exception:
                mn = 0.0
            if t < 0.0:
                t = 0.0
            if mn < 0.0:
                mn = 0.0
            if mn > t:
                mn = t
            orig[idx] = t
            cur[idx] = t
            t_min[idx] = mn
            active.append(idx)

        scalable_orig = sum(cur.values())
        if scalable_orig <= float(scalable_target) + 0.0001:
            # No scaling required.
            return {idx: int(round(ms)) for idx, ms in cur.items()}

        delta = float(scalable_orig) - float(scalable_target)
        if delta <= 0.0:
            return {idx: int(round(ms)) for idx, ms in cur.items()}

        alpha = float(self._duration_normalization_exponent)
        k = float(self._duration_soft_clamp_k)
        remaining = float(delta)

        # Residual redistribution loop.
        for _ in range(64):
            if remaining <= 0.5:
                remaining = 0.0
                break
            if not active:
                break

            # Weight computation (power normalization).
            if abs(alpha) < 1e-9:
                weights = {idx: 1.0 for idx in active}
            else:
                weights = {}
                for idx in active:
                    v = orig.get(idx, 0.0)
                    try:
                        weights[idx] = float(v) ** float(alpha)
                    except Exception:
                        weights[idx] = 0.0

            sum_w = sum(weights.values())
            if sum_w <= 0.0:
                sum_w = float(len(active))
                weights = {idx: 1.0 for idx in active}

            total_r = 0.0
            saturated = []
            for idx in list(active):
                w = float(weights.get(idx, 0.0) or 0.0)
                r_ideal = remaining * (w / sum_w)

                r_max = float(cur.get(idx, 0.0) - t_min.get(idx, 0.0))
                if r_max <= 0.0:
                    saturated.append(idx)
                    continue

                x = float(r_ideal) / float(r_max)
                if x < 0.0:
                    x = 0.0

                r = r_max * (1.0 - math.exp(-k * x))
                if r > r_max:
                    r = r_max
                if r < 0.0:
                    r = 0.0

                cur[idx] = float(cur.get(idx, 0.0)) - float(r)
                if cur[idx] <= float(t_min.get(idx, 0.0)) + 0.5:
                    cur[idx] = float(t_min.get(idx, 0.0))
                    saturated.append(idx)

                total_r += float(r)

            if total_r <= 0.0001:
                break

            remaining -= float(total_r)
            if saturated:
                active = [i for i in active if i not in set(saturated)]

        if remaining > 1.0:
            # Not enough reduction capacity to fit into target.
            return None

        # Integer rounding: floor then distribute remainder by fractional parts.
        base = {}
        fracs = []
        mins = {}
        base_sum = 0
        for idx, v in cur.items():
            b = int(v)
            mn = int(round(t_min.get(idx, 0.0)))
            mins[idx] = mn
            if b < mn:
                b = mn
            base[idx] = b
            base_sum += b
            fracs.append((idx, float(v) - float(int(v))))

        remainder = int(scalable_target) - int(base_sum)
        if remainder > 0 and fracs:
            fracs.sort(key=lambda t: t[1], reverse=True)
            j = 0
            while remainder > 0:
                idx = fracs[j % len(fracs)][0]
                base[idx] += 1
                remainder -= 1
                j += 1

        if remainder < 0 and fracs:
            # We exceeded the target (typically due to min-duration clamps).
            # Subtract 1ms from cards (above their min) with the smallest fractional
            # part first (least rounding error impact).
            take = -remainder
            fracs.sort(key=lambda t: t[1])
            guard = 0
            while take > 0 and guard < 100000:
                guard += 1
                progressed = False
                for idx, _ in fracs:
                    if take <= 0:
                        break
                    mn = int(mins.get(idx, 0) or 0)
                    if int(base.get(idx, 0)) > mn:
                        base[idx] = int(base[idx]) - 1
                        take -= 1
                        progressed = True
                if not progressed:
                    # Not enough slack to hit the target exactly.
                    return None

        return {idx: int(ms) for idx, ms in base.items()}

    def _materialize_script_for_music(self, script: dict, music_duration_ms: int):
        """Return a new script dict with card durations fitted to music, or None."""
        timing = script.get('_timing') if isinstance(script, dict) else None
        if not isinstance(timing, dict):
            timing = self._analyze_script_timing(script)

        # For materialization we care about actual fit feasibility.
        # The estimated-duration overage check is only a heuristic for selection; if the
        # solver can compress the scalable cards into the target, allow it.
        try:
            T_music = int(music_duration_ms)
        except Exception:
            return None
        if T_music <= 0:
            return None
        T_target = min(int(T_music), int(self._bump_target_ms))
        try:
            min_possible = int(timing.get('min_possible_ms', 0) or 0)
        except Exception:
            min_possible = 0
        if int(min_possible) > int(T_target):
            return None

        fitted = self._fit_scalable_durations(timing, music_duration_ms=music_duration_ms)
        if fitted is None:
            return None

        cards = script.get('cards') if isinstance(script, dict) else None
        if not isinstance(cards, list) or not cards:
            return None

        out_cards = []
        total = 0
        for i, c in enumerate(cards):
            if not isinstance(c, dict):
                continue

            mode = str(c.get('_duration_mode', 'auto') or 'auto').lower()
            delta_ms = 0
            try:
                delta_ms = int(c.get('_delta_ms', 0) or 0)
            except Exception:
                delta_ms = 0

            if mode in {'fixed', 'abs'}:
                try:
                    d = int(c.get('duration', 0) or 0)
                except Exception:
                    d = 0
                if d < 1:
                    d = 1
            else:
                base_ms = fitted.get(int(i), None)
                if base_ms is None:
                    # If this card wasn't in the scalable list, treat it as fixed.
                    try:
                        base_ms = int(c.get('duration', 0) or 0)
                    except Exception:
                        base_ms = 0
                d = int(base_ms) + int(delta_ms)
                if d < 1:
                    d = 1

            nc = dict(c)
            nc['duration'] = int(d)
            # Remove internal timing helper keys from the materialized script.
            if '_base_duration_ms' in nc:
                del nc['_base_duration_ms']
            if '_delta_ms' in nc:
                del nc['_delta_ms']
            out_cards.append(nc)
            total += int(d)

        out = dict(script)
        out['cards'] = out_cards
        out['duration'] = int(total)
        # Remove template-only timing fields.
        if '_timing' in out:
            del out['_timing']
        return out

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

    def _rebuild_bump_queue(self):
        """Build a queue of complete bump items.

        Supports two bump types:
        - Music bumps: script + selected music track (+ optional outro audio)
        - Video bumps: script + selected video file (+ optional outro audio)

        New strategy:
        - Complete bumps are transient and assembled during queue generation.
        - Queue size is capped by the bottleneck component count.
        - Selection favors the least-exposed components (random among ties).
        """
        self._bump_queue = []

        if not self.bump_scripts:
            return

        video_script_indices = []
        audio_script_indices = []
        for i, s0 in enumerate(list(self.bump_scripts or [])):
            if not isinstance(s0, dict):
                continue
            vinfo = s0.get('video')
            is_video = False
            try:
                is_video = isinstance(vinfo, dict) and bool(str(vinfo.get('path') or '').strip())
            except Exception:
                is_video = False
            if is_video:
                video_script_indices.append(int(i))
            else:
                audio_script_indices.append(int(i))

        has_video = bool(video_script_indices)
        has_music = bool(self.music_files)

        # Nothing eligible.
        if (not has_video) and (not has_music or not audio_script_indices):
            return

        # Cap by configured target and available sources.
        try:
            target_cap = int(getattr(self, '_bump_queue_size', 0) or 0)
        except Exception:
            target_cap = 0

        max_audio = 0
        if has_music and audio_script_indices:
            # Allow reusing bump music across the queue (with spacing penalties),
            # so we can cover far more scripts than the raw number of music files.
            max_audio = int(len(audio_script_indices))
        max_video = int(len(video_script_indices))
        max_possible = int(max_audio) + int(max_video)
        if target_cap <= 0:
            target_cap = int(max_possible)
        max_n = min(int(target_cap), int(max_possible))
        if max_n <= 0:
            return

        # Update the public-ish size field so debugging/UI can reflect the true cap.
        self._bump_queue_size = int(max_n)

        # Start with all scripts in a single pool; music selection happens only for audio scripts.
        # If there is no music available, exclude audio scripts entirely.
        if has_music and audio_script_indices:
            script_pool = list(video_script_indices) + list(audio_script_indices)
        else:
            script_pool = list(video_script_indices)
        music_pool = list(range(len(self.music_files)))

        # Precompute which scripts can actually be compressed to fit within the short-bump duration.
        short_target_ms = int(round(float(self._short_bump_s) * 1000.0))
        try:
            short_eps = float(getattr(self, '_short_bump_overage_tolerance', 0.533) or 0.533)
        except Exception:
            short_eps = 0.533
        short_fit_scripts = set()
        try:
            for i, s0 in enumerate(list(self.bump_scripts or [])):
                if not isinstance(s0, dict):
                    continue
                timing0 = s0.get('_timing')
                if not isinstance(timing0, dict):
                    timing0 = self._analyze_script_timing(s0)
                    s0['_timing'] = dict(timing0)
                if self._can_fit_short_clip(timing0, target_ms=int(short_target_ms), overage_tolerance=float(short_eps)):
                    short_fit_scripts.add(int(i))
        except Exception:
            short_fit_scripts = set()

        # Temporary exposure/penalty maps for queue composition.
        # These are NOT persisted; they exist only to prevent repeats inside the
        # queue and across rebuild boundaries.
        temp_script_scores = dict(self.script_exposure_scores or {})
        temp_music_scores = dict(self.music_exposure_scores or {})
        temp_video_scores = dict(getattr(self, 'video_exposure_scores', {}) or {})
        temp_outro_scores = dict(self.outro_exposure_scores or {})
        temp_music_basename_penalty = {}

        try:
            recent_basenames = [str(x).lower() for x in list(self._recent_music_basenames or []) if str(x)]
        except Exception:
            recent_basenames = []
        recent_set = set(recent_basenames[-int(self._recent_spread_n):]) if recent_basenames else set()

        # Give recently-used music a strong penalty so it is very unlikely to be
        # picked early in a newly rebuilt queue.
        try:
            base_penalty = (max([float(v) for v in temp_music_scores.values()] + [0.0]) + 1000.0)
        except Exception:
            base_penalty = 1000.0

        if recent_set:
            for entry0 in list(self.music_files or []):
                entry = entry0 if isinstance(entry0, dict) else {'path': str(entry0)}
                p = str(entry.get('path') or '')
                if not p:
                    continue
                try:
                    bn = os.path.basename(p).lower()
                except Exception:
                    bn = ''
                if not bn or bn not in recent_set:
                    continue
                k = self._norm_path_key(p)
                if not k:
                    continue
                try:
                    temp_music_scores[k] = float(temp_music_scores.get(k, 0.0) or 0.0) + float(base_penalty)
                except Exception:
                    temp_music_scores[k] = float(base_penalty)

        def _script_score(idx: int) -> float:
            try:
                s = self.bump_scripts[int(idx)]
            except Exception:
                return 0.0
            k = self._script_key(s if isinstance(s, dict) else {})
            base = 0.0
            try:
                base = float(temp_script_scores.get(k, 0.0) or 0.0)
            except Exception:
                base = 0.0

            # Prefer scripts that can be compressed to fit the short bump duration.
            # This prevents long (~20s+) scripts from dominating the early queue.
            # Video bumps are excluded from this rule (they don't target music fit).
            try:
                vinfo = s.get('video') if isinstance(s, dict) else None
                is_video = isinstance(vinfo, dict) and bool(str(vinfo.get('path') or '').strip())
            except Exception:
                is_video = False

            if bool(is_video):
                # Video bumps also consider the exposure of the referenced video asset.
                try:
                    vp = str(vinfo.get('path') or '').strip() if isinstance(vinfo, dict) else ''
                except Exception:
                    vp = ''
                if vp:
                    try:
                        vk = self._norm_path_key(vp)
                    except Exception:
                        vk = vp
                    if vk:
                        try:
                            base = float(base) + float(temp_video_scores.get(vk, 0.0) or 0.0)
                        except Exception:
                            pass

            if not bool(is_video):
                try:
                    timing = s.get('_timing') if isinstance(s, dict) else None
                    if not isinstance(timing, dict):
                        timing = self._analyze_script_timing(s)
                        if isinstance(s, dict):
                            s['_timing'] = dict(timing)
                    can_fit_short = self._can_fit_short_clip(
                        timing,
                        target_ms=int(round(float(self._short_bump_s) * 1000.0)),
                        overage_tolerance=float(short_eps),
                    )
                    if not bool(can_fit_short):
                        base = float(base) + float(base_penalty)
                except Exception:
                    pass

            return float(base)

        def _music_score(idx: int) -> float:
            try:
                entry0 = self.music_files[int(idx)]
            except Exception:
                return 0.0
            entry = entry0 if isinstance(entry0, dict) else {'path': str(entry0)}
            p = str(entry.get('path') or '')
            k = self._norm_path_key(p)
            bn = ''
            try:
                bn = os.path.basename(p).lower()
            except Exception:
                bn = ''
            try:
                base = float(temp_music_scores.get(k, 0.0) or 0.0)
            except Exception:
                base = 0.0
            try:
                extra = float(temp_music_basename_penalty.get(bn, 0.0) or 0.0) if bn else 0.0
            except Exception:
                extra = 0.0
            return float(base) + float(extra)

        def _pick_min_exposure(indices, score_fn):
            if not indices:
                return None
            best_score = None
            ties = []
            for x in list(indices):
                try:
                    sc = float(score_fn(int(x)))
                except Exception:
                    sc = 0.0
                if best_score is None or sc < best_score:
                    best_score = sc
                    ties = [int(x)]
                elif abs(sc - float(best_score)) < 1e-9:
                    ties.append(int(x))
            if not ties:
                return None
            return random.choice(ties)

        def _music_duration_ms(entry: dict):
            dur_ms = entry.get('duration_ms', None)
            if dur_ms is None and entry.get('duration_s', None) is not None:
                try:
                    dur_ms = int(round(float(entry.get('duration_s')) * 1000.0))
                except Exception:
                    dur_ms = None
            return dur_ms

        # Avoid near-repeats in a single rebuild, but allow reuse so we can
        # build a long queue even with a small music library.
        used_music_basenames_recent = []  # list[str]
        try:
            music_spread_n = int(getattr(self, '_recent_spread_n', 8) or 8)
        except Exception:
            music_spread_n = 8
        if music_spread_n <= 0:
            music_spread_n = 8

        def _is_reserved_music_basename(name_lower: str) -> bool:
            """Return True if a basename is reserved for explicit script requests.

            Reserved tracks are excluded from auto-selection when a script uses
            `music=any`, but are allowed when a script explicitly requests them.
            """
            try:
                n = str(name_lower or '').strip().lower()
            except Exception:
                n = ''
            if not n:
                return False
            return n.startswith('xmas') or n.startswith('special')

        def _select_music_index_for_script(
            script: dict,
            pool_indices,
            *,
            avoid_basename: str | None = None,
            disallow_basenames: set | None = None,
        ):
            if not isinstance(script, dict) or not pool_indices:
                return None

            timing = script.get('_timing')
            if not isinstance(timing, dict):
                timing = self._analyze_script_timing(script)
                script['_timing'] = dict(timing)

            music_pref = str(script.get('music') or 'any').strip()

            # Prefer <=15s tracks whenever the script can actually be compressed to fit a 15s clip.
            prefer_short = False
            try:
                prefer_short = self._can_fit_short_clip(
                    timing,
                    target_ms=int(round(float(self._short_bump_s) * 1000.0)),
                    overage_tolerance=float(short_eps),
                )
            except Exception:
                prefer_short = False

            # Explicit track request.
            if music_pref and music_pref.lower() != 'any':
                want = music_pref.lower()
                # Hard rule: if a script specifies a music track, never substitute.
                # If the requested track can't be used (missing/ineligible), the
                # script must fail selection for this queue build.
                for idx in list(pool_indices):
                    entry0 = self.music_files[int(idx)]
                    entry = entry0 if isinstance(entry0, dict) else {'path': str(entry0)}
                    p = str(entry.get('path') or '')
                    if not p:
                        continue
                    if os.path.basename(p).lower() != want:
                        continue
                    # If the script explicitly requests the track, honor it even
                    # if it matches the avoid rule.
                    dur_ms = _music_duration_ms(entry)
                    if dur_ms is not None and not self._is_music_eligible_for_script(timing, music_duration_ms=int(dur_ms)):
                        return None
                    return int(idx)
                return None

            eligible = []
            eligible_short = []
            try:
                short_cap_ms = int(round(float(self._short_bump_s) * 1000.0)) + 750
            except Exception:
                short_cap_ms = 15_750
            for idx in list(pool_indices):
                entry0 = self.music_files[int(idx)]
                entry = entry0 if isinstance(entry0, dict) else {'path': str(entry0)}
                p = str(entry.get('path') or '')
                if not p:
                    continue

                try:
                    bn_here = os.path.basename(p).lower()
                except Exception:
                    bn_here = ''
                if disallow_basenames is not None and bn_here:
                    try:
                        if bn_here in set([str(x).lower() for x in disallow_basenames]):
                            continue
                    except Exception:
                        pass

                if avoid_basename:
                    try:
                        if os.path.basename(p).lower() == str(avoid_basename).lower():
                            continue
                    except Exception:
                        pass

                try:
                    name = os.path.basename(p).lower()
                    if _is_reserved_music_basename(name):
                        continue
                except Exception:
                    continue

                dur_ms = _music_duration_ms(entry)
                if dur_ms is None:
                    continue

                # For short tracks, allow compression (up to the short-clip overage window)
                # as long as the fitter can actually succeed.
                if prefer_short and int(dur_ms) <= int(short_cap_ms):
                    try:
                        T_est = int(timing.get('estimated_ms', 0) or 0)
                    except Exception:
                        T_est = 0
                    try:
                        max_est_ms = int(round(float(short_target_ms) * (1.0 + float(short_eps))))
                    except Exception:
                        max_est_ms = int(short_target_ms)
                    if int(T_est) > int(max_est_ms):
                        continue
                    if self._fit_scalable_durations(timing, music_duration_ms=int(dur_ms)) is None:
                        continue
                else:
                    if not self._is_music_eligible_for_script(timing, music_duration_ms=int(dur_ms)):
                        continue

                eligible.append(int(idx))
                if int(dur_ms) <= int(short_cap_ms):
                    eligible_short.append(int(idx))

            if prefer_short and eligible_short:
                return _pick_min_exposure(eligible_short, _music_score)
            if eligible:
                return _pick_min_exposure(eligible, _music_score)
            return None

        def _pick_outro_by_exposure():
            if not self.outro_sounds:
                return None
            best_score = None
            ties = []
            for p in list(self.outro_sounds or []):
                pk = self._norm_path_key(p)
                try:
                    sc = float(temp_outro_scores.get(pk, 0.0) or 0.0)
                except Exception:
                    sc = 0.0
                if best_score is None or sc < best_score:
                    best_score = sc
                    ties = [str(p)]
                elif abs(sc - float(best_score)) < 1e-9:
                    ties.append(str(p))
            if not ties:
                return None
            return random.choice(ties)

        # Prevent the first item of a rebuilt queue from repeating the most recent
        # bump's music (best-effort).
        last_music_basename = None
        try:
            if recent_basenames:
                last_music_basename = str(recent_basenames[-1] or '').lower() or None
        except Exception:
            last_music_basename = None

        # Temporary exposure delta applied when an item is added to the queue.
        # This makes subsequent picks within the same rebuild avoid repeats even
        # if there are duplicate tracks (e.g. same basename in different folders).
        queue_delta = float(base_penalty) if float(base_penalty) > 0 else 1000.0

        guard = max_n * 6
        # Queue build stats: helps diagnose "why some bumps never appear".
        stats = {
            'queue_target': int(max_n),
            'scripts_total': int(len(self.bump_scripts or [])),
            'scripts_audio': int(len(audio_script_indices)),
            'scripts_video': int(len(video_script_indices)),
            'music_total': int(len(self.music_files or [])),
            'skipped_audio_no_music_fit': 0,
            'skipped_audio_missing_or_ineligible': 0,
        }
        while len(self._bump_queue) < max_n and script_pool and guard > 0:
            guard -= 1

            # If there are any scripts that can fit a 15s clip, enforce that the
            # first few queue entries are chosen only from that short-fit set.
            # This prevents long/non-compressible scripts from ever being first
            # (or otherwise dominating the opening) just because their exposure
            # is lower than well-worn short scripts.
            candidate_scripts = list(script_pool)
            try:
                early_slots = int(getattr(self, '_early_short_only_slots', 0) or 0)
            except Exception:
                early_slots = 0
            if early_slots > 0 and short_fit_scripts and int(len(self._bump_queue)) < int(early_slots):
                # Short-only gate applies to music bumps; never exclude video bumps.
                filtered = [i for i in candidate_scripts if int(i) in short_fit_scripts]
                if filtered:
                    try:
                        vids = [i for i in candidate_scripts if int(i) in set(video_script_indices)]
                    except Exception:
                        vids = []
                    # Preserve both: all video scripts + short-fit audio scripts.
                    candidate_scripts = list(dict.fromkeys(list(vids) + list(filtered)))

            script_idx = _pick_min_exposure(candidate_scripts, _script_score)
            if script_idx is None:
                break
            try:
                script = self.bump_scripts[int(script_idx)]
            except Exception:
                script_pool = [i for i in script_pool if int(i) != int(script_idx)]
                continue
            if not isinstance(script, dict):
                script_pool = [i for i in script_pool if int(i) != int(script_idx)]
                continue

            vinfo = None
            is_video_bump = False
            try:
                vinfo = script.get('video') if isinstance(script, dict) else None
                is_video_bump = isinstance(vinfo, dict) and bool(str(vinfo.get('path') or '').strip())
            except Exception:
                vinfo = None
                is_video_bump = False

            # --- Video bump: no music selection required. ---
            if is_video_bump:
                materialized_script = self._materialize_script_without_music(script)
                if not materialized_script:
                    script_pool = [i for i in script_pool if int(i) != int(script_idx)]
                    continue

                item = {
                    'type': 'bump',
                    'script': materialized_script,
                    'video': str(vinfo.get('path') or ''),
                    'video_inclusive': bool(vinfo.get('inclusive', False)),
                }
                if self._script_needs_outro_audio(script):
                    outro_path = _pick_outro_by_exposure()
                    if outro_path:
                        item['outro_audio_path'] = str(outro_path)

                # Apply temporary exposure penalties.
                try:
                    sk = self._script_key(script)
                    if sk:
                        temp_script_scores[sk] = float(temp_script_scores.get(sk, 0.0) or 0.0) + float(queue_delta)
                except Exception:
                    pass
                try:
                    vp = str(vinfo.get('path') or '').strip() if isinstance(vinfo, dict) else ''
                except Exception:
                    vp = ''
                if vp:
                    try:
                        vk = self._norm_path_key(vp)
                    except Exception:
                        vk = vp
                    if vk:
                        try:
                            temp_video_scores[vk] = float(temp_video_scores.get(vk, 0.0) or 0.0) + float(queue_delta)
                        except Exception:
                            temp_video_scores[vk] = float(queue_delta)
                try:
                    op = item.get('outro_audio_path')
                    if op:
                        ok = self._norm_path_key(str(op))
                        if ok:
                            temp_outro_scores[ok] = float(temp_outro_scores.get(ok, 0.0) or 0.0) + float(queue_delta)
                except Exception:
                    pass

                self._bump_queue.append(item)
                script_pool = [i for i in script_pool if int(i) != int(script_idx)]
                continue

            # --- Music bump: requires music selection. ---
            if not music_pool:
                script_pool = [i for i in script_pool if int(i) != int(script_idx)]
                continue

            # Only disallow the most recently used basenames within this rebuild.
            disallow_recent = None
            try:
                if used_music_basenames_recent:
                    disallow_recent = set([str(x).lower() for x in used_music_basenames_recent[-int(music_spread_n):] if str(x)])
            except Exception:
                disallow_recent = None

            music_idx = _select_music_index_for_script(
                script,
                music_pool,
                avoid_basename=last_music_basename,
                disallow_basenames=disallow_recent,
            )
            if music_idx is None:
                # If the avoid rule blocked everything, retry once without it.
                music_idx = _select_music_index_for_script(
                    script,
                    music_pool,
                    avoid_basename=None,
                    disallow_basenames=disallow_recent,
                )

            if music_idx is None:
                # This script can't find any eligible music right now; drop it.
                try:
                    stats['skipped_audio_no_music_fit'] = int(stats.get('skipped_audio_no_music_fit', 0) or 0) + 1
                except Exception:
                    pass
                script_pool = [i for i in script_pool if int(i) != int(script_idx)]
                continue

            entry0 = self.music_files[int(music_idx)]
            entry = entry0 if isinstance(entry0, dict) else {'path': str(entry0)}
            audio_path = str(entry.get('path') or '')
            if not audio_path:
                music_pool = [i for i in music_pool if int(i) != int(music_idx)]
                continue

            try:
                last_music_basename = os.path.basename(audio_path).lower() or None
            except Exception:
                last_music_basename = None
            try:
                if last_music_basename:
                    used_music_basenames_recent.append(str(last_music_basename).lower())
            except Exception:
                pass

            dur_ms = _music_duration_ms(entry)
            if dur_ms is None:
                materialized_script = self._materialize_script_without_music(script)
            else:
                materialized_script = self._materialize_script_for_music(script, int(dur_ms))
            if not materialized_script:
                # If fitting fails, drop this music and retry later.
                music_pool = [i for i in music_pool if int(i) != int(music_idx)]
                continue

            item = {
                'type': 'bump',
                'script': materialized_script,
                'audio': str(audio_path),
            }
            if self._script_needs_outro_audio(script):
                outro_path = _pick_outro_by_exposure()
                if outro_path:
                    item['outro_audio_path'] = str(outro_path)

            # Apply temporary exposure penalties so subsequent items in this queue
            # strongly prefer different components.
            try:
                sk = self._script_key(script)
                if sk:
                    temp_script_scores[sk] = float(temp_script_scores.get(sk, 0.0) or 0.0) + float(queue_delta)
            except Exception:
                pass
            try:
                mk = self._norm_path_key(audio_path)
                if mk:
                    temp_music_scores[mk] = float(temp_music_scores.get(mk, 0.0) or 0.0) + float(queue_delta)
            except Exception:
                pass
            try:
                if last_music_basename:
                    temp_music_basename_penalty[last_music_basename] = float(temp_music_basename_penalty.get(last_music_basename, 0.0) or 0.0) + float(queue_delta)
            except Exception:
                pass
            try:
                op = item.get('outro_audio_path')
                if op:
                    ok = self._norm_path_key(str(op))
                    if ok:
                        temp_outro_scores[ok] = float(temp_outro_scores.get(ok, 0.0) or 0.0) + float(queue_delta)
            except Exception:
                pass

            self._bump_queue.append(item)
            script_pool = [i for i in script_pool if int(i) != int(script_idx)]
            # Allow reuse of music across the queue; penalties + disallow_recent
            # provide spacing without artificially capping queue length.

        try:
            stats['queue_built'] = int(len(self._bump_queue))
        except Exception:
            pass
        try:
            self._last_bump_queue_stats = dict(stats)
        except Exception:
            self._last_bump_queue_stats = None

    def get_random_bump(self):
        """
        Returns {'script': dict, 'audio': str} or None
        """
        # Backward-compatible public API: now draws from a unified bump queue.
        if not self._bump_queue:
            self._rebuild_bump_queue()
        if not self._bump_queue:
            return None

        try:
            item = self._bump_queue.pop(0)
        except Exception:
            return None

        # Track recent usage for spacing across queue rebuilds.
        try:
            if isinstance(item, dict):
                p = item.get('audio')
                if p:
                    self._note_recent_music_path(str(p))
                v = item.get('video')
                if v:
                    self._note_recent_video_path(str(v))
                op = item.get('outro_audio_path')
                if op:
                    self._note_recent_outro_path(str(op))
        except Exception:
            pass

        return item

    def _make_next_bump_item(self):
        # Legacy helper no longer used by the exposure-based queue builder.
        return None

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

    def _note_recent_video_path(self, path: str):
        try:
            name = os.path.basename(str(path or '')).lower()
            if not name:
                return
            self._recent_video_basenames.append(name)
            self._recent_video_basenames = self._recent_video_basenames[-int(self._recent_spread_n):]
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
        ms = float(ms) * float(self._duration_estimate_scale)
        ms = int(ms)
        if ms < self._min_card_ms:
            ms = self._min_card_ms
        if ms > self._max_card_ms:
            ms = self._max_card_ms
        return ms

    def _duration_from_audio_file_ms(self, path: str):
        """Return exact duration in ms via mutagen, or None."""
        p = str(path or '').strip()
        if not p:
            return None
        if _mutagen_file is None:
            return None
        try:
            audio = _mutagen_file(p)
            # Important: mutagen File objects can be falsy if they have no tags.
            # We care about the decoded stream info, not whether tags exist.
            if audio is None:
                return None
            info = getattr(audio, 'info', None)
            if info is None:
                return None
            length = getattr(info, 'length', None)
            if length is None:
                return None
            ms = int(round(float(length) * 1000.0))
            if ms <= 0:
                return None
            return ms
        except Exception:
            return None

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
        
    def load_bumps(self, folder_path, *, max_files: int | None = None, max_depth: int | None = None):
        """Load bump scripts from a folder.

        Optional bounds are used to avoid stalling on very large/slow external drives.
        - max_files: stop after parsing this many candidate script files
        - max_depth: stop descending deeper than this many subdirectory levels
        """
        self.bump_scripts = []

        folder_path = str(folder_path or '').strip()
        if not folder_path or not os.path.exists(folder_path):
            return

        try:
            max_files_v = int(max_files) if max_files is not None else None
            if max_files_v is not None and max_files_v <= 0:
                max_files_v = None
        except Exception:
            max_files_v = None

        try:
            max_depth_v = int(max_depth) if max_depth is not None else None
            if max_depth_v is not None and max_depth_v < 0:
                max_depth_v = None
        except Exception:
            max_depth_v = None

        parsed = 0
        base_depth = folder_path.rstrip(os.sep).count(os.sep)

        for root, dirs, files in os.walk(folder_path):
            # Depth limiting (best-effort).
            if max_depth_v is not None:
                depth = root.rstrip(os.sep).count(os.sep) - base_depth
                if depth >= max_depth_v:
                    dirs[:] = []

            for file in files:
                ext = os.path.splitext(file)[1].lower()
                # Many users store scripts without an extension (e.g. "script1").
                if ext in {'.txt', '.text', ''}:
                    self._parse_bump_file(os.path.join(root, file))
                    parsed += 1
                    if max_files_v is not None and parsed >= max_files_v:
                        dirs[:] = []
                        break

        # Seed script exposure defaults (one-time) for scripts that cannot be
        # compressed to fit the short bump duration.
        try:
            self.seed_initial_script_exposure_scores(initial_score=1.0)
        except Exception:
            self._script_exposure_seeded_last_changed = False

        # Scripts inventory changed; rebuild complete-bump queue lazily.
        self._bump_queue = []
                    
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
                # Stable-ish script identity for exposure scoring.
                try:
                    source_key = f"{os.path.normpath(str(filepath))}#bump{int(i)}"
                except Exception:
                    source_key = None
                self._parse_single_bump(body, header, base_dir=base_dir, source_key=source_key)
        except Exception as e:
            print(f"Error parsing {filepath}: {e}")

    def _parse_bump_music_pref(self, bump_header):
        if not bump_header:
            return 'any'

        # Supports:
        # - music=any
        # - music=myfile.mp3
        # - music="my file.mp3"
        # - music=my file.mp3   (unquoted; best-effort until next key= or tag end)
        m = re.search(
            r'music\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            bump_header,
            flags=re.IGNORECASE,
        )
        if m:
            # If the value was quoted, we can trust it.
            if (m.group(1) or m.group(2)):
                value = (m.group(1) or m.group(2) or '').strip()
                if value:
                    return value

            # If the value was unquoted, it may include spaces (e.g. music=special campfire.mp3).
            # Only accept the single-token capture if it looks like a complete value.
            token = (m.group(3) or '').strip()
            if token:
                try:
                    # If there is additional non-attribute text after the token before the tag ends,
                    # fall back to the space-tolerant parse below.
                    if re.search(r'\bmusic\s*=\s*' + re.escape(token) + r'\s+[^\s>]', str(bump_header), flags=re.IGNORECASE):
                        token = ''
                except Exception:
                    pass
            if token:
                return token

        # Fallback: handle unquoted values with spaces.
        # Example: <bump music=special campfire.mp3>
        try:
            s = str(bump_header)
            m2 = re.search(r'\bmusic\s*=\s*', s, flags=re.IGNORECASE)
            if not m2:
                return 'any'
            rest = s[m2.end():]
            rest = re.sub(r'>\s*$', '', rest).strip()

            # Stop before another attribute like " foo=bar".
            m3 = re.search(r'\s+\w[\w-]*\s*=', rest)
            if m3:
                rest = rest[:m3.start()].strip()

            if (rest.startswith('"') and rest.endswith('"')) or (rest.startswith("'") and rest.endswith("'")):
                rest = rest[1:-1].strip()

            return rest or 'any'
        except Exception:
            return 'any'

    def _parse_bump_video_pref(self, bump_header):
        if not bump_header:
            return None

        # Supports:
        # - video=clip.mp4
        # - video="clip name.mp4"
        # - video=clip name.mp4   (unquoted; best-effort)
        m = re.search(
            r'video\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
            bump_header,
            flags=re.IGNORECASE,
        )
        if m:
            if (m.group(1) or m.group(2)):
                value = (m.group(1) or m.group(2) or '').strip()
                return value or None

            token = (m.group(3) or '').strip()
            if token:
                try:
                    # If the next word is the standalone 'inclusive' flag, keep token.
                    # Otherwise, unquoted whitespace after the filename is ambiguous (likely a filename with spaces).
                    trailer = None
                    try:
                        s0 = str(bump_header)
                        mtrail = re.search(r'\bvideo\s*=\s*' + re.escape(token) + r'(?P<rest>[^>]*)', s0, flags=re.IGNORECASE)
                        if mtrail:
                            trailer = str(mtrail.group('rest') or '')
                    except Exception:
                        trailer = None

                    if trailer:
                        # Remove quoted segments from trailer.
                        try:
                            t = re.sub(r'"[^"]*"|\'[^\']*\'', '', trailer)
                        except Exception:
                            t = str(trailer)
                        # If there's extra stuff and it's not *exactly* the standalone
                        # inclusive flag, reject (unquoted filenames with spaces are ambiguous).
                        try:
                            rest_words = [w for w in re.split(r'\s+', str(t).strip()) if w]
                        except Exception:
                            rest_words = []

                        if rest_words:
                            if not (len(rest_words) == 1 and str(rest_words[0]).lower() == 'inclusive'):
                                token = ''
                except Exception:
                    pass
            if token:
                return token

        try:
            s = str(bump_header)
            m2 = re.search(r'\bvideo\s*=\s*', s, flags=re.IGNORECASE)
            if not m2:
                return None
            rest = s[m2.end():]
            rest = re.sub(r'>\s*$', '', rest).strip()

            # If the user wrote: video=clip.mp4 inclusive
            # treat 'inclusive' as a flag, not part of the filename.
            try:
                parts = [p for p in re.split(r'\s+', rest) if p]
            except Exception:
                parts = []
            if len(parts) >= 2:
                try:
                    if str(parts[-1]).lower() == 'inclusive':
                        candidate = " ".join(parts[:-1]).strip()
                        # Only accept if it's not an unquoted multi-word filename.
                        if candidate and (' ' not in candidate):
                            return candidate
                except Exception:
                    pass

            # If there are any remaining spaces here, it's an unquoted filename with spaces.
            # Require quotes for that; otherwise we'd misparse flags/attributes.
            try:
                if re.search(r'\s+', rest):
                    return None
            except Exception:
                pass

            # Stop before another attribute like " foo=bar".
            m3 = re.search(r'\s+\w[\w-]*\s*=', rest)
            if m3:
                rest = rest[:m3.start()].strip()

            if (rest.startswith('"') and rest.endswith('"')) or (rest.startswith("'") and rest.endswith("'")):
                rest = rest[1:-1].strip()

            return rest or None
        except Exception:
            return None

    def _parse_bump_inclusive_flag(self, bump_header) -> bool:
        if not bump_header:
            return False
        try:
            s = str(bump_header)
            # Remove quoted segments so a quoted word doesn't trigger.
            s = re.sub(r'"[^"]*"|\'[^\']*\'', '', s)
            return re.search(r'\binclusive\b', s, flags=re.IGNORECASE) is not None
        except Exception:
            return False

    def _parse_outro_text(self, outro_tag):
        default_text = '[sleepy shows]'
        if not outro_tag:
            return str(default_text)

        # Supports:
        # - <outro>
        # - <outro="[sleepy shows]">
        # - <outro="[sleepy shows]" audio>
        # - <outro='[sleepy shows]'>
        # - <outro=[sleepy shows]>
        # - <outro "[sleepy shows]" 0.6s>
        try:
            s = str(outro_tag).strip()
        except Exception:
            return str(default_text)

        # Prefer an explicitly quoted value anywhere in the tag.
        try:
            m = re.search(r'"([^"]*)"|\'([^\']*)\'', s)
        except Exception:
            m = None
        if m:
            try:
                value = (m.group(1) or m.group(2) or '').strip()
            except Exception:
                value = ''
            return value or str(default_text)

        # Fallback: take the payload inside the tag and strip known trailing args.
        m2 = None
        try:
            m2 = re.match(r'^\s*<\s*outro\b\s*([^>]*)>\s*$', s, flags=re.IGNORECASE)
        except Exception:
            m2 = None
        if not m2:
            return str(default_text)

        payload = (m2.group(1) or '').strip()
        if not payload:
            return str(default_text)

        if payload.startswith('='):
            payload = payload[1:].strip()

        # Remove a standalone trailing "audio" argument.
        try:
            payload = re.sub(r'\s+audio\s*$', '', payload, flags=re.IGNORECASE).strip()
        except Exception:
            payload = payload.strip()

        # Remove a trailing duration token (e.g. "400ms", "0.6s", "400").
        try:
            payload = re.sub(r'\s+\d+(?:\.\d+)?\s*(?:ms|s)?\s*$', '', payload, flags=re.IGNORECASE).strip()
        except Exception:
            payload = payload.strip()

        return payload or str(default_text)

    def _parse_outro_duration_ms(self, outro_tag):
        """Parse optional outro duration.

        Supported:
          - <outro>                     -> 800ms (default)
          - <outro 400ms>               -> 400
          - <outro "[sleepy]" 0.6s>     -> 600
          - <outro="[sleepy]" 400>     -> 400 (ms assumed)

        Notes:
          - Ignores quoted text segments.
          - Ignores the standalone "audio" argument.
        """
        default_ms = 800
        if not outro_tag:
            return int(default_ms)
        try:
            s = str(outro_tag)
        except Exception:
            return int(default_ms)

        try:
            s2 = re.sub(r'"[^"]*"|\'[^\']*\'', '', s)
        except Exception:
            s2 = s

        m = re.match(r'^\s*<\s*outro\b\s*([^>]*)>\s*$', s2, flags=re.IGNORECASE)
        if not m:
            return int(default_ms)

        payload = (m.group(1) or '').strip()
        if not payload:
            return int(default_ms)

        tokens = [t for t in re.split(r'\s+', payload) if t]
        best = None
        for t in tokens:
            tl = str(t).strip().lower()
            if not tl or tl == 'audio':
                continue
            if tl.startswith('='):
                tl = tl[1:].strip()
            tm = re.match(r'^(\d+(?:\.\d+)?)(ms|s)?$', tl)
            if not tm:
                continue
            try:
                v = float(tm.group(1))
                unit = (tm.group(2) or 'ms').lower()
                ms = int(round(v * 1000.0)) if unit == 's' else int(round(v))
                if ms < 0:
                    ms = abs(ms)
                best = int(ms)
            except Exception:
                continue

        return int(best) if best is not None else int(default_ms)

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

    def _resolve_bump_video_path(self, filename, base_dir=None):
        name = str(filename or '').strip().strip('"\'')
        if not name:
            return ''

        base_name = os.path.basename(name)
        root, ext = os.path.splitext(base_name)
        candidates = [name]
        if not ext:
            candidates = [root + e for e in self._video_exts]

        vid_dir = str(getattr(self, 'bump_videos_dir', None) or '').strip()
        if vid_dir:
            for cand in candidates:
                candidate = os.path.normpath(os.path.join(vid_dir, cand))
                if os.path.exists(candidate):
                    return candidate

            # Refresh-safe fallback: walk the folder if needed.
            try:
                for cand in candidates:
                    hit = self._find_case_insensitive(vid_dir, cand)
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
                # Count lines including intentional blank lines.
                # Use split('\n') (not splitlines()) so a trailing newline from a final
                # explicit blank line is preserved as an empty string element.
                cleaned = cleaned.replace('\r\n', '\n').replace('\r', '\n')
                raw_lines = cleaned.split('\n')
                # If the card is truly empty/whitespace-only, treat it as 0 lines.
                if str(cleaned).strip() == '':
                    info['lines_count'] = 0
                else:
                    info['lines_count'] = int(len(raw_lines))
            except Exception:
                info['lines_count'] = 0

        return info

    def _parse_single_bump(self, content, bump_header=None, base_dir=None, source_key=None):
        script = {
            'cards': [],
            'duration': 0,
            'music': self._parse_bump_music_pref(bump_header),
            'video': None,
            '_script_key': (str(source_key) if source_key else None),
        }

        # Optional bump video header.
        try:
            video_name = self._parse_bump_video_pref(bump_header)
        except Exception:
            video_name = None
        if video_name:
            try:
                resolved = self._resolve_bump_video_path(video_name, base_dir=base_dir)
            except Exception:
                resolved = str(video_name)
            script['video'] = {
                'filename': str(video_name),
                'path': str(resolved),
                'inclusive': bool(self._parse_bump_inclusive_flag(bump_header)),
            }
        
        # Split by tags but keep delimiters to process order
        # Regex to find <card>, <outro>, <pause...>
        # Simplification: We can iterate linewise or split by tags.
        # User format: <card>\nText...
        
        # Let's use a tokenizing approach
        # Tokens: <card>, <outro>, <pause=X>, <pause>
        
        tokens = re.split(r'(<(?:card\b[^>]*|outro\b[^>]*|pause\b[^>]*)>)', content, flags=re.IGNORECASE)
        
        # Store card body as a list of (line_text, is_explicit_blank).
        # is_explicit_blank is True when the line was authored using explicit
        # whitespace tags (<\t>, <\s>, <\n>) so it should be preserved even if
        # it is otherwise whitespace-only.
        current_card_text = []
        current_card_duration_spec = None
        in_outro = False

        def append_card_text_fragment(fragment: str):
            try:
                raw = '' if fragment is None else str(fragment)
            except Exception:
                raw = ''
            if raw == '':
                return

            # Normalize newlines, but preserve trailing empty lines.
            raw = raw.replace('\r\n', '\n').replace('\r', '\n')
            for ln in raw.split('\n'):
                try:
                    explicit_blank = ('<\\t>' in ln) or ('<\\s>' in ln) or ('<\\n>' in ln)
                except Exception:
                    explicit_blank = False

                try:
                    expanded = self._expand_whitespace_tags(ln)
                except Exception:
                    expanded = str(ln)

                # If the line is whitespace-only, treat it as a blank line.
                if str(expanded).strip() == '':
                    current_card_text.append(('', bool(explicit_blank)))
                else:
                    # Keep the user's spacing, but drop trailing whitespace.
                    current_card_text.append((str(expanded).rstrip(), False))
        
        def finalize_card():
            if current_card_text:
                # Trim incidental leading/trailing blank lines that come from
                # formatting around tags, but preserve explicit blank lines.
                lines = list(current_card_text)

                while lines and lines[0][0] == '' and not bool(lines[0][1]):
                    lines.pop(0)
                while lines and lines[-1][0] == '' and not bool(lines[-1][1]):
                    lines.pop()

                raw_text = "\n".join([t for (t, _explicit) in lines])

                # Important: allow intentionally blank cards.
                # If the user authored explicit whitespace tags (<\t>/<\s>/<\n>) or provided
                # an explicit duration (<card 4500ms>), we should keep the card even if the
                # resulting text is whitespace-only.
                has_explicit_blank = any(bool(explicit) for (_t, explicit) in lines)
                has_duration_spec = current_card_duration_spec is not None

                if raw_text.strip() or has_explicit_blank or has_duration_spec:
                    text = str(raw_text)
                    # Duration is based on character count (comprehension score).
                    # Do not include <img ...> or <sound ...> markup in the timing model.
                    timing_text = re.sub(r'<\s*(?:img|sound)\b[^>]*>', '', text, flags=re.IGNORECASE)
                    standard_duration = self._card_duration_ms_for_text(timing_text)
                    duration = int(standard_duration)

                    spec = current_card_duration_spec
                    duration_mode = 'auto'
                    base_duration_ms = int(standard_duration)
                    delta_ms = 0
                    if spec and isinstance(spec, tuple) and len(spec) == 2:
                        mode, val = spec
                        try:
                            if mode == 'abs':
                                duration = int(val)
                                duration_mode = 'abs'
                            elif mode == 'delta':
                                delta_ms = int(val)
                                duration = int(base_duration_ms) + int(delta_ms)
                                duration_mode = 'delta'
                        except Exception:
                            duration = int(base_duration_ms)
                            duration_mode = 'auto'

                    # Clamp only the readability-model baseline. Explicit abs and deltas are
                    # intended to be literal time adjustments.
                    base_duration_ms = int(self._clamp_card_duration_ms(base_duration_ms))
                    if duration_mode == 'auto':
                        duration = int(base_duration_ms)
                    elif duration_mode == 'delta':
                        duration = int(base_duration_ms) + int(delta_ms)

                    if duration < 1:
                        duration = 1

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

                            def _count_lines_preserve_trailing(s):
                                try:
                                    raw = str(s or '').replace('\r\n', '\n').replace('\r', '\n')
                                except Exception:
                                    raw = ''
                                if raw.strip() == '':
                                    return 0
                                # split('\n') preserves a final empty line when the string ends with '\n'
                                return len(raw.split('\n'))

                            def _display_text_preserve_blank_lines(s):
                                try:
                                    raw = str(s or '').replace('\r\n', '\n').replace('\r', '\n')
                                except Exception:
                                    raw = ''
                                if raw == '':
                                    return ''
                                parts = raw.split('\n')
                                # Convert blank lines to NBSP so QLabel renders the line height.
                                parts = [('\u00A0' if str(ln).strip() == '' else str(ln).rstrip()) for ln in parts]
                                return "\n".join(parts)

                            before_display = _display_text_preserve_blank_lines(before)
                            after_display = _display_text_preserve_blank_lines(after)

                            if str(img_info.get('mode')) == 'char':
                                template = _strip_sound_markup((text[:img_m.start()] or '')) + '[[IMG]]' + _strip_sound_markup((text[img_m.end():] or ''))
                                card_obj = {
                                    'type': 'img_char',
                                    'template': template,
                                    'image': img_info,
                                    'duration': duration,
                                    '_duration_mode': duration_mode,
                                    '_base_duration_ms': int(base_duration_ms),
                                    '_delta_ms': int(delta_ms),
                                }
                            else:
                                card_obj = {
                                    'type': 'img',
                                    # Preserve explicit blank lines (<\s> on its own line) so that
                                    # <img ... lines> can reserve stable line-height and avoid
                                    # image jumping between cards.
                                    'text_before': before_display,
                                    'text_after': after_display,
                                    'image': img_info,
                                    'before_lines': _count_lines_preserve_trailing(before),
                                    'after_lines': _count_lines_preserve_trailing(after),
                                    'duration': duration,
                                    '_duration_mode': duration_mode,
                                    '_base_duration_ms': int(base_duration_ms),
                                    '_delta_ms': int(delta_ms),
                                }

                            if sound_info:
                                card_obj['sound'] = sound_info

                            script['cards'].append(card_obj)

                            script['duration'] += duration
                            current_card_text.clear()
                            return

                    # Preserve blank lines visually by converting whitespace-only
                    # lines into NBSP so QLabel renders the line height.
                    display_raw = _strip_sound_markup(text)
                    display_lines = str(display_raw).replace('\r\n', '\n').replace('\r', '\n').split('\n')
                    display_lines = [('\u00A0' if ln.strip() == '' else ln.rstrip()) for ln in display_lines]
                    display_text = "\n".join(display_lines)

                    card_obj = {
                        'type': 'text',
                        'text': display_text,
                        'duration': duration,
                        '_duration_mode': duration_mode,
                        '_base_duration_ms': int(base_duration_ms),
                        '_delta_ms': int(delta_ms),
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
                duration = int(self._parse_outro_duration_ms(token_clean))
                script['cards'].append({
                    'type': 'text',
                    'text': text,
                    'duration': duration,
                    '_duration_mode': 'fixed',
                    'outro_audio': bool(outro_audio),
                    'is_outro': True,
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
                if token:
                    append_card_text_fragment(token)
        
        finalize_card()

        if script['cards']:
            timing = self._analyze_script_timing(script)
            script['_timing'] = dict(timing)
            # Estimated (pre-scaling) duration is used for heuristics and debugging.
            script['duration'] = int(timing.get('estimated_ms', 0) or 0)

            # Reject scripts that cannot possibly fit under the target cap, even at max scaling.
            # Video bumps don't require music fitting.
            try:
                vinfo = script.get('video') if isinstance(script, dict) else None
                is_video_bump = isinstance(vinfo, dict) and bool(str(vinfo.get('path') or '').strip())
            except Exception:
                is_video_bump = False

            if is_video_bump or self._script_can_fit_any_track(timing):
                self.bump_scripts.append(script)

    def scan_music(
        self,
        folder_path,
        *,
        recursive: bool = True,
        max_files: int | None = None,
        max_depth: int | None = None,
        time_budget_s: float | None = None,
        probe_durations: bool = True,
    ):
        """Scan for bump music files.

        Defaults are backward-compatible (recursive scan + duration probing).
        Startup can pass bounds to ensure scanning never stalls launch.
        """
        self.music_files = []

        folder_path = str(folder_path or '').strip()
        if not folder_path or not os.path.exists(folder_path):
            return

        audio_exts = set(self._audio_exts)

        try:
            max_files_v = int(max_files) if max_files is not None else None
            if max_files_v is not None and max_files_v <= 0:
                max_files_v = None
        except Exception:
            max_files_v = None

        try:
            max_depth_v = int(max_depth) if max_depth is not None else None
            if max_depth_v is not None and max_depth_v < 0:
                max_depth_v = None
        except Exception:
            max_depth_v = None

        try:
            budget_v = float(time_budget_s) if time_budget_s is not None else None
            if budget_v is not None and budget_v <= 0:
                budget_v = None
        except Exception:
            budget_v = None

        start = time.monotonic()
        scanned = 0

        def _add_file(full_path: str):
            nonlocal scanned
            dur_ms = None
            if probe_durations:
                dur_ms = self._duration_from_audio_file_ms(full_path)
            dur_s = (float(dur_ms) / 1000.0) if dur_ms is not None else self._duration_from_music_filename(full_path)
            self.music_files.append({'path': full_path, 'duration_ms': dur_ms, 'duration_s': dur_s})
            scanned += 1

        def _time_exceeded() -> bool:
            if budget_v is None:
                return False
            return (time.monotonic() - start) >= budget_v

        if not recursive:
            try:
                with os.scandir(folder_path) as it:
                    for entry in it:
                        if _time_exceeded():
                            break
                        if max_files_v is not None and scanned >= max_files_v:
                            break
                        try:
                            if not entry.is_file():
                                continue
                            p = entry.path
                            if os.path.splitext(p)[1].lower() in audio_exts:
                                _add_file(p)
                        except Exception:
                            continue
            except Exception:
                pass
        else:
            base_depth = folder_path.rstrip(os.sep).count(os.sep)
            for root, dirs, files in os.walk(folder_path):
                if _time_exceeded():
                    break

                if max_depth_v is not None:
                    depth = root.rstrip(os.sep).count(os.sep) - base_depth
                    if depth >= max_depth_v:
                        dirs[:] = []

                for f in files:
                    if _time_exceeded():
                        break
                    if max_files_v is not None and scanned >= max_files_v:
                        dirs[:] = []
                        break
                    if os.path.splitext(f)[1].lower() in audio_exts:
                        _add_file(os.path.join(root, f))

        # Music inventory changed; rebuild complete-bump queue lazily.
        # Also seed initial exposure scores for selected "starter" tracks.
        try:
            self.seed_initial_music_exposure_scores(initial_score=1.0)
        except Exception:
            self._music_exposure_seeded_last_changed = False
        self._bump_queue = []

    def scan_bump_videos(
        self,
        folder_path,
        *,
        recursive: bool = True,
        max_files: int | None = None,
        max_depth: int | None = None,
        time_budget_s: float | None = None,
        probe_durations: bool = True,
    ):
        """Scan for bump video assets and (optionally) probe exact durations.

        Durations are cached into `video_durations_ms` using normalized absolute paths.

        Probing strategy:
        - Prefer `ffprobe` if available (fast, deterministic).
        - Fall back to a minimal headless python-mpv probe if ffprobe is missing.
        """
        folder_path = str(folder_path or '').strip()
        if not folder_path or not os.path.isdir(folder_path):
            return

        try:
            self.bump_videos_dir = folder_path
        except Exception:
            pass

        video_exts = set(getattr(self, '_video_exts', ('.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v')))

        try:
            max_files_v = int(max_files) if max_files is not None else None
            if max_files_v is not None and max_files_v <= 0:
                max_files_v = None
        except Exception:
            max_files_v = None

        try:
            max_depth_v = int(max_depth) if max_depth is not None else None
            if max_depth_v is not None and max_depth_v < 0:
                max_depth_v = None
        except Exception:
            max_depth_v = None

        try:
            budget_v = float(time_budget_s) if time_budget_s is not None else None
            if budget_v is not None and budget_v <= 0:
                budget_v = None
        except Exception:
            budget_v = None

        start = time.monotonic()
        scanned = 0

        def _time_exceeded() -> bool:
            if budget_v is None:
                return False
            return (time.monotonic() - start) >= budget_v

        def _ffprobe_duration_ms(path: str) -> int | None:
            # ffprobe -v error -select_streams v:0 -show_entries format=duration -of json <file>
            try:
                res = subprocess.run(
                    [
                        'ffprobe',
                        '-v',
                        'error',
                        '-show_entries',
                        'format=duration',
                        '-of',
                        'json',
                        str(path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            except Exception:
                return None
            if res.returncode != 0:
                return None
            try:
                payload = json.loads(res.stdout or '{}')
                fmt = payload.get('format') if isinstance(payload, dict) else None
                dur_s = None
                if isinstance(fmt, dict):
                    dur_s = fmt.get('duration')
                if dur_s is None:
                    return None
                dur = float(dur_s)
                if dur <= 0:
                    return None
                return int(round(dur * 1000.0))
            except Exception:
                return None

        def _mpv_duration_ms(path: str, *, timeout_s: float = 2.5) -> int | None:
            # Best-effort headless probe; slower than ffprobe.
            try:
                import mpv  # type: ignore
            except Exception:
                return None

            player = None
            try:
                player = mpv.MPV(
                    input_default_bindings=False,
                    input_vo_keyboard=False,
                    osc=False,
                    vo='null',
                    ao='null',
                )
                try:
                    player.keep_open = 'no'
                except Exception:
                    pass
                try:
                    player.pause = True
                except Exception:
                    pass
                try:
                    player.play(str(path))
                except Exception:
                    return None

                t0 = time.monotonic()
                while (time.monotonic() - t0) < float(timeout_s):
                    try:
                        d = getattr(player, 'duration', None)
                    except Exception:
                        d = None
                    if d is not None:
                        try:
                            dur = float(d)
                            if dur > 0:
                                return int(round(dur * 1000.0))
                        except Exception:
                            pass
                    time.sleep(0.03)
                return None
            finally:
                try:
                    if player is not None:
                        player.terminate()
                except Exception:
                    pass

        def _probe_duration_ms(path: str) -> int | None:
            if not probe_durations:
                return None

            d = _ffprobe_duration_ms(path)
            if d is not None:
                return int(d)
            return _mpv_duration_ms(path)

        def _note(path: str):
            nonlocal scanned
            try:
                ap = os.path.abspath(str(path))
            except Exception:
                ap = str(path)
            k = self._norm_path_key(ap)
            if not k:
                return
            if k in (self.video_durations_ms or {}):
                return

            dur_ms = _probe_duration_ms(ap)
            if dur_ms is not None:
                try:
                    self.video_durations_ms[k] = int(dur_ms)
                except Exception:
                    self.video_durations_ms[k] = int(dur_ms)
            scanned += 1

        if not recursive:
            try:
                with os.scandir(folder_path) as it:
                    for entry in it:
                        if _time_exceeded():
                            break
                        if max_files_v is not None and scanned >= max_files_v:
                            break
                        try:
                            if not entry.is_file():
                                continue
                            p = entry.path
                            if os.path.splitext(p)[1].lower() in video_exts:
                                _note(p)
                        except Exception:
                            continue
            except Exception:
                pass
        else:
            base_depth = folder_path.rstrip(os.sep).count(os.sep)
            for root, dirs, files in os.walk(folder_path):
                if _time_exceeded():
                    break

                if max_depth_v is not None:
                    depth = root.rstrip(os.sep).count(os.sep) - base_depth
                    if depth >= max_depth_v:
                        dirs[:] = []

                for f in files:
                    if _time_exceeded():
                        break
                    if max_files_v is not None and scanned >= max_files_v:
                        dirs[:] = []
                        break
                    if os.path.splitext(f)[1].lower() in video_exts:
                        _note(os.path.join(root, f))


    def _iter_music_entries(self):
        # Backward compatibility: allow either dict entries or raw paths.
        for entry in self.music_files:
            if isinstance(entry, dict):
                p = entry.get('path')
                d_s = entry.get('duration_s', None)
                d_ms = entry.get('duration_ms', None)
                yield {'path': str(p), 'duration_s': d_s, 'duration_ms': d_ms}
            else:
                p = str(entry)
                d_s = self._duration_from_music_filename(p)
                d_ms = int(round(float(d_s) * 1000.0)) if d_s is not None else None
                yield {'path': p, 'duration_s': d_s, 'duration_ms': d_ms}

    def _find_music_by_basename(self, basename_lower):
        for entry in self._iter_music_entries():
            p = entry.get('path')
            if not p:
                continue
            if os.path.basename(str(p)).lower() == basename_lower:
                return entry
        return None

    def _is_music_entry_eligible(self, entry, min_duration_s, allow_xmas=False, max_duration_s=None):
        """Legacy helper kept for older internal callers.

        The new algorithm uses exact ms durations + script-based eligibility.
        This helper only checks length constraints.
        """
        try:
            dur_s = entry.get('duration_s', None)
            if dur_s is None:
                dur_ms = entry.get('duration_ms', None)
                if dur_ms is not None:
                    dur_s = float(dur_ms) / 1000.0
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
                if name.startswith('xmas') or name.startswith('special'):
                    return False
            except Exception:
                return False

        return True

    def _materialize_script_without_music(self, script: dict):
        """Return a new script dict using the template's pre-scaling durations."""
        if not isinstance(script, dict):
            return None
        cards = script.get('cards')
        if not isinstance(cards, list) or not cards:
            return None

        out_cards = []
        total = 0
        for c in cards:
            if not isinstance(c, dict):
                continue
            d = 0
            try:
                d = int(c.get('duration', 0) or 0)
            except Exception:
                d = 0
            if d < 1:
                d = 1
            nc = dict(c)
            nc['duration'] = int(d)
            if '_base_duration_ms' in nc:
                del nc['_base_duration_ms']
            if '_delta_ms' in nc:
                del nc['_delta_ms']
            out_cards.append(nc)
            total += int(d)

        out = dict(script)
        out['cards'] = out_cards
        out['duration'] = int(total)
        if '_timing' in out:
            del out['_timing']
        return out

    def _pick_music_entry_for_script(self, script: dict):
        """Pick a music entry for a script using eligibility + queue spacing.

        Returns a dict: {'path': str, 'duration_ms': int|None, 'duration_s': float|None}
        or None.
        """
        if not isinstance(script, dict) or not self.music_files:
            return None

        timing = script.get('_timing')
        if not isinstance(timing, dict):
            timing = self._analyze_script_timing(script)
            script['_timing'] = dict(timing)

        music_pref = str(script.get('music') or 'any').strip()
        if music_pref and music_pref.lower() != 'any':
            entry = self._find_music_by_basename(music_pref.lower())
            if not entry:
                return None
            dur_ms = entry.get('duration_ms', None)
            if dur_ms is None:
                try:
                    dur_ms = self._duration_from_audio_file_ms(str(entry.get('path') or ''))
                except Exception:
                    dur_ms = None
            if dur_ms is None and entry.get('duration_s', None) is not None:
                try:
                    dur_ms = int(round(float(entry.get('duration_s')) * 1000.0))
                except Exception:
                    dur_ms = None
            if dur_ms is None:
                # Best-effort: still allow explicitly requested tracks.
                p = str(entry.get('path') or '')
                if p:
                    self._note_recent_music_path(p)
                return {'path': p, 'duration_ms': None, 'duration_s': entry.get('duration_s', None)}
            if not self._is_music_eligible_for_script(timing, music_duration_ms=int(dur_ms)):
                return None
            p = str(entry.get('path') or '')
            if p:
                self._note_recent_music_path(p)
            return {'path': p, 'duration_ms': int(dur_ms), 'duration_s': entry.get('duration_s', None)}

        # music=any: use queue selection.
        if not self._music_queue:
            self._rebuild_music_queue()
        if not self._music_queue:
            return None

        # Prefer <=15s tracks for short scripts (based on pre-scaling estimate).
        prefer_short = False
        try:
            prefer_short = float(timing.get('estimated_ms', 0) or 0) <= float(self._short_bump_s) * 1000.0
        except Exception:
            prefer_short = False

        def _try_pick(max_ms=None):
            attempts = len(self._music_queue)
            for _ in range(attempts):
                idx = self._music_queue.pop(0)
                entry0 = self.music_files[idx]
                entry = entry0 if isinstance(entry0, dict) else {'path': str(entry0)}
                p = str(entry.get('path') or '')
                if not p:
                    self._music_queue.append(idx)
                    continue

                name = os.path.basename(p).lower()
                if name.startswith('xmas') or name.startswith('special'):
                    self._music_queue.append(idx)
                    continue

                dur_ms = entry.get('duration_ms', None)
                if dur_ms is None and entry.get('duration_s', None) is not None:
                    try:
                        dur_ms = int(round(float(entry.get('duration_s')) * 1000.0))
                    except Exception:
                        dur_ms = None
                if dur_ms is None:
                    self._music_queue.append(idx)
                    continue

                if max_ms is not None and int(dur_ms) > int(max_ms):
                    self._music_queue.append(idx)
                    continue

                if not self._is_music_eligible_for_script(timing, music_duration_ms=int(dur_ms)):
                    self._music_queue.append(idx)
                    continue

                # Consume it to maximize spacing.
                self._note_recent_music_path(p)
                return {'path': p, 'duration_ms': int(dur_ms), 'duration_s': entry.get('duration_s', None)}
            return None

        if prefer_short:
            picked = _try_pick(max_ms=int(round(float(self._short_bump_s) * 1000.0)))
            if picked:
                return picked
        return _try_pick(max_ms=None)

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
                    if name.startswith('xmas') or name.startswith('special'):
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
                if name.startswith('xmas') or name.startswith('special'):
                    continue

            candidates.append({'path': str(path), 'duration_s': float(dur_s)})

        return candidates

    def get_next_bump(self):
        """Preferred API: returns the next complete bump item."""
        return self.get_random_bump()

    def warm_bump_queue(self, *, max_items: int = 6, time_budget_s: float = 1.5):
        """Best-effort: prefill a small number of complete bump items quickly.

        This is used during startup to reduce the first-bump latency, but it must
        never block app launch indefinitely.
        """
        try:
            max_items_v = int(max_items)
        except Exception:
            max_items_v = 6
        if max_items_v <= 0:
            return

        try:
            budget_v = float(time_budget_s)
        except Exception:
            budget_v = 1.5
        if budget_v <= 0:
            budget_v = 0.25

        # Ensure underlying spaced queues exist.
        try:
            if not self._script_queue:
                self._rebuild_script_queue()
        except Exception:
            pass
        try:
            if self.music_files and not self._music_queue:
                self._rebuild_music_queue()
        except Exception:
            pass
        try:
            if self.outro_sounds and not self._outro_queue:
                self._rebuild_outro_queue()
        except Exception:
            pass

        target = max_items_v
        try:
            target = min(target, int(self._bump_queue_size) if int(self._bump_queue_size) > 0 else target)
        except Exception:
            pass

        start = time.monotonic()
        while len(self._bump_queue) < target:
            if (time.monotonic() - start) >= budget_v:
                break
            item = None
            try:
                item = self._make_next_bump_item()
            except Exception:
                item = None
            if not item:
                break
            self._bump_queue.append(item)

