"""Missing media recovery controller.

Extracted from MainWindow to isolate the media-reconnect polling loop.

When media disappears (drive disconnected, file moved), MainWindow calls
``start(target, reason)``.  The controller polls every 2 s and emits
``recovered(resume_state)`` once the path reappears, so the main window
can call ``_apply_resume_state`` without knowing anything about the loop.
"""

import os
import time

from PySide6.QtCore import QObject, QTimer, Signal

_POLL_INTERVAL_MS = 2000
_MAX_RECOVERY_SECONDS = 600.0


class MissingMediaRecovery(QObject):
    """Polls for a missing media file and emits ``recovered`` when it returns.

    Signals:
        recovered(dict): emitted with the best-available resume-state payload
            when the target path reappears.  Connect to
            ``MainWindow._apply_resume_state``.
    """

    recovered = Signal(dict)

    def __init__(self, main_window):
        super().__init__(main_window)
        self._mw = main_window

        self._target: str = ''
        self._reason: str = ''
        self._started_mono: float = 0.0
        self._attempts: int = 0
        self._waiting_for: str | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._attempt)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, *, reason: str) -> None:
        """Start recovery for the current ``_last_play_target`` on main_window.

        Does nothing if the target is present or unknown.
        """
        mw = self._mw
        try:
            target = str(getattr(mw, '_last_play_target', '') or '').strip()
        except Exception:
            target = ''
        if not target:
            return

        try:
            if os.path.exists(target):
                return
        except Exception:
            return

        try:
            mw._log_event('media_missing', reason=str(reason or ''), target=target)
        except Exception:
            pass

        try:
            mw._persist_resume_state(force=True, reason=f'media_missing:{reason}')
        except Exception:
            pass

        self._enter_wait_state(target, reason=reason)

        self._target = target
        self._reason = reason
        self._started_mono = float(time.monotonic())
        self._attempts = 0
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        """Cancel any in-progress recovery loop."""
        self._timer.stop()

    @property
    def is_waiting(self) -> bool:
        """True while awaiting a media reconnect (suppresses redundant stall triggers)."""
        return bool(self._waiting_for)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _enter_wait_state(self, target: str, reason: str = '') -> None:
        t = str(target or '').strip()
        if not t:
            return
        if self._waiting_for == t:
            return

        self._waiting_for = t

        mw = self._mw
        try:
            mw.stop_bump_playback()
        except Exception:
            pass
        try:
            mw._pending_next_index = None
        except Exception:
            pass
        try:
            mw._pending_bump_item = None
        except Exception:
            pass
        try:
            if hasattr(mw, 'player') and mw.player:
                mw.player.stop()
        except Exception:
            pass
        try:
            msg = "Media disconnected — waiting for reconnect"
            if reason:
                msg = f"{msg} ({reason})"
            mw._show_mpv_osd_text(msg, duration_ms=2500)
        except Exception:
            pass

    def _attempt(self) -> None:
        # Give up after the timeout.
        if self._started_mono and (time.monotonic() - self._started_mono) > _MAX_RECOVERY_SECONDS:
            self._timer.stop()
            return

        target = self._target
        if not target:
            self._timer.stop()
            return

        self._attempts += 1

        try:
            if not os.path.exists(target):
                return
        except Exception:
            return

        # Target is back.
        mw = self._mw
        try:
            mw._log_event('media_reappeared', target=target, attempts=self._attempts)
        except Exception:
            pass

        self._timer.stop()
        self._waiting_for = None

        try:
            mw._show_mpv_osd_text("Media reconnected — resuming", duration_ms=2000)
        except Exception:
            pass

        # Prefer in-memory last payload; fall back to disk.
        st = getattr(mw, '_resume_last_payload', None)
        if not isinstance(st, dict):
            try:
                st = mw._load_resume_state()
            except Exception:
                st = None
        if not isinstance(st, dict):
            st = {}

        self.recovered.emit(st)
