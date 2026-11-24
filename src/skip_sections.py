"""Per-video skip ranges and the two-step timestamp editor."""

from math import isfinite

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout


def saved_sections(settings, episode_key):
    videos = settings.get('skip_sections')
    raw = videos.get(episode_key) if isinstance(videos, dict) else None
    if not isinstance(raw, list):
        return ()

    sections = set()
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        try:
            start, end = map(float, pair)
        except (TypeError, ValueError, OverflowError):
            continue
        if isfinite(start) and isfinite(end) and 0 <= start < end:
            sections.add((start, end))
    return tuple(sorted(sections))


def merge_sections(sections, start_trim=0, end_trim=0, duration=None):
    """Merge touching intervals so playback can skip them at the earliest start."""
    ranges = list(sections)
    if start_trim:
        ranges.append((0, start_trim))
    if duration is not None and end_trim:
        ranges.append((max(0.0, duration - end_trim), duration))

    merged = []
    for start, end in sorted(ranges):
        if duration is not None:
            end = min(end, duration)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(merged[-1][1], end)
        else:
            merged.append((start, end))
    return tuple(merged)


def section_at(sections, position):
    for start, end in sections:
        if position < start:
            break
        if position < end:
            return start, end
    return None


def format_timestamp(seconds):
    whole = int(seconds)
    total_ms = whole * 1000 + round((seconds - whole) * 1000)
    whole, milliseconds = divmod(total_ms, 1000)
    minutes, second = divmod(whole, 60)
    hours, minute = divmod(minutes, 60)
    timestamp = f'{hours}:{minute:02d}:{second:02d}' if hours else f'{minute:02d}:{second:02d}'
    if milliseconds:
        timestamp += f'.{milliseconds:03d}'
    return timestamp


def parse_timestamp(text):
    """Accept seconds, MM:SS, or HH:MM:SS, with fractional seconds."""
    parts = text.strip().split(':')
    if len(parts) > 3 or any(not part.isdecimal() for part in parts[:-1]):
        raise ValueError
    seconds = float(parts[-1])
    if not isfinite(seconds) or seconds < 0 or (len(parts) > 1 and seconds >= 60):
        raise ValueError
    minutes = int(parts[-2]) if len(parts) > 1 else 0
    if len(parts) == 3 and minutes >= 60:
        raise ValueError
    hours = int(parts[0]) if len(parts) == 3 else 0
    return hours * 3600 + minutes * 60 + seconds


class SkipSectionDialog(QDialog):
    def __init__(self, parent, *, position, duration):
        super().__init__(parent, Qt.Tool)
        self.setWindowTitle('New Skip Section')
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.duration = duration
        self.section = None
        self._start = None
        self._position = position
        self._follow_playhead = True

        layout = QVBoxLayout(self)
        self.prompt = QLabel()
        layout.addWidget(self.prompt)
        self.time_field = QLineEdit()
        self.prompt.setBuddy(self.time_field)
        self.time_field.setToolTip('Enter seconds, MM:SS, or HH:MM:SS.')
        self.time_field.textEdited.connect(self._manual_edit)
        self.time_field.returnPressed.connect(self._confirm)
        layout.addWidget(self.time_field)
        self.status = QLabel()
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        use_playhead = QPushButton('Use Playhead')
        use_playhead.clicked.connect(self._use_playhead)
        buttons.addWidget(use_playhead)
        buttons.addStretch(1)
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.confirm = QPushButton()
        self.confirm.setAutoDefault(False)
        self.confirm.clicked.connect(self._confirm)
        buttons.addWidget(self.confirm)
        layout.addLayout(buttons)
        self._show_step('Start time', 'Confirm Start')

    def set_playhead(self, position):
        self._position = position
        if self._follow_playhead:
            timestamp = format_timestamp(position)
            if self.time_field.text() != timestamp:
                self.time_field.setText(timestamp)

    def _show_step(self, prompt, action):
        self.prompt.setText(prompt)
        self.confirm.setText(action)
        self._use_playhead()
        self.time_field.setFocus()
        self.time_field.selectAll()

    def _manual_edit(self):
        self._follow_playhead = False
        self.status.clear()

    def _use_playhead(self):
        self._follow_playhead = True
        self.status.clear()
        self.set_playhead(self._position)

    def _confirm(self):
        try:
            position = parse_timestamp(self.time_field.text())
            if position > self.duration + 0.0005:
                raise ValueError
        except (ValueError, OverflowError):
            self.status.setText('Enter a time within this video.')
            return
        position = min(position, self.duration)

        if self._start is None:
            if position >= self.duration:
                self.status.setText('The start must be before the end of the video.')
                return
            self._start = position
            self._show_step('End time', 'Save Skip Section')
            return

        if position <= self._start:
            self.status.setText('The end must be after the start.')
            return
        self.section = self._start, position
        self.accept()
