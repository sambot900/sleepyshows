"""Sleep timer controller.

Extracted from SleepyShowsWindow to give the sleep timer a single, clear
owner.  All countdown state lives here; MainWindow keeps thin delegation
wrappers and read-only proxy properties so existing call sites are
unchanged.
"""

import time

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

    def cancel(self) -> None:
        try:
            self.active = False
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
        if not self.active or self.remaining_ms <= 0:
            return 0
        return max(1, int((self.remaining_ms + 59999) // 60000))

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
        if not self.active or self.remaining_ms <= 0:
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

        remaining_min = self.remaining_minutes()
        mw.lbl_sleep_status.setText(f"Sleep in {remaining_min}m")
        if hasattr(mw, 'play_mode_widget') and hasattr(mw.play_mode_widget, 'btn_sleep_timer'):
            mw.play_mode_widget.btn_sleep_timer.setText(f"SLEEP\n{remaining_min}m")

    def _ensure_status_label(self) -> None:
        mw = self._mw
        if hasattr(mw, 'lbl_sleep_status') and mw.lbl_sleep_status is not None:
            return
        mw.lbl_sleep_status = QLabel("")
        mw.lbl_sleep_status.setStyleSheet("color: white; padding-right: 10px;")
        mw.statusBar().addPermanentWidget(mw.lbl_sleep_status)

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
