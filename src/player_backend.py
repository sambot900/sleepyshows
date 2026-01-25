import os
import sys
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Signal, Slot, Qt, QMetaObject, Q_ARG

class MpvPlayer(QWidget):
    # Signals to communicate with the main application
    positionChanged = Signal(float)
    durationChanged = Signal(float)
    playbackFinished = Signal()
    errorOccurred = Signal(str)
    endFileReason = Signal(str)
    playbackPaused = Signal(bool)
    mouseMoved = Signal()
    fullscreenRequested = Signal()
    escapePressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self._init_error = None
        self._dll_dir_handles = []
        
        self.mpv = None
        self._init_mpv()

    def _prepare_windows_mpv_dll_search(self):
        if not sys.platform.startswith('win'):
            return

        # python-mpv loads libmpv via LoadLibrary, so the DLL must be reachable
        # via the process DLL search path. The build script copies libmpv-2.dll
        # into dist/SleepyShows; dev runs may have it in repo root or scripts/.

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = []

        # 1) Next to the running executable (PyInstaller dist, or venv python)
        try:
            candidates.append(os.path.dirname(sys.executable))
        except Exception:
            pass

        # 2) PyInstaller temporary extraction folder
        try:
            meipass = getattr(sys, '_MEIPASS', None)
            if meipass:
                candidates.append(str(meipass))
        except Exception:
            pass

        # 3) Common repo locations
        candidates.extend([
            os.getcwd(),
            base_dir,
            os.path.join(base_dir, 'scripts'),
            os.path.join(base_dir, 'drivers'),
            os.path.join(base_dir, 'dist', 'SleepyShows'),
            os.path.join(base_dir, 'build', 'SleepyShows'),
        ])

        dll_names = ('libmpv-2.dll', 'mpv-2.dll', 'mpv-1.dll')
        usable_dirs = []
        for d in candidates:
            try:
                if not d or not os.path.isdir(d):
                    continue
                for dll_name in dll_names:
                    if os.path.isfile(os.path.join(d, dll_name)):
                        usable_dirs.append(d)
                        break
            except Exception:
                continue

        # Prepend to PATH as well. python-mpv (and/or ctypes.util.find_library)
        # may inspect PATH directly when locating mpv-*.dll.
        try:
            cur = os.environ.get('PATH', '')
            to_prepend = []
            for d in usable_dirs:
                if d and d not in cur:
                    to_prepend.append(d)
            if to_prepend:
                os.environ['PATH'] = os.pathsep.join(to_prepend) + os.pathsep + cur
        except Exception:
            pass

        # Prefer the more explicit DLL directory mechanism when available.
        add_dll_dir = getattr(os, 'add_dll_directory', None)
        if callable(add_dll_dir):
            for d in usable_dirs:
                try:
                    self._dll_dir_handles.append(add_dll_dir(d))
                except Exception:
                    continue
            return

        return

    @Slot(float)
    def _emit_position(self, value: float):
        self.positionChanged.emit(float(value))

    @Slot(float)
    def _emit_duration(self, value: float):
        self.durationChanged.emit(float(value))

    @Slot(bool)
    def _emit_paused(self, value: bool):
        self.playbackPaused.emit(bool(value))

    @Slot()
    def _emit_finished(self):
        self.playbackFinished.emit()

    @Slot(str)
    def _emit_end_file_reason(self, reason: str):
        try:
            self.endFileReason.emit(str(reason or ''))
        except Exception:
            return

    @Slot()
    def _emit_mouse_moved(self):
        self.mouseMoved.emit()

    @Slot()
    def _emit_fullscreen_requested(self):
        self.fullscreenRequested.emit()

    @Slot()
    def _emit_escape_pressed(self):
        self.escapePressed.emit()
        
    def _init_mpv(self):
        try:
            self._prepare_windows_mpv_dll_search()
            import mpv
            # Initialize MPV with default options
            # vo='gpu' is standard, keeping input-default-bindings=yes allows keyboard control if focused
            # Disable OSC (On Screen Controller) to avoid redundant controls
            def _log_handler(*args):
                try:
                    parts = [str(a) for a in args if a is not None]
                    if parts:
                        print("MPV:", " ".join(parts))
                except Exception:
                    return

            # Some python-mpv builds don't support log_handler; fall back cleanly.
            try:
                self.mpv = mpv.MPV(
                    input_default_bindings=False,
                    input_vo_keyboard=True,
                    osc=False,
                    log_handler=_log_handler,
                )
            except TypeError:
                self.mpv = mpv.MPV(
                    input_default_bindings=False,
                    input_vo_keyboard=True,
                    osc=False,
                )

            try:
                wid = int(self.winId())
            except Exception:
                wid = 0
            if wid:
                self.mpv.wid = wid
            
            # Key bindings
            @self.mpv.on_key_press('MOUSE_BTN0')
            def mouse_click_handler():
                self.toggle_pause()

            @self.mpv.on_key_press('SPACE')
            def space_handler():
                self.toggle_pause()

            @self.mpv.on_key_press('MOUSE_BTN0_DBL')
            def mouse_dbl_click_handler():
                try:
                    QMetaObject.invokeMethod(self, "_emit_fullscreen_requested", Qt.QueuedConnection)
                except Exception:
                    pass

            # Fullscreen toggle is handled by the Windows native message hook in main.py.

            @self.mpv.on_key_press('ESC')
            def esc_key_handler():
                try:
                    QMetaObject.invokeMethod(self, "_emit_escape_pressed", Qt.QueuedConnection)
                except Exception:
                    pass

            # Setup event callbacks
            @self.mpv.property_observer('time-pos')
            def time_observer(_name, value):
                if value is not None:
                    try:
                        QMetaObject.invokeMethod(self, "_emit_position", Qt.QueuedConnection, Q_ARG(float, float(value)))
                    except Exception:
                        pass

            @self.mpv.property_observer('duration')
            def duration_observer(_name, value):
                if value is not None:
                    try:
                        QMetaObject.invokeMethod(self, "_emit_duration", Qt.QueuedConnection, Q_ARG(float, float(value)))
                    except Exception:
                        pass

            @self.mpv.property_observer('pause')
            def pause_observer(_name, value):
                try:
                    QMetaObject.invokeMethod(self, "_emit_paused", Qt.QueuedConnection, Q_ARG(bool, bool(value if value is not None else False)))
                except Exception:
                    pass

            # NOTE: We use property observer for mouse position to detect hover
            @self.mpv.property_observer('mouse-pos')
            def mouse_pos_observer(_name, value):
                 try:
                     QMetaObject.invokeMethod(self, "_emit_mouse_moved", Qt.QueuedConnection)
                 except Exception:
                     pass

            @self.mpv.event_callback('end-file')
            def end_file_callback(event):
                try:
                    props = event.get('event_props', {}) if isinstance(event, dict) else {}
                    reason = props.get('reason')
                    # Normalize to a readable string.
                    if isinstance(reason, int):
                        # Older bindings sometimes surface eof as 0.
                        reason_str = 'eof' if reason == 0 else str(reason)
                    else:
                        reason_str = str(reason or '')

                    try:
                        QMetaObject.invokeMethod(
                            self,
                            "_emit_end_file_reason",
                            Qt.QueuedConnection,
                            Q_ARG(str, reason_str),
                        )
                    except Exception:
                        pass

                    if reason_str.lower() == 'eof':
                        try:
                            QMetaObject.invokeMethod(self, "_emit_finished", Qt.QueuedConnection)
                        except Exception:
                            pass
                except Exception:
                    # Never let callback errors break playback.
                    pass

        except ImportError:
            self._init_error = "python-mpv not installed"
            try:
                self.errorOccurred.emit(self._init_error)
            except Exception:
                pass
        except Exception as e:
            self._init_error = f"MPV Init Error: {str(e)}"
            try:
                self.errorOccurred.emit(self._init_error)
            except Exception:
                pass

    def play(self, filepath):
        if self.mpv:
            # On some platforms/window-manager transitions (notably fullscreen
            # toggles on Windows), the underlying native window handle can be
            # recreated. If mpv keeps an old wid, subsequent loadfile/play calls
            # can result in audio-only playback or a gray screen.
            try:
                wid = int(self.winId())
            except Exception:
                wid = 0
            if wid:
                try:
                    self.mpv.wid = wid
                except Exception:
                    pass
            try:
                self.mpv.play(filepath)
                self.mpv.pause = False
            except Exception as e:
                try:
                    self.errorOccurred.emit(f"MPV play failed: {e}")
                except Exception:
                    pass

    def pause(self):
        if self.mpv:
            self.mpv.pause = True

    def toggle_pause(self):
        if self.mpv:
            self.mpv.pause = not self.mpv.pause

    def stop(self):
        if self.mpv:
            self.mpv.stop()
    
    def seek(self, position):
        if self.mpv:
            self.mpv.seek(position, reference="absolute")

    def seek_relative(self, offset):
        if self.mpv:
            self.mpv.seek(offset, reference="relative")
            
    def set_volume(self, volume):
        if self.mpv:
            self.mpv.volume = volume

    def set_audio_normalization(self, enabled: bool):
        """Enable/disable audio normalization (dynamic range leveling).

        Uses mpv's ffmpeg lavfi filter. Best-effort; if unsupported it fails silently.
        """
        if not self.mpv:
            return
        try:
            if enabled:
                # Dynamic audio normalization to reduce loud/quiet swings.
                self.mpv.af = "lavfi=[dynaudnorm]"
            else:
                # Clear audio filters.
                self.mpv.af = ""
        except Exception:
            return

    def shutdown(self):
        if self.mpv:
            self.mpv.terminate()


class MpvAudioPlayer:
    """Audio-only MPV instance for short sound effects.

    This is intentionally minimal: it does not render video and does not bind input.
    """

    def __init__(self):
        self.mpv = None
        self._init_mpv()

    def _init_mpv(self):
        try:
            import mpv
            # vo=null ensures no video output; input bindings disabled to avoid stealing keys.
            self.mpv = mpv.MPV(
                input_default_bindings=False,
                input_vo_keyboard=False,
                osc=False,
                vo='null',
            )
            try:
                # Ensure we never try to render a video track.
                self.mpv.vid = 'no'
            except Exception:
                pass
        except Exception:
            self.mpv = None

    def play(self, filepath):
        if self.mpv:
            try:
                self.mpv.play(filepath)
                self.mpv.pause = False
            except Exception:
                return

    def stop(self):
        if self.mpv:
            try:
                self.mpv.stop()
            except Exception:
                return

    def set_volume(self, volume):
        if self.mpv:
            try:
                self.mpv.volume = volume
            except Exception:
                return

    def shutdown(self):
        if self.mpv:
            try:
                self.mpv.terminate()
            except Exception:
                return
