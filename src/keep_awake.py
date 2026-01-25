import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class KeepAwakeStatus:
    enabled: bool
    backend: str
    detail: str = ''


class KeepAwakeInhibitor:
    """Best-effort cross-platform "keep awake".

    Goal: prevent system idle/sleep while media is actively playing.

    Implementation is intentionally dependency-free (stdlib only) and degrades
    gracefully when platform tools aren't available.
    """

    def __init__(self):
        self._system = (platform.system() or '').strip().lower()

        self._enabled = False
        self._status = KeepAwakeStatus(enabled=False, backend='none', detail='')

        # Windows
        self._win_active = False

        # macOS
        self._caffeinate_proc = None

        # Linux
        self._dbus_cookie = None
        self._dbus_bus = 'session'
        self._dbus_service = 'org.freedesktop.ScreenSaver'
        self._dbus_path = '/org/freedesktop/ScreenSaver'
        self._dbus_iface = 'org.freedesktop.ScreenSaver'
        self._systemd_inhibit_proc = None

    def status(self) -> KeepAwakeStatus:
        return self._status

    def enable(self, *, reason: str = 'Playing media') -> KeepAwakeStatus:
        if self._enabled:
            return self._status

        reason = str(reason or 'Playing media')

        if self._system.startswith('win'):
            self._status = self._enable_windows()
        elif self._system == 'darwin':
            self._status = self._enable_macos(reason=reason)
        else:
            self._status = self._enable_linux(reason=reason)

        self._enabled = bool(self._status.enabled)
        return self._status

    def disable(self) -> KeepAwakeStatus:
        if not self._enabled:
            return self._status

        if self._system.startswith('win'):
            self._status = self._disable_windows()
        elif self._system == 'darwin':
            self._status = self._disable_macos()
        else:
            self._status = self._disable_linux()

        self._enabled = bool(self._status.enabled)
        return self._status

    # --- Windows ---

    def _enable_windows(self) -> KeepAwakeStatus:
        try:
            import ctypes

            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002

            res = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
            ok = bool(res)
            self._win_active = ok
            return KeepAwakeStatus(enabled=ok, backend='windows', detail='SetThreadExecutionState')
        except Exception as e:
            self._win_active = False
            return KeepAwakeStatus(enabled=False, backend='windows', detail=f'failed: {e}')

    def _disable_windows(self) -> KeepAwakeStatus:
        try:
            import ctypes

            ES_CONTINUOUS = 0x80000000
            res = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            ok = bool(res)
            self._win_active = False
            return KeepAwakeStatus(enabled=False, backend='windows', detail='cleared')
        except Exception as e:
            self._win_active = False
            return KeepAwakeStatus(enabled=False, backend='windows', detail=f'failed: {e}')

    # --- macOS ---

    def _enable_macos(self, *, reason: str) -> KeepAwakeStatus:
        try:
            if shutil.which('caffeinate'):
                # -w PID: exit when this process exits
                # -d: prevent display sleep
                # -i: prevent idle sleep
                # -m: prevent disk sleep
                # -s: prevent system sleep (when plugged in)
                args = ['caffeinate', '-dimsu', '-w', str(os.getpid())]
                self._caffeinate_proc = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return KeepAwakeStatus(enabled=True, backend='macos', detail='caffeinate')
        except Exception as e:
            return KeepAwakeStatus(enabled=False, backend='macos', detail=f'failed: {e}')

        return KeepAwakeStatus(enabled=False, backend='macos', detail='no caffeinate')

    def _disable_macos(self) -> KeepAwakeStatus:
        try:
            if self._caffeinate_proc is not None:
                try:
                    self._caffeinate_proc.terminate()
                except Exception:
                    pass
                self._caffeinate_proc = None
        except Exception:
            self._caffeinate_proc = None
        return KeepAwakeStatus(enabled=False, backend='macos', detail='cleared')

    # --- Linux ---

    def _enable_linux(self, *, reason: str) -> KeepAwakeStatus:
        # 1) Try session DBus screensaver inhibit (cookie-based, no long-running proc).
        st = self._linux_try_dbus_screensaver_inhibit(reason=reason)
        if st.enabled:
            return st

        # 2) Try systemd-inhibit (long-running proc).
        st = self._linux_try_systemd_inhibit(reason=reason)
        if st.enabled:
            return st

        return KeepAwakeStatus(enabled=False, backend='linux', detail=st.detail if st.detail else 'no backend available')

    def _disable_linux(self) -> KeepAwakeStatus:
        # Undo DBus inhibition
        try:
            if self._dbus_cookie is not None and shutil.which('dbus-send'):
                cookie = int(self._dbus_cookie)
                args = [
                    'dbus-send',
                    f'--{self._dbus_bus}',
                    '--print-reply',
                    f'--dest={self._dbus_service}',
                    self._dbus_path,
                    f'{self._dbus_iface}.UnInhibit',
                    f'uint32:{cookie}',
                ]
                subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        self._dbus_cookie = None

        # Stop systemd-inhibit proc
        try:
            if self._systemd_inhibit_proc is not None:
                try:
                    self._systemd_inhibit_proc.terminate()
                except Exception:
                    pass
        except Exception:
            pass
        self._systemd_inhibit_proc = None

        return KeepAwakeStatus(enabled=False, backend='linux', detail='cleared')

    def _linux_try_dbus_screensaver_inhibit(self, *, reason: str) -> KeepAwakeStatus:
        try:
            if not shutil.which('dbus-send'):
                return KeepAwakeStatus(enabled=False, backend='linux-dbus', detail='dbus-send not found')

            args = [
                'dbus-send',
                '--session',
                '--print-reply',
                f'--dest={self._dbus_service}',
                self._dbus_path,
                f'{self._dbus_iface}.Inhibit',
                'string:SleepyShows',
                f'string:{reason}',
            ]
            cp = subprocess.run(args, capture_output=True, text=True, check=False)
            out = (cp.stdout or '') + '\n' + (cp.stderr or '')

            # Typical output contains: "uint32 123"
            m = re.search(r'uint32\s+(\d+)', out)
            if not m:
                return KeepAwakeStatus(enabled=False, backend='linux-dbus', detail='no cookie')

            self._dbus_cookie = int(m.group(1))
            self._dbus_bus = 'session'
            return KeepAwakeStatus(enabled=True, backend='linux-dbus', detail=f'cookie={self._dbus_cookie}')
        except Exception as e:
            self._dbus_cookie = None
            return KeepAwakeStatus(enabled=False, backend='linux-dbus', detail=f'failed: {e}')

    def _linux_try_systemd_inhibit(self, *, reason: str) -> KeepAwakeStatus:
        try:
            if not shutil.which('systemd-inhibit'):
                return KeepAwakeStatus(enabled=False, backend='linux-systemd', detail='systemd-inhibit not found')

            # Keep the inhibitor alive via an infinite sleep.
            args = [
                'systemd-inhibit',
                f'--why={reason}',
                '--what=sleep:idle',
                '--mode=block',
                'sleep',
                'infinity',
            ]
            self._systemd_inhibit_proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return KeepAwakeStatus(enabled=True, backend='linux-systemd', detail='systemd-inhibit')
        except Exception as e:
            self._systemd_inhibit_proc = None
            return KeepAwakeStatus(enabled=False, backend='linux-systemd', detail=f'failed: {e}')
