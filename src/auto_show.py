"""Playback-time Auto-Show countdown and temporary nightly overrides."""

import time
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal


class AutoShowController(QObject):
    switch_requested = Signal(str)
    state_changed = Signal()

    def __init__(self, main_window, *, enabled=False, show_name='', delay_minutes=60):
        super().__init__(main_window)
        self._mw = main_window
        self.default_enabled = bool(enabled)
        self.enabled = self.default_enabled
        self.show_name = str(show_name or '').strip()
        self.delay_minutes = self.parse_delay(delay_minutes)
        self.switched = False
        self.error = ''
        self.elapsed_seconds = 0.0
        self._last_tick = None
        self._was_playing = False
        self._night_override = None
        self.reset_at = self._next_boundary(datetime.now())

        override = main_window._settings.get('auto_show_night_override')
        if isinstance(override, dict):
            try:
                until = datetime.fromisoformat(override['until'])
                if until > datetime.now() and isinstance(override.get('enabled'), bool):
                    self._night_override = override['enabled']
                    self.enabled = self._night_override
                    self.reset_at = min(self.reset_at, until)
            except (KeyError, TypeError, ValueError):
                pass

        # Keep the boundary clock running even while stopped or paused.
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    @staticmethod
    def parse_delay(value):
        try:
            return max(1, min(1440, int(value)))
        except (TypeError, ValueError, OverflowError):
            return 60

    def _next_boundary(self, now):
        if self._mw.auto_sleep_enabled:
            return self._mw.sleep_timer._compute_next_trigger(now)
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    def _reset_progress(self):
        self.elapsed_seconds = 0.0
        self.switched = False
        self.error = ''
        self._last_tick = None
        self._was_playing = False

    def _reset_if_due(self):
        now = datetime.now()
        if now >= self.reset_at:
            self.enabled = self.default_enabled
            self._night_override = None
            self.reset_at = self._next_boundary(now)
            self._reset_progress()
            self.state_changed.emit()

    def refresh_boundary(self):
        """Follow changes to the app's auto sleep setting and target time."""
        self._reset_if_due()
        self.reset_at = self._next_boundary(datetime.now())
        self.state_changed.emit()

    def configure(self, enabled, show_name, delay_minutes):
        self.default_enabled = bool(enabled)
        self.enabled = self.default_enabled
        self.show_name = str(show_name or '').strip()
        self.delay_minutes = self.parse_delay(delay_minutes)
        self._night_override = None
        self.reset_at = self._next_boundary(datetime.now())
        self._reset_progress()
        self.state_changed.emit()
        self._tick()

    def toggle_for_night(self):
        self._reset_if_due()
        self.enabled = not self.enabled
        self._night_override = self.enabled
        self._reset_progress()
        self.state_changed.emit()
        self._tick()

    def export_override(self):
        if self._night_override is None:
            return None
        return {'enabled': self._night_override, 'until': self.reset_at.isoformat(timespec='seconds')}

    def _is_content_playing(self):
        mw = self._mw
        if not mw.sleep_timer._is_show_playing():
            return False
        if (getattr(mw, '_in_bump_playback', False)
                or getattr(mw, '_pending_bump_item', None) is not None):
            return False
        pm = mw.playlist_manager
        idx = pm.current_index
        if idx is None or not 0 <= idx < len(pm.current_playlist):
            return False
        if not pm.is_episode_item(pm.current_playlist[idx]):
            return False
        return pm._norm_path_key(getattr(mw, '_last_play_source_path', '')) == pm._norm_path_key(
            pm._episode_path_for_index(idx))

    def _tick(self):
        self._reset_if_due()
        now = time.monotonic()
        playing = self._is_content_playing()
        if self.enabled and not self.switched and self._was_playing and self._last_tick is not None:
            self.elapsed_seconds += max(0.0, now - self._last_tick)
        self._last_tick = now
        self._was_playing = playing

        if (not self.enabled or self.switched or not self.show_name or not playing
                or self.elapsed_seconds < self.delay_minutes * 60):
            return

        # Let a due sleep timer stop playback before attempting a show switch.
        self._mw.sleep_timer._tick()
        if not self._is_content_playing():
            self._was_playing = False
            return
        self.switched = True
        self.state_changed.emit()
        self.switch_requested.emit(self.show_name)

    def report_error(self, message):
        self.error = str(message)
        self.state_changed.emit()
