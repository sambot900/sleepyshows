"""Pure parsing functions for bump script .txt files.

This module contains the stateless parsing logic extracted from BumpManager.
All functions here are pure — they take inputs and return outputs with no
side effects or instance state.
"""

import re
import shlex
from collections import namedtuple


# ---------------------------------------------------------------------------
# Timing configuration — carries the card duration model constants.
# ---------------------------------------------------------------------------

TimingConfig = namedtuple('TimingConfig', [
    'ms_per_char',
    'ms_per_char_scale',
    'base_card_ms',
    'one_line_bonus_ms',
    'min_card_ms',
    'max_card_ms',
    'duration_scale',
    'duration_estimate_scale',
    'min_scalable_fraction',
])

DEFAULT_TIMING = TimingConfig(
    ms_per_char=41,
    ms_per_char_scale=1.15,
    base_card_ms=550,
    one_line_bonus_ms=800,
    min_card_ms=900,
    max_card_ms=6000,
    duration_scale=1.26,
    duration_estimate_scale=1.0,
    min_scalable_fraction=0.40,
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalize_card_text(text):
    """Collapse whitespace for stable character counting."""
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def is_single_line_card(text):
    """True if *text* contains at most one non-empty line."""
    raw = str(text or '').strip()
    if not raw:
        return True
    non_empty_lines = [ln for ln in raw.splitlines() if ln.strip()]
    return len(non_empty_lines) <= 1


def expand_whitespace_tags(text):
    r"""Replace explicit whitespace tags in bump scripts.

    ``<\s>`` → space, ``<\t>`` → tab, ``<\n>`` → newline.
    """
    if text is None:
        return ''
    s = str(text)
    s = s.replace('<\\s>', ' ')
    s = s.replace('<\\t>', '\t')
    s = s.replace('<\\n>', '\n')
    return s


# ---------------------------------------------------------------------------
# Card duration model
# ---------------------------------------------------------------------------

def card_duration_ms(text, cfg=DEFAULT_TIMING):
    """Estimate display duration (ms) for a text card."""
    single = is_single_line_card(text)
    t = normalize_card_text(text)
    chars = len(t)
    ms = (cfg.base_card_ms + (chars * cfg.ms_per_char * float(cfg.ms_per_char_scale))) * float(cfg.duration_scale)
    if single:
        ms += int(cfg.one_line_bonus_ms)
    ms = float(ms) * float(cfg.duration_estimate_scale)
    ms = int(ms)
    if ms < cfg.min_card_ms:
        ms = cfg.min_card_ms
    if ms > cfg.max_card_ms:
        ms = cfg.max_card_ms
    return ms


def clamp_card_duration(ms, cfg=DEFAULT_TIMING):
    """Clamp *ms* to the configured [min, max] card range."""
    try:
        ms = int(ms)
    except Exception:
        ms = int(cfg.min_card_ms)
    if ms < int(cfg.min_card_ms):
        ms = int(cfg.min_card_ms)
    if ms > int(cfg.max_card_ms):
        ms = int(cfg.max_card_ms)
    return int(ms)


# ---------------------------------------------------------------------------
# Bump header parsers — operate on the ``<bump …>`` opening tag text.
# ---------------------------------------------------------------------------

def parse_bump_music_pref(bump_header):
    """Return ``'any'`` or an explicit music filename from a bump header."""
    if not bump_header:
        return 'any'

    m = re.search(
        r'music\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
        bump_header,
        flags=re.IGNORECASE,
    )
    if m:
        if m.group(1) or m.group(2):
            value = (m.group(1) or m.group(2) or '').strip()
            if value:
                return value

        token = (m.group(3) or '').strip()
        if token:
            if re.search(r'\bmusic\s*=\s*' + re.escape(token) + r'\s+[^\s>]',
                         str(bump_header), flags=re.IGNORECASE):
                token = ''
        if token:
            return token

    # Fallback: unquoted values with spaces.
    s = str(bump_header)
    m2 = re.search(r'\bmusic\s*=\s*', s, flags=re.IGNORECASE)
    if not m2:
        return 'any'
    rest = s[m2.end():]
    rest = re.sub(r'>\s*$', '', rest).strip()

    m3 = re.search(r'\s+\w[\w-]*\s*=', rest)
    if m3:
        rest = rest[:m3.start()].strip()

    if (rest.startswith('"') and rest.endswith('"')) or (rest.startswith("'") and rest.endswith("'")):
        rest = rest[1:-1].strip()

    return rest or 'any'


def parse_bump_video_pref(bump_header):
    """Return a video filename from a bump header, or ``None``."""
    if not bump_header:
        return None

    m = re.search(
        r'video\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
        bump_header,
        flags=re.IGNORECASE,
    )
    if m:
        if m.group(1) or m.group(2):
            value = (m.group(1) or m.group(2) or '').strip()
            return value or None

        token = (m.group(3) or '').strip()
        if token:
            trailer = None
            s0 = str(bump_header)
            mtrail = re.search(
                r'\bvideo\s*=\s*' + re.escape(token) + r'(?P<rest>[^>]*)',
                s0, flags=re.IGNORECASE,
            )
            if mtrail:
                trailer = str(mtrail.group('rest') or '')

            if trailer:
                t = re.sub(r'"[^"]*"|\'[^\']*\'', '', trailer)
                rest_words = [w for w in re.split(r'\s+', str(t).strip()) if w]
                if rest_words:
                    if not (len(rest_words) == 1 and str(rest_words[0]).lower() == 'inclusive'):
                        token = ''
        if token:
            return token

    s = str(bump_header)
    m2 = re.search(r'\bvideo\s*=\s*', s, flags=re.IGNORECASE)
    if not m2:
        return None
    rest = s[m2.end():]
    rest = re.sub(r'>\s*$', '', rest).strip()

    parts = [p for p in re.split(r'\s+', rest) if p]
    if len(parts) >= 2:
        if str(parts[-1]).lower() == 'inclusive':
            candidate = " ".join(parts[:-1]).strip()
            if candidate and ' ' not in candidate:
                return candidate

    if re.search(r'\s+', rest):
        return None

    m3 = re.search(r'\s+\w[\w-]*\s*=', rest)
    if m3:
        rest = rest[:m3.start()].strip()

    if (rest.startswith('"') and rest.endswith('"')) or (rest.startswith("'") and rest.endswith("'")):
        rest = rest[1:-1].strip()

    return rest or None


def parse_bump_inclusive_flag(bump_header):
    """Return ``True`` if the bump header contains ``inclusive``."""
    if not bump_header:
        return False
    s = re.sub(r'"[^"]*"|\'[^\']*\'', '', str(bump_header))
    return re.search(r'\binclusive\b', s, flags=re.IGNORECASE) is not None


# ---------------------------------------------------------------------------
# ``<outro>`` tag parsers
# ---------------------------------------------------------------------------

def parse_outro_text(outro_tag):
    """Extract display text from an ``<outro …>`` tag."""
    default_text = '[sleepy shows]'
    if not outro_tag:
        return str(default_text)

    s = str(outro_tag).strip()

    # Prefer an explicitly quoted value.
    m = re.search(r'"([^"]*)"|\'([^\']*)\'', s)
    if m:
        value = (m.group(1) or m.group(2) or '').strip()
        return value or str(default_text)

    # Fallback: payload inside the tag.
    m2 = re.match(r'^\s*<\s*outro\b\s*([^>]*)>\s*$', s, flags=re.IGNORECASE)
    if not m2:
        return str(default_text)

    payload = (m2.group(1) or '').strip()
    if not payload:
        return str(default_text)

    if payload.startswith('='):
        payload = payload[1:].strip()

    payload = re.sub(r'\s+audio\s*$', '', payload, flags=re.IGNORECASE).strip()
    payload = re.sub(r'\s+\d+(?:\.\d+)?\s*(?:ms|s)?\s*$', '', payload, flags=re.IGNORECASE).strip()

    return payload or str(default_text)


def parse_outro_duration_ms(outro_tag):
    """Parse optional duration from an ``<outro>`` tag (default 800 ms)."""
    default_ms = 800
    if not outro_tag:
        return int(default_ms)

    s = str(outro_tag)
    s2 = re.sub(r'"[^"]*"|\'[^\']*\'', '', s)

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
        v = float(tm.group(1))
        unit = (tm.group(2) or 'ms').lower()
        ms = int(round(v * 1000.0)) if unit == 's' else int(round(v))
        if ms < 0:
            ms = abs(ms)
        best = int(ms)

    return int(best) if best is not None else int(default_ms)


def parse_outro_audio_flag(outro_tag):
    """Return ``True`` if the ``<outro>`` tag includes ``audio``."""
    if not outro_tag:
        return False
    s = re.sub(r'"[^"]*"|\'[^\']*\'', '', str(outro_tag))
    return re.search(r'\baudio\b', s, flags=re.IGNORECASE) is not None


# ---------------------------------------------------------------------------
# ``<pause>`` / ``<card>`` tag parsers
# ---------------------------------------------------------------------------

def parse_pause_ms(pause_tag):
    """Return pause duration in ms (default 1200)."""
    if not pause_tag:
        return 1200
    m = re.search(r'(\d+)', pause_tag)
    if not m:
        return 1200
    return int(m.group(1))


def parse_card_duration_spec(card_tag):
    """Parse optional duration override from a ``<card …>`` tag.

    Returns ``('abs', ms)``, ``('delta', ms)``, or ``None``.
    """
    if not card_tag:
        return None
    s = str(card_tag).strip()
    if not s:
        return None

    if not re.match(r'^<\s*card\b', s, flags=re.IGNORECASE):
        return None

    m = re.match(r'^<\s*card\b\s*([^>]*)>\s*$', s, flags=re.IGNORECASE)
    if not m:
        return None

    payload = (m.group(1) or '').strip()
    if not payload:
        return None

    m2 = re.match(r'^([+-]?)\s*(\d+(?:\.\d+)?)\s*(ms|s)?\s*$', payload, flags=re.IGNORECASE)
    if not m2:
        return None

    sign = (m2.group(1) or '').strip()
    num_s = (m2.group(2) or '').strip()
    unit = (m2.group(3) or '').strip().lower()

    value = float(num_s)
    if value < 0:
        value = abs(value)

    if unit == 's':
        ms = int(round(value * 1000.0))
    else:
        ms = int(round(value))

    if sign == '+':
        return ('delta', ms)
    if sign == '-':
        return ('delta', -ms)
    return ('abs', ms)


# ---------------------------------------------------------------------------
# ``<sound>`` tag parser
# ---------------------------------------------------------------------------

def parse_sound_tag(sound_tag):
    """Parse a ``<sound …>`` tag into a dict (without path resolution).

    Returns a dict with keys ``filename``, ``mix``, ``play_for``, and
    optionally ``ms``, or ``None`` if the tag is invalid.
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
    play_for = 'card'
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
            v = float(tm.group(1))
            unit = tm.group(2)
            ms = int(round(v * 1000.0)) if unit == 's' else int(round(v))
            if ms < 0:
                ms = abs(ms)
            play_for = 'ms'
            continue

        if filename is None:
            filename = str(t).strip()

    if not filename:
        return None

    info = {
        'filename': str(filename),
        'mix': str(mix),
        'play_for': str(play_for),
    }
    if play_for == 'ms' and ms is not None:
        info['ms'] = int(ms)

    return info


# ---------------------------------------------------------------------------
# ``<img>`` tag parser
# ---------------------------------------------------------------------------

def parse_img_tag(img_tag, *, full_card_text=None):
    """Parse an ``<img …>`` tag into a dict (without path resolution).

    Returns a dict with keys ``filename``, ``mode``, and optionally
    ``percent`` or ``lines_count``, or ``None`` if the tag is invalid.
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
            percent = float(pm.group(1))
            continue
        if filename is None:
            filename = str(t).strip()

    if not filename:
        return None

    info = {
        'filename': str(filename),
        'mode': str(mode),
    }
    if percent is not None:
        info['percent'] = float(percent)

    if mode == 'lines':
        cleaned = str(full_card_text or '')
        cleaned = re.sub(r'<\s*img\b[^>]*>', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'<\s*sound\b[^>]*>', '', cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace('\r\n', '\n').replace('\r', '\n')
        raw_lines = cleaned.split('\n')
        if str(cleaned).strip() == '':
            info['lines_count'] = 0
        else:
            info['lines_count'] = int(len(raw_lines))

    return info


# ---------------------------------------------------------------------------
# Script timing analysis
# ---------------------------------------------------------------------------

def analyze_script_timing(script, min_scalable_fraction=0.40):
    """Compute timing properties for a parsed script template.

    Returns a dict with ``fixed_ms``, ``scalable_orig_ms``, ``estimated_ms``,
    ``min_possible_ms``, and ``scalable_cards``.
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
    scalable_cards = []

    min_frac = float(min_scalable_fraction)

    for i, c in enumerate(cards):
        if not isinstance(c, dict):
            continue

        mode = str(c.get('_duration_mode', 'auto') or 'auto').lower()
        base_ms = c.get('_base_duration_ms', None)
        delta_ms = c.get('_delta_ms', 0) or 0

        if mode in ('fixed', 'abs'):
            fixed_ms += int(c.get('duration', 0) or 0)
            continue

        t = float(base_ms) if base_ms is not None else float(c.get('duration', 0) or 0)
        if t < 0.0:
            t = 0.0
        t_min = t * min_frac
        r_delta = int(delta_ms)

        fixed_ms += r_delta
        scalable_orig_ms += int(round(t))
        min_possible_ms += int(round(t_min))
        scalable_cards.append({
            'idx': int(i),
            't': float(t),
            't_min': float(t_min),
            'delta_ms': r_delta,
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
