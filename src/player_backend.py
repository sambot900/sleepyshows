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
    playbackPaused = Signal(bool)
    mouseMoved = Signal()
    fullscreenRequested = Signal()
    escapePressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow, True)
        
        self.mpv = None
        self._init_mpv()

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
            import mpv
            # Initialize MPV with default options
            # vo='gpu' is standard, keeping input-default-bindings=yes allows keyboard control if focused
            # Disable OSC (On Screen Controller) to avoid redundant controls
            self.mpv = mpv.MPV(input_default_bindings=True, input_vo_keyboard=True, osc=False)
            self.mpv.wid = self.winId()
            
            # Key bindings
            @self.mpv.on_key_press('MOUSE_BTN0')
            def mouse_click_handler():
                self.toggle_pause()

            @self.mpv.on_key_press('MOUSE_BTN0_DBL')
            def mouse_dbl_click_handler():
                try:
                    QMetaObject.invokeMethod(self, "_emit_fullscreen_requested", Qt.QueuedConnection)
                except Exception:
                    pass

            # Key triggers
            @self.mpv.on_key_press('f')
            def f_key_handler():
                try:
                    QMetaObject.invokeMethod(self, "_emit_fullscreen_requested", Qt.QueuedConnection)
                except Exception:
                    pass
                
            @self.mpv.on_key_press('F')
            def big_f_key_handler():
                try:
                    QMetaObject.invokeMethod(self, "_emit_fullscreen_requested", Qt.QueuedConnection)
                except Exception:
                    pass

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

            # Some mpv configurations may not trigger end-file in a way that
            # reaches us (e.g., keep-open behavior). eof-reached is a reliable
            # signal that playback hit the end.
            @self.mpv.property_observer('eof-reached')
            def eof_observer(_name, value):
                if value:
                    try:
                        QMetaObject.invokeMethod(self, "_emit_finished", Qt.QueuedConnection)
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
                # mpv reports end-of-file as reason "eof" (sometimes surfaces as 0 in older bindings).
                try:
                    reason = None
                    props = event.get('event_props', {}) if isinstance(event, dict) else {}
                    reason = props.get('reason')
                    if reason in (0, 'eof', 'EOF'):
                        try:
                            QMetaObject.invokeMethod(self, "_emit_finished", Qt.QueuedConnection)
                        except Exception:
                            pass
                except Exception:
                    # Never let callback errors break playback.
                    pass

        except ImportError:
            self.errorOccurred.emit("python-mpv not installed")
        except Exception as e:
            self.errorOccurred.emit(f"MPV Init Error: {str(e)}")

    def play(self, filepath):
        if self.mpv:
            self.mpv.play(filepath)
            self.mpv.pause = False

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
