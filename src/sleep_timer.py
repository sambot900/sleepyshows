"""Sleep timer controller.

Extracted from SleepyShowsWindow to give the sleep timer a single, clear
owner.  All countdown state lives here; MainWindow keeps thin delegation
wrappers and read-only proxy properties so existing call sites are
unchanged.
"""

import time
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QLabel


class SleepTimerController(QObject):
    """Owns sleep-timer state and tick logic.

    Emits ``expired`` when the countdown reaches zero so the main window
    can stop playback and update its mode.
    """

    expired = Signal()

    def __init__(self, main_window, default_minutes: int = 180):
        super().__init__(main_window)
        self._mw = main_window

        self.default_minutes: int = default_minutes
        self.current_minutes: int = default_minutes
        self.active: bool = False
        self.remaining_ms: int = 0
        self._last_tick: float | None = None

        # Sleep timer mode: 'countdown' (fixed number of minutes) or 'auto'
        # (stops playback at a fixed local wall-clock time, recurring nightly).
        self.mode: str = 'countdown'
        self.auto_hour: int = 2
        self.auto_minute: int = 0
        self._auto_next_trigger: datetime | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, minutes: int) -> None:
        try:
            minutes = int(minutes) if minutes is not None else 0
            if minutes <= 0:
                minutes = self.default_minutes

            self.mode = 'countdown'
            self.current_minutes = minutes
            self.active = True
            self._notify_exposure(True)
            self.remaining_ms = int(minutes * 60 * 1000)
            self._last_tick = None

            print(f"DEBUG: Start Timer {minutes}m")

            self._update_ui()
            self._pause()
            self._resume_if_needed()
            self._sync_menu()
            self._sync_welcome_screen(True)
        except Exception as e:
            print(f"Error starting timer: {e}")

    def set_auto_target(self, time_str: str) -> None:
        """Set the local wall-clock time (``"HH:MM"``, 24-hour) for AUTO mode."""
        h, m = self._parse_time_str(time_str)
        self.auto_hour = h
        self.auto_minute = m
        # If AUTO is already armed, recompute the next trigger from the new time.
        if self.active and self.mode == 'auto':
            self._auto_next_trigger = self._compute_next_trigger(datetime.now())
            self._update_ui()

    def start_auto(self) -> None:
        """Arm AUTO mode: stop playback at the configured local time, nightly."""
        try:
            self.mode = 'auto'
            self.active = True
            self._notify_exposure(True)
            self.remaining_ms = 0
            self._last_tick = None
            self._auto_next_trigger = self._compute_next_trigger(datetime.now())

            print(f"DEBUG: Start AUTO Timer at {self.auto_hour:02d}:{self.auto_minute:02d}")

            self._update_ui()
            self._pause()
            self._resume_if_needed()
            self._sync_menu()
            self._sync_welcome_screen(True)
        except Exception as e:
            print(f"Error starting auto timer: {e}")

    def cancel(self) -> None:
        try:
            self.active = False
            self.mode = 'countdown'
            self._auto_next_trigger = None
            self._notify_exposure(False)
            self.remaining_ms = 0
            self._pause()
            self._update_ui()
            self._sync_menu()
            self._sync_welcome_screen(False)
        except Exception as e:
            print(f"Error cancelling timer: {e}")
            self._sync_welcome_screen(False)

    def remaining_minutes(self) -> int:
        """Return countdown minutes remaining (ceiling), or 0 if inactive."""
        if not self.active or self.mode == 'auto' or self.remaining_ms <= 0:
            return 0
        return max(1, int((self.remaining_ms + 59999) // 60000))

    @property
    def auto_active(self) -> bool:
        return bool(self.active and self.mode == 'auto')

    def auto_time_label(self) -> str:
        """Return the AUTO target time as a friendly 12-hour string (e.g. "2:00 AM")."""
        h = int(self.auto_hour) % 24
        m = int(self.auto_minute) % 60
        suffix = "AM" if h < 12 else "PM"
        hour12 = h % 12
        if hour12 == 0:
            hour12 = 12
        return f"{hour12}:{m:02d} {suffix}"

    @staticmethod
    def _parse_time_str(time_str: str) -> tuple[int, int]:
        try:
            parts = str(time_str or '').strip().split(':')
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            h = max(0, min(23, h))
            m = max(0, min(59, m))
            return h, m
        except Exception:
            return 2, 0

    def _compute_next_trigger(self, now: datetime) -> datetime:
        target = now.replace(hour=self.auto_hour, minute=self.auto_minute,
                             second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def pause(self) -> None:
        self._pause()

    def resume_if_needed(self) -> None:
        self._resume_if_needed()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _pause(self) -> None:
        self._last_tick = None
        if self._timer.isActive():
            self._timer.stop()

    def _resume_if_needed(self) -> None:
        if not self.active:
            self._pause()
            return

        # In AUTO mode, remaining_ms is intentionally 0; gate countdown-only
        # expiry checks to countdown mode so the AUTO wall-clock trigger can run.
        if self.mode != 'auto' and self.remaining_ms <= 0:
            self._pause()
            return
        if not self._is_show_playing():
            self._pause()
            return
        if not self._timer.isActive():
            self._last_tick = time.monotonic()
            self._timer.start()

    def _tick(self) -> None:
        if not self.active:
            self._pause()
            self._update_ui()
            return

        if not self._is_show_playing():
            self._pause()
            self._update_ui()
            return

        if self.mode == 'auto':
            now = datetime.now()
            if self._auto_next_trigger is None:
                self._auto_next_trigger = self._compute_next_trigger(now)
            if now >= self._auto_next_trigger:
                # Re-arm for the following night before firing so AUTO keeps
                # working across nights without any user action.
                self._auto_next_trigger = self._compute_next_trigger(now)
                self.expired.emit()
            self._update_ui()
            return

        now = time.monotonic()
        if self._last_tick is None:
            self._last_tick = now
            self._update_ui()
            return

        elapsed_ms = int((now - self._last_tick) * 1000)
        self._last_tick = now
        if elapsed_ms <= 0:
            self._update_ui()
            return

        self.remaining_ms = max(0, self.remaining_ms - elapsed_ms)
        self._update_ui()
        if self.remaining_ms <= 0:
            self.expired.emit()

    def _is_show_playing(self) -> bool:
        try:
            mw = self._mw
            if not hasattr(mw, 'player') or not mw.player or not mw.player.mpv:
                return False
            mpv = mw.player.mpv
            return (not bool(getattr(mpv, 'pause', True))) and (not bool(getattr(mpv, 'core_idle', True)))
        except Exception:
            return False

    def _notify_exposure(self, active: bool) -> None:
        try:
            self._mw.playlist_manager.set_sleep_timer_active_for_exposure(active)
        except Exception:
            pass

    def _update_ui(self) -> None:
        mw = self._mw
        self._ensure_status_label()

        if not self.active:
            mw.lbl_sleep_status.setText("")
            if hasattr(mw, 'play_mode_widget') and hasattr(mw.play_mode_widget, 'btn_sleep_timer'):
                mw.play_mode_widget.btn_sleep_timer.setText("SLEEP\nOFF")
            return

        if self.mode == 'auto':
            label = self.auto_time_label()
            mw.lbl_sleep_status.setText(f"Sleep at {label}")
            if hasattr(mw, 'play_mode_widget') and hasattr(mw.play_mode_widget, 'btn_sleep_timer'):
                mw.play_mode_widget.btn_sleep_timer.setText("SLEEP\nAUTO")
            return

        remaining_min = self.remaining_minutes()
        mw.lbl_sleep_status.setText(f"Sleep in {remaining_min}m")
        if hasattr(mw, 'play_mode_widget') and hasattr(mw.play_mode_widget, 'btn_sleep_timer'):
            mw.play_mode_widget.btn_sleep_timer.setText(f"SLEEP\n{remaining_min}m")

    def _ensure_status_label(self) -> None:
        mw = self._mw
        if hasattr(mw, 'lbl_sleep_status') and mw.lbl_sleep_status is not None:
            return
        mw.lbl_sleep_status = QLabel("")

    def _sync_menu(self) -> None:
        try:
            self._mw.update_sleep_menu_state()
        except Exception:
            pass

    def _sync_welcome_screen(self, on: bool) -> None:
        try:
            mw = self._mw
            if not hasattr(mw, 'welcome_screen'):
                return
            ws = mw.welcome_screen
            if on and not ws.is_sleep_on:
                ws.is_sleep_on = True
                ws.update_checkbox(ws.btn_sleep_check, True)
            elif not on and ws.is_sleep_on:
                ws.is_sleep_on = False
                ws.update_checkbox(ws.btn_sleep_check, False)
        except Exception:
            pass
