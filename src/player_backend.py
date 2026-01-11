import os
import sys
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Signal, Slot, Qt

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
                self.fullscreenRequested.emit()

            # Key triggers
            @self.mpv.on_key_press('f')
            def f_key_handler():
                self.fullscreenRequested.emit()
                
            @self.mpv.on_key_press('F')
            def big_f_key_handler():
                self.fullscreenRequested.emit()

            @self.mpv.on_key_press('ESC')
            def esc_key_handler():
                self.escapePressed.emit()

            # Setup event callbacks
            @self.mpv.property_observer('time-pos')
            def time_observer(_name, value):
                if value is not None:
                    self.positionChanged.emit(value)

            @self.mpv.property_observer('duration')
            def duration_observer(_name, value):
                if value is not None:
                    self.durationChanged.emit(value)

            @self.mpv.property_observer('pause')
            def pause_observer(_name, value):
                self.playbackPaused.emit(value if value is not None else False)

            # Some mpv configurations may not trigger end-file in a way that
            # reaches us (e.g., keep-open behavior). eof-reached is a reliable
            # signal that playback hit the end.
            @self.mpv.property_observer('eof-reached')
            def eof_observer(_name, value):
                if value:
                    self.playbackFinished.emit()

            # NOTE: We use property observer for mouse position to detect hover
            @self.mpv.property_observer('mouse-pos')
            def mouse_pos_observer(_name, value):
                 self.mouseMoved.emit()

            @self.mpv.event_callback('end-file')
            def end_file_callback(event):
                # mpv reports end-of-file as reason "eof" (sometimes surfaces as 0 in older bindings).
                try:
                    reason = None
                    props = event.get('event_props', {}) if isinstance(event, dict) else {}
                    reason = props.get('reason')
                    if reason in (0, 'eof', 'EOF'):
                        self.playbackFinished.emit()
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

    def shutdown(self):
        if self.mpv:
            self.mpv.terminate()
