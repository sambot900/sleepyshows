import sys
import os
import json
import time
import platform
import re
import html
import random
import glob
import hashlib
import shutil
import tempfile
import threading
import datetime

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QPushButton, QFileDialog, QTreeWidget, 
                               QTreeWidgetItem, QSplitter, QLabel, QSlider, QTabWidget,
                               QListWidget, QListWidgetItem, QInputDialog, QMessageBox, QMenu, QStackedWidget,
                               QDockWidget, QFrame, QSizePolicy, QToolButton, QStyle, QGridLayout,
                               QStyleOptionButton, QStyleOptionToolButton, QStylePainter, QStyleOptionSlider,
                               QLineEdit, QProgressBar, QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
                               QAbstractButton)
from PySide6.QtCore import Qt, QTimer, QSize, Signal, QPropertyAnimation, QEasingCurve, QRect, QEvent, QObject, QThread, Slot, QPoint, QEventLoop, QFileSystemWatcher
from PySide6.QtGui import QAction, QActionGroup, QIcon, QFont, QFontDatabase, QColor, QPalette, QPixmap, QPainter, QBrush, QLinearGradient, QRadialGradient, QPen, QPainterPath, QImage, QKeySequence, QShortcut, QCursor, QGuiApplication
from PySide6.QtCore import QUrl

from player_backend import MpvPlayer, MpvAudioPlayer
from PySide6.QtCore import QAbstractNativeEventFilter
from keep_awake import KeepAwakeInhibitor
from playlist_manager import PlaylistManager, VIDEO_EXTENSIONS, natural_sort_key
from ui_styles import DARK_THEME

from services import playlist_io


from services import web_mode_paths


THEME_COLOR = "#0e1a77"

# White strokes are intentionally transparent so the global background gradient shows through.
WHITE_STROKE_ALPHA = 0


class _WinFullscreenKeyFilter(QAbstractNativeEventFilter):
    """Windows-only key hook to toggle fullscreen on F.

    Qt key handling can miss key events when focus is inside a native child window
    (mpv embedding). A native event filter sees the Win32 message first.
    """

    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    VK_F = 0x46  # 'F'

    def __init__(self, main_window):
        super().__init__()
        self._mw = main_window

    def nativeEventFilter(self, eventType, message):
        try:
            if not sys.platform.startswith('win'):
                return False, 0
            if eventType not in ('windows_generic_MSG', 'windows_dispatcher_MSG'):
                return False, 0

            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message not in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                return False, 0

            if int(msg.wParam) != self.VK_F:
                return False, 0

            # Ignore auto-repeat (bit 30 set means key was already down).
            try:
                if int(msg.lParam) & (1 << 30):
                    return True, 0
            except Exception:
                pass

            try:
                QTimer.singleShot(0, self._mw.toggle_fullscreen)
            except Exception:
                pass

            # Consume the key so mpv/other widgets don't also process it.
            return True, 0
        except Exception:
            return False, 0


class BumpImageView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._mode = 'default'
        self._percent = None
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def clear(self):
        self._pixmap = None
        self._mode = 'default'
        self._percent = None
        self.update()

    def set_image(self, pixmap: QPixmap, *, mode: str = 'default', percent: float | None = None):
        self._pixmap = pixmap if (pixmap is not None and not pixmap.isNull()) else None
        self._mode = str(mode or 'default')
        self._percent = percent
        self.update()

    def _compute_target_rect(self, vw: int, vh: int, iw: int, ih: int):
        if vw <= 0 or vh <= 0 or iw <= 0 or ih <= 0:
            return QRect(0, 0, 0, 0)

        if self._mode == 'percent':
            p = self._percent
            try:
                p = float(p)
            except Exception:
                p = None
            if p is None:
                p = 20.0
            p = max(0.0, float(p)) / 100.0
            target_w = vw * p
            target_h = vh * p
            if target_w <= 0 or target_h <= 0:
                return QRect(0, 0, 0, 0)

            # Scale down maintaining aspect ratio until either width or height hits the target percent.
            s_w = target_w / float(iw)
            s_h = target_h / float(ih)
            scale = max(s_w, s_h)
            # Safety: never exceed viewport.
            scale = min(scale, min(vw / float(iw), vh / float(ih)))
            w = int(round(iw * scale))
            h = int(round(ih * scale))
            x = int(round((vw - w) / 2.0))
            y = int(round((vh - h) / 2.0))
            return QRect(x, y, w, h)

        # Default/lines behavior: fit-to-viewport with optional stretch.
        #
        # Key rule:
        # - Plain <img filename> (mode 'default') may upscale beyond 200% to fill
        #   the available viewport.
        # - Explicit sizing modes (esp. 'lines') must NOT upscale beyond 200%
        #   to avoid blowing up small UI/animation frames.
        fit_scale = min(vw / float(iw), vh / float(ih))
        if self._mode == 'default':
            scale = fit_scale
        else:
            scale = fit_scale if fit_scale <= 1.0 else min(2.0, fit_scale)

        w0 = float(iw) * float(scale)
        h0 = float(ih) * float(scale)

        w = w0
        h = h0

        # Stretch rules when one dimension matches and the other is deficient.
        if abs(h0 - vh) <= 2 and w0 < (vw - 2):
            # Height matches viewport, width is deficient.
            need = vw / float(w0) if w0 > 0 else 999.0
            if need <= 1.2:
                w = float(vw)
            else:
                w = float(w0) * 1.15
            h = float(vh)
        elif abs(w0 - vw) <= 2 and h0 < (vh - 2):
            # Width matches viewport, height is deficient.
            need = vh / float(h0) if h0 > 0 else 999.0
            if need <= 1.2:
                h = float(vh)
            else:
                h = float(h0) * 1.10
            w = float(vw)

        x = int(round((vw - w) / 2.0))
        y = int(round((vh - h) / 2.0))
        return QRect(x, y, int(round(w)), int(round(h)))

    def paintEvent(self, event):
        if self._pixmap is None or self._pixmap.isNull():
            return

        vw = int(self.width())
        vh = int(self.height())
        iw = int(self._pixmap.width())
        ih = int(self._pixmap.height())
        target = self._compute_target_rect(vw, vh, iw, ih)
        if target.isNull() or target.width() <= 0 or target.height() <= 0:
            return

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(target, self._pixmap)
        finally:
            p.end()


def _derive_theme_hsl():
    base = QColor(THEME_COLOR)
    h, s, l, _a = base.getHsl()
    if h < 0:
        h = 220
    s = max(120, min(255, int(s if s >= 0 else 180)))
    l = max(90, min(200, int(l if l >= 0 else 120)))
    return int(h), int(s), int(l)


def _with_theme_hue(h: int, s: int, l: int, deg_delta: int, *, sat_delta: int = 0, light_delta: int = 0, alpha: int = 255) -> QColor:
    hh = int((h + deg_delta) % 360)
    ss = max(0, min(255, int(s + sat_delta)))
    ll = max(0, min(255, int(l + light_delta)))
    c = QColor()
    c.setHsl(hh, ss, ll, max(0, min(255, int(alpha))))
    return c


def _find_controls_gradient_anchor(widget: QWidget) -> QWidget:
    """Find the widget whose coordinate system defines the shared gradient.

    Prefer the full-window gradient background if present; fall back to the controls bar.
    """
    w = widget
    while w is not None:
        try:
            name = w.objectName()
            if name == 'gradient_background':
                return w
        except Exception:
            pass
        w = w.parentWidget()

    w = widget
    while w is not None:
        try:
            if w.objectName() == 'controls_widget':
                return w
        except Exception:
            pass
        w = w.parentWidget()

    return widget.window() if widget is not None else None


def _make_alpha_outline_mask(icon_img: QImage, thickness: int) -> QImage:
    """Return an ARGB image where alpha is an outline ring around the icon alpha."""
    t = max(1, int(thickness))
    src = icon_img
    if src.format() != QImage.Format_ARGB32_Premultiplied:
        src = src.convertToFormat(QImage.Format_ARGB32_Premultiplied)

    w = src.width()
    h = src.height()
    outline = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    outline.fill(Qt.transparent)

    p = QPainter(outline)
    try:
        p.setRenderHint(QPainter.Antialiasing, False)
        # Dilate alpha by drawing multiple offsets.
        for dy in range(-t, t + 1):
            for dx in range(-t, t + 1):
                if dx == 0 and dy == 0:
                    continue
                p.drawImage(dx, dy, src)

        # Subtract original alpha to leave only the ring.
        p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
        p.drawImage(0, 0, src)
    finally:
        p.end()

    return outline


def _draw_gradient_outlined_icon(painter: QPainter, widget: QWidget, icon: QIcon, rect: QRect, outline_px: int):
    if icon.isNull() or rect.isNull():
        return
    pm = icon.pixmap(rect.size())
    if pm.isNull():
        return

    img = pm.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
    outline_mask = _make_alpha_outline_mask(img, outline_px)

    # Build a gradient-colored image aligned to the widget coordinate system.
    colored = QImage(rect.width(), rect.height(), QImage.Format_ARGB32_Premultiplied)
    colored.fill(Qt.transparent)
    gp = QPainter(colored)
    try:
        # Translate so that filling "rect" samples the right slice of the shared gradient.
        gp.translate(-rect.x(), -rect.y())
        _fill_rect_with_shared_modern_gradient(gp, widget, rect)
        gp.resetTransform()

        gp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        gp.drawImage(0, 0, outline_mask)
    finally:
        gp.end()

    painter.drawImage(rect.topLeft(), colored)


def _draw_gradient_outlined_text(painter: QPainter, widget: QWidget, rect: QRect, text: str, font: QFont, outline_px: int):
    text = (text or "").strip()
    if not text or rect.isNull():
        return

    painter.save()
    try:
        painter.setFont(font)

        fm = painter.fontMetrics()
        elided = fm.elidedText(text, Qt.ElideRight, rect.width())
        w = fm.horizontalAdvance(elided)
        h = fm.height()

        x = rect.center().x() - w // 2
        y = rect.top() + (rect.height() + fm.ascent() - fm.descent()) // 2

        path = QPainterPath()
        path.addText(QPoint(x, y), font, elided)

        # Use the shared gradient as the pen brush.
        anchor = _find_controls_gradient_anchor(widget)
        if anchor is None:
            return

        try:
            anchor_tl = widget.mapFromGlobal(anchor.mapToGlobal(QPoint(0, 0)))
            anchor_br = widget.mapFromGlobal(anchor.mapToGlobal(QPoint(anchor.width(), anchor.height())))
            anchor_rect = QRect(anchor_tl, anchor_br)
        except Exception:
            anchor_rect = widget.rect()

        h0, s0, l0 = _derive_theme_hsl()
        grad = QLinearGradient(anchor_rect.topLeft(), anchor_rect.bottomRight())
        # Keep this aligned with the shared stops.
        c1 = _with_theme_hue(h0, s0, l0, -25, sat_delta=45, light_delta=18)
        c2 = _with_theme_hue(h0, s0, l0, 35, sat_delta=35, light_delta=10)
        c3 = _with_theme_hue(h0, s0, l0, 85, sat_delta=25, light_delta=0)
        c4 = _with_theme_hue(h0, s0, l0, 160, sat_delta=15, light_delta=-8)
        c5 = _with_theme_hue(h0, s0, l0, 245, sat_delta=30, light_delta=8)
        c6 = _with_theme_hue(h0, s0, l0, 310, sat_delta=35, light_delta=6)
        grad.setColorAt(0.00, c1)
        grad.setColorAt(0.14, c1)
        grad.setColorAt(0.141, c2)
        grad.setColorAt(0.30, c2)
        grad.setColorAt(0.301, c3)
        grad.setColorAt(0.50, c3)
        grad.setColorAt(0.501, c4)
        grad.setColorAt(0.70, c4)
        grad.setColorAt(0.701, c5)
        grad.setColorAt(0.86, c5)
        grad.setColorAt(0.861, c6)
        grad.setColorAt(1.00, c6)

        pen = QPen(QBrush(grad), max(1, int(outline_px)))
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
    finally:
        painter.restore()


def _fill_rect_with_shared_modern_gradient(painter: QPainter, widget: QWidget, target_rect: QRect):
    """Fill a rect with the shared chunky gradient spanning the whole controls bar."""
    anchor = _find_controls_gradient_anchor(widget)
    if anchor is None:
        return

    try:
        anchor_tl = widget.mapFromGlobal(anchor.mapToGlobal(QPoint(0, 0)))
        anchor_br = widget.mapFromGlobal(anchor.mapToGlobal(QPoint(anchor.width(), anchor.height())))
        anchor_rect = QRect(anchor_tl, anchor_br)
    except Exception:
        anchor_rect = widget.rect()

    h, s, l = _derive_theme_hsl()

    # Base gradient (chunky portions with hard-ish transitions)
    grad = QLinearGradient(anchor_rect.topLeft(), anchor_rect.bottomRight())
    c1 = _with_theme_hue(h, s, l, -25, sat_delta=45, light_delta=18)
    c2 = _with_theme_hue(h, s, l, 35, sat_delta=35, light_delta=10)
    c3 = _with_theme_hue(h, s, l, 85, sat_delta=25, light_delta=0)
    c4 = _with_theme_hue(h, s, l, 160, sat_delta=15, light_delta=-8)
    c5 = _with_theme_hue(h, s, l, 245, sat_delta=30, light_delta=8)
    c6 = _with_theme_hue(h, s, l, 310, sat_delta=35, light_delta=6)

    # Hard stop pairs: two stops nearly at the same position.
    grad.setColorAt(0.00, c1)
    grad.setColorAt(0.14, c1)
    grad.setColorAt(0.141, c2)
    grad.setColorAt(0.30, c2)
    grad.setColorAt(0.301, c3)
    grad.setColorAt(0.50, c3)
    grad.setColorAt(0.501, c4)
    grad.setColorAt(0.70, c4)
    grad.setColorAt(0.701, c5)
    grad.setColorAt(0.86, c5)
    grad.setColorAt(0.861, c6)
    grad.setColorAt(1.00, c6)

    painter.fillRect(target_rect, QBrush(grad))

    # Layer "blobs" (larger and with sharper falloff for chunkier variation).
    aw = max(1, anchor_rect.width())
    ah = max(1, anchor_rect.height())
    blobs = [
        (QPoint(anchor_rect.left() + int(aw * 0.18), anchor_rect.top() + int(ah * 0.28)), int(aw * 0.55), 60),
        (QPoint(anchor_rect.left() + int(aw * 0.58), anchor_rect.top() + int(ah * 0.62)), int(aw * 0.60), 175),
        (QPoint(anchor_rect.left() + int(aw * 0.88), anchor_rect.top() + int(ah * 0.35)), int(aw * 0.52), 280),
    ]
    for center, radius_px, hue_delta in blobs:
        rg = QRadialGradient(center, float(max(10, radius_px)))
        blob = _with_theme_hue(h, s, l, hue_delta, sat_delta=55, light_delta=24, alpha=220)
        rg.setColorAt(0.0, blob)
        rg.setColorAt(0.62, blob)  # flatter center
        rg.setColorAt(1.0, _with_theme_hue(h, s, l, hue_delta, sat_delta=0, light_delta=0, alpha=0))
        painter.fillRect(target_rect, QBrush(rg))


def _paint_shared_modern_gradient(painter: QPainter, widget: QWidget, fill_rect: QRect, radius: int):
    """Paint a shared multi-color gradient that spans the whole controls bar.

    This makes each button sample a slice of the same background, like modern web gradients.
    """
    painter.save()
    try:
        clip = QPainterPath()
        clip.addRoundedRect(fill_rect, radius, radius)
        painter.setClipPath(clip)
        _fill_rect_with_shared_modern_gradient(painter, widget, fill_rect)
    finally:
        painter.restore()


def _paint_modern_background(painter: QPainter, widget: QWidget):
    """Paint the same chunky gradient style as a full background."""
    if widget is None:
        return

    h, s, l = _derive_theme_hsl()
    r = widget.rect()
    if r.isNull():
        return

    # Base gradient with hard-ish transitions.
    grad = QLinearGradient(r.topLeft(), r.bottomRight())
    c1 = _with_theme_hue(h, s, l, -25, sat_delta=45, light_delta=18)
    c2 = _with_theme_hue(h, s, l, 35, sat_delta=35, light_delta=10)
    c3 = _with_theme_hue(h, s, l, 85, sat_delta=25, light_delta=0)
    c4 = _with_theme_hue(h, s, l, 160, sat_delta=15, light_delta=-8)
    c5 = _with_theme_hue(h, s, l, 245, sat_delta=30, light_delta=8)
    c6 = _with_theme_hue(h, s, l, 310, sat_delta=35, light_delta=6)

    grad.setColorAt(0.00, c1)
    grad.setColorAt(0.14, c1)
    grad.setColorAt(0.141, c2)
    grad.setColorAt(0.30, c2)
    grad.setColorAt(0.301, c3)
    grad.setColorAt(0.50, c3)
    grad.setColorAt(0.501, c4)
    grad.setColorAt(0.70, c4)
    grad.setColorAt(0.701, c5)
    grad.setColorAt(0.86, c5)
    grad.setColorAt(0.861, c6)
    grad.setColorAt(1.00, c6)

    painter.fillRect(r, QBrush(grad))

    # Blobs for chunkier variation.
    aw = max(1, r.width())
    ah = max(1, r.height())
    blobs = [
        (QPoint(r.left() + int(aw * 0.20), r.top() + int(ah * 0.25)), int(aw * 0.60), 60),
        (QPoint(r.left() + int(aw * 0.55), r.top() + int(ah * 0.65)), int(aw * 0.70), 175),
        (QPoint(r.left() + int(aw * 0.88), r.top() + int(ah * 0.35)), int(aw * 0.58), 280),
    ]
    for center, radius_px, hue_delta in blobs:
        rg = QRadialGradient(center, float(max(10, radius_px)))
        blob = _with_theme_hue(h, s, l, hue_delta, sat_delta=55, light_delta=24, alpha=210)
        rg.setColorAt(0.0, blob)
        rg.setColorAt(0.62, blob)
        rg.setColorAt(1.0, _with_theme_hue(h, s, l, hue_delta, sat_delta=0, light_delta=0, alpha=0))
        painter.fillRect(r, QBrush(rg))


class GradientBackgroundWidget(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            _paint_modern_background(painter, self)
        finally:
            painter.end()


def _get_user_settings_path() -> str:
    home = os.path.expanduser("~")
    if platform.system().lower().startswith("win"):
        base = os.getenv("APPDATA") or os.path.join(home, "AppData", "Roaming")
        cfg_dir = os.path.join(base, "SleepyShows")
    elif platform.system().lower() == "darwin":
        cfg_dir = os.path.join(home, "Library", "Application Support", "SleepyShows")
    else:
        xdg = os.getenv("XDG_CONFIG_HOME")
        cfg_dir = os.path.join(xdg if xdg else os.path.join(home, ".config"), "SleepyShows")
    try:
        os.makedirs(cfg_dir, exist_ok=True)
    except Exception:
        pass
    return os.path.join(cfg_dir, "settings.json")


def _get_user_config_dir() -> str:
    try:
        return os.path.dirname(_get_user_settings_path())
    except Exception:
        home = os.path.expanduser("~")
        return os.path.join(home, ".config", "SleepyShows")


def _get_resume_state_path() -> str:
    try:
        return os.path.join(_get_user_config_dir(), 'resume_state.json')
    except Exception:
        home = os.path.expanduser('~')
        return os.path.join(home, '.config', 'SleepyShows', 'resume_state.json')


def _append_startup_geometry_log(window: QMainWindow):
    try:
        settings_path = _get_user_settings_path()
        cfg_dir = os.path.dirname(settings_path)
        os.makedirs(cfg_dir, exist_ok=True)
        log_path = os.path.join(cfg_dir, 'startup_geometry.log')

        try:
            s = window.screen()
        except Exception:
            s = None
        try:
            name = s.name() if s is not None else None
        except Exception:
            name = None
        try:
            geo = s.availableGeometry() if s is not None else None
        except Exception:
            geo = None

        avail = getattr(window, '_startup_available_size', None)
        req = getattr(window, '_startup_requested_size', None)
        try:
            actual = (int(window.size().width()), int(window.size().height()))
        except Exception:
            actual = None

        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        line = (
            f"{stamp} screen={name} avail={avail} requested={req} actual={actual} "
            f"maximized={bool(getattr(window, 'isMaximized', lambda: False)())} "
            f"screen_avail_geom={(geo.width(), geo.height()) if geo is not None else None}\n"
        )
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


class Spinner(QWidget):
    def __init__(self, parent=None, radius=18, line_width=4, speed_ms=40):
        super().__init__(parent)
        self._angle = 0
        self._radius = int(radius)
        self._line_width = int(line_width)
        self._timer = QTimer(self)
        self._timer.setInterval(int(speed_ms))
        self._timer.timeout.connect(self._tick)

        size = (self._radius + self._line_width) * 2
        self.setFixedSize(size, size)

    def start(self):
        if not self._timer.isActive():
            self._timer.start()
            self.show()

    def stop(self):
        if self._timer.isActive():
            self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pen = painter.pen()
        pen.setWidth(self._line_width)
        pen.setColor(QColor(255, 255, 255, 220))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        pad = self._line_width
        rect = QRect(pad, pad, self.width() - 2 * pad, self.height() - 2 * pad)

        # Draw an arc segment to look like a spinner.
        span_deg = 280
        start_deg = -self._angle
        painter.drawArc(rect, int(start_deg * 16), int(-span_deg * 16))


class ToggleSwitch(QAbstractButton):
    """A small switch-style toggle (not a checkbox).

    Visual: pill track + sliding thumb.
    """

    def __init__(self, parent=None, *, width: int = 54, height: int = 30):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._w = int(width)
        self._h = int(height)
        self.setFixedSize(self._w, self._h)

    def sizeHint(self):
        return QSize(self._w, self._h)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)

            r = self.rect().adjusted(1, 1, -1, -1)
            radius = int(r.height() / 2)

            # Track
            track_color = QColor(THEME_COLOR) if self.isChecked() else QColor('#444444')
            p.setPen(Qt.NoPen)
            p.setBrush(track_color)
            p.drawRoundedRect(r, radius, radius)

            # Thumb
            pad = 3
            d = r.height() - 2 * pad
            x_off = r.right() - pad - d if self.isChecked() else r.left() + pad
            thumb_rect = QRect(int(x_off), int(r.top() + pad), int(d), int(d))
            p.setBrush(QColor('#e0e0e0'))
            p.drawEllipse(thumb_rect)
        finally:
            p.end()


class TriStrokeButton(QPushButton):
    def __init__(self, *args, radius=10, stroke=2, **kwargs):
        super().__init__(*args, **kwargs)
        self._radius = int(radius)
        self._stroke = int(stroke)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)

            r = self.rect()
            s = self._stroke

            # Thinner than before (roughly half of prior default).
            gradient_w = max(3, int(round(s * 1.5)))

            # No fill: let the global background show through.

            painter.setBrush(Qt.NoBrush)

            # Outer stroke: paint the shared gradient into the stroke ring.
            outer_rect = r.adjusted(gradient_w // 2, gradient_w // 2, -(gradient_w // 2), -(gradient_w // 2))
            inner_inset = max(1, gradient_w)
            inner_rect = outer_rect.adjusted(inner_inset, inner_inset, -inner_inset, -inner_inset)

            ring = QPainterPath()
            ring.addRoundedRect(outer_rect, self._radius, self._radius)
            cutout = QPainterPath()
            cutout.addRoundedRect(inner_rect, max(1, self._radius - gradient_w), max(1, self._radius - gradient_w))
            ring = ring.subtracted(cutout)

            painter.save()
            try:
                painter.setClipPath(ring)
                _fill_rect_with_shared_modern_gradient(painter, self, self.rect())
            finally:
                painter.restore()

            # Draw icon/text normally (no gradient stroke on player icons).
            opt = QStyleOptionButton()
            opt.initFrom(self)
            opt.text = self.text()
            opt.icon = self.icon()
            opt.iconSize = self.iconSize()
            # Ensure the label draws in white.
            try:
                opt.palette.setColor(QPalette.ButtonText, Qt.white)
                opt.palette.setColor(QPalette.WindowText, Qt.white)
            except Exception:
                pass
            self.style().drawControl(QStyle.CE_PushButtonLabel, opt, painter, self)
        finally:
            painter.end()

class TriStrokeToolButton(QToolButton):
    def __init__(self, *args, radius=10, stroke=2, **kwargs):
        super().__init__(*args, **kwargs)
        self._radius = int(radius)
        self._stroke = int(stroke)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)

            r = self.rect()
            s = self._stroke

            gradient_w = max(3, int(round(s * 1.5)))

            # No fill: let the global background show through.
            painter.setBrush(Qt.NoBrush)

            outer_rect = r.adjusted(gradient_w // 2, gradient_w // 2, -(gradient_w // 2), -(gradient_w // 2))
            inner_inset = max(1, gradient_w)
            inner_rect = outer_rect.adjusted(inner_inset, inner_inset, -inner_inset, -inner_inset)

            ring = QPainterPath()
            ring.addRoundedRect(outer_rect, self._radius, self._radius)
            cutout = QPainterPath()
            cutout.addRoundedRect(inner_rect, max(1, self._radius - gradient_w), max(1, self._radius - gradient_w))
            ring = ring.subtracted(cutout)

            painter.save()
            try:
                painter.setClipPath(ring)
                _fill_rect_with_shared_modern_gradient(painter, self, self.rect())
            finally:
                painter.restore()

            # For TextUnderIcon, custom draw to control vertical alignment and spacing.
            if self.toolButtonStyle() == Qt.ToolButtonTextUnderIcon:
                content = r.adjusted(gradient_w + 6, gradient_w + 6, -(gradient_w + 6), -(gradient_w + 6))
                icon_size = self.iconSize()
                if icon_size.isEmpty():
                    icon_size = QSize(24, 24)

                icon_x = content.center().x() - icon_size.width() // 2
                icon_y = content.top() + 9  # slightly higher, keep icon/text spacing
                icon_rect = QRect(icon_x, icon_y, icon_size.width(), icon_size.height())
                icon_pm = self.icon().pixmap(icon_size)
                painter.drawPixmap(icon_rect.topLeft(), icon_pm)

                text = self.text() or ""
                text_rect = QRect(content.left(), icon_y + icon_size.height() + 4, content.width(), content.bottom() - (icon_y + icon_size.height() + 4))
                painter.setPen(QColor(255, 255, 255))
                fm = painter.fontMetrics()
                painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, fm.elidedText(text, Qt.ElideRight, text_rect.width()))
            else:
                opt = QStyleOptionToolButton()
                opt.initFrom(self)
                opt.text = self.text()
                opt.icon = self.icon()
                opt.iconSize = self.iconSize()
                opt.toolButtonStyle = self.toolButtonStyle()
                if self.isDown():
                    opt.state |= QStyle.State_Sunken
                if self.underMouse():
                    opt.state |= QStyle.State_MouseOver

                self.style().drawControl(QStyle.CE_ToolButtonLabel, opt, painter, self)
        finally:
            painter.end()


class BumpsModeWidget(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Settings")
        title.setStyleSheet("font-size: 28px; font-weight: bold; color: white;")
        layout.addWidget(title)

        self.btn_clear_history = QPushButton("Clear Viewing History…")
        self.btn_clear_history.clicked.connect(self.main_window.show_clear_viewing_history_dialog)
        layout.addWidget(self.btn_clear_history)

        # --- Sound settings ---
        def add_toggle_row(label_text, initial_checked, on_toggle):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)

            lbl = QLabel(label_text)
            lbl.setStyleSheet("font-size: 16px; color: white;")

            btn = ToggleSwitch()
            btn.setChecked(bool(initial_checked))
            btn.toggled.connect(lambda checked: on_toggle(bool(checked)))

            row.addWidget(btn)
            row.addWidget(lbl)
            row.addStretch(1)
            layout.addLayout(row)
            return btn, lbl

        self.btn_startup_crickets, self.lbl_startup_crickets = add_toggle_row(
            "Startup cricket sound",
            getattr(self.main_window, "startup_crickets_enabled", True),
            lambda checked: self.main_window.set_startup_crickets_enabled(checked),
        )

        self.btn_normalize_audio, self.lbl_normalize_audio = add_toggle_row(
            "Normalize volume",
            getattr(self.main_window, "normalize_audio_enabled", False),
            lambda checked: self.main_window.set_normalize_audio_enabled(checked),
        )

        initial_web_mode = (
            str(getattr(self.main_window, 'playback_mode', 'portable') or 'portable').strip().lower() == 'web'
        )

        self.btn_web_mode, self.lbl_playback_mode = add_toggle_row(
            "Playback mode: Web" if initial_web_mode else "Playback mode: Portable",
            initial_web_mode,
            lambda checked: (self.main_window.set_web_mode_enabled(checked), self.refresh_status()),
        )

        # Web mode configuration (filesystem-based via mounted share)

        web_files_row = QHBoxLayout()
        web_files_row.setContentsMargins(0, 0, 0, 0)
        web_files_row.setSpacing(10)

        web_files_lbl = QLabel("Web Files Root:")
        web_files_lbl.setStyleSheet("font-size: 16px; color: white;")

        self.input_web_files_root = QLineEdit()
        self.input_web_files_root.setText(str(getattr(self.main_window, 'web_files_root', '') or ''))
        self.input_web_files_root.setPlaceholderText("/mnt/shows  (or \\\\10.0.0.210\\shows on Windows)")
        self.input_web_files_root.setStyleSheet(
            "QLineEdit { background: #333; color: white; padding: 6px 10px; border: 1px solid #111; border-radius: 4px; }"
            "QLineEdit:focus { border: 1px solid #0e1a77; }"
        )

        def _commit_web_files_root():
            try:
                self.main_window.set_web_files_root(self.input_web_files_root.text())
            except Exception:
                pass

        self.input_web_files_root.editingFinished.connect(_commit_web_files_root)
        web_files_row.addWidget(web_files_lbl)
        web_files_row.addWidget(self.input_web_files_root, 1)
        layout.addLayout(web_files_row)

        # Global interludes/interstitials folder
        inter_row = QHBoxLayout()
        inter_row.setContentsMargins(0, 0, 0, 0)
        inter_row.setSpacing(10)

        inter_lbl = QLabel("Interludes Folder:")
        inter_lbl.setStyleSheet("font-size: 16px; color: white;")

        self.input_interludes_dir = QLineEdit()
        self.input_interludes_dir.setText(str(getattr(self.main_window, '_interstitials_dir', '') or ''))
        self.input_interludes_dir.setPlaceholderText("Auto-detected: Sleepy Shows Data/TV Vibe/interludes")
        self.input_interludes_dir.setStyleSheet(
            "QLineEdit { background: #333; color: white; padding: 6px 10px; border: 1px solid #111; border-radius: 4px; }"
            "QLineEdit:focus { border: 1px solid #0e1a77; }"
        )

        def _commit_interludes_dir():
            try:
                self.main_window.set_interludes_folder(self.input_interludes_dir.text())
            except Exception:
                pass
            try:
                self.refresh_status()
            except Exception:
                pass

        self.input_interludes_dir.editingFinished.connect(_commit_interludes_dir)

        btn_browse_inter = QPushButton("Browse…")
        btn_browse_inter.clicked.connect(lambda: self.main_window.choose_interstitial_folder())

        inter_row.addWidget(inter_lbl)
        inter_row.addWidget(self.input_interludes_dir, 1)
        inter_row.addWidget(btn_browse_inter)
        layout.addLayout(inter_row)

        self.lbl_interludes = QLabel("Interludes: (not set)")
        self.lbl_interludes.setStyleSheet("font-size: 14px; color: #e0e0e0;")
        layout.addWidget(self.lbl_interludes)

        # Auto-config external drive name
        drive_row = QHBoxLayout()
        drive_row.setContentsMargins(0, 0, 0, 0)
        drive_row.setSpacing(10)

        drive_lbl = QLabel("Auto-Config External Drive Name:")
        drive_lbl.setStyleSheet("font-size: 16px; color: white;")

        self.input_auto_drive = QLineEdit()
        self.input_auto_drive.setText(str(getattr(self.main_window, 'auto_config_volume_label', 'T7') or 'T7'))
        self.input_auto_drive.setPlaceholderText("T7")
        self.input_auto_drive.setStyleSheet(
            "QLineEdit { background: #333; color: white; padding: 6px 10px; border: 1px solid #111; border-radius: 4px; }"
            "QLineEdit:focus { border: 1px solid #0e1a77; }"
        )

        def _commit_drive_name():
            try:
                self.main_window.set_auto_config_volume_label(self.input_auto_drive.text())
            except Exception:
                pass

        self.input_auto_drive.editingFinished.connect(_commit_drive_name)

        drive_row.addWidget(drive_lbl)
        drive_row.addWidget(self.input_auto_drive, 1)
        layout.addLayout(drive_row)

        info = QLabel("Global bumps play between episodes.")
        info.setStyleSheet("font-size: 14px; color: #e0e0e0;")
        layout.addWidget(info)

        self.btn_scripts = QPushButton("Reload Local Bump Scripts")
        self.btn_scripts.clicked.connect(self.main_window.choose_bump_scripts)
        layout.addWidget(self.btn_scripts)

        self.lbl_scripts = QLabel("Scripts: 0")
        self.lbl_scripts.setStyleSheet("font-size: 16px; color: white;")
        layout.addWidget(self.lbl_scripts)

        layout.addSpacing(10)

        self.btn_music = QPushButton("Set Bump Music Folder")
        self.btn_music.clicked.connect(self.main_window.choose_bump_music)
        layout.addWidget(self.btn_music)

        self.lbl_music = QLabel("Music: 0")
        self.lbl_music.setStyleSheet("font-size: 16px; color: white;")
        layout.addWidget(self.lbl_music)

        layout.addSpacing(10)

        self.btn_images = QPushButton("Set Bump Images Folder")
        self.btn_images.clicked.connect(self.main_window.choose_bump_images)
        layout.addWidget(self.btn_images)

        self.lbl_images = QLabel("Images: (not set)")
        self.lbl_images.setStyleSheet("font-size: 14px; color: #e0e0e0;")
        layout.addWidget(self.lbl_images)

        layout.addSpacing(10)

        self.btn_audio_fx = QPushButton("Set Bump Audio FX Folder")
        self.btn_audio_fx.clicked.connect(self.main_window.choose_bump_audio_fx)
        layout.addWidget(self.btn_audio_fx)

        self.lbl_audio_fx = QLabel("Audio FX: (not set)")
        self.lbl_audio_fx.setStyleSheet("font-size: 14px; color: #e0e0e0;")
        layout.addWidget(self.lbl_audio_fx)

        layout.addStretch(1)

    def refresh_status(self):
        def _count_files(folder, exts):
            try:
                folder = str(folder or '')
                if not folder or not os.path.isdir(folder):
                    return 0
                exts_l = {str(e).lower() for e in (exts or set())}
                n = 0
                for root, _, files in os.walk(folder):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in exts_l:
                            n += 1
                return int(n)
            except Exception:
                return 0

        image_exts = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}
        audio_exts = {'.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.opus', '.webm', '.mp4'}

        try:
            scripts_n = len(self.main_window.playlist_manager.bump_manager.bump_scripts)
        except Exception:
            scripts_n = 0

        try:
            music_n = len(self.main_window.playlist_manager.bump_manager.music_files)
        except Exception:
            music_n = 0

        self.lbl_scripts.setText(f"Scripts: {scripts_n}")
        self.lbl_music.setText(f"Music: {music_n}")

        try:
            img_dir = getattr(self.main_window, 'bump_images_dir', None)
            img_n = _count_files(img_dir, image_exts)
            self.lbl_images.setText(f"Images: {img_n}")
        except Exception:
            pass

        try:
            fx_dir = getattr(self.main_window, 'bump_audio_fx_dir', None)
            fx_n = _count_files(fx_dir, audio_exts)
            self.lbl_audio_fx.setText(f"Audio FX: {fx_n}")
        except Exception:
            pass

        try:
            inter_dir = str(getattr(self.main_window, '_interstitials_dir', '') or '').strip()
        except Exception:
            inter_dir = ''
        try:
            inter_n = len(list(getattr(self.main_window.playlist_manager, 'interstitials', []) or []))
        except Exception:
            inter_n = 0
        try:
            if inter_dir:
                self.lbl_interludes.setText(f"Interludes: {inter_n}")
            else:
                self.lbl_interludes.setText("Interludes: (not set)")
        except Exception:
            pass

        # Sync toggle state from main window settings.
        try:
            if hasattr(self, 'btn_startup_crickets'):
                self.btn_startup_crickets.setChecked(bool(getattr(self.main_window, 'startup_crickets_enabled', True)))
            if hasattr(self, 'btn_normalize_audio'):
                self.btn_normalize_audio.setChecked(bool(getattr(self.main_window, 'normalize_audio_enabled', False)))
            if hasattr(self, 'btn_web_mode'):
                self.btn_web_mode.setChecked(
                    (str(getattr(self.main_window, 'playback_mode', 'portable') or 'portable').strip().lower() == 'web')
                )
            if hasattr(self, 'lbl_playback_mode'):
                is_web = (
                    str(getattr(self.main_window, 'playback_mode', 'portable') or 'portable').strip().lower() == 'web'
                )
                self.lbl_playback_mode.setText("Playback mode: Web" if is_web else "Playback mode: Portable")
            if hasattr(self, 'input_interludes_dir'):
                self.input_interludes_dir.setText(str(getattr(self.main_window, '_interstitials_dir', '') or ''))
        except Exception:
            pass


def _next_shuffle_mode(mode):
    order = ['off', 'standard', 'season']
    try:
        i = order.index(mode)
    except ValueError:
        i = 0
    return order[(i + 1) % len(order)]

# --- Path Helpers ---
def get_asset_path(filename):
    # Resolves asset path whether running as script or frozen exe
    if getattr(sys, 'frozen', False):
        base_dir = sys._MEIPASS
    else:
        # src/main.py -> src -> root
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    path = os.path.join(base_dir, 'assets', filename)
    if not os.path.exists(path):
        print(f"DEBUG: Asset missing at {path}")
    return path


def _darker_hex(hex_color: str, factor: float = 0.5) -> str:
    """Return a darker hex color (factor in [0..1], where 0.5 is 50% darker)."""
    try:
        c = QColor(str(hex_color or '').strip())
        if not c.isValid():
            return str(hex_color)
        f = float(factor)
        f = max(0.0, min(1.0, f))
        r = int(round(c.red() * f))
        g = int(round(c.green() * f))
        b = int(round(c.blue() * f))
        return QColor(r, g, b).name()
    except Exception:
        return str(hex_color)


class StartupLoadingScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Dialog
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._progress = 0
        self._status = "Starting..."

        self._tick_timer = None
        self._run_loop = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignCenter)

        self.logo = QLabel()
        self.logo.setAlignment(Qt.AlignCenter)
        self.logo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._logo_pm = QPixmap(get_asset_path("sleepy-shows-logo.png"))
        layout.addWidget(self.logo, 0, Qt.AlignHCenter)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(22)
        self.progress_bar.setMaximumWidth(420)
        self.progress_bar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.progress_bar.setStyleSheet(
            "QProgressBar{background: rgba(0,0,0,90); border: 1px solid rgba(255,255,255,60); border-radius: 10px;}"
            f"QProgressBar::chunk{{background: {THEME_COLOR}; border-radius: 10px;}}"
        )
        layout.addWidget(self.progress_bar, 0, Qt.AlignHCenter)

        self.percent_label = QLabel("0%")
        self.percent_label.setAlignment(Qt.AlignCenter)
        self.percent_label.setStyleSheet("color: white; font-size: 16px; font-weight: bold;")
        layout.addWidget(self.percent_label, 0, Qt.AlignHCenter)

        self.status_label = QLabel(self._status)
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: rgba(255,255,255,210); font-size: 14px;")
        layout.addWidget(self.status_label, 0, Qt.AlignHCenter)

        # Smaller default splash, with responsive logo scaling.
        self.setMinimumSize(520, 360)
        self.resize(620, 420)

        self._update_logo_pixmap()

    def resizeEvent(self, event):
        try:
            self._update_logo_pixmap()
        except Exception:
            pass
        return super().resizeEvent(event)

    def _update_logo_pixmap(self):
        pm = getattr(self, '_logo_pm', None)
        if pm is None or pm.isNull():
            self.logo.clear()
            return

        # Keep the logo comfortably inside the splash.
        avail_w = max(1, int(self.width() * 0.82))
        avail_h = max(1, int(self.height() * 0.33))
        scaled = pm.scaled(avail_w, avail_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.logo.setPixmap(scaled)

    def paintEvent(self, event):
        painter = QPainter(self)
        grad = QLinearGradient(0, 0, 0, self.height())
        top = QColor(THEME_COLOR)
        bottom = QColor(_darker_hex(THEME_COLOR, 0.5))
        grad.setColorAt(0, top)
        grad.setColorAt(1, bottom)
        painter.fillRect(self.rect(), grad)

    @Slot(int, str)
    def set_progress(self, percent: int, status: str):
        try:
            p = int(percent)
        except Exception:
            p = 0
        p = max(0, min(100, p))
        self._progress = p
        self._status = str(status or "")
        self.progress_bar.setValue(p)
        self.percent_label.setText(f"{p}%")
        self.status_label.setText(self._status)

    def run_blocking_fake_load(self, app=None, *, min_seconds: float = 2.0, max_seconds: float = 4.0):
        """Block until a fake progress bar completes.

        This keeps the UI responsive by running a nested Qt event loop.
        """
        if min_seconds < 0.2:
            min_seconds = 0.2
        if max_seconds < min_seconds:
            max_seconds = min_seconds

        total = float(random.uniform(float(min_seconds), float(max_seconds)))
        hang_percent = float(random.uniform(83.0, 88.0))
        hang_seconds = float(min(0.55, max(0.18, total * 0.12)))
        active = max(0.05, total - hang_seconds)
        pre = active * (hang_percent / 100.0)
        post = max(0.01, active - pre)

        start = time.monotonic()
        self.set_progress(0, "Loading...")

        loop = QEventLoop()
        self._run_loop = loop

        # Smooth but not too fast.
        timer = QTimer(self)
        timer.setInterval(16)
        self._tick_timer = timer

        def _tick():
            elapsed = max(0.0, time.monotonic() - start)
            if elapsed < pre:
                p = (elapsed / pre) * hang_percent if pre > 0 else hang_percent
            elif elapsed < (pre + hang_seconds):
                p = hang_percent
            else:
                tail = elapsed - pre - hang_seconds
                p = hang_percent + (tail / post) * (100.0 - hang_percent) if post > 0 else 100.0

            p_i = int(round(max(0.0, min(100.0, p))))

            # Keep the "hang" feeling: freeze the numeric readout too.
            self.set_progress(p_i, "Loading...")

            if p_i >= 100:
                try:
                    timer.stop()
                except Exception:
                    pass
                self.set_progress(100, "Loaded")
                try:
                    loop.quit()
                except Exception:
                    pass

        timer.timeout.connect(_tick)
        timer.start()

        try:
            if app is not None:
                app.processEvents()
        except Exception:
            pass

        loop.exec()



def get_local_bumps_scripts_dir():
    # Store bump scripts alongside the app, similar to the local `playlists/` folder.
    if getattr(sys, 'frozen', False):
        # In a frozen build, sys._MEIPASS points to the unpacked internal bundle.
        # Use the executable directory so the folder is user-visible and persistent.
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, 'bumps')


def get_local_playlists_dir() -> str:
    """Return the directory where playlist JSONs should be stored.

    - Source/dev runs: use the repo-local `playlists/` folder.
    - Frozen builds: use a writable per-user app data folder.
    """
    try:
        if getattr(sys, 'frozen', False):
            home = os.path.expanduser('~')
            if platform.system().lower().startswith('win'):
                base = os.getenv('APPDATA') or os.path.join(home, 'AppData', 'Roaming')
                root = os.path.join(base, 'SleepyShows')
            elif platform.system().lower() == 'darwin':
                root = os.path.join(home, 'Library', 'Application Support', 'SleepyShows')
            else:
                xdg = os.getenv('XDG_CONFIG_HOME')
                root = os.path.join(xdg if xdg else os.path.join(home, '.config'), 'SleepyShows')
            folder = os.path.join(root, 'playlists')
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            folder = os.path.join(base_dir, 'playlists')

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass
        return folder
    except Exception:
        return os.path.join(os.getcwd(), 'playlists')


def resolve_playlist_path(filename: str) -> str:
    """Resolve a playlist filename/relative path to an absolute path in the playlists dir."""
    try:
        p = str(filename or '').strip()
    except Exception:
        p = ''
    if not p:
        return ''
    try:
        if os.path.isabs(p):
            return p
    except Exception:
        pass

    # Accept callers passing "playlists/<name>.json".
    try:
        base = os.path.basename(p)
    except Exception:
        base = p
    return os.path.join(get_local_playlists_dir(), base)


def migrate_legacy_playlist_filenames() -> bool:
    """Best-effort migration for older playlist filenames.

    Historically some users had playlists named like `koth.json`. The app now uses
    show-name JSONs (e.g. `King of the Hill.json`). This migrates/copies legacy
    files into the canonical names without deleting the originals.
    """
    try:
        playlists_dir = get_local_playlists_dir()
        if not playlists_dir or not os.path.isdir(playlists_dir):
            return False

        mapping = {
            'koth.json': 'King of the Hill.json',
            'king_of_the_hill.json': 'King of the Hill.json',
            'king of the hill.json': 'King of the Hill.json',
            'bobs.json': "Bob's Burgers.json",
            'bob.json': "Bob's Burgers.json",
            'bobs_burgers.json': "Bob's Burgers.json",
            "bob's burgers.json": "Bob's Burgers.json",
        }

        changed = False
        for src_name, dst_name in mapping.items():
            src_path = os.path.join(playlists_dir, src_name)
            dst_path = os.path.join(playlists_dir, dst_name)
            try:
                if not os.path.exists(src_path):
                    continue
                if os.path.exists(dst_path):
                    continue

                with open(src_path, 'rb') as rf:
                    data = rf.read()
                tmp = dst_path + '.tmp'
                with open(tmp, 'wb') as wf:
                    wf.write(data)
                os.replace(tmp, dst_path)
                changed = True
            except Exception:
                continue

        return changed
    except Exception:
        return False


def migrate_playlists_to_global_interludes(*, playlists_dir: str | None = None) -> int:
    """Remove obsolete per-playlist interludes fields.

    Interludes/interstitials are now configured globally in user settings.
    Older playlist JSONs may have persisted these keys:
    - interlude_folder
    - interstitial_folder

    This migration strips those keys from playlist files (excluding
    exposure_scores.json) and returns the number of files modified.
    """
    try:
        base = str(playlists_dir or '').strip()
    except Exception:
        base = ''
    if not base:
        base = get_local_playlists_dir()

    try:
        if not base or not os.path.isdir(base):
            return 0
    except Exception:
        return 0

    changed = 0
    try:
        names = list(os.listdir(base))
    except Exception:
        return 0

    for name in names:
        try:
            low = str(name).lower()
        except Exception:
            continue
        if not low.endswith('.json'):
            continue
        if low == 'exposure_scores.json':
            continue

        path = os.path.join(base, str(name))
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        had = False
        if 'interlude_folder' in data:
            try:
                del data['interlude_folder']
                had = True
            except Exception:
                pass
        if 'interstitial_folder' in data:
            try:
                del data['interstitial_folder']
                had = True
            except Exception:
                pass

        if not had:
            continue

        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
            changed += 1
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            continue

    return int(changed)


def _windows_iter_drive_roots():
    try:
        import ctypes
        from ctypes import wintypes

        # Bitmask of drives, where bit 0 = A:, bit 1 = B:, etc.
        get_logical_drives = ctypes.windll.kernel32.GetLogicalDrives
        get_logical_drives.restype = wintypes.DWORD
        mask = int(get_logical_drives())

        for i in range(26):
            if mask & (1 << i):
                letter = chr(ord('A') + i)
                yield f"{letter}:\\"
    except Exception:
        return


def _windows_volume_label(drive_root):
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        get_vol = kernel32.GetVolumeInformationW
        get_vol.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        get_vol.restype = wintypes.BOOL

        vol_name_buf = ctypes.create_unicode_buffer(261)
        fs_name_buf = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        max_comp_len = wintypes.DWORD()
        fs_flags = wintypes.DWORD()

        ok = get_vol(
            drive_root,
            vol_name_buf,
            len(vol_name_buf),
            ctypes.byref(serial),
            ctypes.byref(max_comp_len),
            ctypes.byref(fs_flags),
            fs_name_buf,
            len(fs_name_buf),
        )
        if not ok:
            return ""
        return (vol_name_buf.value or "").strip()
    except Exception:
        return ""


def _iter_mount_roots_for_label(label):
    """Yield potential mount roots for a volume label across OSes."""
    label = (label or "").strip()
    if not label:
        return

    system = platform.system().lower()
    if system == 'windows':
        for drive_root in _windows_iter_drive_roots() or []:
            if _windows_volume_label(drive_root).lower() == label.lower():
                yield drive_root
        return

    if system == 'darwin':
        candidate = os.path.join('/Volumes', label)
        if os.path.isdir(candidate):
            yield candidate
        return

    # Linux (and others): common mount locations.
    user = os.environ.get('USER') or os.environ.get('LOGNAME') or ""
    candidates = []
    if user:
        candidates.extend([
            os.path.join('/run/media', user, label),
            os.path.join('/media', user, label),
        ])
    candidates.extend([
        os.path.join('/mnt', label),
        os.path.join('/media', label),
    ])
    for p in candidates:
        if os.path.isdir(p):
            yield p


def _volume_label_is_mounted(label: str) -> bool:
    """Return True if we can find any mount root for the given label.

    This is used to decide whether Portable mode should be the default at startup.
    """
    try:
        lab = str(label or '').strip()
    except Exception:
        lab = ''
    if not lab:
        return False

    try:
        for p in _iter_mount_roots_for_label(lab) or []:
            try:
                if p and os.path.isdir(p) and os.access(p, os.R_OK):
                    return True
            except Exception:
                continue
    except Exception:
        return False

    return False


def _iter_mount_roots_fallback():
    """Yield mount roots to probe when label-based detection is unavailable."""
    system = platform.system().lower()
    if system == 'windows':
        for drive_root in _windows_iter_drive_roots() or []:
            yield drive_root
        return

    if system == 'darwin':
        base = '/Volumes'
        if os.path.isdir(base):
            try:
                for name in os.listdir(base):
                    p = os.path.join(base, name)
                    if os.path.isdir(p):
                        yield p
            except Exception:
                pass
        return

    # Linux (and others)
    user = os.environ.get('USER') or os.environ.get('LOGNAME') or ""
    bases = []
    if user:
        bases.extend([
            os.path.join('/run/media', user),
            os.path.join('/media', user),
        ])
    bases.extend(['/run/media', '/media', '/mnt'])

    seen = set()
    for base in bases:
        if not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                p = os.path.join(base, name)
                if os.path.isdir(p) and p not in seen:
                    seen.add(p)
                    yield p
        except Exception:
            continue


def _looks_like_show_folder(folder_path):
    """Fast check: find at least one video file within a few directory levels."""
    if not folder_path or not os.path.isdir(folder_path):
        return False
    base_depth = folder_path.rstrip(os.sep).count(os.sep)

    try:
        for root, dirs, files in os.walk(folder_path):
            depth = root.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= 3:
                # Don't descend deeper than 3 levels.
                dirs[:] = []

            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    return True
        return False
    except Exception:
        return False


def auto_detect_default_show_sources(volume_label='T7'):
    """Best-effort detection for known show folders on an external drive.

    Returns a list of folder paths suitable to pass to PlaylistManager.add_source().
    """
    show_folders = auto_detect_show_folders(volume_label=volume_label)
    if show_folders:
        # Preserve stable ordering.
        ordered = []
        for key in ("King of the Hill", "Bob's Burgers", "Squidbillies", "Aqua Teen Hunger Force"):
            p = show_folders.get(key)
            if p:
                ordered.append(p)
        # Add any additional detected shows.
        for k, p in show_folders.items():
            if p and p not in ordered:
                ordered.append(p)
        return ordered

    # Backward-compatible fallback (should be rare; kept for safety)
    # Folder name patterns to try, relative to a mount root.
    show_patterns = [
        # (display, [relative paths to probe])
        ("King of the Hill", [
            # Preferred layout
            os.path.join('Shows', 'King of the Hill', 'Episodes'),
            os.path.join('Shows', 'King of the Hill', 'King of the Hill'),
            os.path.join('Shows', 'King of the Hill'),
            os.path.join('King of the Hill', 'Episodes'),
            # Older/fallback layouts
            os.path.join('King of the Hill', 'King of the Hill'),
            os.path.join('King of the Hill'),
        ]),
        ("Bob's Burgers", [
            # Preferred layout (note: "Episodesl" per user)
            os.path.join('Shows', "Bob's Burgers", 'Episodesl'),
            os.path.join('Shows', "Bob's Burgers", 'Episodes'),
            os.path.join('Shows', "Bob's Burgers", "Bob's Burgers"),
            os.path.join('Shows', "Bob's Burgers", "Bob's Burgersl"),
            os.path.join('Shows', "Bob's Burgers"),
            os.path.join('Shows', "Bob's Burgersl"),
            os.path.join('Shows', 'Bobs Burgers', 'Episodesl'),
            os.path.join('Shows', 'Bobs Burgers', 'Episodes'),
            os.path.join('Shows', 'Bobs Burgers', 'Bobs Burgers'),
            os.path.join('Shows', 'Bobs Burgers'),
            os.path.join("Bob's Burgers", 'Episodesl'),
            # Common fallback in case of spelling differences
            os.path.join("Bob's Burgers", 'Episodes'),
            # Older/fallback layouts
            os.path.join("Bob's Burgers", "Bob's Burgers"),
            os.path.join("Bob's Burgers", "Bob's Burgersl"),
            os.path.join("Bob's Burgers"),
            os.path.join("Bob's Burgersl"),
            os.path.join('Bobs Burgers', 'Episodesl'),
            os.path.join('Bobs Burgers', 'Episodes'),
            os.path.join('Bobs Burgers', 'Bobs Burgers'),
            os.path.join('Bobs Burgers'),
        ]),
        ("Squidbillies", [
            os.path.join('Shows', 'Squidbillies', 'Episodes'),
            os.path.join('Shows', 'Squidbillies'),
            os.path.join('Squidbillies', 'Episodes'),
            os.path.join('Squidbillies'),
        ]),
        ("Aqua Teen Hunger Force", [
            os.path.join('Shows', 'Aqua Teen Hunger Force', 'Episodes'),
            os.path.join('Shows', 'Aqua Teen Hunger Force'),
            os.path.join('Aqua Teen Hunger Force', 'Episodes'),
            os.path.join('Aqua Teen Hunger Force'),
            # Common abbreviation fallback
            os.path.join('Shows', 'ATHF', 'Episodes'),
            os.path.join('Shows', 'ATHF'),
            os.path.join('ATHF', 'Episodes'),
            os.path.join('ATHF'),
        ]),
    ]

    found = []
    checked = set()

    def probe_mount(mount_root):
        if not mount_root or not os.path.isdir(mount_root):
            return

        # Prefer the new top-level folder if present, but keep backward compatibility.
        data_root = os.path.join(mount_root, 'Sleepy Shows Data')
        roots_to_probe = []
        if os.path.isdir(data_root):
            roots_to_probe.append(data_root)
        roots_to_probe.append(mount_root)

        for root in roots_to_probe:
            for _, rels in show_patterns:
                for rel in rels:
                    candidate = os.path.join(root, rel)
                    norm = os.path.normpath(candidate)
                    if norm in checked:
                        continue
                    checked.add(norm)
                    if os.path.isdir(norm) and _looks_like_show_folder(norm):
                        found.append(norm)

    # Prefer the named volume, but fall back to scanning mount points.
    for mount_root in _iter_mount_roots_for_label(volume_label) or []:
        probe_mount(mount_root)

    if not found:
        for mount_root in _iter_mount_roots_fallback() or []:
            probe_mount(mount_root)

    # De-dupe while keeping order.
    unique = []
    seen = set()
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def auto_detect_show_folders(volume_label='T7'):
    """Return best-effort mapping of show name -> episodes folder path."""
    show_patterns = [
        ("King of the Hill", [
            os.path.join('Shows', 'King of the Hill', 'Episodes'),
            os.path.join('Shows', 'King of the Hill', 'King of the Hill'),
            os.path.join('Shows', 'King of the Hill'),
            os.path.join('King of the Hill', 'Episodes'),
            os.path.join('King of the Hill', 'King of the Hill'),
            os.path.join('King of the Hill'),
        ]),
        ("Bob's Burgers", [
            os.path.join('Shows', "Bob's Burgers", 'Episodesl'),
            os.path.join('Shows', "Bob's Burgers", 'Episodes'),
            os.path.join('Shows', "Bob's Burgers", "Bob's Burgers"),
            os.path.join('Shows', "Bob's Burgers", "Bob's Burgersl"),
            os.path.join('Shows', "Bob's Burgers"),
            os.path.join('Shows', "Bob's Burgersl"),
            os.path.join('Shows', 'Bobs Burgers', 'Episodesl'),
            os.path.join('Shows', 'Bobs Burgers', 'Episodes'),
            os.path.join('Shows', 'Bobs Burgers', 'Bobs Burgers'),
            os.path.join('Shows', 'Bobs Burgers'),
            os.path.join("Bob's Burgers", 'Episodesl'),
            os.path.join("Bob's Burgers", 'Episodes'),
            os.path.join("Bob's Burgers", "Bob's Burgers"),
            os.path.join("Bob's Burgers", "Bob's Burgersl"),
            os.path.join("Bob's Burgers"),
            os.path.join("Bob's Burgersl"),
            os.path.join('Bobs Burgers', 'Episodesl'),
            os.path.join('Bobs Burgers', 'Episodes'),
            os.path.join('Bobs Burgers', 'Bobs Burgers'),
            os.path.join('Bobs Burgers'),
        ]),
        ("Squidbillies", [
            os.path.join('Shows', 'Squidbillies', 'Episodes'),
            os.path.join('Shows', 'Squidbillies'),
            os.path.join('Squidbillies', 'Episodes'),
            os.path.join('Squidbillies'),
        ]),
        ("Aqua Teen Hunger Force", [
            os.path.join('Shows', 'Aqua Teen Hunger Force', 'Episodes'),
            os.path.join('Shows', 'Aqua Teen Hunger Force'),
            os.path.join('Aqua Teen Hunger Force', 'Episodes'),
            os.path.join('Aqua Teen Hunger Force'),
            # Common abbreviation fallback
            os.path.join('Shows', 'ATHF', 'Episodes'),
            os.path.join('Shows', 'ATHF'),
            os.path.join('ATHF', 'Episodes'),
            os.path.join('ATHF'),
        ]),
    ]

    found = {}
    checked = set()

    def probe_mount(mount_root):
        if not mount_root or not os.path.isdir(mount_root):
            return

        # Prefer the new top-level folder if present, but keep backward compatibility.
        data_root = os.path.join(mount_root, 'Sleepy Shows Data')
        roots_to_probe = []
        if os.path.isdir(data_root):
            roots_to_probe.append(data_root)
        roots_to_probe.append(mount_root)

        for root in roots_to_probe:
            for show_name, rels in show_patterns:
                # First match wins.
                if show_name in found:
                    continue
                for rel in rels:
                    candidate = os.path.join(root, rel)
                    norm = os.path.normpath(candidate)
                    if norm in checked:
                        continue
                    checked.add(norm)
                    if os.path.isdir(norm) and _looks_like_show_folder(norm):
                        found[show_name] = norm
                        break

    for mount_root in _iter_mount_roots_for_label(volume_label) or []:
        probe_mount(mount_root)

    if not found:
        for mount_root in _iter_mount_roots_fallback() or []:
            probe_mount(mount_root)
            # Stop early if we've found all known shows.
            if len(found) >= len(show_patterns):
                break

    return found


def _find_child_dir_case_insensitive(parent_dir, desired_name):
    """Return the first child directory matching desired_name (case-insensitive)."""
    try:
        if not parent_dir or not os.path.isdir(parent_dir):
            return None
        desired = str(desired_name or '').strip().lower()
        if not desired:
            return None

        for name in os.listdir(parent_dir):
            if name.lower() == desired:
                p = os.path.join(parent_dir, name)
                if os.path.isdir(p):
                    return p
    except Exception:
        return None
    return None


def auto_detect_tv_vibe_scripts_dir(volume_label='T7'):
    """Best-effort detection for bump scripts/music on the same drive as episodes.

    Expected layout: <mount_root>/TV Vibe/scripts
    Returns the scripts folder path if found, else None.
    """
    def probe_mount(mount_root):
        if not mount_root or not os.path.isdir(mount_root):
            return None

        # Prefer the new top-level folder if present, but keep backward compatibility.
        data_root = os.path.join(mount_root, 'Sleepy Shows Data')
        roots_to_probe = []
        if os.path.isdir(data_root):
            roots_to_probe.append(data_root)
        roots_to_probe.append(mount_root)

        for root in roots_to_probe:
            # Fast path for the expected exact casing.
            direct = os.path.join(root, 'TV Vibe', 'scripts')
            if os.path.isdir(direct):
                return direct

            # Case-insensitive fallback.
            tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
            if not tv_vibe_dir:
                continue

            scripts_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'scripts')
            if scripts_dir and os.path.isdir(scripts_dir):
                return scripts_dir

        return None

    for mount_root in _iter_mount_roots_for_label(volume_label) or []:
        found = probe_mount(mount_root)
        if found:
            return found

    for mount_root in _iter_mount_roots_fallback() or []:
        found = probe_mount(mount_root)
        if found:
            return found

    return None


def _normalize_mount_roots_override(mount_roots_override):
    """Best-effort cleanup for a list of user-provided mount roots."""
    roots = []
    try:
        it = list(mount_roots_override or [])
    except Exception:
        it = []

    for r in it:
        try:
            s = str(r or '').strip().strip('"').strip("'")
        except Exception:
            continue
        if not s:
            continue
        try:
            s = os.path.expanduser(s)
        except Exception:
            pass
        try:
            s = os.path.normpath(s)
        except Exception:
            pass
        if s and s not in roots:
            roots.append(s)
    return roots


def auto_detect_default_show_sources_web(mount_roots_override):
    """Web-mode helper: detect show sources by probing only provided roots."""
    roots = _normalize_mount_roots_override(mount_roots_override)
    if not roots:
        return []
    show_folders = auto_detect_show_folders_web(roots)
    ordered = []
    for key in ("King of the Hill", "Bob's Burgers", "Squidbillies", "Aqua Teen Hunger Force"):
        p = show_folders.get(key)
        if p:
            ordered.append(p)
    for k, p in show_folders.items():
        if p and p not in ordered:
            ordered.append(p)
    return ordered


def auto_detect_show_folders_web(mount_roots_override):
    """Web-mode helper: detect show folders by probing only provided roots."""
    roots = _normalize_mount_roots_override(mount_roots_override)
    if not roots:
        return {}

    show_patterns = [
        ("King of the Hill", [
            os.path.join('Shows', 'King of the Hill', 'Episodes'),
            os.path.join('Shows', 'King of the Hill', 'King of the Hill'),
            os.path.join('Shows', 'King of the Hill'),
            os.path.join('King of the Hill', 'Episodes'),
            os.path.join('King of the Hill', 'King of the Hill'),
            os.path.join('King of the Hill'),
        ]),
        ("Bob's Burgers", [
            os.path.join('Shows', "Bob's Burgers", 'Episodesl'),
            os.path.join('Shows', "Bob's Burgers", 'Episodes'),
            os.path.join('Shows', "Bob's Burgers", "Bob's Burgers"),
            os.path.join('Shows', "Bob's Burgers", "Bob's Burgersl"),
            os.path.join('Shows', "Bob's Burgers"),
            os.path.join('Shows', "Bob's Burgersl"),
            os.path.join('Shows', 'Bobs Burgers', 'Episodesl'),
            os.path.join('Shows', 'Bobs Burgers', 'Episodes'),
            os.path.join('Shows', 'Bobs Burgers', 'Bobs Burgers'),
            os.path.join('Shows', 'Bobs Burgers'),
            os.path.join("Bob's Burgers", 'Episodesl'),
            os.path.join("Bob's Burgers", 'Episodes'),
            os.path.join("Bob's Burgers", "Bob's Burgers"),
            os.path.join("Bob's Burgers", "Bob's Burgersl"),
            os.path.join("Bob's Burgers"),
            os.path.join("Bob's Burgersl"),
            os.path.join('Bobs Burgers', 'Episodesl'),
            os.path.join('Bobs Burgers', 'Episodes'),
            os.path.join('Bobs Burgers', 'Bobs Burgers'),
            os.path.join('Bobs Burgers'),
        ]),
        ("Squidbillies", [
            os.path.join('Shows', 'Squidbillies', 'Episodes'),
            os.path.join('Shows', 'Squidbillies'),
            os.path.join('Squidbillies', 'Episodes'),
            os.path.join('Squidbillies'),
        ]),
        ("Aqua Teen Hunger Force", [
            os.path.join('Shows', 'Aqua Teen Hunger Force', 'Episodes'),
            os.path.join('Shows', 'Aqua Teen Hunger Force'),
            os.path.join('Aqua Teen Hunger Force', 'Episodes'),
            os.path.join('Aqua Teen Hunger Force'),
            # Common abbreviation fallback
            os.path.join('Shows', 'ATHF', 'Episodes'),
            os.path.join('Shows', 'ATHF'),
            os.path.join('ATHF', 'Episodes'),
            os.path.join('ATHF'),
        ]),
    ]

    found = {}
    checked = set()

    def probe_mount(mount_root):
        if not mount_root or not os.path.isdir(mount_root):
            return

        data_root = os.path.join(mount_root, 'Sleepy Shows Data')
        roots_to_probe = []
        if os.path.isdir(data_root):
            roots_to_probe.append(data_root)
        roots_to_probe.append(mount_root)

        for root in roots_to_probe:
            for show_name, rels in show_patterns:
                if show_name in found:
                    continue
                for rel in rels:
                    candidate = os.path.join(root, rel)
                    norm = os.path.normpath(candidate)
                    if norm in checked:
                        continue
                    checked.add(norm)
                    if os.path.isdir(norm) and _looks_like_show_folder(norm):
                        found[show_name] = norm
                        break

    for mount_root in roots:
        probe_mount(mount_root)
        if len(found) >= len(show_patterns):
            break

    return found


def auto_detect_tv_vibe_scripts_dir_web(mount_roots_override):
    roots = _normalize_mount_roots_override(mount_roots_override)
    if not roots:
        return None
    for mount_root in roots:
        try:
            data_root = os.path.join(mount_root, 'Sleepy Shows Data')
            roots_to_probe = []
            if os.path.isdir(data_root):
                roots_to_probe.append(data_root)
            roots_to_probe.append(mount_root)

            for root in roots_to_probe:
                direct = os.path.join(root, 'TV Vibe', 'scripts')
                if os.path.isdir(direct):
                    return direct

                tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
                if not tv_vibe_dir:
                    continue

                scripts_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'scripts')
                if scripts_dir and os.path.isdir(scripts_dir):
                    return scripts_dir
        except Exception:
            continue
    return None


def auto_detect_tv_vibe_music_dir_web(mount_roots_override):
    roots = _normalize_mount_roots_override(mount_roots_override)
    if not roots:
        return None
    for mount_root in roots:
        try:
            data_root = os.path.join(mount_root, 'Sleepy Shows Data')
            roots_to_probe = []
            if os.path.isdir(data_root):
                roots_to_probe.append(data_root)
            roots_to_probe.append(mount_root)

            for root in roots_to_probe:
                direct = os.path.join(root, 'TV Vibe', 'music')
                if os.path.isdir(direct):
                    return direct

                tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
                if not tv_vibe_dir:
                    continue

                music_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'music')
                if music_dir and os.path.isdir(music_dir):
                    return music_dir
        except Exception:
            continue
    return None


def auto_detect_tv_vibe_images_dir_web(mount_roots_override):
    roots = _normalize_mount_roots_override(mount_roots_override)
    if not roots:
        return None
    for mount_root in roots:
        try:
            data_root = os.path.join(mount_root, 'Sleepy Shows Data')
            roots_to_probe = []
            if os.path.isdir(data_root):
                roots_to_probe.append(data_root)
            roots_to_probe.append(mount_root)

            for root in roots_to_probe:
                direct = os.path.join(root, 'TV Vibe', 'images')
                if os.path.isdir(direct):
                    return direct

                tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
                if not tv_vibe_dir:
                    continue

                images_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'images')
                if images_dir and os.path.isdir(images_dir):
                    return images_dir
        except Exception:
            continue
    return None


def auto_detect_tv_vibe_audio_fx_dir_web(mount_roots_override):
    roots = _normalize_mount_roots_override(mount_roots_override)
    if not roots:
        return None
    for mount_root in roots:
        try:
            data_root = os.path.join(mount_root, 'Sleepy Shows Data')
            roots_to_probe = []
            if os.path.isdir(data_root):
                roots_to_probe.append(data_root)
            roots_to_probe.append(mount_root)

            for root in roots_to_probe:
                direct = os.path.join(root, 'TV Vibe', 'audio')
                if os.path.isdir(direct):
                    return direct

                tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
                if not tv_vibe_dir:
                    continue

                audio_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'audio')
                if audio_dir and os.path.isdir(audio_dir):
                    return audio_dir
        except Exception:
            continue
    return None


def auto_detect_tv_vibe_videos_dir_web(mount_roots_override):
    """Best-effort detection for TV Vibe bump videos in Web mode.

    Expected layout: <mount_root>/Sleepy Shows Data/TV Vibe/videos
    (Falls back to legacy <mount_root>/.../TV Vibe/videos.)
    """
    roots = _normalize_mount_roots_override(mount_roots_override)
    if not roots:
        return None
    for mount_root in roots:
        try:
            data_root = os.path.join(mount_root, 'Sleepy Shows Data')
            roots_to_probe = []
            if os.path.isdir(data_root):
                roots_to_probe.append(data_root)
            roots_to_probe.append(mount_root)

            for root in roots_to_probe:
                direct = os.path.join(root, 'TV Vibe', 'videos')
                if os.path.isdir(direct):
                    return direct

                tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
                if not tv_vibe_dir:
                    continue

                videos_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'videos')
                if videos_dir and os.path.isdir(videos_dir):
                    return videos_dir
        except Exception:
            continue
    return None


def auto_detect_tv_vibe_interstitials_dir_web(mount_roots_override):
    """Best-effort detection for TV Vibe interludes in Web mode.

    Expected layout: <mount_root>/Sleepy Shows Data/TV Vibe/interludes
    (Falls back to legacy <mount_root>/.../TV Vibe/interstitials.)
    """
    roots = _normalize_mount_roots_override(mount_roots_override)
    if not roots:
        return None
    for mount_root in roots:
        try:
            data_root = os.path.join(mount_root, 'Sleepy Shows Data')
            roots_to_probe = []
            if os.path.isdir(data_root):
                roots_to_probe.append(data_root)
            roots_to_probe.append(mount_root)

            for root in roots_to_probe:
                # New naming
                direct = os.path.join(root, 'TV Vibe', 'interludes')
                if os.path.isdir(direct):
                    return direct

                # Legacy naming
                direct = os.path.join(root, 'TV Vibe', 'interstitials')
                if os.path.isdir(direct):
                    return direct

                tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
                if not tv_vibe_dir:
                    continue

                inter_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'interludes')
                if inter_dir and os.path.isdir(inter_dir):
                    return inter_dir

                inter_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'interstitials')
                if inter_dir and os.path.isdir(inter_dir):
                    return inter_dir
        except Exception:
            continue
    return None


def auto_detect_tv_vibe_music_dir(volume_label='T7'):
    """Best-effort detection for bump music on the same drive as episodes.

    Expected layout: <mount_root>/Sleepy Shows Data/TV Vibe/music
    (Falls back to <mount_root>/TV Vibe/music for older layouts.)
    Returns the music folder path if found, else None.
    """
    def probe_mount(mount_root):
        if not mount_root or not os.path.isdir(mount_root):
            return None

        data_root = os.path.join(mount_root, 'Sleepy Shows Data')
        roots_to_probe = []
        if os.path.isdir(data_root):
            roots_to_probe.append(data_root)
        roots_to_probe.append(mount_root)

        for root in roots_to_probe:
            direct = os.path.join(root, 'TV Vibe', 'music')
            if os.path.isdir(direct):
                return direct

            tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
            if not tv_vibe_dir:
                continue

            music_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'music')
            if music_dir and os.path.isdir(music_dir):
                return music_dir

        return None

    for mount_root in _iter_mount_roots_for_label(volume_label) or []:
        found = probe_mount(mount_root)
        if found:
            return found

    for mount_root in _iter_mount_roots_fallback() or []:
        found = probe_mount(mount_root)
        if found:
            return found

    return None


def auto_detect_tv_vibe_images_dir(volume_label='T7'):
    """Best-effort detection for bump images on the same drive as episodes.

    Expected layout: <mount_root>/Sleepy Shows Data/TV Vibe/images
    (Falls back to <mount_root>/TV Vibe/images for older layouts.)
    Returns the images folder path if found, else None.
    """
    def probe_mount(mount_root):
        if not mount_root or not os.path.isdir(mount_root):
            return None

        data_root = os.path.join(mount_root, 'Sleepy Shows Data')
        roots_to_probe = []
        if os.path.isdir(data_root):
            roots_to_probe.append(data_root)
        roots_to_probe.append(mount_root)

        for root in roots_to_probe:
            direct = os.path.join(root, 'TV Vibe', 'images')
            if os.path.isdir(direct):
                return direct

            tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
            if not tv_vibe_dir:
                continue

            images_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'images')
            if images_dir and os.path.isdir(images_dir):
                return images_dir

        return None

    for mount_root in _iter_mount_roots_for_label(volume_label) or []:
        found = probe_mount(mount_root)
        if found:
            return found

    for mount_root in _iter_mount_roots_fallback() or []:
        found = probe_mount(mount_root)
        if found:
            return found

    return None


def auto_detect_tv_vibe_audio_fx_dir(volume_label='T7'):
    """Best-effort detection for bump audio FX on the same drive as episodes.

    Expected layout: <mount_root>/Sleepy Shows Data/TV Vibe/audio
    (Falls back to <mount_root>/TV Vibe/audio for older layouts.)
    Returns the audio folder path if found, else None.
    """
    def probe_mount(mount_root):
        if not mount_root or not os.path.isdir(mount_root):
            return None

        data_root = os.path.join(mount_root, 'Sleepy Shows Data')
        roots_to_probe = []
        if os.path.isdir(data_root):
            roots_to_probe.append(data_root)
        roots_to_probe.append(mount_root)

        for root in roots_to_probe:
            direct = os.path.join(root, 'TV Vibe', 'audio')
            if os.path.isdir(direct):
                return direct

            tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
            if not tv_vibe_dir:
                continue

            audio_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'audio')
            if audio_dir and os.path.isdir(audio_dir):
                return audio_dir

        return None

    for mount_root in _iter_mount_roots_for_label(volume_label) or []:
        found = probe_mount(mount_root)
        if found:
            return found

    for mount_root in _iter_mount_roots_fallback() or []:
        found = probe_mount(mount_root)
        if found:
            return found

    return None


def auto_detect_tv_vibe_videos_dir(volume_label='T7'):
    """Best-effort detection for TV Vibe bump videos on the same drive as episodes.

    Expected layout: <mount_root>/Sleepy Shows Data/TV Vibe/videos
    (Falls back to <mount_root>/TV Vibe/videos for older layouts.)
    Returns the videos folder path if found, else None.
    """
    def probe_mount(mount_root):
        if not mount_root or not os.path.isdir(mount_root):
            return None

        data_root = os.path.join(mount_root, 'Sleepy Shows Data')
        roots_to_probe = []
        if os.path.isdir(data_root):
            roots_to_probe.append(data_root)
        roots_to_probe.append(mount_root)

        for root in roots_to_probe:
            direct = os.path.join(root, 'TV Vibe', 'videos')
            if os.path.isdir(direct):
                return direct

            tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
            if not tv_vibe_dir:
                continue

            videos_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'videos')
            if videos_dir and os.path.isdir(videos_dir):
                return videos_dir

        return None

    for mount_root in _iter_mount_roots_for_label(volume_label) or []:
        found = probe_mount(mount_root)
        if found:
            return found

    for mount_root in _iter_mount_roots_fallback() or []:
        found = probe_mount(mount_root)
        if found:
            return found

    return None


def auto_detect_tv_vibe_interstitials_dir(volume_label='T7'):
    """Best-effort detection for TV Vibe interludes on the same drive as episodes.

    Expected layout: <mount_root>/Sleepy Shows Data/TV Vibe/interludes
    (Falls back to legacy <mount_root>/.../TV Vibe/interstitials.)
    Returns the interludes folder path if found, else None.
    """
    def probe_mount(mount_root):
        if not mount_root or not os.path.isdir(mount_root):
            return None

        data_root = os.path.join(mount_root, 'Sleepy Shows Data')
        roots_to_probe = []
        if os.path.isdir(data_root):
            roots_to_probe.append(data_root)
        roots_to_probe.append(mount_root)

        for root in roots_to_probe:
            # New naming
            direct = os.path.join(root, 'TV Vibe', 'interludes')
            if os.path.isdir(direct):
                return direct

            # Legacy naming
            direct = os.path.join(root, 'TV Vibe', 'interstitials')
            if os.path.isdir(direct):
                return direct

            tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
            if not tv_vibe_dir:
                continue

            inter_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'interludes')
            if inter_dir and os.path.isdir(inter_dir):
                return inter_dir

            inter_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'interstitials')
            if inter_dir and os.path.isdir(inter_dir):
                return inter_dir

        return None

    for mount_root in _iter_mount_roots_for_label(volume_label) or []:
        found = probe_mount(mount_root)
        if found:
            return found

    for mount_root in _iter_mount_roots_fallback() or []:
        found = probe_mount(mount_root)
        if found:
            return found

    return None


def _scan_episode_files(folder_path, *, use_cache: bool = True):
    """Return naturally sorted full paths of video files under folder_path.

    Note: cache is helpful for slow network scans, but must be bypassable so
    portable/external-drive mode can detect deletions and rebuild stale playlists.
    """
    results = []
    if not folder_path or not os.path.isdir(folder_path):
        # Try loading from static manifest (for network paths)
        return _load_from_manifest(folder_path)

    # Cache scan results to avoid slow network scans on every startup
    cache_key = hashlib.md5(folder_path.encode('utf-8')).hexdigest()
    cache_dir = os.path.join(get_local_playlists_dir(), '.scan_cache')
    cache_file = os.path.join(cache_dir, f'{cache_key}.json')
    
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        pass
    
    # Try to load from cache (valid for 24 hours)
    if bool(use_cache) and os.path.exists(cache_file):
        try:
            cache_age = time.time() - os.path.getmtime(cache_file)
            if cache_age < 86400:  # 24 hours
                with open(cache_file, 'r') as f:
                    cached = json.load(f)
                    if isinstance(cached, dict) and cached.get('folder') == folder_path:
                        return cached.get('files', [])
        except Exception:
            pass

    # Try manifest before expensive network scan
    manifest_results = _load_from_manifest(folder_path)
    if manifest_results:
        return manifest_results

    try:
        for root, dirs, files in os.walk(folder_path):
            dirs.sort(key=natural_sort_key)
            files.sort(key=natural_sort_key)
            for f in files:
                if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                    results.append(os.path.join(root, f))
    except Exception:
        return []

    # Save to cache
    if bool(use_cache):
        try:
            with open(cache_file, 'w') as f:
                json.dump({'folder': folder_path, 'files': results}, f)
        except Exception:
            pass

    return results


def _load_from_manifest(folder_path):
    """Load episode list from static manifest instead of scanning network."""
    try:
        manifest_path = os.path.join(get_local_playlists_dir(), 'network_manifest.json')
        if not os.path.exists(manifest_path):
            return []
        
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        
        # Check if this is HTTP streaming mode
        streaming_mode = manifest.get('streaming_mode', 'filesystem')
        base_url = manifest.get('base_url', '')
        base_path = manifest.get('base_path', '')
        shows = manifest.get('shows', {})
        
        # Match folder_path to a show
        for show_name, episodes in shows.items():
            # Check if folder_path matches this show's episodes folder
            if show_name in str(folder_path):
                result = []
                if streaming_mode == 'http' and base_url:
                    # HTTP streaming - construct URLs
                    for url_path in episodes:
                        full_url = f"{base_url}/{url_path}"
                        result.append(full_url)
                else:
                    # Filesystem mode - reconstruct full paths
                    for rel_path in episodes:
                        full_path = os.path.join(base_path, rel_path)
                        result.append(full_path)
                return result
        
        return []
    except Exception:
        return []


def _write_auto_playlist_json(
    playlist_filename,
    episode_folder,
    default_shuffle_mode='standard',
    *,
    prefer_existing_playlist_paths: bool = False,
):
    """Create/update a playlist JSON under playlists/ for a given episodes folder.

    Regeneration policy:
    - If the playlist file does not exist, create it.
    - If the playlist exists and its stored 'source_folder' still exists on disk, do nothing.
      (This allows users to freely change settings like shuffle mode without triggering rewrites.)
    - If the playlist exists but its 'source_folder' is missing (e.g. drive letter changed),
      rewrite it using the newly detected episode_folder and preserve user settings when possible.
    """
    try:
        files_dir = get_local_playlists_dir()
        os.makedirs(files_dir, exist_ok=True)
        playlist_path = os.path.join(files_dir, playlist_filename)

        existing = None
        if os.path.exists(playlist_path):
            try:
                with open(playlist_path, 'r') as f:
                    existing = json.load(f)
            except Exception:
                existing = None

        def _season_from_path(p: str) -> int:
            try:
                parts = re.split(r'[\\/]+', str(p or ''))
                for part in parts:
                    m = re.search(r'(?:season|s)[ _-]?(\d{1,2})', part, flags=re.IGNORECASE)
                    if m:
                        try:
                            return int(m.group(1))
                        except Exception:
                            return 0
            except Exception:
                return 0
            return 0

        def _norm_path_key(p: str) -> str:
            try:
                s = str(p or '').strip()
            except Exception:
                s = ''
            if not s:
                return ''
            try:
                return os.path.normcase(os.path.normpath(s))
            except Exception:
                return s

        def _default_frequency_settings_for(playlist_filename: str, episode_paths: list[str]) -> dict:
            name = str(playlist_filename or '').lower()
            episode_paths = [str(p) for p in (episode_paths or []) if p]
            out = {
                'episode_offsets': {},
                'season_offsets': {},
                'episode_factors': {},
                'season_factors': {},
            }

            if 'king of the hill' in name:
                out['season_offsets']['season:1'] = 101.0
                out['season_factors']['season:1'] = 3.0
                out['season_offsets']['season:2'] = 1.0
                out['season_factors']['season:2'] = 2.0
                out['season_offsets']['season:3'] = 1.0
                out['season_factors']['season:3'] = 1.5
                out['season_factors']['season:11'] = 1.25
                return out

            if "bob's burgers" in name or 'bobs burgers' in name or 'bob' in name:
                # Season 1 episode 1/2 offsets by natural file ordering within season 1.
                s1 = [p for p in episode_paths if _season_from_path(p) == 1]
                s1.sort(key=lambda p: natural_sort_key(os.path.basename(p)) + natural_sort_key(p))
                if len(s1) >= 1:
                    out['episode_offsets'][s1[0]] = 101.0
                if len(s1) >= 2:
                    out['episode_offsets'][s1[1]] = 1.0

                # Seasons 11+ have an offset and factor.
                seasons = set()
                for p in episode_paths:
                    n = _season_from_path(p)
                    if n >= 11:
                        seasons.add(int(n))
                for n in sorted(seasons):
                    out['season_offsets'][f'season:{n}'] = 50.0
                    out['season_factors'][f'season:{n}'] = 1.5
                return out

            return out

        def _has_any_frequency_settings(fs: dict | None) -> bool:
            if not isinstance(fs, dict):
                return False
            for k in ('episode_offsets', 'season_offsets', 'episode_factors', 'season_factors'):
                v = fs.get(k, None)
                if isinstance(v, dict) and v:
                    return True
            return False

        def _extract_video_paths(existing_data: dict | None) -> list[str]:
            out: list[str] = []
            if not isinstance(existing_data, dict):
                return out
            try:
                for it in list(existing_data.get('playlist', []) or []):
                    if isinstance(it, dict) and it.get('type', 'video') == 'video':
                        p = str(it.get('path') or '').strip()
                        if p:
                            out.append(p)
            except Exception:
                return []
            return out

        def _playlist_needs_rebuild(existing_data: dict | None, source_folder: str) -> bool:
            """Return True if existing playlist differs from on-disk source folder."""
            if not isinstance(existing_data, dict):
                return True

            src = str(source_folder or '').strip()
            if not src or not os.path.isdir(src):
                return True

            playlist_paths = _extract_video_paths(existing_data)
            if not playlist_paths:
                return True

            # Fresh scan (portable correctness over cached speed).
            disk_paths = _scan_episode_files(src, use_cache=False)
            if not disk_paths:
                return True

            disk_set = {_norm_path_key(p) for p in disk_paths if p}
            playlist_set: set[str] = set()

            missing = 0
            checked = 0
            for p in playlist_paths:
                checked += 1
                # Relative paths are considered stale in portable mode.
                try:
                    if not os.path.isabs(p):
                        missing += 1
                        continue
                except Exception:
                    missing += 1
                    continue

                k = _norm_path_key(p)
                playlist_set.add(k)
                if k not in disk_set:
                    missing += 1

            if missing > 0:
                return True

            # If on-disk has additional episodes not in playlist, rebuild.
            if len(disk_set - playlist_set) > 0:
                return True

            return False

        def _retarget_frequency_settings(
            fs: dict | None,
            *,
            old_source_folder: str,
            new_episode_folder: str,
            new_episode_paths: list[str],
        ) -> dict | None:
            if not isinstance(fs, dict):
                return None

            new_paths = [str(p) for p in (new_episode_paths or []) if p]
            new_key_set = {_norm_path_key(p) for p in new_paths}

            def _best_match_for_old_path(old_path: str) -> str | None:
                op = str(old_path or '').strip()
                if not op:
                    return None

                # 1) If it already matches one of the new paths, keep it.
                if _norm_path_key(op) in new_key_set:
                    # Preserve exact new-path casing by looking it up.
                    nk = _norm_path_key(op)
                    for p in new_paths:
                        if _norm_path_key(p) == nk:
                            return p
                    return op

                # 2) If it was under the old source, translate relative to new source.
                try:
                    old_src = str(old_source_folder or '').strip()
                    new_src = str(new_episode_folder or '').strip()
                except Exception:
                    old_src = ''
                    new_src = ''

                if old_src and new_src:
                    try:
                        common = os.path.commonpath([os.path.normpath(old_src), os.path.normpath(op)])
                    except Exception:
                        common = ''
                    if common and _norm_path_key(common) == _norm_path_key(old_src):
                        try:
                            rel = os.path.relpath(op, old_src)
                            candidate = os.path.normpath(os.path.join(new_src, rel))
                            if _norm_path_key(candidate) in new_key_set:
                                for p in new_paths:
                                    if _norm_path_key(p) == _norm_path_key(candidate):
                                        return p
                                return candidate
                        except Exception:
                            pass

                # 3) Basename match fallback (best-effort).
                try:
                    base = os.path.basename(op).lower()
                except Exception:
                    base = ''
                if not base:
                    return None

                matches = []
                for p in new_paths:
                    try:
                        if os.path.basename(p).lower() == base:
                            matches.append(p)
                    except Exception:
                        continue

                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    try:
                        matches.sort(key=lambda p: natural_sort_key(os.path.basename(p)) + natural_sort_key(p))
                    except Exception:
                        pass
                    return matches[0]

                return None

            def _retarget_path_map(m: object) -> dict:
                if not isinstance(m, dict):
                    return {}
                out: dict = {}
                for k, v in list(m.items()):
                    try:
                        nk = _best_match_for_old_path(str(k))
                        if not nk:
                            continue
                        out[nk] = float(v)
                    except Exception:
                        continue
                return out

            out = {
                'episode_offsets': _retarget_path_map(fs.get('episode_offsets', {})),
                'season_offsets': dict(fs.get('season_offsets', {}) or {}),
                'episode_factors': _retarget_path_map(fs.get('episode_factors', {})),
                'season_factors': dict(fs.get('season_factors', {}) or {}),
            }
            return out

        def _playlist_paths_match_source(existing_data: dict | None, source_folder: str) -> bool:
            """Return True if the existing playlist's episode paths appear rooted in source_folder.

            This is a string-based heuristic (no filesystem probing) to avoid stale playlists
            when switching between Web/Portable topologies.
            """
            if not isinstance(existing_data, dict):
                return True

            try:
                src = os.path.normpath(str(source_folder or ''))
            except Exception:
                src = str(source_folder or '')
            if not src:
                return True

            # Grab a small sample of episode paths.
            sample: list[str] = []
            try:
                for it in list(existing_data.get('playlist', []) or []):
                    if not isinstance(it, dict) or it.get('type', 'video') != 'video':
                        continue
                    p = str(it.get('path') or '').strip()
                    if not p:
                        continue
                    sample.append(p)
                    if len(sample) >= 6:
                        break
            except Exception:
                sample = []

            if not sample:
                return True

            checked = 0
            mismatched = 0
            for p in sample:
                # Relative paths can't be verified here; assume OK.
                try:
                    if not os.path.isabs(p):
                        return True
                except Exception:
                    return True

                checked += 1
                try:
                    p_norm = os.path.normpath(p)
                    common = os.path.commonpath([src, p_norm])
                    if common != src:
                        mismatched += 1
                except Exception:
                    mismatched += 1

            # If *all* sampled episode paths point outside the detected source folder,
            # treat the playlist as stale.
            return not (checked > 0 and mismatched >= checked)

        # If the existing playlist still points at a valid folder and matches on-disk
        # files, don't touch it, except to backfill missing frequency settings.
        try:
            existing_source = (existing or {}).get('source_folder', '')
            keep_existing = bool(
                existing_source
                and os.path.isdir(existing_source)
                and _playlist_paths_match_source(existing, existing_source)
            )

            # If auto-detect found a different folder, treat the playlist as stale.
            if keep_existing:
                try:
                    if _norm_path_key(existing_source) != _norm_path_key(episode_folder):
                        keep_existing = False
                except Exception:
                    keep_existing = False

            # Portable correctness: ensure the playlist still matches local files.
            # In Web mode we intentionally avoid expensive scans.
            if keep_existing and (not bool(prefer_existing_playlist_paths)):
                try:
                    if _playlist_needs_rebuild(existing, existing_source):
                        keep_existing = False
                except Exception:
                    keep_existing = False

            if keep_existing:
                try:
                    existing_fs = (existing or {}).get('frequency_settings', None)
                except Exception:
                    existing_fs = None

                if _has_any_frequency_settings(existing_fs):
                    return False

                # Compute defaults from the existing playlist entries (no rescan).
                episode_paths = []
                try:
                    for it in list((existing or {}).get('playlist', []) or []):
                        if isinstance(it, dict) and it.get('type', 'video') == 'video':
                            p = it.get('path')
                            if p:
                                episode_paths.append(str(p))
                except Exception:
                    episode_paths = []

                defaults = _default_frequency_settings_for(playlist_filename, episode_paths)
                if _has_any_frequency_settings(defaults):
                    existing = dict(existing or {})
                    existing['frequency_settings'] = defaults
                    with open(playlist_path, 'w') as f:
                        json.dump(existing, f, indent=2)
                    return True

                return False
        except Exception:
            pass

        # Web mode optimization: avoid scanning network folders by reusing existing
        # playlist entries when available.
        #
        # Portable mode fix: when the external drive is present, we MUST rescan so
        # stale paths (e.g. /mnt/shows/...) get rewritten to the real mounted drive.
        eps = []
        if bool(prefer_existing_playlist_paths):
            try:
                if os.path.exists(playlist_path):
                    with open(playlist_path, 'r') as f:
                        existing_data = json.load(f)
                        for item in existing_data.get('playlist', []):
                            if item.get('type') == 'video':
                                eps.append(item.get('path'))
            except Exception:
                eps = []

        # Scan the detected episode folder when we don't have reusable entries.
        if not eps:
            # Portable mode: bypass cache to detect deletions.
            eps = _scan_episode_files(episode_folder, use_cache=bool(prefer_existing_playlist_paths))
            if not eps:
                return False

        # Preserve user settings if the file already existed.
        shuffle_mode = None
        frequency_settings = None
        try:
            if isinstance(existing, dict):
                shuffle_mode = existing.get('shuffle_mode', None)
                frequency_settings = existing.get('frequency_settings', None)
                if shuffle_mode is None:
                    shuffle_default = bool(existing.get('shuffle_default', False))
                    shuffle_mode = 'standard' if shuffle_default else 'off'
        except Exception:
            shuffle_mode = None

        if shuffle_mode not in ('off', 'standard', 'season'):
            shuffle_mode = default_shuffle_mode

        # Preserve/retarget user frequency settings when paths changed.
        try:
            if isinstance(existing, dict) and isinstance(frequency_settings, dict):
                old_src = str(existing.get('source_folder', '') or '')
                retargeted = _retarget_frequency_settings(
                    frequency_settings,
                    old_source_folder=old_src,
                    new_episode_folder=str(episode_folder or ''),
                    new_episode_paths=eps,
                )
                if isinstance(retargeted, dict) and _has_any_frequency_settings(retargeted):
                    frequency_settings = retargeted
        except Exception:
            pass

        if not _has_any_frequency_settings(frequency_settings):
            frequency_settings = _default_frequency_settings_for(playlist_filename, eps)

        data = {
            'playlist': [{'type': 'video', 'path': p} for p in eps],
            'shuffle_default': (shuffle_mode != 'off'),
            'shuffle_mode': shuffle_mode,
            'auto_generated': True,
            'source_folder': episode_folder,
            'frequency_settings': frequency_settings,
        }
        with open(playlist_path, 'w') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


class AutoConfigWorker(QObject):
    finished = Signal(object)

    def __init__(self, volume_label='T7', mount_roots_override=None):
        super().__init__()
        self.volume_label = volume_label
        self.mount_roots_override = mount_roots_override

    @Slot()
    def run(self):
        result = {
            'show_folders': {},
            'sources': [],
            'library_structure': {},
            'source_folders': [],
            'episodes': [],
            'playlists_updated': False,
            'tv_vibe_scripts_dir': None,
            'tv_vibe_music_dir': None,
            'tv_vibe_images_dir': None,
            'tv_vibe_audio_fx_dir': None,
            'tv_vibe_interstitials_dir': None,
        }

        try:
            roots = _normalize_mount_roots_override(getattr(self, 'mount_roots_override', None))
            if roots:
                show_folders = auto_detect_show_folders_web(roots)
                result['tv_vibe_scripts_dir'] = auto_detect_tv_vibe_scripts_dir_web(roots)
                result['tv_vibe_music_dir'] = auto_detect_tv_vibe_music_dir_web(roots)
                result['tv_vibe_images_dir'] = auto_detect_tv_vibe_images_dir_web(roots)
                result['tv_vibe_audio_fx_dir'] = auto_detect_tv_vibe_audio_fx_dir_web(roots)
                result['tv_vibe_interstitials_dir'] = auto_detect_tv_vibe_interstitials_dir_web(roots)
            else:
                show_folders = auto_detect_show_folders(volume_label=self.volume_label)
                result['tv_vibe_scripts_dir'] = auto_detect_tv_vibe_scripts_dir(volume_label=self.volume_label)
                result['tv_vibe_music_dir'] = auto_detect_tv_vibe_music_dir(volume_label=self.volume_label)
                result['tv_vibe_images_dir'] = auto_detect_tv_vibe_images_dir(volume_label=self.volume_label)
                result['tv_vibe_audio_fx_dir'] = auto_detect_tv_vibe_audio_fx_dir(volume_label=self.volume_label)
                result['tv_vibe_interstitials_dir'] = auto_detect_tv_vibe_interstitials_dir(volume_label=self.volume_label)
            sources = []
            for key in ("King of the Hill", "Bob's Burgers", "Squidbillies", "Aqua Teen Hunger Force"):
                p = show_folders.get(key)
                if p:
                    sources.append(p)

            # Add any additional detected show folders (stable order).
            for k, p in (show_folders or {}).items():
                try:
                    if p and p not in sources:
                        sources.append(p)
                except Exception:
                    continue

            result['show_folders'] = show_folders
            result['sources'] = sources

            # Build library scan in the worker so the UI thread stays responsive.
            # Skip slow network scans in web mode - rely on existing playlist files
            if sources and not roots:
                pm = PlaylistManager()
                for folder in sources:
                    pm.add_source(folder)
                result['library_structure'] = pm.library_structure
                result['source_folders'] = pm.source_folders
                result['episodes'] = pm.episodes

            updated = False
            if show_folders.get("Bob's Burgers"):
                updated = _write_auto_playlist_json(
                    "Bob's Burgers.json",
                    show_folders["Bob's Burgers"],
                    default_shuffle_mode='standard',
                    prefer_existing_playlist_paths=bool(roots),
                ) or updated
            if show_folders.get("King of the Hill"):
                updated = _write_auto_playlist_json(
                    "King of the Hill.json",
                    show_folders["King of the Hill"],
                    default_shuffle_mode='standard',
                    prefer_existing_playlist_paths=bool(roots),
                ) or updated
            if show_folders.get("Squidbillies"):
                updated = _write_auto_playlist_json(
                    "Squidbillies.json",
                    show_folders["Squidbillies"],
                    default_shuffle_mode='standard',
                    prefer_existing_playlist_paths=bool(roots),
                ) or updated
            if show_folders.get("Aqua Teen Hunger Force"):
                updated = _write_auto_playlist_json(
                    "Aqua Teen Hunger Force.json",
                    show_folders["Aqua Teen Hunger Force"],
                    default_shuffle_mode='standard',
                    prefer_existing_playlist_paths=bool(roots),
                ) or updated
            result['playlists_updated'] = bool(updated)
        except Exception:
            # Best-effort only.
            pass

        self.finished.emit(result)

# --- Custom Widgets ---

class ShowCardButton(QPushButton):
    """A show card that can scale its icon without forcing the window minimum size.

    Qt layouts use widgets' minimumSizeHint() to decide how far a window can be
    shrunk. If we dynamically set a large iconSize on a QPushButton, the default
    minimumSizeHint grows and can prevent shrinking on the Welcome screen.

    This button reports a tiny minimumSizeHint, but still scales its icon to the
    available button size.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig_pixmap = None

    def set_original_pixmap(self, pixmap: QPixmap):
        self._orig_pixmap = pixmap if (pixmap is not None and not pixmap.isNull()) else None
        self._update_scaled_icon()

    def minimumSizeHint(self):
        return QSize(1, 1)

    def sizeHint(self):
        return QSize(220, 320)

    def resizeEvent(self, event):
        try:
            self._update_scaled_icon()
        except Exception:
            pass
        return super().resizeEvent(event)

    def _update_scaled_icon(self):
        pm = self._orig_pixmap
        if pm is None or pm.isNull():
            return
        # Scale to the current button size; keep aspect ratio.
        w = max(1, int(self.width()))
        h = max(1, int(self.height()))
        scaled = pm.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.setIcon(QIcon(scaled))
        self.setIconSize(scaled.size())

class WelcomeScreen(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.btn_vibes_label = None
        self.btn_vibes_check = None
        self.btn_sleep_label = None
        self.btn_sleep_check = None
        # Defaults: TV Vibes ON and Sleepy Time ON (3 hours).
        self.is_vibes_on = True
        self.is_sleep_on = True
        self.show_btns = [] # Track buttons for resizing

        # Footer scaling state (populated in setup_ui)
        self._footer_widget = None
        self._footer_layout = None
        self._footer_icon_items = []
        self._footer_composites = []
        self._footer_checkbox_target_size = None
        self.setup_ui()

    def minimumSizeHint(self):
        # Don't let the Welcome screen's content force a large window minimum.
        return QSize(1, 1)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        grad = QLinearGradient(0, 0, 0, self.height())
        # Fade to black faster: Top is black, Middle is Black, Bottom is Blue
        grad.setColorAt(0, Qt.black)
        grad.setColorAt(0.6, Qt.black) # Stay black until 60% down
        grad.setColorAt(1, QColor("#0e1a77"))
        painter.fillRect(self.rect(), grad)
        
        # Draw Stars (Stretched/Scaled to fill width)
        stars = QPixmap(get_asset_path("stars.png"))
        if not stars.isNull():
             scaled_stars = stars.scaledToWidth(self.width(), Qt.SmoothTransformation)
             y_pos = 80
             painter.drawPixmap(0, y_pos, scaled_stars)

    def setup_ui(self):
        # Use absolute positioning for header elements (Logo, Clouds)
        # Main layout handles the content (Shows) and Footer
        main_layout = QVBoxLayout(self)
        
        # 0. Header Space (Push content down)
        # Clouds are 150px high. Logo sits in there.
        # Let's give ~160px top margin to the main layout's content
        main_layout.setContentsMargins(0, 160, 0, 0)
        
        # 1. Main Area (Shows Icons)
        main_layout.addStretch(1) 
        
        shows_layout = QHBoxLayout()
        shows_layout.setSpacing(20) # "closer to each other" (was 50)
        shows_layout.setAlignment(Qt.AlignCenter)
        
        # Helper for Show Buttons
        def create_show_btn(icon_name, callback):
            btn = ShowCardButton()
            
            # Store original pixmap for resizing later
            path = get_asset_path(icon_name)
            pix = QPixmap(path)
            btn.setProperty("original_pixmap", pix)
            try:
                btn.set_original_pixmap(pix)
            except Exception:
                pass
            
            # Keep a reasonable visible minimum, but don't lock the window width.
            btn.setMinimumSize(160, 240)
            # Do NOT expand to fill the row; that makes the hover background enormous.
            btn.setMaximumSize(280, 420)
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            btn.setFlat(True)
            
            btn.setStyleSheet("""
                QPushButton { border: none; background: transparent; } 
                QPushButton:hover { background: rgba(255,255,255,0.1); border-radius: 20px; }
            """)

            # Pending overlay (dims icon + shows a spinner)
            overlay = QWidget(btn)
            overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            overlay.setVisible(False)
            overlay.setStyleSheet("background: rgba(0,0,0,110); border-radius: 20px;")
            ov_layout = QVBoxLayout(overlay)
            ov_layout.setContentsMargins(0, 0, 0, 0)
            ov_layout.setAlignment(Qt.AlignCenter)
            spinner = Spinner(overlay)
            spinner.stop()
            ov_layout.addWidget(spinner)

            btn._pending_overlay = overlay
            btn._pending_spinner = spinner
            btn._pending = False
            btn.clicked.connect(callback)
            return btn
        
        # King of the Hill
        self.btn_koth = create_show_btn("koth-icon.png", lambda: self.load_show_playlist("King of the Hill"))
        shows_layout.addWidget(self.btn_koth, 0, Qt.AlignCenter)
        self.show_btns.append(self.btn_koth)
        
        # Aqua Teen Hunger Force
        self.btn_athf = create_show_btn("athf-icon.png", lambda: self.load_show_playlist("Aqua Teen Hunger Force"))
        shows_layout.addWidget(self.btn_athf, 0, Qt.AlignCenter)
        self.show_btns.append(self.btn_athf)

        # Bobs Burgers
        self.btn_bobs = create_show_btn("bobs-icon.png", lambda: self.load_show_playlist("Bob's Burgers"))
        shows_layout.addWidget(self.btn_bobs, 0, Qt.AlignCenter)
        self.show_btns.append(self.btn_bobs)

        # Squidbillies
        self.btn_squid = create_show_btn("squid-icon.png", lambda: self.load_show_playlist("Squidbillies"))
        shows_layout.addWidget(self.btn_squid, 0, Qt.AlignCenter)
        self.show_btns.append(self.btn_squid)
        
        main_layout.addLayout(shows_layout)
        main_layout.addStretch(1) # Balanced vertical centering
        
        # 3. Footer Bar
        footer_widget = QWidget()
        footer_widget.setFixedHeight(80)
        footer_widget.setStyleSheet("background-color: rgba(40, 40, 90, 200);")
        footer_layout = QHBoxLayout(footer_widget)
        footer_layout.setContentsMargins(40, 5, 40, 5)

        self._footer_widget = footer_widget
        self._footer_layout = footer_layout
        
        # Helper for image buttons
        def create_img_btn(filename, callback):
            btn = QPushButton()
            path = get_asset_path(filename)
            pix = QPixmap(path)
            
            if not pix.isNull():
                # Sized by _update_footer_graphics_scale() based on available width.
                btn.setIcon(QIcon(pix))
                btn.setProperty("_footer_pixmap", pix)
                self._footer_icon_items.append(btn)
            else:
                # Debugging Fallback
                print(f"FAILED TO LOAD: {path}")
                btn.setText(f"MISSING:\n{filename}")
                btn.setStyleSheet("color: red; font-weight: bold; background: rgba(255,255,255,0.8); border: 2px solid red;") 
                btn.setFixedSize(120, 60)
                
            if not pix.isNull():
                btn.setFlat(True)
                # Hover effect on buttons
                btn.setStyleSheet("""
                    QPushButton { border: none; background: transparent; border-radius: 5px; }
                    QPushButton:hover { background: rgba(255,255,255,0.2); }
                """)
                
            btn.clicked.connect(callback)
            return btn
            
        def create_composite_btn(text_img_name, check_btn_ref_name, toggle_callback):
            # Container
            container = QWidget()
            # Allow styling
            container.setAttribute(Qt.WA_StyledBackground, True)
            container.setStyleSheet("""
                QWidget { background: transparent; border-radius: 5px; }
                QWidget:hover { background: rgba(255,255,255,0.2); }
            """)

            # Layout
            layout = QHBoxLayout(container)
            layout.setContentsMargins(5, 5, 5, 5)
            layout.setSpacing(10)

            # Checkbox (Custom Button)
            chk = QPushButton()
            chk.setFlat(True)
            chk.setStyleSheet("border: none; background: transparent;")
            chk.setFixedSize(40, 40)
            chk.clicked.connect(toggle_callback)
            setattr(self, check_btn_ref_name, chk)  # Save ref
            layout.addWidget(chk)

            # Text Label (Image Button)
            path = get_asset_path(text_img_name)
            pix = QPixmap(path)

            txt_btn = QPushButton()
            if not pix.isNull():
                # Sized by _update_footer_graphics_scale() based on available width.
                txt_btn.setIcon(QIcon(pix))
                txt_btn.setProperty("_footer_pixmap", pix)
            txt_btn.setFlat(True)
            txt_btn.setStyleSheet("border: none; background: transparent;")
            txt_btn.clicked.connect(toggle_callback)
            layout.addWidget(txt_btn)

            # Track for responsive scaling.
            container._footer_layout = layout
            container._footer_check_btn = chk
            container._footer_text_btn = txt_btn
            container._footer_text_pixmap = pix
            self._footer_composites.append(container)

            return container

        # MENU
        btn_menu = create_img_btn("menu.png", self.go_to_player_menu)
        footer_layout.addWidget(btn_menu)
        footer_layout.addStretch(1)
        
        # EDIT
        btn_edit = create_img_btn("edit.png", self.go_to_edit)
        footer_layout.addWidget(btn_edit)
        footer_layout.addStretch(1)
        
        # TV VIBES
        vibes_widget = create_composite_btn("tv-vibes.png", "btn_vibes_check", self.toggle_vibes)
        footer_layout.addWidget(vibes_widget)
        
        footer_layout.addStretch(1)
        
        # SLEEPY TIME

        sleep_widget = create_composite_btn("sleepy-time.png", "btn_sleep_check", self.toggle_sleep)
        footer_layout.addWidget(sleep_widget)

        main_layout.addWidget(footer_widget)

        # Initial sizing pass after the widget is laid out.
        try:
            QTimer.singleShot(0, self._update_footer_graphics_scale)
        except Exception:
            pass
        
        # 4. Clouds (Absolute Positioned, Top)
        self.lbl_clouds = QLabel(self)
        self.lbl_clouds.setProperty("original_pixmap", QPixmap(get_asset_path("clouds.png")))
        self.lbl_clouds.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.lbl_clouds.setStyleSheet("background: transparent;") # Crucial for gradient visibility
        # Don't hard-code a width here; let resizeEvent size it to the window.
        self.lbl_clouds.setGeometry(0, 0, 0, 0)
        
        # 5. Logo (Absolute Positioned, Top Center)
        self.lbl_logo = QLabel(self)
        self.lbl_logo.setProperty("original_pixmap", QPixmap(get_asset_path("sleepy-shows-logo.png")))
        self.lbl_logo.setStyleSheet("background: transparent;")
        self.lbl_logo.setAlignment(Qt.AlignCenter)
        self.lbl_logo.setGeometry(0, 0, 0, 0)
        
        # Z-Order
        self.lbl_clouds.raise_()
        self.lbl_logo.raise_() 

        # Init visual state
        self.update_checkbox(self.btn_vibes_check, self.is_vibes_on)
        self.update_checkbox(self.btn_sleep_check, self.is_sleep_on)

        # Sync underlying app state with the toggles.
        try:
            self.main_window.set_bumps_enabled(self.is_vibes_on)
        except Exception:
            pass
        try:
            if self.is_sleep_on:
                self.main_window.start_sleep_timer(getattr(self.main_window, 'sleep_timer_default_minutes', 180))
            else:
                self.main_window.cancel_sleep_timer()
        except Exception:
            pass

    def set_show_pending(self, show_name, pending):
        btn = None
        if show_name == "King of the Hill":
            btn = getattr(self, 'btn_koth', None)
        elif show_name == "Aqua Teen Hunger Force":
            btn = getattr(self, 'btn_athf', None)
        elif show_name == "Bob's Burgers":
            btn = getattr(self, 'btn_bobs', None)
        elif show_name == "Squidbillies":
            btn = getattr(self, 'btn_squid', None)

        if btn is None:
            return

        overlay = getattr(btn, '_pending_overlay', None)
        spinner = getattr(btn, '_pending_spinner', None)
        if overlay is None or spinner is None:
            return

        btn._pending = bool(pending)
        overlay.setGeometry(btn.rect())

        if pending:
            overlay.setVisible(True)
            spinner.start()
        else:
            spinner.stop()
            overlay.setVisible(False)

    def resizeEvent(self, event):
        w = event.size().width()
        h = event.size().height()
        
        # 1. Resize Clouds to span width
        if hasattr(self, 'lbl_clouds'):
            orig_clouds = self.lbl_clouds.property("original_pixmap")
            if orig_clouds and not orig_clouds.isNull():
                scaled = orig_clouds.scaledToWidth(w, Qt.SmoothTransformation)
                self.lbl_clouds.setPixmap(scaled)
                self.lbl_clouds.setGeometry(0, 0, w, scaled.height())
                
        # 2. Position Logo (Much Bigger)
        if hasattr(self, 'lbl_logo'):
            orig_logo = self.lbl_logo.property("original_pixmap")
            if orig_logo and not orig_logo.isNull():
                 # Scale logo based on available width (prevents it from forcing a wide window).
                 # Keep an upper bound for large monitors, but always allow it to shrink.
                 logo_w = int(min(600, max(220, w * 0.55)))
                 logo_h = int(max(120, h * 0.22))
                 scaled_logo = orig_logo.scaled(logo_w, logo_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                 self.lbl_logo.setPixmap(scaled_logo)
                 # Center X, Top Y (e.g. 20px down)
                 x_pos = (w - scaled_logo.width()) // 2
                 self.lbl_logo.setGeometry(x_pos, 20, scaled_logo.width(), scaled_logo.height())

        # 3. Keep pending overlays in sync with their buttons.
        for btn in self.show_btns:
            overlay = getattr(btn, '_pending_overlay', None)
            if overlay is not None:
                overlay.setGeometry(btn.rect())

        # 4. Scale footer graphics so they fit the current width.
        try:
            self._update_footer_graphics_scale()
        except Exception:
            pass
        
        super().resizeEvent(event)

        
    def update_checkbox(self, btn, checked, target_size: QSize | None = None):
        base = QPixmap(get_asset_path("checkbox.png"))
        if base.isNull(): return
        
        if target_size is None:
            target_size = base.size()

        result = QPixmap(base.size())
        result.fill(Qt.transparent)
        
        painter = QPainter(result)
        painter.drawPixmap(0, 0, base)
        
        overlay_name = "check.png" if checked else "ex.png"
        overlay = QPixmap(get_asset_path(overlay_name))
        
        if not overlay.isNull():
            # Center overlay
            ox = (base.width() - overlay.width()) // 2
            oy = (base.height() - overlay.height()) // 2
            painter.drawPixmap(ox, oy, overlay)
            
        painter.end()

        scaled = result.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        btn.setIcon(QIcon(scaled))
        btn.setIconSize(scaled.size())
        btn.setFixedSize(scaled.size())

    def _update_footer_graphics_scale(self):
        if self._footer_widget is None or self._footer_layout is None:
            return

        fw = int(self._footer_widget.width())
        fh = int(self._footer_widget.height())
        if fw <= 0 or fh <= 0:
            return

        margins = self._footer_layout.contentsMargins()
        available_w = max(1, fw - int(margins.left()) - int(margins.right()))

        # Base sizes (what the UI was designed around)
        base_icon_h = 50
        base_pad = 10
        base_chk = 40
        base_comp_spacing = 10
        comp_lr_margins = 10  # composite layout left+right (5+5)

        def scaled_icon_width(pix: QPixmap, icon_h: int) -> int:
            if pix is None or pix.isNull() or pix.height() <= 0:
                return icon_h
            return int(pix.width() * (icon_h / pix.height()))

        # Compute required width at base size.
        required_w = 0

        for btn in self._footer_icon_items:
            pix = btn.property("_footer_pixmap")
            required_w += scaled_icon_width(pix, base_icon_h) + base_pad

        for comp in self._footer_composites:
            pix = getattr(comp, '_footer_text_pixmap', None)
            required_w += comp_lr_margins + base_chk + base_comp_spacing + (scaled_icon_width(pix, base_icon_h) + base_pad)

        # Scale down if needed so everything fits.
        scale = 1.0
        if required_w > 0 and required_w > available_w:
            scale = max(0.35, min(1.0, available_w / required_w))

        icon_h = max(22, int(base_icon_h * scale))
        pad = max(6, int(base_pad * scale))
        chk = max(22, int(base_chk * scale))
        comp_spacing = max(4, int(base_comp_spacing * scale))

        # Also respect vertical space.
        max_icon_h_by_height = max(18, fh - 20)
        if icon_h > max_icon_h_by_height:
            icon_h = max_icon_h_by_height

        for btn in self._footer_icon_items:
            pix = btn.property("_footer_pixmap")
            if pix is None or pix.isNull():
                continue
            w = max(1, scaled_icon_width(pix, icon_h))
            btn.setIcon(QIcon(pix))
            btn.setIconSize(QSize(w, icon_h))
            btn.setFixedSize(w + pad, icon_h + pad)

        self._footer_checkbox_target_size = QSize(chk, chk)

        for comp in self._footer_composites:
            layout = getattr(comp, '_footer_layout', None)
            chk_btn = getattr(comp, '_footer_check_btn', None)
            txt_btn = getattr(comp, '_footer_text_btn', None)
            pix = getattr(comp, '_footer_text_pixmap', None)
            if layout is not None:
                layout.setSpacing(comp_spacing)

            if chk_btn is not None:
                # keep check mark rendering crisp at the new size
                if chk_btn is getattr(self, 'btn_vibes_check', None):
                    self.update_checkbox(chk_btn, self.is_vibes_on, target_size=self._footer_checkbox_target_size)
                elif chk_btn is getattr(self, 'btn_sleep_check', None):
                    self.update_checkbox(chk_btn, self.is_sleep_on, target_size=self._footer_checkbox_target_size)
                else:
                    chk_btn.setFixedSize(chk, chk)

            if txt_btn is not None and pix is not None and not pix.isNull():
                w = max(1, scaled_icon_width(pix, icon_h))
                txt_btn.setIcon(QIcon(pix))
                txt_btn.setIconSize(QSize(w, icon_h))
                txt_btn.setFixedSize(w + pad, icon_h + pad)

    def load_show_playlist(self, show_name):
        try:
            self.main_window.ensure_show_playlist_loaded(show_name, auto_play=True)
        except Exception:
            filename = resolve_playlist_path(os.path.join("playlists", f"{show_name}.json"))
            if filename and os.path.exists(filename):
                self.main_window.load_playlist(filename, auto_play=True)
            else:
                print(f"Playlist not found: {filename}")

    def go_to_player_menu(self):
        self.main_window.set_mode(2) # Play mode
        if not self.main_window.play_mode_widget.sidebar_visible:
            self.main_window.play_mode_widget.toggle_sidebar()
            
    def go_to_edit(self):
        self.main_window.set_mode(1) # Edit mode

    def toggle_vibes(self):
        self.is_vibes_on = not self.is_vibes_on
        self.update_checkbox(self.btn_vibes_check, self.is_vibes_on, target_size=self._footer_checkbox_target_size)
        self.main_window.set_bumps_enabled(self.is_vibes_on)

    def toggle_sleep(self):
        self.is_sleep_on = not self.is_sleep_on
        self.update_checkbox(self.btn_sleep_check, self.is_sleep_on, target_size=self._footer_checkbox_target_size)
        
        if self.is_sleep_on:
            self.main_window.start_sleep_timer(180) 
        else:
            self.main_window.cancel_sleep_timer()

class DropListWidget(QListWidget):
    """A list widget that accepts drag-and-drop from the library tree."""
    filesDropped = Signal(list) # emits list of selected items from Tree
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDrop) 
        self.setDefaultDropAction(Qt.MoveAction)
        
    def dragEnterEvent(self, event):
        if event.source() and isinstance(event.source(), QTreeWidget):
            event.accept()
        else:
            super().dragEnterEvent(event)
            
    def dragMoveEvent(self, event):
        if event.source() and isinstance(event.source(), QTreeWidget):
            event.accept()
        else:
            super().dragMoveEvent(event)
            
    def dropEvent(self, event):
        if event.source() and isinstance(event.source(), QTreeWidget):
            items = event.source().selectedItems()
            self.filesDropped.emit(items)
            event.accept()
        else:
            super().dropEvent(event)

class EditModeWidget(QWidget):
    """
    Widget for managing the library and building playlists.
    Left: Library Tree (File Browser)
    Right: Playlist Builder (Drag & Drop target)
    """
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        # We access playlist_manager via main_window for shared state
        self.setup_ui()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Splitter (Left: Library, Right: Playlist)
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)
        
        # --- 1. Library Column ---
        library_widget = QWidget()
        lib_layout = QVBoxLayout(library_widget)

        self.left_tabs = QTabWidget()
        lib_layout.addWidget(self.left_tabs)

        # --- Library tab ---
        library_tab = QWidget()
        library_tab_layout = QVBoxLayout(library_tab)
        library_tab_layout.addWidget(QLabel("Library"))

        self.library_tree = QTreeWidget()
        self.library_tree.setHeaderLabel("Episodes")
        self.library_tree.setDragEnabled(True)
        self.library_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.library_tree.itemDoubleClicked.connect(self.main_window.play_from_library)
        library_tab_layout.addWidget(self.library_tree)

        lib_controls = QHBoxLayout()
        self.btn_add_source = QPushButton("Add Folder")
        self.btn_add_source.clicked.connect(self.main_window.add_source_folder)
        lib_controls.addWidget(self.btn_add_source)

        self.btn_clear_library = QPushButton("Clear")
        self.btn_clear_library.clicked.connect(self.main_window.clear_library)
        lib_controls.addWidget(self.btn_clear_library)

        self.btn_add_to_playlist = QPushButton("Add Selection")
        self.btn_add_to_playlist.clicked.connect(self.add_selected_to_playlist)
        lib_controls.addWidget(self.btn_add_to_playlist)
        library_tab_layout.addLayout(lib_controls)

        self.left_tabs.addTab(library_tab, "Library")

        # --- Playlists tab ---
        playlists_tab = QWidget()
        playlists_tab_layout = QVBoxLayout(playlists_tab)
        playlists_tab_layout.addWidget(QLabel("Saved Playlists"))

        self.saved_playlists_list = QListWidget()
        self.saved_playlists_list.setSelectionMode(QListWidget.SingleSelection)
        self.saved_playlists_list.itemDoubleClicked.connect(self.load_selected_saved_playlist_into_editor)
        playlists_tab_layout.addWidget(self.saved_playlists_list)

        playlists_controls = QHBoxLayout()
        self.btn_refresh_saved_playlists = QPushButton("Refresh")
        self.btn_refresh_saved_playlists.clicked.connect(self.refresh_saved_playlists_list)
        playlists_controls.addWidget(self.btn_refresh_saved_playlists)

        self.btn_load_saved_playlist = QPushButton("Load into Editor")
        self.btn_load_saved_playlist.clicked.connect(self.load_selected_saved_playlist_into_editor)
        playlists_controls.addWidget(self.btn_load_saved_playlist)
        playlists_controls.addStretch(1)
        playlists_tab_layout.addLayout(playlists_controls)

        self.left_tabs.addTab(playlists_tab, "Playlists")
        
        splitter.addWidget(library_widget)
        
        # --- 2. Playlist Column ---
        playlist_widget = QWidget()
        plist_layout = QVBoxLayout(playlist_widget)
        plist_layout.addWidget(QLabel("Playlist Builder"))
        
        self.playlist_list = DropListWidget()
        self.playlist_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.playlist_list.filesDropped.connect(self.add_dropped_items)
        
        # Playlist Context Menu
        self.playlist_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.playlist_list.customContextMenuRequested.connect(self.main_window.show_playlist_context_menu)
        
        plist_layout.addWidget(self.playlist_list)
        
        selected_controls = QHBoxLayout()
        self.btn_remove_selected = QPushButton("Remove Selected")
        self.btn_remove_selected.clicked.connect(self.main_window.remove_from_playlist)
        selected_controls.addWidget(self.btn_remove_selected)
        plist_layout.addLayout(selected_controls)
        
        # Playlist Generator Controls
        gen_controls = QVBoxLayout()
        
        h_gen = QHBoxLayout()
        self.shuffle_mode = 'off'
        self.btn_shuffle_mode = QPushButton("Shuffle: Off")
        self.btn_shuffle_mode.clicked.connect(self.cycle_shuffle_mode)
        h_gen.addWidget(self.btn_shuffle_mode)
        
        self.chk_interstitials = QPushButton("Interludes: OFF")
        self.chk_interstitials.setCheckable(True)
        self.chk_interstitials.toggled.connect(lambda c: self.chk_interstitials.setText(f"Interludes: {'ON' if c else 'OFF'}"))
        # Interludes only apply when TV Vibes (bumps) are enabled.
        try:
            vibes_on = bool(getattr(self.main_window, 'bumps_enabled', False))
        except Exception:
            vibes_on = False
        try:
            self.chk_interstitials.setEnabled(bool(vibes_on))
            self.chk_interstitials.setToolTip("Requires TV Vibes")
        except Exception:
            pass
        h_gen.addWidget(self.chk_interstitials)
        gen_controls.addLayout(h_gen)
        
        self.btn_set_interstitial = QPushButton("Set Interludes Folder")
        self.btn_set_interstitial.clicked.connect(self.main_window.choose_interstitial_folder)
        gen_controls.addWidget(self.btn_set_interstitial)

        self.btn_frequency_settings = QPushButton("Frequency Settings…")
        self.btn_frequency_settings.clicked.connect(self.open_frequency_settings_dialog)
        gen_controls.addWidget(self.btn_frequency_settings)
        
        self.btn_generate_playlist = QPushButton("Generate Playlist from Library")
        self.btn_generate_playlist.clicked.connect(self.generate_playlist)
        gen_controls.addWidget(self.btn_generate_playlist)
        
        self.btn_clear_playlist = QPushButton("Clear Playlist")
        self.btn_clear_playlist.clicked.connect(self.main_window.clear_playlist)
        gen_controls.addWidget(self.btn_clear_playlist)
        
        # Save/Load
        h_save = QHBoxLayout()
        btn_save = QPushButton("Save Playlist")
        btn_save.clicked.connect(self.main_window.save_playlist)
        h_save.addWidget(btn_save)
        
        btn_load = QPushButton("Load Playlist")
        btn_load.clicked.connect(self.main_window.load_playlist)
        h_save.addWidget(btn_load)
        gen_controls.addLayout(h_save)
        
        plist_layout.addLayout(gen_controls)
        splitter.addWidget(playlist_widget)
        
        # Expose widgets to MainWindow via properties or direct access
        # but cleaner if we handle logic here or call main window

        self.refresh_saved_playlists_list()

    def refresh_saved_playlists_list(self):
        try:
            self.saved_playlists_list.clear()
        except Exception:
            return

        files = []
        try:
            files = list(self.main_window.playlist_manager.list_saved_playlists() or [])
        except Exception:
            files = []

        files = sorted([f for f in files if isinstance(f, str)], key=natural_sort_key)
        for f in files:
            item = QListWidgetItem(f)
            item.setData(Qt.UserRole, os.path.join('playlists', f))
            self.saved_playlists_list.addItem(item)

    def load_selected_saved_playlist_into_editor(self, item=None):
        if not isinstance(item, QListWidgetItem):
            try:
                item = self.saved_playlists_list.currentItem()
            except Exception:
                item = None
        if item is None:
            return
        filename = None
        try:
            filename = item.data(Qt.UserRole)
        except Exception:
            filename = None
        if not filename:
            filename = item.text()
            if isinstance(filename, str) and not filename.lower().endswith('.json'):
                filename = filename + '.json'
            filename = os.path.join('playlists', filename)
        self.main_window.load_playlist_into_editor(filename)

    def open_frequency_settings_dialog(self):
        pm = self.main_window.playlist_manager

        episode_paths = []
        for it in list(getattr(pm, 'current_playlist', []) or []):
            try:
                if pm.is_episode_item(it):
                    p = it.get('path') if isinstance(it, dict) else None
                    if p:
                        episode_paths.append(p)
            except Exception:
                continue

        if not episode_paths:
            QMessageBox.information(self, "Frequency Settings", "This playlist has no episodes.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Frequency Settings")
        dlg.setModal(True)
        dlg.resize(900, 520)

        root = QVBoxLayout(dlg)
        tabs = QTabWidget()
        root.addWidget(tabs)

        # Seasons tab
        seasons_widget = QWidget()
        seasons_layout = QVBoxLayout(seasons_widget)
        seasons_table = QTableWidget()
        seasons_table.setColumnCount(3)
        seasons_table.setHorizontalHeaderLabels(["Season", "Exposure Offset", "Exposure Factor"])
        seasons_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        seasons_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        seasons_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        seasons_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        seasons_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        seasons_layout.addWidget(seasons_table)
        tabs.addTab(seasons_widget, "Seasons")

        season_nums = {}
        for p in episode_paths:
            try:
                n = int(pm._season_key_from_path(p) or 0)
            except Exception:
                n = 0
            if n > 0:
                season_nums[int(n)] = True
        season_nums = sorted(list(season_nums.keys()))

        season_keys = [f"season:{n}" for n in season_nums]

        seasons_table.setRowCount(len(season_keys))
        for r, sk in enumerate(season_keys):
            # Display as "Season N" but store canonical key in UserRole.
            try:
                n = int(str(sk).split(':', 1)[1])
            except Exception:
                n = 0
            it0 = QTableWidgetItem(f"Season {n}" if n else str(sk))
            it0.setFlags(it0.flags() & ~Qt.ItemIsEditable)
            it0.setData(Qt.UserRole, str(sk))
            seasons_table.setItem(r, 0, it0)

            try:
                off = float(getattr(pm, 'season_exposure_offsets', {}).get(sk, 0.0) or 0.0)
            except Exception:
                off = 0.0
            it1 = QTableWidgetItem(f"{off:.0f}" if off else "0")
            seasons_table.setItem(r, 1, it1)

            try:
                fac = float(getattr(pm, 'season_exposure_factors', {}).get(sk, 1.0) or 1.0)
            except Exception:
                fac = 1.0
            it2 = QTableWidgetItem(f"{fac:.3f}" if fac else "1.000")
            seasons_table.setItem(r, 2, it2)

        # Episodes tab
        episodes_widget = QWidget()
        episodes_layout = QVBoxLayout(episodes_widget)
        episodes_table = QTableWidget()
        episodes_table.setColumnCount(4)
        episodes_table.setHorizontalHeaderLabels(["Episode", "Season", "Exposure Offset", "Exposure Factor"])
        episodes_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        episodes_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        episodes_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        episodes_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        episodes_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        episodes_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        episodes_layout.addWidget(episodes_table)
        tabs.addTab(episodes_widget, "Episodes")

        # Stable ordering by basename then full path.
        rows = []
        for p in episode_paths:
            rows.append((os.path.basename(str(p)), str(p)))
        rows.sort(key=lambda t: natural_sort_key(t[0]) + natural_sort_key(t[1]))

        episodes_table.setRowCount(len(rows))
        for r, (bn, p) in enumerate(rows):
            ep_item = QTableWidgetItem(str(bn))
            ep_item.setFlags(ep_item.flags() & ~Qt.ItemIsEditable)
            ep_item.setData(Qt.UserRole, str(p))
            episodes_table.setItem(r, 0, ep_item)

            try:
                n = int(pm._season_key_from_path(p) or 0)
            except Exception:
                n = 0
            sk = f"season:{n}" if n else ''
            sk_item = QTableWidgetItem(f"Season {n}" if n else "")
            sk_item.setFlags(sk_item.flags() & ~Qt.ItemIsEditable)
            sk_item.setData(Qt.UserRole, str(sk))
            episodes_table.setItem(r, 1, sk_item)

            key = None
            try:
                key = pm._norm_path_key(p)
            except Exception:
                key = None
            try:
                off = float(getattr(pm, 'episode_exposure_offsets', {}).get(key, 0.0) or 0.0)
            except Exception:
                off = 0.0
            it1 = QTableWidgetItem(f"{off:.0f}" if off else "0")
            episodes_table.setItem(r, 2, it1)

            try:
                fac = float(getattr(pm, 'episode_exposure_factors', {}).get(key, 1.0) or 1.0)
            except Exception:
                fac = 1.0
            it2 = QTableWidgetItem(f"{fac:.3f}" if fac else "1.000")
            episodes_table.setItem(r, 3, it2)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_ok = QPushButton("Save")
        btn_cancel.clicked.connect(dlg.reject)
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        root.addLayout(btn_row)

        if dlg.exec() != QDialog.Accepted:
            return

        # Merge changes for the visible playlist items only.
        ep_off = dict(getattr(pm, 'episode_exposure_offsets', {}) or {})
        s_off = dict(getattr(pm, 'season_exposure_offsets', {}) or {})
        ep_fac = dict(getattr(pm, 'episode_exposure_factors', {}) or {})
        s_fac = dict(getattr(pm, 'season_exposure_factors', {}) or {})

        def _parse_float(item, default):
            try:
                if item is None:
                    return float(default)
                txt = str(item.text()).strip()
                if txt == '':
                    return float(default)
                return float(txt)
            except Exception:
                return float(default)

        for r in range(seasons_table.rowCount()):
            sk_item = seasons_table.item(r, 0)
            if sk_item is None:
                continue
            try:
                sk = str(sk_item.data(Qt.UserRole) or '').strip()
            except Exception:
                sk = ''
            if not sk:
                continue
            off = max(0.0, _parse_float(seasons_table.item(r, 1), 0.0))
            fac = _parse_float(seasons_table.item(r, 2), 1.0)
            if fac <= 0.0:
                fac = 1.0

            if off > 0.0:
                s_off[sk] = float(off)
            else:
                s_off.pop(sk, None)

            if abs(fac - 1.0) > 1e-9:
                s_fac[sk] = float(fac)
            else:
                s_fac.pop(sk, None)

        for r in range(episodes_table.rowCount()):
            ep_item = episodes_table.item(r, 0)
            if ep_item is None:
                continue
            try:
                p = ep_item.data(Qt.UserRole)
            except Exception:
                p = None
            if not p:
                continue
            try:
                key = pm._norm_path_key(p)
            except Exception:
                key = None
            if not key:
                continue

            off = max(0.0, _parse_float(episodes_table.item(r, 2), 0.0))
            fac = _parse_float(episodes_table.item(r, 3), 1.0)
            if fac <= 0.0:
                fac = 1.0

            if off > 0.0:
                ep_off[key] = float(off)
            else:
                ep_off.pop(key, None)

            if abs(fac - 1.0) > 1e-9:
                ep_fac[key] = float(fac)
            else:
                ep_fac.pop(key, None)

        pm.apply_frequency_settings(
            episode_offsets=ep_off,
            season_offsets=s_off,
            episode_factors=ep_fac,
            season_factors=s_fac,
        )

        try:
            self.main_window.persist_current_playlist_frequency_settings()
        except Exception:
            pass

        QMessageBox.information(self, "Frequency Settings", "Saved exposure frequency settings.")
    
    def add_dropped_items(self, items):
        self.main_window.add_dropped_items(items)

    def add_selected_to_playlist(self):
        self.main_window.add_selected_to_playlist()

    def generate_playlist(self):
        # Gather settings from local buttons and call main window
        try:
            vibes_on = bool(getattr(self.main_window, 'bumps_enabled', False))
        except Exception:
            vibes_on = False
        self.main_window.generate_playlist_logic(
            shuffle_mode=self.shuffle_mode,
            interstitials=(self.chk_interstitials.isChecked() and bool(vibes_on)),
            bumps=bool(vibes_on)
        )

    def cycle_shuffle_mode(self):
        self.shuffle_mode = _next_shuffle_mode(self.shuffle_mode)
        self.btn_shuffle_mode.setText(f"Shuffle: {self.shuffle_mode.title()}")
        # Update runtime mode immediately (does not change current episode).
        self.main_window.set_shuffle_mode(self.shuffle_mode)
        
    def refresh_playlist_list(self):
        self.playlist_list.clear()
        for i, item in enumerate(self.main_window.playlist_manager.current_playlist):
            if isinstance(item, dict):
                itype = item.get('type', 'video')
                if itype == 'video':
                    name = os.path.basename(item['path'])
                    self.playlist_list.addItem(f"{i+1}. {name}")
                elif itype == 'interstitial':
                    name = os.path.basename(item['path'])
                    self.playlist_list.addItem(f"{i+1}. [IL] {name}")
                elif itype == 'bump':
                   self.playlist_list.addItem(f"{i+1}. [BUMP] {os.path.basename(item.get('audio', 'Unknown'))}")
            else:
                name = os.path.basename(item)
                self.playlist_list.addItem(f"{i+1}. {name}")

class ClickableSlider(QSlider):
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            val = self.minimum() + ((self.maximum() - self.minimum()) * event.position().x()) / self.width()
            val = self.maximum() - val if self.invertedAppearance() else val
            self.setValue(int(val))
            event.accept()
            # Also emit sliderMoved to trigger live seek
            self.sliderMoved.emit(int(val))


class GradientScrubberSlider(ClickableSlider):
    """Slider whose circular handle is filled with the shared gradient.

    We let the stylesheet paint the track/progress, then overlay the handle.
    """

    def paintEvent(self, event):
        super().paintEvent(event)

        if self.orientation() != Qt.Horizontal:
            return

        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove_rect = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle_rect = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        if handle_rect.isNull():
            return

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)

            # Paint the timeline groove: left of scrubber is theme, remainder is shared gradient.
            if not groove_rect.isNull():
                gr = groove_rect.adjusted(0, 0, 0, 0)
                groove_h = 10
                gr = QRect(gr.left(), gr.center().y() - groove_h // 2, gr.width(), groove_h)

                clip = QPainterPath()
                clip.addRoundedRect(gr, groove_h // 2, groove_h // 2)
                painter.save()
                try:
                    painter.setClipPath(clip)

                    handle_x = handle_rect.center().x()
                    split_x = max(gr.left(), min(gr.right(), handle_x))

                    left_rect = QRect(gr.left(), gr.top(), max(0, split_x - gr.left()), gr.height())
                    right_rect = QRect(split_x, gr.top(), max(0, gr.right() - split_x + 1), gr.height())

                    if left_rect.width() > 0:
                        painter.fillRect(left_rect, QColor(THEME_COLOR))
                    if right_rect.width() > 0:
                        _fill_rect_with_shared_modern_gradient(painter, self, right_rect)
                finally:
                    painter.restore()

            # Draw a solid white scrubber handle.
            painter.setPen(Qt.NoPen)
            r = handle_rect.adjusted(1, 1, -1, -1)
            painter.setBrush(QColor(255, 255, 255))
            painter.drawEllipse(r)
        finally:
            painter.end()

class PlayModeWidget(QWidget):
    """
    Widget for Playback.
    Layout: Sidebar (Left) | Video (Right)
    """
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setup_ui()

    def minimumSizeHint(self):
        # Don't let Play Mode's widgets force the entire app window to a huge
        # minimum width at startup (QStackedWidget may use the max minHint).
        return QSize(1, 1)

    def _tint_pixmap(self, pixmap, color=Qt.white):
        if pixmap is None or pixmap.isNull():
            return pixmap
        out = QPixmap(pixmap.size())
        out.fill(Qt.transparent)

        painter = QPainter(out)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.drawPixmap(0, 0, pixmap)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(out.rect(), QColor(color))
        painter.end()
        return out

    def _tint_icon(self, icon, size: QSize, color=Qt.white):
        if icon is None or icon.isNull():
            return icon
        pm = icon.pixmap(size)
        if pm.isNull():
            return icon
        return QIcon(self._tint_pixmap(pm, color=color))

    def _make_play_slash_pause_icon(self, icon_h=40):
        play_pm = self._tint_pixmap(self.style().standardIcon(QStyle.SP_MediaPlay).pixmap(icon_h, icon_h), Qt.white)
        pause_pm = self._tint_pixmap(self.style().standardIcon(QStyle.SP_MediaPause).pixmap(icon_h, icon_h), Qt.white)

        gap = max(6, icon_h // 6)
        slash_w = max(10, icon_h // 3)
        w = int(play_pm.width() + pause_pm.width() + slash_w + gap * 2)
        h = int(max(play_pm.height(), pause_pm.height()))

        pm = QPixmap(w, h)
        pm.fill(Qt.transparent)

        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing, True)

        y_play = (h - play_pm.height()) // 2
        painter.drawPixmap(0, y_play, play_pm)

        pen = painter.pen()
        pen.setColor(Qt.white)
        pen.setWidth(max(2, icon_h // 14))
        painter.setPen(pen)
        x1 = play_pm.width() + gap
        x2 = x1 + slash_w
        painter.drawLine(int(x2), 2, int(x1), h - 2)

        x_pause = play_pm.width() + gap + slash_w + gap
        y_pause = (h - pause_pm.height()) // 2
        painter.drawPixmap(int(x_pause), y_pause, pause_pm)

        painter.end()
        return QIcon(pm)

    def _make_hamburger_icon(self, size=32):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)

        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing, True)

        pen = painter.pen()
        pen.setColor(Qt.white)
        pen.setWidth(max(2, size // 10))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)

        margin = max(4, size // 6)
        y1 = margin
        y2 = size // 2
        y3 = size - margin
        painter.drawLine(margin, y1, size - margin, y1)
        painter.drawLine(margin, y2, size - margin, y2)
        painter.drawLine(margin, y3, size - margin, y3)

        painter.end()
        return QIcon(pm)
        
    def setup_ui(self):
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        
        # --- Sidebar ---
        self.sidebar_container = QWidget()
        self.sidebar_container.setFixedWidth(300)
        self.sidebar_container.setStyleSheet("background-color: #2b2b2b;")
        side_layout = QVBoxLayout(self.sidebar_container)
        
        # Close Button for Sidebar
        self.btn_close_menu = QPushButton("Close Menu")
        self.btn_close_menu.setMinimumHeight(40)
        self.btn_close_menu.clicked.connect(self.toggle_sidebar)
        side_layout.addWidget(self.btn_close_menu)

        # Back to Main
        self.btn_back_main = QPushButton("Back to Main")
        self.btn_back_main.setMinimumHeight(40)
        self.btn_back_main.setStyleSheet("background-color: #444; color: white;")
        self.btn_back_main.clicked.connect(self.main_window.go_to_welcome)
        side_layout.addWidget(self.btn_back_main)

        side_layout.addWidget(QLabel("Saved Playlists:"))
        self.playlists_list_widget = QListWidget()
        self.playlists_list_widget.setStyleSheet("font-size: 14pt;")
        self.playlists_list_widget.itemClicked.connect(self.load_selected_playlist)
        self.playlists_list_widget.itemDoubleClicked.connect(self.load_and_play_playlist)
        side_layout.addWidget(self.playlists_list_widget)
        
        side_layout.addWidget(QLabel("Current Playlist Episodes:"))
        self.episode_list_widget = QListWidget()
        self.episode_list_widget.itemDoubleClicked.connect(self.play_episode_from_list)
        side_layout.addWidget(self.episode_list_widget)
        
        side_refresh_btn = QPushButton("Refresh Playlists")
        side_refresh_btn.clicked.connect(self.refresh_playlists)
        side_layout.addWidget(side_refresh_btn)
        
        self.layout.addWidget(self.sidebar_container)
        
        # --- Video Area ---
        self.video_area = QWidget()
        self.video_layout = QVBoxLayout(self.video_area)
        self.video_layout.setContentsMargins(0, 0, 0, 0)
        self.video_layout.setSpacing(0)
        
        # Placeholder for Video Stack (filled by MainWindow)
        self.video_placeholder = QWidget() 
        # MainWindow will add specific widgets here
        
        # Controls Group
        self.controls_widget = QWidget()
        self.controls_widget.setObjectName("controls_widget")
        # Single-row controls bar; we dynamically shrink controls on resize.
        self.controls_widget.setFixedHeight(180)
        self.controls_widget.setStyleSheet("background-color: #1a1a1a;")
        controls_layout = QVBoxLayout(self.controls_widget)
        
        # Sliders Row
        seek_layout = QHBoxLayout()
        self.lbl_current_time = QLabel("00:00 / 00:00")
        self.lbl_current_time.setStyleSheet("font-size: 18pt; color: white; margin-right: 10px;")
        seek_layout.addWidget(self.lbl_current_time)
        
        self.slider_seek = GradientScrubberSlider(Qt.Horizontal)
        self.slider_seek.setFixedHeight(60)
        self.slider_seek.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 10px;
                margin: 0px;
                border-radius: 5px;
                background: transparent;
            }}
            QSlider::sub-page:horizontal {{
                background: transparent;
                border-radius: 5px;
            }}
            QSlider::add-page:horizontal {{
                background: transparent;
                border-radius: 5px;
            }}
            QSlider::handle:horizontal {{
                width: 30px;
                height: 30px;
                margin: -10px 0;
                border-radius: 15px;
                background: transparent;
                border: none;
            }}
        """)
        self.slider_seek.setRange(0, 100)
        self.slider_seek.sliderMoved.connect(self.main_window.seek_video) # On drag/click
        self.slider_seek.sliderPressed.connect(self.main_window.on_seek_start)
        self.slider_seek.sliderReleased.connect(self.main_window.on_seek_end)
        seek_layout.addWidget(self.slider_seek)
        
        controls_layout.addLayout(seek_layout)
        
        # Buttons Row (single line, responsive)
        # Use explicit groups (left / playback / right) to preserve visual grouping.
        btns_layout = QHBoxLayout()
        btns_layout.setContentsMargins(10, 0, 10, 0)
        btns_layout.setSpacing(12)
        self._controls_btns_layout = btns_layout

        self._controls_group_left = QWidget()
        left_layout = QHBoxLayout(self._controls_group_left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        self._controls_left_layout = left_layout

        self._controls_group_playback = QWidget()
        playback_layout = QHBoxLayout(self._controls_group_playback)
        playback_layout.setContentsMargins(0, 0, 0, 0)
        playback_layout.setSpacing(8)
        self._controls_playback_layout = playback_layout

        self._controls_group_right = QWidget()
        right_layout = QHBoxLayout(self._controls_group_right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        self._controls_right_layout = right_layout

        button_height = 80
        font_style = "font-size: 14pt; font-weight: bold;"

        # --- Left Group: Menu ---
        self.btn_menu = TriStrokeButton()
        self.btn_menu.setFixedSize(120, button_height)
        self.btn_menu.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        menu_icon = QIcon.fromTheme(
            "application-menu",
            QIcon.fromTheme(
                "open-menu-symbolic",
                QIcon.fromTheme("menu")
            ),
        )
        if menu_icon.isNull():
            menu_icon = self._make_hamburger_icon(size=32)
        else:
            menu_icon = self._tint_icon(menu_icon, QSize(32, 32), Qt.white)
        self.btn_menu.setIcon(menu_icon)
        self.btn_menu.setIconSize(QSize(32, 32))
        self.btn_menu.clicked.connect(self.toggle_sidebar)
        left_layout.addWidget(self.btn_menu)
        
        # --- Center Group: Playback Controls ---
        self.btn_seek_back = TriStrokeButton("-20s")
        self.btn_seek_back.setFixedSize(100, button_height)
        self.btn_seek_back.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_seek_back.clicked.connect(lambda: self.main_window.seek_relative(-20))
        playback_layout.addWidget(self.btn_seek_back)
        
        self.btn_prev = TriStrokeButton("<<")
        self.btn_prev.setText("")
        self.btn_prev.setIcon(self._tint_icon(self.style().standardIcon(QStyle.SP_MediaSkipBackward), QSize(32, 32), Qt.white))
        self.btn_prev.setIconSize(QSize(32, 32))
        self.btn_prev.setFixedSize(100, button_height)
        self.btn_prev.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_prev.clicked.connect(self.main_window.skip_to_previous_episode)
        playback_layout.addWidget(self.btn_prev)
        
        # Static play/pause icon button (doesn't change dynamically)
        self.btn_play = TriStrokeButton()
        self.btn_play.setIcon(self._make_play_slash_pause_icon(icon_h=40))
        self.btn_play.setIconSize(QSize(90, 40))
        self.btn_play.setFixedSize(140, button_height)
        self.btn_play.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_play.clicked.connect(self.main_window.toggle_play)
        playback_layout.addWidget(self.btn_play)
        
        self.btn_next = TriStrokeButton(">>")
        self.btn_next.setText("")
        self.btn_next.setIcon(self._tint_icon(self.style().standardIcon(QStyle.SP_MediaSkipForward), QSize(32, 32), Qt.white))
        self.btn_next.setIconSize(QSize(32, 32))
        self.btn_next.setFixedSize(100, button_height)
        self.btn_next.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_next.clicked.connect(self.main_window.skip_to_next_episode)
        playback_layout.addWidget(self.btn_next)
        
        self.btn_seek_fwd = TriStrokeButton("+20s")
        self.btn_seek_fwd.setFixedSize(100, button_height)
        self.btn_seek_fwd.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_seek_fwd.clicked.connect(lambda: self.main_window.seek_relative(20))
        playback_layout.addWidget(self.btn_seek_fwd)

        # Sleep Timer Button (shows remaining minutes)
        self.btn_sleep_timer = TriStrokeButton("SLEEP\nOFF")
        self.btn_sleep_timer.setFixedSize(120, button_height)
        self.btn_sleep_timer.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        # Single-press cycle (menu dropdown is still available from the top menu).
        self.btn_sleep_timer.clicked.connect(self.main_window.cycle_sleep_timer_quick)
        right_layout.addWidget(self.btn_sleep_timer)
        
        # Shuffle Button (Icon with text)
        self.btn_shuffle = TriStrokeToolButton()
        self.btn_shuffle.setFixedSize(80, button_height)
        shuffle_icon = QIcon.fromTheme(
            "media-playlist-shuffle",
            QIcon.fromTheme(
                "media-playlist-random",
                QIcon.fromTheme("shuffle")
            ),
        )
        if shuffle_icon.isNull():
            shuffle_icon = self.style().standardIcon(QStyle.SP_BrowserReload)
        shuffle_icon = self._tint_icon(shuffle_icon, QSize(32, 32), Qt.white)
        self.btn_shuffle.setIcon(shuffle_icon)
        self.btn_shuffle.setIconSize(QSize(32, 32))
        self.btn_shuffle.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.btn_shuffle.setText("OFF")
        self.btn_shuffle.setStyleSheet("QToolButton { font-size: 10pt; font-weight: bold; color: white; background: transparent; border: none; }")
        self.btn_shuffle.clicked.connect(self.main_window.cycle_shuffle_mode)
        right_layout.addWidget(self.btn_shuffle)
        
        self.lbl_volume = QLabel("Vol:")
        self.lbl_volume.setStyleSheet("font-size: 14pt; color: white;")
        right_layout.addWidget(self.lbl_volume)
        
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(100)
        self.slider_vol.setFixedWidth(150)
        self.slider_vol.setFixedHeight(50)
        self.slider_vol.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                border: 1px solid #444;
                height: 10px;
                background: #333;
                margin: 0px;
                border-radius: 5px;
            }}
            QSlider::sub-page:horizontal {{
                background: {THEME_COLOR};
                border-radius: 5px;
            }}
            QSlider::add-page:horizontal {{
                background: #555;
                border-radius: 5px;
            }}
            QSlider::handle:horizontal {{
                width: 24px;
                height: 24px;
                margin: -7px 0;
                background: white;
                border: none;
                border-radius: 12px;
            }}
        """)
        self.slider_vol.valueChanged.connect(self.main_window.set_volume)
        right_layout.addWidget(self.slider_vol)

        self.btn_fullscreen = TriStrokeButton()
        enter_fs_icon = QIcon.fromTheme(
            "view-fullscreen",
            QIcon.fromTheme("fullscreen"),
        )
        if enter_fs_icon.isNull():
            enter_fs_icon = self.style().standardIcon(QStyle.SP_TitleBarMaxButton)
        enter_fs_icon = self._tint_icon(enter_fs_icon, QSize(32, 32), Qt.white)
        self.btn_fullscreen.setIcon(enter_fs_icon)
        self.btn_fullscreen.setIconSize(QSize(32, 32))
        self.btn_fullscreen.setFixedSize(button_height, button_height)
        self.btn_fullscreen.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_fullscreen.setCheckable(True)
        self.btn_fullscreen.clicked.connect(self.main_window.toggle_fullscreen)
        right_layout.addWidget(self.btn_fullscreen)

        # Assemble groups into the row.
        # Keep menu left, utility controls right, and center the primary playback cluster.
        btns_layout.addWidget(self._controls_group_left)
        btns_layout.addStretch(1)
        btns_layout.addWidget(self._controls_group_playback)
        btns_layout.addStretch(1)
        btns_layout.addWidget(self._controls_group_right)

        controls_layout.addLayout(btns_layout)

        # Apply an initial size mode (and keep it updated via resizeEvent).
        self._controls_size_mode = None
        try:
            self._update_controls_size_mode(force=True)
        except Exception:
            pass
        
        # Assemble Video Area
        self.video_layout.addWidget(self.video_placeholder, 1) # This will be replaced
        self.video_layout.addWidget(self.controls_widget)
        
        self.layout.addWidget(self.video_area, 1) # Expand
        
        self.sidebar_visible = True
        self.sidebar_container.setVisible(True)
        self.refresh_playlists()

    def resizeEvent(self, event):
        try:
            self._update_controls_size_mode(force=False)
        except Exception:
            pass
        return super().resizeEvent(event)

    def _controls_size_mode_for_width(self, w: int) -> str:
        try:
            w = int(w)
        except Exception:
            w = 0
        if w and w < 760:
            return 'xs'
        if w and w < 980:
            return 'sm'
        return 'md'

    def _update_controls_size_mode(self, *, force: bool = False):
        # Use the actual controls widget width (it excludes the sidebar).
        try:
            w = int(self.controls_widget.width())
        except Exception:
            w = int(self.width())

        mode = self._controls_size_mode_for_width(w)
        # Even if the breakpoint "mode" doesn't change, we still need to recompute widths
        # on every resize (fullscreen toggles can change width without crossing breakpoints).
        self._controls_size_mode = mode

        # Defaults (we will shrink-to-fit deterministically).
        if mode == 'md':
            button_h = 80
            seek_h = 60
            icon = 32
            shuffle_style = Qt.ToolButtonTextUnderIcon
            show_vol_label = True
            outer_spacing = 12
            playback_spacing = 8
            right_spacing = 10
        elif mode == 'sm':
            button_h = 72
            seek_h = 58
            icon = 28
            shuffle_style = Qt.ToolButtonTextUnderIcon
            show_vol_label = True
            outer_spacing = 10
            playback_spacing = 6
            right_spacing = 8
        else:
            button_h = 64
            seek_h = 54
            icon = 24
            shuffle_style = Qt.ToolButtonIconOnly
            # Hide low-priority label to keep everything on one line.
            show_vol_label = False
            outer_spacing = 8
            playback_spacing = 4
            right_spacing = 6

        # Update layout spacings to match mode.
        try:
            if getattr(self, '_controls_btns_layout', None) is not None:
                self._controls_btns_layout.setSpacing(int(outer_spacing))
            if getattr(self, '_controls_playback_layout', None) is not None:
                self._controls_playback_layout.setSpacing(int(playback_spacing))
            if getattr(self, '_controls_left_layout', None) is not None:
                self._controls_left_layout.setSpacing(int(playback_spacing))
            if getattr(self, '_controls_right_layout', None) is not None:
                self._controls_right_layout.setSpacing(int(right_spacing))
        except Exception:
            pass

        self.controls_widget.setFixedHeight(180)

        try:
            self.btn_menu.setIconSize(QSize(icon, icon))
            self.btn_prev.setIconSize(QSize(icon, icon))
            self.btn_next.setIconSize(QSize(icon, icon))
            self.btn_fullscreen.setIconSize(QSize(icon, icon))
            self.btn_shuffle.setIconSize(QSize(icon, icon))
        except Exception:
            pass

        try:
            self.btn_shuffle.setToolButtonStyle(shuffle_style)
        except Exception:
            pass

        try:
            self.lbl_volume.setVisible(bool(show_vol_label))
        except Exception:
            pass

        # Width plan: set a mode-specific base width, then shrink-to-fit in priority order.
        layout = getattr(self, '_controls_btns_layout', None)
        if layout is None:
            return

        try:
            m = layout.contentsMargins()
            avail = max(0, int(self.controls_widget.width()) - int(m.left()) - int(m.right()))
        except Exception:
            avail = int(self.controls_widget.width())

        def _set_w(widget, width, height=None):
            if widget is None:
                return
            try:
                if height is not None:
                    widget.setFixedHeight(int(height))
            except Exception:
                pass
            try:
                widget.setFixedWidth(int(width))
                return
            except Exception:
                pass
            try:
                widget.setMinimumWidth(int(width))
                widget.setMaximumWidth(int(width))
            except Exception:
                pass

        def _cur_w(widget) -> int:
            try:
                w = int(widget.fixedWidth())
                if w > 0:
                    return w
            except Exception:
                pass
            try:
                return int(widget.width())
            except Exception:
                return 0

        def _visible(widget) -> bool:
            try:
                return bool(widget is not None and widget.isVisible())
            except Exception:
                return bool(widget is not None)

        # Base widths (match the "look" per mode).
        if mode == 'md':
            base = {
                'menu': 120,
                'seek': 100,
                'prevnext': 100,
                'play': 140,
                'sleep': 120,
                'shuffle': 80,
                'vol_label': 44,
                'vol': 150,
                'fs': button_h,
            }
        elif mode == 'sm':
            base = {
                'menu': 104,
                'seek': 92,
                'prevnext': 88,
                'play': 128,
                'sleep': 112,
                'shuffle': 72,
                'vol_label': 40,
                'vol': 130,
                'fs': button_h,
            }
        else:
            base = {
                'menu': 64,
                'seek': 68,
                'prevnext': 64,
                'play': 96,
                'sleep': 86,
                'shuffle': 58,
                'vol_label': 0,
                'vol': 96,
                'fs': button_h,
            }

        mins = {
            'menu': 56,
            'seek': 54,
            'prevnext': 54,
            'play': 70,
            'sleep': 66,
            'shuffle': 52,
            'vol_label': 30,
            'vol': 70,
            'fs': 54,
        }

        # Apply base sizes.
        _set_w(self.btn_menu, base['menu'], button_h)
        _set_w(self.btn_seek_back, base['seek'], button_h)
        _set_w(self.btn_prev, base['prevnext'], button_h)
        _set_w(self.btn_play, base['play'], button_h)
        _set_w(self.btn_next, base['prevnext'], button_h)
        _set_w(self.btn_seek_fwd, base['seek'], button_h)
        _set_w(self.btn_sleep_timer, base['sleep'], button_h)
        _set_w(self.btn_shuffle, base['shuffle'], button_h)
        if show_vol_label:
            _set_w(self.lbl_volume, base['vol_label'], button_h)
        _set_w(self.slider_vol, base['vol'], 50)
        _set_w(self.btn_fullscreen, base['fs'], button_h)

        # Compute the minimum used width for current fixed widths, including layout spacing.
        def _group_used(group_layout, widgets_in_group):
            if group_layout is None:
                return 0
            visible = [w for w in widgets_in_group if _visible(w)]
            if not visible:
                return 0
            try:
                sp = int(group_layout.spacing())
            except Exception:
                sp = 0
            return sum(_cur_w(w) for w in visible) + max(0, (len(visible) - 1) * sp)

        used_left = _group_used(getattr(self, '_controls_left_layout', None), [self.btn_menu])
        used_playback = _group_used(getattr(self, '_controls_playback_layout', None), [
            self.btn_seek_back,
            self.btn_prev,
            self.btn_play,
            self.btn_next,
            self.btn_seek_fwd,
        ])
        used_right = _group_used(getattr(self, '_controls_right_layout', None), [
            self.btn_sleep_timer,
            self.btn_shuffle,
            self.lbl_volume if show_vol_label else None,
            self.slider_vol,
            self.btn_fullscreen,
        ])

        # Outer layout items are: left group, stretch, playback group, stretch, right group.
        try:
            outer_sp = int(layout.spacing())
        except Exception:
            outer_sp = 0
        outer_space_total = 4 * outer_sp
        used = int(used_left + used_playback + used_right + outer_space_total)

        overflow = int(used - avail)
        if overflow > 0:
            # Shrink lowest-priority / widest items first.
            shrink_order = [
                (self.slider_vol, mins['vol']),
                (self.btn_sleep_timer, mins['sleep']),
                (self.btn_menu, mins['menu']),
                (self.btn_shuffle, mins['shuffle']),
                (self.btn_seek_back, mins['seek']),
                (self.btn_seek_fwd, mins['seek']),
                (self.btn_prev, mins['prevnext']),
                (self.btn_next, mins['prevnext']),
                (self.btn_play, mins['play']),
                (self.btn_fullscreen, mins['fs']),
            ]
            if show_vol_label:
                shrink_order.insert(0, (self.lbl_volume, mins['vol_label']))

            for widget, min_w in shrink_order:
                if overflow <= 0:
                    break
                if widget is None:
                    continue
                try:
                    if not widget.isVisible():
                        continue
                except Exception:
                    pass

                cur = _cur_w(widget)
                floor = int(min_w)
                if cur <= floor:
                    continue
                delta = min(int(overflow), int(cur - floor))
                _set_w(widget, int(cur - delta))
                overflow -= int(delta)

        try:
            self.slider_seek.setFixedHeight(int(seek_h))
        except Exception:
            pass

    def set_controls_overlay(self, enabled):
        if enabled:
            # Remove from layout, reparent to video stack/container (done by main window mostly, 
            # but we prepare widget to be floating)
            self.video_layout.removeWidget(self.controls_widget)
            self.controls_widget.setParent(self.main_window.video_container) # Parent to video container to act as overlay
            self.controls_widget.show()
            self.controls_widget.raise_()
             # Colors/Style for overlay?
            self.controls_widget.setStyleSheet("background-color: rgba(26, 26, 26, 200);") # Semi transparent?

            # Overlay mode changes parent/geometry; recalc widths after the event loop settles.
            try:
                QTimer.singleShot(0, lambda: self._update_controls_size_mode(force=True))
            except Exception:
                pass
        else:
            self.controls_widget.setParent(self.video_area) # Make child of video area again
            self.video_layout.addWidget(self.controls_widget)
            self.controls_widget.setStyleSheet("background-color: #1a1a1a;") # Solid

            # Reinserted into layout; recalc widths after relayout.
            try:
                QTimer.singleShot(0, lambda: self._update_controls_size_mode(force=True))
            except Exception:
                pass
            
    def toggle_sidebar(self):
        self.sidebar_visible = not self.sidebar_visible
        self.sidebar_container.setVisible(self.sidebar_visible)
        if self.sidebar_visible:
            self.refresh_playlists()
            self.refresh_episode_list()

    def refresh_playlists(self):
        self.playlists_list_widget.clear()
        playlists = self.main_window.playlist_manager.list_saved_playlists()
        for p in playlists:
            display = p
            try:
                if isinstance(p, str) and p.lower().endswith('.json'):
                    display = p[:-5]
            except Exception:
                display = p

            item = QListWidgetItem(str(display))
            # Preserve the actual filename for loading.
            try:
                item.setData(Qt.UserRole, p)
            except Exception:
                pass
            self.playlists_list_widget.addItem(item)

    def refresh_episode_list(self):
        self.episode_list_widget.clear()
        current = self.main_window.playlist_manager.current_playlist
        for i, item in enumerate(current):
             if isinstance(item, dict):
                 name = os.path.basename(item.get('path', 'Unknown'))
                 if item.get('type') == 'bump':
                     name = "[BUMP] " + os.path.basename(item.get('audio', 'Audio'))
             else:
                 name = os.path.basename(item)
             
             prefix = "> " if i == self.main_window.playlist_manager.current_index else ""
             self.episode_list_widget.addItem(f"{prefix}{name}")

    def load_selected_playlist(self, item):
        filename = None
        try:
            filename = item.data(Qt.UserRole)
        except Exception:
            filename = None
        if not filename:
            filename = item.text()
            if isinstance(filename, str) and not filename.lower().endswith('.json'):
                filename = filename + '.json'
        self.main_window.load_playlist(os.path.join("playlists", filename))

    def load_and_play_playlist(self, item):
        filename = None
        try:
            filename = item.data(Qt.UserRole)
        except Exception:
            filename = None
        if not filename:
            filename = item.text()
            if isinstance(filename, str) and not filename.lower().endswith('.json'):
                filename = filename + '.json'
        self.main_window.load_playlist(os.path.join("playlists", filename), auto_play=True)

    def play_episode_from_list(self, item):
        idx = self.episode_list_widget.row(item)
        self.main_window.play_index(idx)


# --- Main Window ---

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sleepy Shows Player")

        # Startup sizing: percent of the *screen the window is on*.
        # Note: do not use cursor position (it can be on another monitor).
        self._startup_w_ratio = 0.65
        self._startup_h_ratio = 0.75
        self._startup_min_w = 360*2
        self._startup_min_h = 480*2

        # Apply once using primary screen as a safe default, then re-apply on the
        # actual screen after the window is created/shown.
        try:
            self._apply_startup_size_for_screen(QGuiApplication.primaryScreen(), center=True)
        except Exception:
            try:
                self.resize(1200, 800)
            except Exception:
                pass

        try:
            QTimer.singleShot(0, self._apply_startup_size_for_current_screen)
        except Exception:
            pass

    def _apply_startup_size_for_current_screen(self):
        try:
            screen = None
            try:
                # QWidget.screen() returns the screen the widget is on (Qt6).
                screen = self.screen()
            except Exception:
                screen = None
            if screen is None:
                try:
                    wh = self.windowHandle()
                    if wh is not None:
                        screen = wh.screen()
                except Exception:
                    screen = None
            if screen is None:
                try:
                    screen = QGuiApplication.primaryScreen()
                except Exception:
                    screen = None

            self._apply_startup_size_for_screen(screen, center=True)
        except Exception:
            pass

    def _apply_startup_size_for_screen(self, screen, center: bool = True):
        if screen is None:
            return

        geo = screen.availableGeometry()
        avail_w, avail_h = int(geo.width()), int(geo.height())
        if avail_w <= 0 or avail_h <= 0:
            return

        w = int(max(int(getattr(self, '_startup_min_w', 360)), avail_w * float(getattr(self, '_startup_w_ratio', 0.65))))
        h = int(max(int(getattr(self, '_startup_min_h', 480)), avail_h * float(getattr(self, '_startup_h_ratio', 0.75))))

        # Never spill off-screen.
        w = min(w, avail_w)
        h = min(h, avail_h)

        try:
            self._startup_available_size = (avail_w, avail_h)
            self._startup_requested_size = (int(w), int(h))
        except Exception:
            pass

        self.resize(w, h)

        if center:
            try:
                fr = self.frameGeometry()
                fr.moveCenter(geo.center())
                self.move(fr.topLeft())
            except Exception:
                pass
        
        # Data
        self.playlist_manager = PlaylistManager()

        # If the user already has legacy playlist names (e.g. koth.json),
        # make sure the canonical show-name playlists exist too.
        try:
            migrate_legacy_playlist_filenames()
        except Exception:
            pass

        # Tracks the current playlist file on disk (if loaded/saved).
        self.current_playlist_filename = None

        # If a show card is clicked before its auto-playlist exists, we'll
        # auto-config and then auto-load it after the worker finishes.
        self._pending_show_autoload = None

        # Local bumps scripts folder (like `playlists/`).
        self.bump_scripts_dir = get_local_bumps_scripts_dir()
        try:
            os.makedirs(self.bump_scripts_dir, exist_ok=True)
        except Exception:
            pass
        try:
            self.playlist_manager.bump_manager.load_bumps(self.bump_scripts_dir)
        except Exception:
            pass

        # Persisted user settings
        self._settings_path = _get_user_settings_path()
        self._settings = self._load_user_settings()
        self.startup_crickets_enabled = bool(self._settings.get('startup_crickets_enabled', True))
        self.normalize_audio_enabled = bool(self._settings.get('normalize_audio_enabled', False))
        self.bump_images_dir = self._settings.get('bump_images_dir', None)
        self.bump_audio_fx_dir = self._settings.get('bump_audio_fx_dir', None)
        self.bump_videos_dir = self._settings.get('bump_videos_dir', None)
        # Prefer the new key name, but keep backward compatibility.
        self._interstitials_dir = str(self._settings.get('interlude_folder', self._settings.get('interstitial_folder', '')) or '').strip()
        # One-time migration: if the legacy key exists, copy it to the new key.
        try:
            if (not str(self._settings.get('interlude_folder', '') or '').strip()) and str(self._settings.get('interstitial_folder', '') or '').strip():
                self._settings['interlude_folder'] = str(self._settings.get('interstitial_folder', '') or '').strip()
                self._save_user_settings()
        except Exception:
            pass

        # One-time migration: interludes are global-only; strip per-playlist folder keys.
        try:
            if not bool(self._settings.get('migrated_playlists_global_interludes_v1', False)):
                migrate_playlists_to_global_interludes()
                self._settings['migrated_playlists_global_interludes_v1'] = True
                # Save even if nothing changed so we don't keep scanning every launch.
                self._save_user_settings()
        except Exception:
            pass
        self.auto_config_volume_label = str(self._settings.get('auto_config_volume_label', 'T7') or 'T7').strip() or 'T7'

        # Playback topology
        # - portable: play local files from the external drive (auto-detected by volume label)
        # - web: play from a network filesystem root (SMB/UNC mounted as a local folder)
        configured_mode = str(self._settings.get('playback_mode', 'portable') or 'portable').strip().lower()
        if configured_mode not in {'portable', 'web'}:
            configured_mode = 'portable'

        # Startup behavior: prefer Portable mode when the configured external drive is present.
        # If it's not mounted, fall back to Web mode.
        try:
            has_portable_drive = _volume_label_is_mounted(self.auto_config_volume_label)
        except Exception:
            has_portable_drive = False
        self.playback_mode = 'portable' if has_portable_drive else 'web'

        # Startup fix: in Portable mode, proactively auto-config so playlists are
        # validated against the external drive and rebuilt if stale.
        try:
            if self.playback_mode == 'portable':
                QTimer.singleShot(0, self._try_auto_populate_library)
        except Exception:
            pass

        # Persist the effective mode so UI + next launch match reality.
        # (If the drive comes/goes, this will flip accordingly on next launch.)
        try:
            if self._settings.get('playback_mode') != self.playback_mode:
                self._settings['playback_mode'] = self.playback_mode
                self._save_user_settings()
        except Exception:
            pass

        # Web mode configuration (filesystem/mount based).
        # Optional network/mounted filesystem root for Web mode (SMB/UNC path or mounted folder).
        # If set, the app can resolve relative playlist paths into this root.
        self.web_files_root = str(self._settings.get('web_files_root', '') or '').strip()

        # Best-effort auto-defaults so switching to Web mode "just works" on a typical LAN.
        # Users can override in Settings.
        if self.playback_mode == 'web':
            self._ensure_web_defaults()
        # Web mode is filesystem-only; no remote playlist plumbing.

        try:
            self.playlist_manager.bump_manager.bump_images_dir = self.bump_images_dir
        except Exception:
            pass

        try:
            self.playlist_manager.bump_manager.bump_audio_fx_dir = self.bump_audio_fx_dir
        except Exception:
            pass

        # Bump video assets folder (TV Vibe/videos).
        # IMPORTANT: do not hardcode OS/user-specific mount paths here.
        # Derive from the detected Portable/Web roots so this works on Linux + Windows.
        try:
            vdir = str(getattr(self, 'bump_videos_dir', '') or '').strip()
        except Exception:
            vdir = ''

        # In Web mode, re-root an old absolute path under the configured Web Files Root.
        try:
            if vdir and self._is_web_mode() and str(getattr(self, 'web_files_root', '') or '').strip():
                vdir = self._path_to_web_files_path(str(vdir))
        except Exception:
            pass

        needs_autofix = False
        try:
            if not vdir:
                needs_autofix = True
            else:
                # Best-effort existence check; if a path from another OS is stored,
                # it will fail and we'll auto-detect a correct one.
                needs_autofix = (not os.path.isdir(str(vdir)))
        except Exception:
            needs_autofix = True

        if needs_autofix:
            detected = None
            try:
                if self._is_web_mode():
                    wfr = str(getattr(self, 'web_files_root', '') or '').strip()
                    if wfr:
                        detected = auto_detect_tv_vibe_videos_dir_web([wfr])
                        if not detected:
                            wd = self._web_data_root_for_files_root(wfr)
                            if wd:
                                candidate = os.path.join(str(wd), 'TV Vibe', 'videos')
                                if os.path.isdir(candidate):
                                    detected = candidate
                else:
                    detected = auto_detect_tv_vibe_videos_dir(volume_label=str(getattr(self, 'auto_config_volume_label', 'T7') or 'T7'))
            except Exception:
                detected = None

            if detected:
                vdir = str(detected)

        # Persist the value (if we have one) so bump scripts can resolve video=... consistently.
        try:
            self.bump_videos_dir = vdir
        except Exception:
            self.bump_videos_dir = vdir
        try:
            if vdir:
                self._settings['bump_videos_dir'] = vdir
                self._save_user_settings()
        except Exception:
            pass

        try:
            self.playlist_manager.bump_manager.bump_videos_dir = str(self.bump_videos_dir or '').strip() or None
        except Exception:
            pass

        # Startup: probe exact durations for bump videos in the background.
        try:
            QTimer.singleShot(0, self._schedule_probe_bump_video_durations)
        except Exception:
            pass

        # Interstitials folder (commercials). Persisted independently of playlists.
        self._interstitial_watcher = QFileSystemWatcher(self)
        try:
            self._interstitial_watcher.directoryChanged.connect(self._on_interstitials_dir_changed)
        except Exception:
            pass
        try:
            inter_dir = str(getattr(self, '_interstitials_dir', '') or '').strip()
        except Exception:
            inter_dir = ''

        # In Web mode, re-root a stored absolute path under the configured Web Files Root.
        try:
            if inter_dir and self._is_web_mode() and str(getattr(self, 'web_files_root', '') or '').strip():
                inter_dir = self._path_to_web_files_path(str(inter_dir))
        except Exception:
            pass

        inter_needs_autofix = False
        try:
            inter_needs_autofix = (not inter_dir) or (not os.path.isdir(str(inter_dir)))
        except Exception:
            inter_needs_autofix = True

        if inter_needs_autofix:
            # Absolute-path fallback (matches the canonical layout users often quote).
            try:
                abs_candidate = os.path.join(os.sep, 'Sleepy Shows Data', 'TV Vibe', 'interludes')
                if os.path.isdir(abs_candidate):
                    inter_dir = abs_candidate
                    inter_needs_autofix = False
            except Exception:
                pass

            detected_inter = None
            try:
                if self._is_web_mode():
                    wfr = str(getattr(self, 'web_files_root', '') or '').strip()
                    if wfr:
                        detected_inter = auto_detect_tv_vibe_interstitials_dir_web([wfr])
                else:
                    detected_inter = auto_detect_tv_vibe_interstitials_dir(volume_label=str(getattr(self, 'auto_config_volume_label', 'T7') or 'T7'))
            except Exception:
                detected_inter = None
            if detected_inter:
                inter_dir = str(detected_inter)

        if inter_dir:
            try:
                # Persist here so the setting stays correct across launches/OSes.
                self._set_interstitials_folder(inter_dir, persist=True)
            except Exception:
                try:
                    self._set_interstitials_folder(inter_dir, persist=False)
                except Exception:
                    pass

        # Startup ambient audio
        self._startup_ambient_playing = False
        self._startup_ambient_path = get_asset_path("crickets.mp3")

        # Global bumps toggle (controlled from Welcome)
        self.bumps_enabled = False

        # Pending bump used for interstitial-before-bump preroll.
        self._pending_bump_item = None
        
        # Timers
        self.sleep_timer_default_minutes = 180
        # Single source of truth for timer duration
        self.current_sleep_minutes = self.sleep_timer_default_minutes
        # Manual flag to ensure UI sync reliably (do not rely on QTimer.isActive())
        self.sleep_timer_active = False

        # Sleep timer countdown is paused unless a show is actively playing.
        self.sleep_remaining_ms = 0
        self._sleep_last_tick = None
        self.sleep_countdown_timer = QTimer(self)
        self.sleep_countdown_timer.setInterval(1000)
        self.sleep_countdown_timer.timeout.connect(self._on_sleep_countdown_tick)
        
        # Mouse Hover Timer
        self.hover_timer = QTimer(self)
        self.hover_timer.setInterval(2500) # 2.5s hide
        self.hover_timer.setSingleShot(True)
        self.hover_timer.timeout.connect(self.hide_controls)
        self.setMouseTracking(True) # Track mouse without buttons

        # Fullscreen reliability: poll cursor movement so controls can appear even when
        # MPV/Qt doesn't deliver mouse move events (common with native windows on Windows).
        self._fs_cursor_poll_timer = QTimer(self)
        self._fs_cursor_poll_timer.setInterval(100)
        self._fs_cursor_poll_timer.timeout.connect(self._poll_fullscreen_cursor)
        self._fs_last_cursor_pos = None

        # Spacebar play/pause (works in fullscreen too).
        self._space_shortcut = QShortcut(QKeySequence('Space'), self)
        self._space_shortcut.setContext(Qt.ApplicationShortcut)
        self._space_shortcut.activated.connect(self._on_spacebar)

        # Fullscreen toggle uses a Windows native event filter (see _WinFullscreenKeyFilter).
        # Keep the debounce field used by toggle_fullscreen.
        self._last_fullscreen_toggle_mono = 0.0

        self.bump_timer = QTimer(self)
        self.bump_timer.setSingleShot(True)
        self.bump_timer.timeout.connect(self.advance_bump_card)
        self.current_bump_script = None
        self.current_card_index = 0

        # When bumps are enabled, forward navigation detours through a bump first.
        # We store the intended next index here while the bump plays.
        self._pending_next_index = None
        self._pending_next_record_history = True
        
        self.is_seeking = False
        self.total_duration = 0
        self._last_time_pos = None
        self._play_start_monotonic = None
        self._played_since_start = False
        self._advancing_from_eof = False
        self._skip_penalty_applied_for_start = None

        # Fullscreen transition/state helpers (Windows reliability).
        self._pre_fullscreen_geometry = None
        self._pre_fullscreen_was_maximized = False
        self._fullscreen_transitioning = False
        self._fullscreen_cursor_hidden = False
        self.was_maximized = False # Track window state for fullscreen toggle
        self.last_activity_time = time.time()

        # Playback diagnostics: JSONL event log (helps debug overnight stops).
        self._playback_log_path = os.path.join(_get_user_config_dir(), 'playback_events.jsonl')
        self._last_stop_reason = None
        self._last_stop_reason_at = None

        # Resume/recovery: persist the generated episode queue + position locally so we
        # can recover from transient media loss (e.g., USB disconnect) and resume across restarts.
        self._resume_state_path = _get_resume_state_path()
        self._resume_state_save_interval_s = 10.0
        self._resume_state_last_save_mono = 0.0
        self._resume_last_payload = None

        self._resume_recover_timer = QTimer(self)
        self._resume_recover_timer.setInterval(2000)
        self._resume_recover_timer.timeout.connect(self._attempt_missing_media_recovery)
        self._resume_recover_target = None
        self._resume_recover_started_mono = None
        self._resume_recover_attempts = 0

        # Load any prior resume state now.
        # We intentionally do NOT prompt at startup; instead, we auto-resume
        # only when the user starts the same show/playlist again.
        self._resume_loaded_state = self._load_resume_state()

        # Auto-resume is armed when a playlist is loaded, and only triggers on the
        # first manual/auto play for that playlist (and only for the default start index).
        self._pending_auto_resume_state = None
        self._pending_auto_resume_playlist = None

        # Missing-media resiliency: mpv can hang on a gray frame during transient
        # disconnects without emitting a useful error. Track playback progress and
        # enter recovery if playback stalls and the file disappears.
        self._time_pos_last_update_mono = 0.0
        self._time_pos_last_progress_mono = 0.0
        self._time_pos_last_value = None
        self._missing_media_waiting_for_target = None
        self._missing_media_stall_timeout_s = 2.5

        # Best-effort cross-platform sleep/idle inhibitor while actively playing.
        self._keep_awake = KeepAwakeInhibitor()
        self._keep_awake_active = False
        self._keep_awake_timer = QTimer(self)
        self._keep_awake_timer.setInterval(60_000)
        self._keep_awake_timer.timeout.connect(self._sync_keep_awake)
        self._keep_awake_timer.start()

        # Playback watchdog: some MPV setups do not reliably deliver end-file events.
        # This ensures we still auto-advance when a file reaches EOF.
        self.playback_watchdog = QTimer(self)
        self.playback_watchdog.setInterval(500)
        self.playback_watchdog.timeout.connect(self._check_playback_end)
        self.playback_watchdog.start()

        # No startup resume prompt (see note above).
        
        # Failsafe timer for fullscreen
        self.failsafe_timer = QTimer(self)
        self.failsafe_timer.setInterval(2000) # Check every 2s
        self.failsafe_timer.timeout.connect(self.check_fullscreen_inactivity)

        # Create Player Backend (Hidden parent until attached)
        # We need a container for MPV
        self.video_container = QWidget() 
        self.video_container.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.video_container.setAttribute(Qt.WA_NativeWindow, True)
        
        # Layout for video container to ensure player resizes
        container_layout = QVBoxLayout(self.video_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        
        self.player = MpvPlayer(self.video_container)
        container_layout.addWidget(self.player)

        # Audio-only MPV instance used for bump sound effects.
        self.fx_player = MpvAudioPlayer()
        self._bump_fx_stop_timer = QTimer(self)
        self._bump_fx_stop_timer.setSingleShot(True)
        self._bump_fx_stop_timer.timeout.connect(self._stop_bump_fx)
        self._bump_fx_active = False
        self._bump_fx_policy = None  # None | 'duration' | 'card' | 'ms'
        self._bump_fx_interrupt_prev_mute = None
        self._bump_fx_prev_volume = None

        # Bump music cut: when True, bump music is muted for the remainder of the bump.

        # Outro audio exclusivity: when True, block other bump FX so outro audio is the only sound.
        self._bump_outro_audio_exclusive = False


        # Cached list of available outro sounds (paths). Filled lazily.
        self._outro_sounds_cache = None
        self._outro_sound_queue = []  # list[int] indices into _outro_sounds_cache
        self._recent_outro_sound_basenames = []  # list[str]
        self._outro_recent_n = 8

        # Prefetch/staging for bumps (true double-buffer).
        #
        # Active buffer:
        # - Used by the currently playing bump for the entire bump duration.
        # - Cleared when bump playback stops.
        #
        # Next buffer:
        # - Populated in the background while the current bump plays.
        # - Swapped into the active buffer when the next bump begins.
        self._active_bump_index = None
        self._bump_prefetch_images = {}  # active: {original_path: QImage}
        self._bump_staged_audio_map = {}  # active: {original_path: staged_path}
        self._bump_prefetch_files = set()  # active: set[str] staged paths we created

        self._next_prefetched_for_bump_index = None
        self._next_bump_prefetch_images = {}  # next: {original_path: QImage}
        self._next_bump_staged_audio_map = {}  # next: {original_path: staged_path}
        self._next_bump_prefetch_files = set()  # next: set[str] staged paths we created

        # Optional outro audio (<outro ... audio>): pick a random sound from this folder.
        self._outro_sounds_dir = os.path.join('/media', 'tyler', 'T7', 'Sleepy Shows Data', 'TV Vibe', 'outro sounds')
        try:
            if self._is_web_mode():
                wd = self._web_data_root_for_files_root(str(getattr(self, 'web_files_root', '') or '').strip())
                if wd:
                    self._outro_sounds_dir = os.path.join(wd, 'TV Vibe', 'outro sounds')
        except Exception:
            pass

        # Apply audio normalization as early as possible.
        try:
            self.player.set_audio_normalization(self.normalize_audio_enabled)
        except Exception:
            pass

        # Bump script font: load bundled Helvetica Neue Condensed Black.
        # (Use a runtime-loaded TTF so packaged builds behave consistently.)
        self._bump_font_family = None
        try:
            font_path = get_asset_path("HelveticaNeue-CondensedBlack.ttf")
        except Exception:
            font_path = None
        try:
            if font_path and os.path.isfile(str(font_path)):
                font_id = QFontDatabase.addApplicationFont(str(font_path))
                if int(font_id) != -1:
                    fams = QFontDatabase.applicationFontFamilies(int(font_id))
                    if fams:
                        self._bump_font_family = str(fams[0])
        except Exception:
            self._bump_font_family = None
        if not self._bump_font_family:
            # Best-effort fallback: if the OS already has it installed.
            self._bump_font_family = "Helvetica Neue Condensed Black"

        try:
            self._bump_font_px = int(self._settings.get('bump_font_px', 28) or 28)
        except Exception:
            self._bump_font_px = 28
        if int(self._bump_font_px) <= 0:
            self._bump_font_px = 28
        
        # Overlay for Episode Title
        self.overlay_label = QLabel(self.video_container)
        self.overlay_label.setAlignment(Qt.AlignCenter)
        self.overlay_label.setStyleSheet("background-color: rgba(0, 0, 0, 150); color: white; padding: 10px; font-size: 18px; font-weight: bold;")
        self.overlay_label.setVisible(False)
        self.overlay_label.setAttribute(Qt.WA_TransparentForMouseEvents) # Let clicks pass through

        # Overlay for inclusive bump-video outros (drawn over the playing video).
        self.bump_video_overlay_label = QLabel(self.video_container)
        self.bump_video_overlay_label.setAlignment(Qt.AlignCenter)
        self.bump_video_overlay_label.setWordWrap(True)
        try:
            self.bump_video_overlay_label.setFont(QFont(str(self._bump_font_family), int(self._bump_font_px)))
        except Exception:
            self.bump_video_overlay_label.setFont(QFont("Arial", 28, QFont.Bold))
        self.bump_video_overlay_label.setStyleSheet(
            "background-color: rgba(12, 12, 12, 190); color: white; padding: 20px;"
        )
        self.bump_video_overlay_label.setVisible(False)
        self.bump_video_overlay_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._bump_video_overlay_timer = QTimer(self)
        self._bump_video_overlay_timer.setSingleShot(True)
        self._bump_video_overlay_timer.timeout.connect(self._hide_bump_video_overlay)

        # Windowed-mode rule: show briefly at episode start, then hide.
        self._episode_overlay_hide_timer = QTimer(self)
        self._episode_overlay_hide_timer.setSingleShot(True)
        self._episode_overlay_hide_timer.timeout.connect(self._on_episode_overlay_hide_timeout)
        
        # We need to manually position this because it's an overlay
        self.video_container.installEventFilter(self)

        # Also install an application-wide event filter so fullscreen controls can respond
        # to key/mouse events even when focus is not on the video container.
        try:
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)

                # Windows: native hook so F fullscreen works even with mpv native focus.
                try:
                    if sys.platform.startswith('win'):
                        self._win_fullscreen_key_filter = _WinFullscreenKeyFilter(self)
                        app.installNativeEventFilter(self._win_fullscreen_key_filter)
                except Exception:
                    pass
        except Exception:
            pass

        # mpv callbacks can occur off the GUI thread; always queue into Qt's main loop.
        self.player.positionChanged.connect(self.update_seeker, Qt.QueuedConnection)
        self.player.durationChanged.connect(self.update_duration, Qt.QueuedConnection)
        self.player.playbackFinished.connect(self.on_playback_finished, Qt.QueuedConnection)
        try:
            self.player.endFileReason.connect(self.on_mpv_end_file_reason, Qt.QueuedConnection)
        except Exception:
            pass
        self.player.errorOccurred.connect(self.on_player_error, Qt.QueuedConnection)
        self.player.playbackPaused.connect(self.on_player_paused, Qt.QueuedConnection)
        self.player.mouseMoved.connect(self.on_mouse_move, Qt.QueuedConnection)
        # Handle fullscreen requests from MPV
        self.player.fullscreenRequested.connect(self.toggle_fullscreen, Qt.QueuedConnection)
        self.player.escapePressed.connect(self.on_escape_pressed, Qt.QueuedConnection)

        # If MPV failed to initialize, its error can occur before signal wiring and be invisible.
        # Surface it once here so playback failures aren't "silent".
        try:
            if getattr(self.player, 'mpv', None) is None:
                init_err = getattr(self.player, '_init_error', None)
                if init_err:
                    QTimer.singleShot(0, lambda: self.on_player_error(str(init_err)))
        except Exception:
            pass

        try:
            if getattr(self.fx_player, 'mpv', None) is None:
                print("DEBUG: fx_player MPV failed to init")
        except Exception:
            pass
        
        # Create Bump View
        self.bump_widget = QWidget()
        self.bump_widget.setStyleSheet("background-color: #0c0c0c;")
        bump_layout = QVBoxLayout(self.bump_widget)
        self._bump_layout = bump_layout
        self._bump_safe_vpad_ratio = 0.15
        self.lbl_bump_text = QLabel("")
        self.lbl_bump_text.setAlignment(Qt.AlignCenter)
        self.lbl_bump_text.setWordWrap(True)
        try:
            self.lbl_bump_text.setFont(QFont(str(self._bump_font_family), int(self._bump_font_px)))
        except Exception:
            self.lbl_bump_text.setFont(QFont("Arial", 28, QFont.Bold))
        self.lbl_bump_text.setStyleSheet("color: white;")

        self.lbl_bump_text_top = QLabel("")
        self.lbl_bump_text_top.setAlignment(Qt.AlignCenter)
        self.lbl_bump_text_top.setWordWrap(True)
        try:
            self.lbl_bump_text_top.setFont(QFont(str(self._bump_font_family), int(self._bump_font_px)))
        except Exception:
            self.lbl_bump_text_top.setFont(QFont("Arial", 28, QFont.Bold))
        self.lbl_bump_text_top.setStyleSheet("color: white;")

        self.bump_image_view = BumpImageView(self.bump_widget)
        self.bump_image_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.lbl_bump_text_bottom = QLabel("")
        self.lbl_bump_text_bottom.setAlignment(Qt.AlignCenter)
        self.lbl_bump_text_bottom.setWordWrap(True)
        try:
            self.lbl_bump_text_bottom.setFont(QFont(str(self._bump_font_family), int(self._bump_font_px)))
        except Exception:
            self.lbl_bump_text_bottom.setFont(QFont("Arial", 28, QFont.Bold))
        self.lbl_bump_text_bottom.setStyleSheet("color: white;")

        bump_layout.addWidget(self.lbl_bump_text)
        bump_layout.addWidget(self.lbl_bump_text_top)
        bump_layout.addWidget(self.bump_image_view, 1)
        bump_layout.addWidget(self.lbl_bump_text_bottom)

        # Start with text-only mode visible.
        self.lbl_bump_text_top.hide()
        self.bump_image_view.hide()
        self.lbl_bump_text_bottom.hide()

        # Keep bump text out of the extreme top/bottom of the screen.
        self.bump_widget.installEventFilter(self)
        
        # --- UI Setup ---
        self.setup_ui()
        self.setStyleSheet(DARK_THEME)
        
        # Install event filter to track mouse move across application
        self.installEventFilter(self)

        # Best-effort: auto-populate library from an external drive (e.g. "T7").
        # If nothing is found, the user can still add a folder manually.
        QTimer.singleShot(0, self._try_auto_populate_library)

    def _log_event(self, event: str, **fields):
        try:
            os.makedirs(os.path.dirname(self._playback_log_path), exist_ok=True)
        except Exception:
            pass

        try:
            # Soft rotation at ~1MB to keep the file bounded.
            if os.path.exists(self._playback_log_path) and os.path.getsize(self._playback_log_path) > 1_000_000:
                try:
                    backup = self._playback_log_path + '.1'
                    if os.path.exists(backup):
                        os.remove(backup)
                    os.replace(self._playback_log_path, backup)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            payload = {
                'ts': datetime.datetime.now().isoformat(timespec='seconds'),
                'event': str(event or ''),
            }
            for k, v in (fields or {}).items():
                try:
                    payload[str(k)] = v
                except Exception:
                    continue
            with open(self._playback_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            return

    def _set_stop_reason(self, reason: str, **fields):
        try:
            self._last_stop_reason = str(reason or '').strip() or None
            self._last_stop_reason_at = time.time()
        except Exception:
            pass
        self._log_event('stop_reason', reason=self._last_stop_reason, **(fields or {}))

    def _is_actively_playing(self) -> bool:
        try:
            if not hasattr(self, 'player') or not self.player or not getattr(self.player, 'mpv', None):
                return False
            mpv = self.player.mpv
            if bool(getattr(mpv, 'core_idle', True)):
                return False
            if bool(getattr(mpv, 'pause', False)):
                return False
            return True
        except Exception:
            return False

    def _sync_keep_awake(self):
        should = self._is_actively_playing()

        if should and not self._keep_awake_active:
            st = self._keep_awake.enable(reason='SleepyShows playback')
            self._keep_awake_active = bool(st.enabled)
            self._log_event('keep_awake', action='enable', enabled=bool(st.enabled), backend=st.backend, detail=st.detail)
        elif (not should) and self._keep_awake_active:
            st = self._keep_awake.disable()
            self._keep_awake_active = False
            self._log_event('keep_awake', action='disable', enabled=bool(st.enabled), backend=st.backend, detail=st.detail)

    def _start_startup_ambient(self):
        # Play an ambient track on launch (Welcome screen). It is cut off as soon as
        # the user starts playing a show.
        try:
            if not bool(getattr(self, 'startup_crickets_enabled', True)):
                return
            if self._startup_ambient_playing:
                return
            if not self._startup_ambient_path or not os.path.exists(self._startup_ambient_path):
                return

            # Only start this when the app is idle on launch (no playlist playback).
            try:
                if self.playlist_manager.current_playlist and self.playlist_manager.current_index >= 0:
                    return
            except Exception:
                pass

            self.player.play(self._startup_ambient_path)
            self._startup_ambient_playing = True
        except Exception:
            return

    def _load_user_settings(self):
        try:
            if self._settings_path and os.path.exists(self._settings_path):
                with open(self._settings_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def _save_user_settings(self):
        try:
            if not self._settings_path:
                return
            tmp_path = self._settings_path + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self._settings, f, indent=2)
            os.replace(tmp_path, self._settings_path)
        except Exception:
            return

    def _load_resume_state(self) -> dict:
        try:
            path = str(getattr(self, '_resume_state_path', '') or '').strip()
        except Exception:
            path = ''
        if not path:
            return {}
        try:
            if not os.path.exists(path):
                return {}
        except Exception:
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_resume_state(self, payload: dict) -> bool:
        try:
            path = str(getattr(self, '_resume_state_path', '') or '').strip()
        except Exception:
            path = ''
        if not path:
            return False

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass

        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, path)
            return True
        except Exception:
            return False

    def _capture_resume_state(self) -> dict:
        pm = getattr(self, 'playlist_manager', None)
        playlist_items = []
        queue_keys = []
        current_episode_key = None
        current_episode_path = None

        try:
            if pm is not None:
                # Persist a bump-free representation of the working playlist.
                for it in list(getattr(pm, 'current_playlist', []) or []):
                    if isinstance(it, dict):
                        t = str(it.get('type', 'video') or 'video')
                        if t == 'bump':
                            continue
                        p = str(it.get('path', '') or '').strip()
                        if not p:
                            continue
                        playlist_items.append({'type': t, 'path': p})
                    elif isinstance(it, str):
                        p = str(it).strip()
                        if p:
                            playlist_items.append({'type': 'video', 'path': p})
        except Exception:
            playlist_items = []

        try:
            if pm is not None:
                queue_keys = pm.export_episode_queue_keys()
        except Exception:
            queue_keys = []

        try:
            idx = int(getattr(pm, 'current_index', -1) or -1) if pm is not None else -1
        except Exception:
            idx = -1
        if idx >= 0 and pm is not None:
            try:
                item = pm.current_playlist[idx]
            except Exception:
                item = None
            try:
                if item is not None and pm.is_episode_item(item):
                    p = str(pm._episode_path_for_index(idx) or '').strip()
                    current_episode_path = p or None
                    k = pm._norm_path_key(p)
                    current_episode_key = str(k) if k else None
            except Exception:
                pass

        # Best-effort time position (prefer signal-fed cache).
        pos_s = None
        try:
            pos_s = self._last_time_pos
        except Exception:
            pos_s = None
        if pos_s is None:
            try:
                if hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                    pos_s = getattr(self.player.mpv, 'time_pos', None)
            except Exception:
                pos_s = None

        dur_s = None
        try:
            dur_s = self.total_duration
        except Exception:
            dur_s = None
        if not dur_s:
            try:
                if hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                    dur_s = getattr(self.player.mpv, 'duration', None)
            except Exception:
                dur_s = None

        try:
            playlist_filename = getattr(self, 'current_playlist_filename', None)
        except Exception:
            playlist_filename = None

        try:
            target = str(getattr(self, '_last_play_target', '') or '').strip() or None
        except Exception:
            target = None
        try:
            source_path = str(getattr(self, '_last_play_source_path', '') or '').strip() or None
        except Exception:
            source_path = None

        try:
            active = bool(self._is_actively_playing())
        except Exception:
            active = False

        payload = {
            'version': 1,
            'saved_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'active_playback': bool(active),
            'playback_mode': str(getattr(self, 'playback_mode', 'portable') or 'portable'),
            'shuffle_mode': str(getattr(pm, 'shuffle_mode', 'off') or 'off') if pm is not None else 'off',
            'playlist_filename': playlist_filename,
            'playlist_items': playlist_items,
            'current_index': int(idx),
            'current_episode_key': current_episode_key,
            'current_episode_path': current_episode_path,
            'queue_episode_keys': list(queue_keys or []),
            'time_pos_s': float(pos_s) if pos_s is not None else None,
            'duration_s': float(dur_s) if dur_s is not None else None,
            'last_play_target': target,
            'last_play_source_path': source_path,
            'last_stop_reason': getattr(self, '_last_stop_reason', None),
        }
        return payload

    def _persist_resume_state(self, *, force: bool = False, reason: str = '') -> None:
        try:
            now = float(time.monotonic())
        except Exception:
            now = 0.0

        try:
            last = float(getattr(self, '_resume_state_last_save_mono', 0.0) or 0.0)
        except Exception:
            last = 0.0

        if (not force) and last and now and (now - last) < float(getattr(self, '_resume_state_save_interval_s', 10.0) or 10.0):
            return

        try:
            payload = self._capture_resume_state()
        except Exception:
            return

        # Skip pointless writes when nothing is loaded.
        try:
            has_playlist = bool((payload or {}).get('playlist_items'))
            has_target = bool((payload or {}).get('last_play_target'))
            if not has_playlist and not has_target:
                return
        except Exception:
            pass

        try:
            if reason:
                payload['save_reason'] = str(reason)
        except Exception:
            pass

        ok = False
        try:
            ok = bool(self._write_resume_state(payload))
        except Exception:
            ok = False
        if ok:
            try:
                self._resume_state_last_save_mono = float(now)
            except Exception:
                pass
            try:
                self._resume_last_payload = payload
            except Exception:
                pass

    def _maybe_offer_resume_from_disk(self):
        # Only offer if we're idle (not already playing something).
        try:
            if self._is_actively_playing():
                return
        except Exception:
            pass

        st = {}
        try:
            st = dict(getattr(self, '_resume_loaded_state', {}) or {})
        except Exception:
            st = {}
        if not st:
            return

        try:
            # Require at least a target or a playlist to restore.
            if not st.get('last_play_target') and not st.get('playlist_items') and not st.get('playlist_filename'):
                return
        except Exception:
            return

        # If the target exists, we can resume immediately. If it doesn't, user can still
        # accept and we'll wait/retry (useful for USB reconnect).
        target = str(st.get('last_play_target') or '').strip()
        pos_s = st.get('time_pos_s', None)
        try:
            pos_s = float(pos_s) if pos_s is not None else None
        except Exception:
            pos_s = None

        msg = "Resume last playback?"
        detail = ""
        try:
            if target:
                detail = f"\n\nFile: {target}"
        except Exception:
            detail = ""
        try:
            if pos_s is not None and pos_s > 0:
                m, s = divmod(int(pos_s), 60)
                h, m = divmod(m, 60)
                ts = f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
                detail = detail + f"\nResume at: {ts}"
        except Exception:
            pass

        try:
            resp = QMessageBox.question(self, 'Resume Playback', msg + detail, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        except Exception:
            return

        if resp != QMessageBox.Yes:
            # User said no; delete the resume file so it doesn't keep prompting.
            try:
                rp = str(getattr(self, '_resume_state_path', '') or '').strip()
                if rp and os.path.exists(rp):
                    os.remove(rp)
            except Exception:
                pass
            try:
                self._log_event('resume_discarded')
            except Exception:
                pass
            return

        try:
            self._log_event('resume_accepted')
        except Exception:
            pass
        try:
            self._apply_resume_state(st)
        except Exception:
            return

    def _apply_resume_state(self, st: dict):
        pm = getattr(self, 'playlist_manager', None)
        if pm is None:
            return

        # 1) Restore playlist contents (prefer loading from file).
        filename = str(st.get('playlist_filename') or '').strip()
        if filename and os.path.exists(filename):
            try:
                self.load_playlist(filename, auto_play=False)
            except Exception:
                pass
        else:
            items = list(st.get('playlist_items') or [])
            if items:
                try:
                    pm.current_playlist = items
                except Exception:
                    pm.current_playlist = []
                try:
                    pm.reset_playback_state()
                except Exception:
                    pass
                try:
                    mode = str(st.get('shuffle_mode') or getattr(pm, 'shuffle_mode', 'off') or 'off')
                    self.set_shuffle_mode(mode, update_ui=True)
                except Exception:
                    pass

        # 2) Restore queue (best-effort).
        try:
            pm.restore_episode_queue_from_keys(list(st.get('queue_episode_keys') or []))
        except Exception:
            pass

        # 3) Pick the episode index to resume.
        idx = -1
        try:
            k = str(st.get('current_episode_key') or '').strip()
            if k:
                idx = pm.index_for_episode_key(k)
        except Exception:
            idx = -1
        if idx < 0:
            try:
                idx = int(st.get('current_index', -1) or -1)
            except Exception:
                idx = -1

        if idx < 0:
            return

        # Resume playback without bump gating.
        try:
            self.play_index(int(idx), record_history=False, bypass_bump_gate=True, suppress_ui=False)
        except Exception:
            return

        # Seek after playback has had a moment to load.
        try:
            pos_s = st.get('time_pos_s', None)
            pos_s = float(pos_s) if pos_s is not None else None
        except Exception:
            pos_s = None
        if pos_s is not None and pos_s > 0:
            seek_to = max(0.0, float(pos_s) - 3.0)
            try:
                QTimer.singleShot(350, lambda: self.player.seek(seek_to))
            except Exception:
                try:
                    self.player.seek(seek_to)
                except Exception:
                    pass

    def _norm_match_path(self, p: str) -> str:
        try:
            s = str(p or '').strip()
        except Exception:
            s = ''
        if not s:
            return ''
        try:
            s = os.path.expanduser(s)
        except Exception:
            pass
        try:
            s = os.path.abspath(s)
        except Exception:
            pass
        try:
            s = os.path.realpath(s)
        except Exception:
            pass
        try:
            if sys.platform.startswith('win'):
                s = os.path.normcase(s)
        except Exception:
            pass
        return s

    def _arm_auto_resume_for_playlist(self, playlist_source: str) -> None:
        # Clear any prior pending resume.
        try:
            self._pending_auto_resume_state = None
            self._pending_auto_resume_playlist = None
        except Exception:
            pass

        try:
            st = self._load_resume_state()
        except Exception:
            st = {}
        if not isinstance(st, dict) or not st:
            return

        try:
            st_pl = self._norm_match_path(str(st.get('playlist_filename') or ''))
            cur_pl = self._norm_match_path(str(playlist_source or ''))
        except Exception:
            st_pl = ''
            cur_pl = ''
        if not st_pl or not cur_pl or st_pl != cur_pl:
            return

        # Require at least an episode identifier or a target.
        try:
            if not st.get('current_episode_key') and not st.get('last_play_target'):
                return
        except Exception:
            return

        try:
            self._pending_auto_resume_state = st
            self._pending_auto_resume_playlist = cur_pl
        except Exception:
            pass

    def _predict_default_start_index(self) -> int:
        pm = getattr(self, 'playlist_manager', None)
        if pm is None:
            return 0
        try:
            if str(getattr(pm, 'shuffle_mode', 'off') or 'off') == 'off':
                return 0
        except Exception:
            return 0
        try:
            q = list(getattr(pm, 'play_queue', []) or [])
            if q:
                return int(q[0])
        except Exception:
            pass
        return 0

    def _maybe_auto_resume_on_first_play(self, requested_index: int) -> bool:
        st = getattr(self, '_pending_auto_resume_state', None)
        if not isinstance(st, dict) or not st:
            return False

        pm = getattr(self, 'playlist_manager', None)
        try:
            # Only trigger before anything has started for this playlist.
            if pm is not None and int(getattr(pm, 'current_index', -1) or -1) != -1:
                self._pending_auto_resume_state = None
                self._pending_auto_resume_playlist = None
                return False
        except Exception:
            pass

        try:
            default_idx = int(self._predict_default_start_index())
        except Exception:
            default_idx = 0

        # If the user intentionally clicked some other episode, don't override.
        if int(requested_index) != int(default_idx):
            try:
                self._pending_auto_resume_state = None
                self._pending_auto_resume_playlist = None
            except Exception:
                pass
            return False

        # Apply and clear.
        try:
            self._pending_auto_resume_state = None
            self._pending_auto_resume_playlist = None
        except Exception:
            pass

        try:
            self._log_event('resume_auto', playlist=str(st.get('playlist_filename') or ''))
        except Exception:
            pass

        try:
            self._apply_resume_state(st)
            return True
        except Exception:
            return False

    def _maybe_auto_resume_for_target(self, target_path: str) -> bool:
        try:
            st = self._load_resume_state()
        except Exception:
            st = {}
        if not isinstance(st, dict) or not st:
            return False

        try:
            want = self._norm_match_path(str(target_path or ''))
            got = self._norm_match_path(str(st.get('last_play_source_path') or st.get('last_play_target') or ''))
        except Exception:
            want = ''
            got = ''
        if not want or not got or want != got:
            return False

        try:
            self._log_event('resume_auto_single', target=str(target_path or ''))
        except Exception:
            pass

        try:
            self._apply_resume_state(st)
            return True
        except Exception:
            return False

    def _maybe_start_missing_media_recovery(self, *, reason: str):
        # Only start recovery if the current target path is missing.
        try:
            target = str(getattr(self, '_last_play_target', '') or '').strip()
        except Exception:
            target = ''
        if not target:
            return

        missing = False
        try:
            missing = (not os.path.exists(target))
        except Exception:
            missing = False
        if not missing:
            return

        try:
            self._log_event('media_missing', reason=str(reason or ''), target=str(target))
        except Exception:
            pass

        # Persist state immediately so we resume from as close as possible.
        try:
            self._persist_resume_state(force=True, reason=f'media_missing:{reason}')
        except Exception:
            pass

        # Immediately stop mpv to avoid a permanent gray screen/hang while the
        # drive is unplugged, and show a brief on-screen status.
        try:
            self._enter_missing_media_wait_state(target, reason=str(reason or ''))
        except Exception:
            pass

        # Start/restart the recovery loop.
        try:
            self._resume_recover_target = target
            self._resume_recover_started_mono = float(time.monotonic())
            self._resume_recover_attempts = 0
            if not self._resume_recover_timer.isActive():
                self._resume_recover_timer.start()
        except Exception:
            pass

    def _enter_missing_media_wait_state(self, target: str, reason: str = '') -> None:
        try:
            t = str(target or '').strip()
        except Exception:
            t = ''
        if not t:
            return

        try:
            if getattr(self, '_missing_media_waiting_for_target', None) == t:
                return
        except Exception:
            pass

        try:
            self._missing_media_waiting_for_target = t
        except Exception:
            pass

        # Stop any bump state so we don't get stuck behind a bump gate.
        try:
            self.stop_bump_playback()
        except Exception:
            pass

        try:
            if hasattr(self, 'player') and self.player:
                self.player.stop()
        except Exception:
            pass

        # mpv OSD is the most reliable overlay over the native video surface.
        try:
            msg = "Media disconnected — waiting for reconnect"
            if reason:
                msg = msg + f" ({reason})"
            self._show_mpv_osd_text(msg, duration_ms=2500)
        except Exception:
            pass

    def _attempt_missing_media_recovery(self):
        # Stop after a while to avoid infinite polling.
        try:
            started = float(getattr(self, '_resume_recover_started_mono', 0.0) or 0.0)
            if started and (float(time.monotonic()) - started) > 600.0:
                self._resume_recover_timer.stop()
                return
        except Exception:
            pass

        try:
            target = str(getattr(self, '_resume_recover_target', '') or '').strip()
        except Exception:
            target = ''
        if not target:
            try:
                self._resume_recover_timer.stop()
            except Exception:
                pass
            return

        try:
            self._resume_recover_attempts = int(getattr(self, '_resume_recover_attempts', 0) or 0) + 1
        except Exception:
            pass

        try:
            if not os.path.exists(target):
                return
        except Exception:
            return

        # Target is back. Try to resume playback.
        try:
            self._log_event('media_reappeared', target=str(target), attempts=int(getattr(self, '_resume_recover_attempts', 0) or 0))
        except Exception:
            pass

        try:
            self._resume_recover_timer.stop()
        except Exception:
            pass

        try:
            self._missing_media_waiting_for_target = None
        except Exception:
            pass

        try:
            self._show_mpv_osd_text("Media reconnected — resuming", duration_ms=2000)
        except Exception:
            pass

        st = None
        try:
            st = getattr(self, '_resume_last_payload', None)
        except Exception:
            st = None
        if not isinstance(st, dict):
            try:
                st = self._load_resume_state()
            except Exception:
                st = None
        if not isinstance(st, dict):
            st = {}

        try:
            self._apply_resume_state(st)
        except Exception:
            # Fallback: at least try to replay the file.
            try:
                self.player.play(target)
            except Exception:
                pass

    def set_startup_crickets_enabled(self, enabled: bool):
        self.startup_crickets_enabled = bool(enabled)
        try:
            self._settings['startup_crickets_enabled'] = bool(enabled)
            self._save_user_settings()
        except Exception:
            pass

        # Apply immediately (if ambient is currently playing).
        if not enabled:
            self._stop_startup_ambient()
        else:
            try:
                if self.mode_stack.currentIndex() == 0:
                    self._start_startup_ambient()
            except Exception:
                pass

        try:
            if hasattr(self, 'bumps_mode_widget'):
                self.bumps_mode_widget.refresh_status()
        except Exception:
            pass

    def set_normalize_audio_enabled(self, enabled: bool):
        self.normalize_audio_enabled = bool(enabled)
        try:
            self._settings['normalize_audio_enabled'] = bool(enabled)
            self._save_user_settings()
        except Exception:
            pass

        try:
            self.player.set_audio_normalization(self.normalize_audio_enabled)
        except Exception:
            pass

        try:
            if hasattr(self, 'bumps_mode_widget'):
                self.bumps_mode_widget.refresh_status()
        except Exception:
            pass

    def set_web_mode_enabled(self, enabled: bool):
        self.playback_mode = 'web' if bool(enabled) else 'portable'
        try:
            self._settings['playback_mode'] = self.playback_mode
            if self.playback_mode == 'web':
                self._ensure_web_defaults()
            self._save_user_settings()
        except Exception:
            pass

        # Refresh visible playlists list immediately when toggling modes.
        try:
            if hasattr(self, 'play_mode_widget'):
                self.play_mode_widget.refresh_playlists()
        except Exception:
            pass

        try:
            if hasattr(self, 'bumps_mode_widget'):
                self.bumps_mode_widget.refresh_status()
        except Exception:
            pass

    def _ensure_web_defaults(self):
        """Fill in sane Web mode defaults if settings are empty."""
        try:
            # Skip slow network detection if manifest exists - just use config
            manifest_path = os.path.join(get_local_playlists_dir(), 'network_manifest.json')
            if os.path.exists(manifest_path):
                # Manifest exists - use configured web_files_root or default
                if not str(getattr(self, 'web_files_root', '') or '').strip():
                    system = platform.system().lower()
                    if system.startswith('win'):
                        self.web_files_root = r'Z:\\Sleepy Shows Data'
                    elif system == 'darwin':
                        self.web_files_root = '/Volumes/shows/Sleepy Shows Data'
                    else:
                        self.web_files_root = '/mnt/shows/Sleepy Shows Data'
                    self._settings['web_files_root'] = self.web_files_root
                return
            
            # Default Web mode to filesystem-based playback via a mounted share.
            # Users can override this in Settings.
            if not str(getattr(self, 'web_files_root', '') or '').strip():
                label = str(getattr(self, 'auto_config_volume_label', 'T7') or 'T7').strip() or 'T7'
                detected = self._detect_web_files_root(label)
                if detected:
                    self.web_files_root = detected
                else:
                    system = platform.system().lower()
                    if system.startswith('win'):
                        self.web_files_root = r'Z:\\Sleepy Shows Data'
                    elif system == 'darwin':
                        self.web_files_root = '/Volumes/shows/Sleepy Shows Data'
                    else:
                        self.web_files_root = '/mnt/shows/Sleepy Shows Data'
                self._settings['web_files_root'] = self.web_files_root

            try:
                self._maybe_autofix_web_files_root()
            except Exception:
                pass

            # Legacy HTTP defaults are no longer auto-filled because Web mode is
            # intended to be filesystem-based.
            # (We keep the settings keys for compatibility with older configs.)

        except Exception:
            return

    def _maybe_autofix_web_files_root(self):
        """If Web mode is active and the configured root isn't accessible, auto-switch to a detected root."""
        try:
            if not self._is_web_mode():
                return
        except Exception:
            return

        if self._web_files_root_accessible():
            return

        try:
            label = str(getattr(self, 'auto_config_volume_label', 'T7') or 'T7').strip() or 'T7'
        except Exception:
            label = 'T7'

        detected = self._detect_web_files_root(label)
        if not detected:
            return

        # Persist and refresh UI.
        try:
            self.set_web_files_root(detected)
        except Exception:
            # Fallback minimal persistence.
            self.web_files_root = detected
            try:
                self._settings['web_files_root'] = detected
                self._save_user_settings()
            except Exception:
                pass

    def _detect_web_files_root_candidates(self, volume_label: str) -> list[str]:
        """Return best-effort cross-platform candidates for a mounted library root.

        Candidates may point at (or contain) 'Sleepy Shows Data'.
        """
        try:
            label = str(volume_label or 'T7').strip() or 'T7'
        except Exception:
            label = 'T7'

        candidates: list[str] = []
        system = ''
        try:
            system = platform.system().lower()
        except Exception:
            system = ''

        # Universal likely locations (fast checks only).
        candidates.extend([
            '/mnt/shows/Sleepy Shows Data',
            '/mnt/shows',
            '/Volumes/shows/Sleepy Shows Data',
        ])

        if system.startswith('win'):
            # Common mapped-drive convention.
            candidates.extend([
                r'Z:\\Sleepy Shows Data',
                r'Z:\\',
            ])
            # Check other drive letters for a root-level folder.
            for letter in 'ZYXWVUTSRQPONMLKJIHGFEDCBA':
                try:
                    drive = f'{letter}:\\'
                    candidates.append(os.path.join(drive, 'Sleepy Shows Data'))
                except Exception:
                    continue
        elif system == 'darwin':
            # macOS volume mounts.
            for base in ('/Volumes',):
                try:
                    candidates.append(os.path.join(base, label, 'Sleepy Shows Data'))
                except Exception:
                    pass
                try:
                    candidates.append(os.path.join(base, label))
                except Exception:
                    pass
        else:
            # Linux mounts.
            for base in ('/media', '/run/media'):
                # Label-based (fast and targeted).
                for pattern in (
                    os.path.join(base, '*', label, 'Sleepy Shows Data'),
                    os.path.join(base, '*', label),
                ):
                    try:
                        candidates.extend(sorted(glob.glob(pattern)))
                    except Exception:
                        pass
                # Fallback: any mounted volume that contains the folder.
                try:
                    pattern = os.path.join(base, '*', '*', 'Sleepy Shows Data')
                    candidates.extend(sorted(glob.glob(pattern)))
                except Exception:
                    pass

        # Keep order but de-dup.
        seen: set[str] = set()
        ordered: list[str] = []
        for p in candidates:
            if not p:
                continue
            try:
                normalized = os.path.normpath(str(p))
            except Exception:
                normalized = str(p)
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def _detect_web_files_root(self, volume_label: str) -> str:
        """Return the first accessible Web Files Root candidate."""
        for p in self._detect_web_files_root_candidates(volume_label):
            try:
                if os.path.isdir(p) and os.access(p, os.R_OK):
                    return p
            except Exception:
                continue
        return ''

    def set_web_files_root(self, path: str):
        value = str(path or '').strip()
        self.web_files_root = value
        try:
            self._settings['web_files_root'] = value
            self._save_user_settings()
        except Exception:
            pass

        try:
            if hasattr(self, 'bumps_mode_widget') and hasattr(self.bumps_mode_widget, 'input_web_files_root'):
                self.bumps_mode_widget.input_web_files_root.setText(value)
        except Exception:
            pass

    def _web_data_root_for_files_root(self, files_root: str) -> str:
        """Return a directory that should behave like '.../Sleepy Shows Data' for a given files_root."""
        return web_mode_paths.web_data_root_for_files_root(files_root)

    def _path_to_web_files_path(self, path: str) -> str:
        """Best-effort conversion from a playlist path to an on-filesystem path under web_files_root."""
        return web_mode_paths.path_to_web_files_path(path, str(getattr(self, 'web_files_root', '') or '').strip())

    def _is_web_mode(self) -> bool:
        try:
            return str(getattr(self, 'playback_mode', 'portable') or 'portable').strip().lower() == 'web'
        except Exception:
            return False

    def _effective_web_data_root(self) -> str:
        """Return the effective 'Sleepy Shows Data' root for the configured Web Files Root."""
        try:
            wfr = str(getattr(self, 'web_files_root', '') or '').strip()
        except Exception:
            wfr = ''
        if not wfr:
            return ''
        try:
            return self._web_data_root_for_files_root(wfr)
        except Exception:
            return ''

    def _web_files_root_accessible(self) -> bool:
        """Best-effort check that Web mode's filesystem root is mounted and readable."""
        try:
            wfr = str(getattr(self, 'web_files_root', '') or '').strip()
        except Exception:
            wfr = ''
        if not wfr:
            return False

        # In web mode, check if manifest exists instead of checking network paths
        # This avoids slow/hanging network operations during startup
        try:
            manifest_path = os.path.join(get_local_playlists_dir(), 'network_manifest.json')
            if os.path.exists(manifest_path):
                return True
        except Exception:
            pass

        try:
            data_root = str(self._effective_web_data_root() or '')
        except Exception:
            data_root = ''

        candidates = []
        if wfr:
            candidates.append(wfr)
        if data_root and data_root not in candidates:
            candidates.append(data_root)

        for p in candidates:
            try:
                if os.path.isdir(p) and os.access(p, os.R_OK):
                    return True
            except Exception:
                continue
        return False

    def _warn_web_files_root_unavailable_once(self, *, context: str):
        """Warn once per run if Web Files Root isn't accessible (avoids popup spam)."""
        if not self._is_web_mode():
            return

        # Best-effort auto-fix before warning.
        try:
            self._maybe_autofix_web_files_root()
        except Exception:
            pass

        if self._web_files_root_accessible():
            return

        if self._web_files_root_accessible():
            return

        if bool(getattr(self, '_web_files_root_unavailable_warned', False)):
            return
        self._web_files_root_unavailable_warned = True

        try:
            wfr = str(getattr(self, 'web_files_root', '') or '').strip()
        except Exception:
            wfr = ''
        try:
            data_root = str(self._effective_web_data_root() or '')
        except Exception:
            data_root = ''

        suggestions = []
        try:
            label = str(getattr(self, 'auto_config_volume_label', 'T7') or 'T7').strip() or 'T7'
            for sug in self._detect_web_files_root_candidates(label)[:6]:
                if sug and sug not in (wfr, data_root):
                    suggestions.append(sug)
        except Exception:
            suggestions = []

        extra = ''
        if suggestions:
            extra = "\n\nSuggested Web Files Root (detected):\n" + "\n".join(suggestions)

        msg = (
            "Web mode is enabled, but the Web Files Root is not accessible.\n\n"
            f"Context: {context}\n\n"
            f"Web Files Root: {wfr or '(not set)'}\n"
            f"Expected data root: {data_root or '(unknown)'}\n\n"
            "Mount the share (SMB/UNC) in your OS and/or update Settings → Web Files Root."
            f"{extra}"
        )

        try:
            QMessageBox.warning(self, 'Web Files Root Unavailable', msg)
        except Exception:
            print(f"DEBUG: {msg}")

    def _resolve_video_play_target(self, path: str) -> str:
        """Return the string mpv should play for an episode/interstitial."""
        return web_mode_paths.resolve_video_play_target(
            path,
            str(getattr(self, 'playback_mode', 'portable') or 'portable'),
            str(getattr(self, 'web_files_root', '') or ''),
        )

    def _try_auto_populate_library(self):
        try:
            if getattr(self, '_auto_config_running', False):
                return

            # In Web mode we do NOT scan for removable drives. Instead, if the user
            # configured a Web Files Root (mounted share / UNC path), probe that.
            web_mount_roots = None
            try:
                if self._is_web_mode():
                    try:
                        self._maybe_autofix_web_files_root()
                    except Exception:
                        pass
                    wfr = str(getattr(self, 'web_files_root', '') or '').strip()
                    if wfr:
                        if not self._web_files_root_accessible():
                            self._warn_web_files_root_unavailable_once(context='auto-config')
                            self._auto_config_running = False
                            return
                        web_mount_roots = [wfr]
                    else:
                        # Web mode requires a configured/mounted root.
                        self._warn_web_files_root_unavailable_once(context='auto-config (no Web Files Root set)')
                        self._auto_config_running = False
                        return
            except Exception:
                web_mount_roots = None

            self._auto_config_running = True

            # Show pending overlay on the show cards while we probe external storage.
            if hasattr(self, 'welcome_screen'):
                try:
                    self.welcome_screen.set_show_pending("King of the Hill", True)
                    self.welcome_screen.set_show_pending("Aqua Teen Hunger Force", True)
                    self.welcome_screen.set_show_pending("Bob's Burgers", True)
                    self.welcome_screen.set_show_pending("Squidbillies", True)
                except Exception:
                    pass

            self._auto_config_thread = QThread(self)
            self._auto_config_worker = AutoConfigWorker(
                volume_label=getattr(self, 'auto_config_volume_label', 'T7'),
                mount_roots_override=web_mount_roots,
            )
            self._auto_config_worker.moveToThread(self._auto_config_thread)

            self._auto_config_thread.started.connect(self._auto_config_worker.run)
            self._auto_config_worker.finished.connect(self._on_auto_config_finished)
            self._auto_config_worker.finished.connect(self._auto_config_thread.quit)
            self._auto_config_worker.finished.connect(self._auto_config_worker.deleteLater)
            self._auto_config_thread.finished.connect(self._auto_config_thread.deleteLater)

            self._auto_config_thread.start()
        except Exception as e:
            self._auto_config_running = False
            print(f"DEBUG: Auto library detection failed: {e}")

    def ensure_show_playlist_loaded(self, show_name: str, *, auto_play: bool = True):
        """Load a show playlist by show name, generating it via auto-config if needed."""
        try:
            name = str(show_name or '').strip()
        except Exception:
            name = ''
        if not name:
            return

        filename = resolve_playlist_path(os.path.join("playlists", f"{name}.json"))
        if filename and os.path.exists(filename):
            self.load_playlist(filename, auto_play=bool(auto_play))
            return

        # Not present yet: request auto-config, then load after it completes.
        self._pending_show_autoload = name
        try:
            if hasattr(self, 'welcome_screen'):
                self.welcome_screen.set_show_pending(name, True)
        except Exception:
            pass

        # If already running, just wait for completion.
        if bool(getattr(self, '_auto_config_running', False)):
            return
        self._try_auto_populate_library()

    def set_auto_config_volume_label(self, label: str):
        value = str(label or '').strip()
        if not value:
            value = 'T7'

        self.auto_config_volume_label = value
        try:
            self._settings['auto_config_volume_label'] = value
            self._save_user_settings()
        except Exception:
            pass

    def _on_auto_config_finished(self, result):
        try:
            self._auto_config_running = False
            # Remove pending overlays.
            if hasattr(self, 'welcome_screen'):
                try:
                    self.welcome_screen.set_show_pending("King of the Hill", False)
                    self.welcome_screen.set_show_pending("Aqua Teen Hunger Force", False)
                    self.welcome_screen.set_show_pending("Bob's Burgers", False)
                    self.welcome_screen.set_show_pending("Squidbillies", False)
                except Exception:
                    pass

            # If user already added sources while we were scanning, don't override their
            # library state, but still apply other auto-config outputs (playlist refresh,
            # TV Vibe folder detection, etc.).
            already_has_sources = bool(
                getattr(self.playlist_manager, 'source_folders', None)
                and len(self.playlist_manager.source_folders) > 0
            )

            sources = (result or {}).get('sources', [])
            if sources and not already_has_sources:
                self.playlist_manager.source_folders = (result or {}).get('source_folders', [])
                self.playlist_manager.library_structure = (result or {}).get('library_structure', {})
                self.playlist_manager.episodes = (result or {}).get('episodes', [])
                if self.playlist_manager.library_structure:
                    self.populate_library_cumulative(self.playlist_manager.library_structure)

            if (result or {}).get('playlists_updated', False) and hasattr(self, 'play_mode_widget'):
                try:
                    self.play_mode_widget.refresh_playlists()
                except Exception:
                    pass

            # If a show-card click requested an auto-generated playlist, try to load it now.
            pending = None
            try:
                pending = str(getattr(self, '_pending_show_autoload', None) or '').strip() or None
            except Exception:
                pending = None
            if pending:
                try:
                    target = resolve_playlist_path(os.path.join("playlists", f"{pending}.json"))
                except Exception:
                    target = None
                if target and os.path.exists(target):
                    try:
                        self._pending_show_autoload = None
                    except Exception:
                        pass
                    try:
                        self.load_playlist(target, auto_play=True)
                    except Exception:
                        pass

            # Auto-detect bumps scripts/music/assets on the same external drive.
            # Important: images/audio FX can be used even if scripts are local, so don't gate
            # them behind finding a TV Vibe scripts folder.
            tv_vibe_scripts_dir = (result or {}).get('tv_vibe_scripts_dir', None)
            tv_vibe_music_dir = (result or {}).get('tv_vibe_music_dir', None)
            tv_vibe_images_dir = (result or {}).get('tv_vibe_images_dir', None)
            tv_vibe_audio_fx_dir = (result or {}).get('tv_vibe_audio_fx_dir', None)
            tv_vibe_interstitials_dir = (result or {}).get('tv_vibe_interstitials_dir', None)

            try:
                default_scripts_dir = get_local_bumps_scripts_dir()
            except Exception:
                default_scripts_dir = None

            bump_mgr = getattr(self.playlist_manager, 'bump_manager', None)

            # Scripts/music only override if we're still on defaults / nothing is loaded.
            try:
                scripts_loaded = bool(getattr(bump_mgr, 'bump_scripts', []) or []) if bump_mgr else False
            except Exception:
                scripts_loaded = False
            try:
                music_loaded = bool(getattr(bump_mgr, 'music_files', []) or []) if bump_mgr else False
            except Exception:
                music_loaded = False

            if tv_vibe_scripts_dir and os.path.isdir(tv_vibe_scripts_dir):
                try:
                    if getattr(self, 'bump_scripts_dir', None) in (None, '', default_scripts_dir) and not scripts_loaded:
                        self.bump_scripts_dir = tv_vibe_scripts_dir
                        try:
                            if bump_mgr:
                                bump_mgr.load_bumps(tv_vibe_scripts_dir)
                        except Exception:
                            pass
                        # Persist any one-time script exposure seeds immediately.
                        try:
                            if bump_mgr and bool(getattr(bump_mgr, '_script_exposure_seeded_last_changed', False)):
                                self.playlist_manager._exposure_dirty = True
                                self.playlist_manager._save_exposure_scores(force=True)
                        except Exception:
                            pass
                except Exception:
                    pass

            if tv_vibe_music_dir and os.path.isdir(tv_vibe_music_dir) and not music_loaded:
                try:
                    self.bump_music_dir = tv_vibe_music_dir
                except Exception:
                    pass
                try:
                    if bump_mgr:
                        bump_mgr.scan_music(tv_vibe_music_dir)
                except Exception:
                    pass
                # Persist any one-time exposure seeds immediately.
                try:
                    if bump_mgr and bool(getattr(bump_mgr, '_music_exposure_seeded_last_changed', False)):
                        self.playlist_manager._exposure_dirty = True
                        self.playlist_manager._save_exposure_scores(force=True)
                except Exception:
                    pass

            if tv_vibe_images_dir and os.path.isdir(tv_vibe_images_dir):
                # Only override if the user hasn't configured one yet.
                prev_images_dir = getattr(self, 'bump_images_dir', None)
                if not prev_images_dir or not os.path.isdir(str(prev_images_dir)):
                    self.bump_images_dir = tv_vibe_images_dir
                    try:
                        self._settings['bump_images_dir'] = tv_vibe_images_dir
                        self._save_user_settings()
                    except Exception:
                        pass
                try:
                    if bump_mgr:
                        bump_mgr.bump_images_dir = self.bump_images_dir
                except Exception:
                    pass

            if tv_vibe_audio_fx_dir and os.path.isdir(tv_vibe_audio_fx_dir):
                # Only override if the user hasn't configured one yet.
                prev_fx_dir = getattr(self, 'bump_audio_fx_dir', None)
                if not prev_fx_dir or not os.path.isdir(str(prev_fx_dir)):
                    self.bump_audio_fx_dir = tv_vibe_audio_fx_dir
                    try:
                        self._settings['bump_audio_fx_dir'] = tv_vibe_audio_fx_dir
                        self._save_user_settings()
                    except Exception:
                        pass
                try:
                    if bump_mgr:
                        bump_mgr.bump_audio_fx_dir = self.bump_audio_fx_dir
                except Exception:
                    pass

            # Auto-detect interstitials (commercials) folder.
            try:
                prev_inter_dir = str(getattr(self.playlist_manager, 'interstitial_folder', '') or '').strip()
            except Exception:
                prev_inter_dir = ''
            try:
                prev_inter_ok = bool(prev_inter_dir and os.path.isdir(prev_inter_dir))
            except Exception:
                prev_inter_ok = False

            if tv_vibe_interstitials_dir and os.path.isdir(str(tv_vibe_interstitials_dir)):
                # Only override if none set (or stale).
                if not prev_inter_ok:
                    try:
                        self._set_interstitials_folder(str(tv_vibe_interstitials_dir), persist=True)
                    except Exception:
                        pass

            # If we updated asset base directories after scripts were already parsed,
            # re-parse scripts so <img>/<sound> tags re-resolve to valid paths.
            try:
                if (
                    (tv_vibe_images_dir and os.path.isdir(tv_vibe_images_dir) and (not prev_images_dir or not os.path.isdir(str(prev_images_dir))))
                    or (tv_vibe_audio_fx_dir and os.path.isdir(tv_vibe_audio_fx_dir) and (not prev_fx_dir or not os.path.isdir(str(prev_fx_dir))))
                ):
                    self._reload_bump_scripts_for_assets()
            except Exception:
                pass

            # Auto-config implies storage topology may have changed (drive mounted / letter changed).
            # Refresh outro sounds so the UI doesn't get stuck with a stale cached list.
            try:
                self._invalidate_outro_sounds_cache()
                self._ensure_outro_sounds_loaded_force(force=True)
            except Exception:
                pass

            if hasattr(self, 'bumps_mode_widget'):
                try:
                    self.bumps_mode_widget.refresh_status()
                except Exception:
                    pass

            if sources:
                print(f"DEBUG: Auto-added {len(sources)} show source(s) from T7: {sources}")
        finally:
            self._auto_config_running = False

    def _stop_startup_ambient(self):
        if not getattr(self, '_startup_ambient_playing', False):
            return
        try:
            self.player.stop()
        except Exception:
            pass
        self._startup_ambient_playing = False

    def eventFilter(self, obj, event):
        if obj == self.video_container and event.type() == QEvent.Resize:
             # Resize overlay to width of container, fixed height or wrap content
             w = event.size().width()
             h = event.size().height()
             if hasattr(self, 'overlay_label'):
                 self.overlay_label.setGeometry(0, 0, w, 60) # Top 60px

             if hasattr(self, 'bump_video_overlay_label') and self.bump_video_overlay_label is not None:
                 try:
                     self.bump_video_overlay_label.setGeometry(0, 0, w, h)
                 except Exception:
                     pass
                 
             # Positioning Controls in Fullscreen Overlay Mode
             if self.isFullScreen() and hasattr(self, 'play_mode_widget'):
                 try:
                     self._reposition_fullscreen_controls()
                 except Exception:
                     pass

        if obj == getattr(self, 'bump_widget', None) and event.type() == QEvent.Resize:
            try:
                h = int(event.size().height())
                pad = int(round(h * float(getattr(self, '_bump_safe_vpad_ratio', 0.15))))
                # Clamp so we never consume the whole view.
                pad = max(0, min(pad, max(0, (h // 2) - 1)))

                layout = getattr(self, '_bump_layout', None) or self.bump_widget.layout()
                if layout is not None:
                    l, t, r, b = layout.getContentsMargins()
                    if t != pad or b != pad:
                        layout.setContentsMargins(l, pad, r, pad)
            except Exception:
                pass
        
        if event.type() == QEvent.MouseMove:
            # Best-effort: treat any mouse movement as activity.
            self.on_mouse_move()
        elif event.type() == QEvent.KeyPress:
            # Never respond to key events unless this window is actually active.
            # (This avoids surprising behavior and also prevents double-toggles
            # when mpv is handling keys inside the embedded video window.)
            try:
                if not self.isActiveWindow():
                    return super().eventFilter(obj, event)
            except Exception:
                pass

            # Don't steal keys while the user is typing into inputs.
            try:
                fw = QApplication.focusWidget()
                if isinstance(fw, QLineEdit):
                    return super().eventFilter(obj, event)
            except Exception:
                pass

            if event.key() == Qt.Key_Escape and self.isFullScreen():
                self.toggle_fullscreen()
            elif event.key() == Qt.Key_F:
                self.toggle_fullscreen()
        return super().eventFilter(obj, event)

    def _apply_bump_safe_padding_now(self):
        """Apply the current bump safe padding ratio to the bump layout immediately."""
        try:
            bw = getattr(self, 'bump_widget', None)
            if bw is None:
                return
            layout = getattr(self, '_bump_layout', None) or bw.layout()
            if layout is None:
                return
            h = int(bw.height())
            pad = int(round(h * float(getattr(self, '_bump_safe_vpad_ratio', 0.15))))
            pad = max(0, min(pad, max(0, (h // 2) - 1)))
            l, _t, r, _b = layout.getContentsMargins()
            layout.setContentsMargins(l, pad, r, pad)
        except Exception:
            return

    def _on_spacebar(self):
        try:
            fw = QApplication.focusWidget()
            if isinstance(fw, QLineEdit):
                return
        except Exception:
            pass
        # Only act in Play Mode.
        try:
            if self.mode_stack.currentIndex() != 2:
                return
        except Exception:
            return

        try:
            self.toggle_play()
        except Exception:
            pass

    def on_escape_pressed(self):
        if self.isFullScreen():
            self.toggle_fullscreen()

    def on_mouse_move(self):
        # Update activity timestamp
        self.last_activity_time = time.time()
        
        # If in Play Mode
        if self.mode_stack.currentIndex() == 2:
            self.show_controls()

    def _current_episode_title_for_overlay(self):
        try:
            pm = self.playlist_manager
            playlist = getattr(pm, 'current_playlist', None) or []
            if not playlist:
                return None

            idx = int(getattr(pm, 'current_index', -1))
            if idx < 0 or idx >= len(playlist):
                return None

            item = playlist[idx]
            if isinstance(item, dict):
                itype = item.get('type', 'video')
                if itype not in ('video', 'interstitial'):
                    return None
                path = item.get('path') or ''
            else:
                path = str(item or '')

            if not path:
                return None
            return os.path.splitext(os.path.basename(path))[0]
        except Exception:
            return None

    def _hide_episode_overlay(self):
        try:
            self._episode_overlay_hide_timer.stop()
        except Exception:
            pass
        try:
            if hasattr(self, 'overlay_label'):
                self.overlay_label.setVisible(False)
        except Exception:
            pass

    def _on_episode_overlay_hide_timeout(self):
        # Only auto-hide in windowed mode.
        try:
            if self.isFullScreen():
                return
        except Exception:
            pass
        self._hide_episode_overlay()

    def _show_episode_overlay(self, auto_hide_seconds=None):
        title = self._current_episode_title_for_overlay()
        if not title:
            self._hide_episode_overlay()
            return

        try:
            self.overlay_label.setText(title)
            self.overlay_label.setVisible(True)
            self.overlay_label.raise_()
        except Exception:
            return

        try:
            self._episode_overlay_hide_timer.stop()
            if auto_hide_seconds is not None:
                secs = float(auto_hide_seconds)
                if secs > 0:
                    self._episode_overlay_hide_timer.start(int(secs * 1000))
        except Exception:
            pass

    def _sync_episode_overlay_visibility(self):
        # Fullscreen rule: show overlay only while player controls are visible.
        try:
            if not self.isFullScreen():
                return
        except Exception:
            return

        try:
            ctrls_visible = bool(self.play_mode_widget.controls_widget.isVisible())
        except Exception:
            ctrls_visible = False

        if ctrls_visible:
            self._show_episode_overlay(auto_hide_seconds=None)
        else:
            self._hide_episode_overlay()

    def _hide_bump_video_overlay(self):
        try:
            if hasattr(self, '_bump_video_overlay_timer'):
                self._bump_video_overlay_timer.stop()
        except Exception:
            pass
        try:
            if hasattr(self, 'bump_video_overlay_label') and self.bump_video_overlay_label is not None:
                self.bump_video_overlay_label.setVisible(False)
                self.bump_video_overlay_label.setText('')
        except Exception:
            pass
        try:
            self._bump_video_overlay_scheduled = False
        except Exception:
            pass

    def _show_mpv_osd_text(self, text: str, *, duration_ms: int):
        """Show an on-video overlay using mpv's OSD.

        Qt widget overlays don't reliably draw above mpv's native window on Linux,
        so prefer mpv's OSD for bump-video inclusive outros.
        """
        try:
            s = str(text or '')
        except Exception:
            s = ''
        if not s:
            return

        try:
            d = int(duration_ms)
        except Exception:
            d = 0
        if d <= 0:
            d = 1

        # Make the OSD look like a bump card: centered and large.
        # mpv's OSD uses ASS for formatting; use minimal overrides.
        try:
            ass = str(s).replace('{', '(').replace('}', ')')
            ass = ass.replace('\r\n', '\n').replace('\r', '\n')
            ass = ass.replace('\n', r'\N')
            # Center (an5), big font, white with black border.
            # IMPORTANT: ASS tags use single backslashes. Use raw strings so
            # sequences like "\a" are not interpreted by Python.
            ass = r'{\an5\fs52\bord6\shad0\1c&HFFFFFF&\3c&H000000&}' + ass
        except Exception:
            ass = s

        try:
            mpv = getattr(getattr(self, 'player', None), 'mpv', None)
            if mpv is None:
                return
            # mpv command: show-text <string> [duration-ms]
            mpv.command('show-text', str(ass), int(d))
        except Exception:
            return

    def _show_bump_video_overlay(self, text: str, *, duration_ms: int, play_outro_audio: bool = False):
        try:
            s = str(text or '')
        except Exception:
            s = ''
        if not s:
            return

        try:
            self._hide_episode_overlay()
        except Exception:
            pass

        # Prefer mpv OSD so the overlay appears above the video surface.
        try:
            self._show_mpv_osd_text(s, duration_ms=int(duration_ms))
        except Exception:
            pass

        # Best-effort Qt overlay fallback (may be hidden behind mpv's native window).
        try:
            if hasattr(self, 'bump_video_overlay_label') and self.bump_video_overlay_label is not None:
                try:
                    r = self.video_container.rect()
                    self.bump_video_overlay_label.setGeometry(r)
                except Exception:
                    pass
                self.bump_video_overlay_label.setText(s)
                self.bump_video_overlay_label.setVisible(True)
                self.bump_video_overlay_label.raise_()
        except Exception:
            pass

        if play_outro_audio:
            try:
                self._play_outro_audio(duration_ms=int(duration_ms))
            except Exception:
                pass

        try:
            d = int(duration_ms)
        except Exception:
            d = 0
        if d <= 0:
            d = 1
        try:
            self._bump_video_overlay_timer.start(int(d))
        except Exception:
            pass

    def _schedule_inclusive_bump_video_overlay(self, video_duration_ms: int):
        """Schedule the inclusive outro overlay so it ends at video EOF."""
        try:
            if not bool(getattr(self, '_current_bump_is_video', False)):
                return
            if not bool(getattr(self, '_current_bump_video_inclusive', False)):
                return
            if bool(getattr(self, '_bump_video_overlay_scheduled', False)):
                return
        except Exception:
            return

        text = None
        try:
            text = str(getattr(self, '_bump_video_overlay_text', None) or '').strip()
        except Exception:
            text = ''
        if not text:
            return

        try:
            outro_ms = int(getattr(self, '_bump_video_overlay_ms', 800) or 800)
        except Exception:
            outro_ms = 800
        if outro_ms <= 0:
            outro_ms = 1

        try:
            vid_ms = int(video_duration_ms)
        except Exception:
            return
        if vid_ms <= 0:
            return

        start_ms = max(0, int(vid_ms) - int(outro_ms))

        cur_ms = 0
        try:
            pos = getattr(self, '_last_time_pos', None)
            if pos is None and hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                pos = getattr(self.player.mpv, 'time_pos', None)
            if pos is not None:
                cur_ms = int(round(float(pos) * 1000.0))
        except Exception:
            cur_ms = 0

        delay = int(start_ms) - int(cur_ms)
        if delay < 0:
            delay = 0

        try:
            self._log_event(
                'bump_video_overlay_schedule',
                video=str(getattr(self, '_current_bump_video_path', '') or ''),
                vid_ms=int(vid_ms),
                outro_ms=int(outro_ms),
                start_ms=int(start_ms),
                cur_ms=int(cur_ms),
                delay_ms=int(delay),
            )
        except Exception:
            pass

        try:
            play_audio = bool(getattr(self, '_bump_video_overlay_play_audio', False))
        except Exception:
            play_audio = False

        def _do_show():
            try:
                try:
                    self._log_event('bump_video_overlay_show', video=str(getattr(self, '_current_bump_video_path', '') or ''))
                except Exception:
                    pass
                self._show_bump_video_overlay(text, duration_ms=int(outro_ms), play_outro_audio=bool(play_audio))
            except Exception:
                return

        try:
            self._bump_video_overlay_scheduled = True
        except Exception:
            pass

        try:
            if delay <= 0:
                QTimer.singleShot(0, _do_show)
            else:
                QTimer.singleShot(int(delay), _do_show)
        except Exception:
            _do_show()

    def _poll_fullscreen_cursor(self):
        # Only needed in fullscreen play mode.
        try:
            if not self.isFullScreen():
                self._fs_cursor_poll_timer.stop()
                self._fs_last_cursor_pos = None
                return
            if self.mode_stack.currentIndex() != 2:
                return
        except Exception:
            return

        # Important: only react to cursor movement when this window is active and
        # the cursor is actually over this window. Otherwise, moving the cursor on
        # another monitor can incorrectly trigger control visibility.
        try:
            if not self.isActiveWindow():
                try:
                    self._fs_last_cursor_pos = QCursor.pos()
                except Exception:
                    pass
                return
        except Exception:
            pass

        try:
            pos = QCursor.pos()
        except Exception:
            return

        try:
            fg = self.frameGeometry()
            if fg is not None and not fg.contains(pos):
                # Still update last position so we don't repeatedly treat
                # off-window movement as a "new" event.
                self._fs_last_cursor_pos = pos
                return
        except Exception:
            # If we can't determine geometry, fall back to old behavior.
            pass

        if self._fs_last_cursor_pos is None or pos != self._fs_last_cursor_pos:
            self._fs_last_cursor_pos = pos
            self.on_mouse_move()

    def show_controls(self):
        # In windowed mode, controls should always stay visible.
        if not self.isFullScreen():
            self.play_mode_widget.controls_widget.setVisible(True)
            self.hover_timer.stop()
            return

        start_timer = False
        # If paused, keep shown.
        # If playing, start timer to hide.
        if hasattr(self, 'player') and self.player.mpv:
             if not self.player.mpv.pause and not self.player.mpv.core_idle:
                 start_timer = True
        
        self.play_mode_widget.controls_widget.setVisible(True)

        # Fullscreen: show cursor while controls are visible.
        try:
            self._set_fullscreen_cursor_hidden(False)
        except Exception:
            pass

        # Ensure controls are correctly pinned to bottom.
        try:
            self._reposition_fullscreen_controls()
        except Exception:
            pass

        try:
            self._sync_episode_overlay_visibility()
        except Exception:
            pass
        
        if start_timer:
            self.hover_timer.start()
        else:
            self.hover_timer.stop()

    def hide_controls(self):
        # Only auto-hide in fullscreen.
        if not self.isFullScreen():
            return

        # Only hide if playing
        if hasattr(self, 'player') and self.player.mpv:
            if not self.player.mpv.pause and not self.player.mpv.core_idle:
                 # Check if cursor is over controls?
                 # If over controls, don't hide.
                 controls_gm = self.play_mode_widget.controls_widget.geometry()
                 # geometry is relative to play mode widget
                 # map to global
                 # simple check: just hide, moving mouse brings back
                 self.play_mode_widget.controls_widget.setVisible(False)

                 # Fullscreen: hide cursor with the controls.
                 try:
                     self._set_fullscreen_cursor_hidden(True)
                 except Exception:
                     pass

                 try:
                     self._sync_episode_overlay_visibility()
                 except Exception:
                     pass

    def check_fullscreen_inactivity(self):
        # Failsafe: if we are in fullscreen, playing, and controls are visible
        # check if it's been > 3 seconds since last activity.
        if self.isFullScreen() and self.play_mode_widget.controls_widget.isVisible():
            if hasattr(self, 'player') and self.player.mpv:
                 # Check playing state
                 is_playing = (not self.player.mpv.pause) and (not self.player.mpv.core_idle)
                 if is_playing:
                     diff = time.time() - self.last_activity_time
                     if diff > 3.5: # 3.5s threshold (slightly larger than hover)
                         self.hide_controls()

    def _update_fullscreen_button_icon(self):
        if not hasattr(self, 'play_mode_widget'):
            return
        btn = getattr(self.play_mode_widget, 'btn_fullscreen', None)
        if btn is None:
            return

        if self.isFullScreen():
            exit_fs_icon = QIcon.fromTheme(
                "view-restore",
                QIcon.fromTheme("window-restore"),
            )
            if exit_fs_icon.isNull():
                exit_fs_icon = self.style().standardIcon(QStyle.SP_TitleBarNormalButton)
            if hasattr(self, 'play_mode_widget') and self.play_mode_widget is not None:
                exit_fs_icon = self.play_mode_widget._tint_icon(exit_fs_icon, QSize(32, 32), Qt.white)
            btn.setIcon(exit_fs_icon)
        else:
            enter_fs_icon = QIcon.fromTheme(
                "view-fullscreen",
                QIcon.fromTheme("fullscreen"),
            )
            if enter_fs_icon.isNull():
                enter_fs_icon = self.style().standardIcon(QStyle.SP_TitleBarMaxButton)
            if hasattr(self, 'play_mode_widget') and self.play_mode_widget is not None:
                enter_fs_icon = self.play_mode_widget._tint_icon(enter_fs_icon, QSize(32, 32), Qt.white)
            btn.setIcon(enter_fs_icon)

    def toggle_fullscreen(self):
        # Guard against double toggles triggered by both MPV and Qt handling the key.
        try:
            now = time.monotonic()
            last = float(getattr(self, '_last_fullscreen_toggle_mono', 0.0) or 0.0)
            if (now - last) < 0.25:
                return
            self._last_fullscreen_toggle_mono = now
        except Exception:
            pass

        if self.isFullScreen():
            # Exiting Fullscreen
            self.failsafe_timer.stop()
            try:
                self._fs_cursor_poll_timer.stop()
                self._fs_last_cursor_pos = None
            except Exception:
                pass

            try:
                self._set_fullscreen_cursor_hidden(False)
            except Exception:
                pass

            # showNormal() first to exit fullscreen, then restore UI+geometry.
            self.showNormal()

            # Delay UI restoration to avoid "zoom in" effect during OS animation.
            QTimer.singleShot(200, self.restore_ui_after_fullscreen)
        else:
            # Entering Fullscreen (Windows reliability): if not maximized, maximize first.
            if getattr(self, '_fullscreen_transitioning', False):
                return
            self._fullscreen_transitioning = True

            try:
                self._pre_fullscreen_geometry = self.geometry()
            except Exception:
                self._pre_fullscreen_geometry = None

            try:
                self._pre_fullscreen_was_maximized = bool(self.isMaximized())
            except Exception:
                self._pre_fullscreen_was_maximized = False

            # Preserve existing flag used elsewhere.
            self.was_maximized = bool(self._pre_fullscreen_was_maximized)

            if not self._pre_fullscreen_was_maximized:
                try:
                    self.showMaximized()
                except Exception:
                    pass

                # Let the window manager settle before requesting fullscreen.
                QTimer.singleShot(80, self._finish_enter_fullscreen)
            else:
                self._finish_enter_fullscreen()

    def _finish_enter_fullscreen(self):
        try:
            self.showFullScreen()
        except Exception:
            try:
                self._fullscreen_transitioning = False
            except Exception:
                pass
            return

        # Hide sidebar
        try:
            self.play_mode_widget.sidebar_container.setVisible(False)
        except Exception:
            pass

        try:
            self.play_mode_widget.btn_fullscreen.setChecked(True)
        except Exception:
            pass
        self._update_fullscreen_button_icon()

        try:
            self.play_mode_widget.set_controls_overlay(True)
        except Exception:
            pass

        # Hide both native and custom menu bars while fullscreen.
        try:
            self.menuBar().setVisible(False)
        except Exception:
            pass
        if hasattr(self, 'menu_bar_widget') and self.menu_bar_widget is not None:
            try:
                self.menu_bar_widget.setVisible(False)
            except Exception:
                pass
        try:
            self.statusBar().setVisible(False)
        except Exception:
            pass

        self.failsafe_timer.start()
        try:
            self._fs_last_cursor_pos = QCursor.pos()
            self._fs_cursor_poll_timer.start()
        except Exception:
            pass

        # Ensure controls (and episode overlay, if any) are visible initially.
        self.show_controls()

        # Reposition controls after the fullscreen resize completes.
        QTimer.singleShot(0, self._reposition_fullscreen_controls)
        QTimer.singleShot(120, self._reposition_fullscreen_controls)

        try:
            self._fullscreen_transitioning = False
        except Exception:
            pass

    def _reposition_fullscreen_controls(self):
        """Pin the controls overlay to the bottom of the video container."""
        try:
            if not self.isFullScreen():
                return
            if not hasattr(self, 'play_mode_widget') or self.play_mode_widget is None:
                return
            if not hasattr(self, 'video_container') or self.video_container is None:
                return

            ctrls = self.play_mode_widget.controls_widget
            if ctrls is None:
                return
            if ctrls.parent() != self.video_container:
                return

            w = int(self.video_container.width())
            h = int(self.video_container.height())
            try:
                ch = int(ctrls.height())
            except Exception:
                ch = 0
            if ch <= 0:
                try:
                    ch = int(ctrls.sizeHint().height())
                except Exception:
                    ch = 180

            ch = max(1, min(ch, max(1, h)))
            ctrls.setGeometry(0, h - ch, w, ch)
            ctrls.raise_()

            # Fullscreen overlay width changes don't always propagate through PlayModeWidget resize.
            try:
                QTimer.singleShot(0, lambda: self.play_mode_widget._update_controls_size_mode(force=True))
            except Exception:
                pass
        except Exception:
            return

    def _set_fullscreen_cursor_hidden(self, hidden: bool):
        """Hide the cursor in fullscreen when controls are hidden."""
        try:
            want_hidden = bool(hidden)
        except Exception:
            want_hidden = False

        if not self.isFullScreen():
            want_hidden = False

        already = bool(getattr(self, '_fullscreen_cursor_hidden', False))
        if want_hidden == already:
            return

        if want_hidden:
            try:
                QApplication.setOverrideCursor(Qt.BlankCursor)
                self._fullscreen_cursor_hidden = True
            except Exception:
                self._fullscreen_cursor_hidden = False
        else:
            # Restore any overrides we created.
            try:
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
            except Exception:
                pass
            self._fullscreen_cursor_hidden = False

    def restore_ui_after_fullscreen(self):
        if not self.isFullScreen():
            # Restore cursor immediately on exit.
            try:
                self._set_fullscreen_cursor_hidden(False)
            except Exception:
                pass

            # Restore pre-fullscreen window geometry if we forced maximization.
            try:
                if not bool(getattr(self, '_pre_fullscreen_was_maximized', False)):
                    if getattr(self, '_pre_fullscreen_geometry', None) is not None:
                        self.setGeometry(self._pre_fullscreen_geometry)
            except Exception:
                pass

            self.play_mode_widget.sidebar_container.setVisible(self.play_mode_widget.sidebar_visible)
            self.play_mode_widget.btn_fullscreen.setChecked(False)
            self._update_fullscreen_button_icon()
            self.play_mode_widget.set_controls_overlay(False)
            # We always use the custom menu bar; keep native menu hidden.
            self.menuBar().setVisible(False)
            if hasattr(self, 'menu_bar_widget') and self.menu_bar_widget is not None:
                self.menu_bar_widget.setVisible(True)
            self.statusBar().setVisible(True)
            # Ensure controls remain visible after leaving fullscreen.
            self.show_controls()

            # Leaving fullscreen often changes widths without reliable resize timing;
            # force a recompute after geometry/menu restoration settles.
            try:
                QTimer.singleShot(0, lambda: self.play_mode_widget._update_controls_size_mode(force=True))
            except Exception:
                pass

            # Leaving fullscreen: don't keep the episode title overlay hanging around.
            self._hide_episode_overlay()

    def setup_ui(self):
        # 1. Hide Standard Menu Bar
        self.menuBar().setVisible(False)
        self.menuBar().setNativeMenuBar(False)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # --- Custom Menu Bar ---
        self.menu_bar_widget = QWidget()
        self.menu_bar_widget.setStyleSheet("background-color: #2b2b2b; border-bottom: 2px solid #1a1a1a;")
        self.menu_bar_widget.setFixedHeight(35)
        mb_layout = QHBoxLayout(self.menu_bar_widget)
        mb_layout.setContentsMargins(10, 0, 10, 0)
        mb_layout.setSpacing(10)
        
        self.create_custom_menus(mb_layout)
        
        main_layout.addWidget(self.menu_bar_widget)
        # -----------------------
        
        self.mode_stack = QStackedWidget()
        main_layout.addWidget(self.mode_stack)
        
        # 0. Welcome Screen
        self.welcome_screen = WelcomeScreen(self)
        self.mode_stack.addWidget(self.welcome_screen)
        
        # 1. Edit Mode
        self.edit_mode_widget = EditModeWidget(self)
        self.mode_stack.addWidget(self.edit_mode_widget)
        
        # 2. Play Mode
        self.play_mode_widget = PlayModeWidget(self)
        self.mode_stack.addWidget(self.play_mode_widget)

        # 3. Bumps (Global)
        self.bumps_mode_widget = BumpsModeWidget(self)
        self.mode_stack.addWidget(self.bumps_mode_widget)
        
        # Inject Video Container into PlayModeWidget
        self.video_stack = QStackedWidget()
        self.video_stack.addWidget(self.video_container)
        self.video_stack.addWidget(self.bump_widget)
        
        # Replace placeholder in PlayModeWidget layout
        # Finding the layout directly
        video_area = self.play_mode_widget.layout.itemAt(1).widget()
        if video_area:
             # video_area has QVBoxLayout
             v_layout = video_area.layout()
             # Item 0 is placeholder
             placeholder = v_layout.itemAt(0).widget()
             if placeholder:
                 v_layout.replaceWidget(placeholder, self.video_stack)
                 placeholder.deleteLater()
        
        self.mode_stack.setCurrentIndex(0) # Start in Welcome Screen
        self.update_menu_mode_state()

    def create_custom_menus(self, layout):
        # We replace the native menu bar with custom buttons dropping down QMenus
        
        btn_style = """
            QPushButton { 
                color: #e0e0e0; 
                background: transparent; 
                font-size: 14px;
                font-weight: bold; 
                padding: 5px 10px;
                border: none;
            }
            QPushButton:hover { 
                background: #444; 
                border-radius: 4px; 
            }
            QPushButton::menu-indicator { image: none; }
        """
        
        def add_menu_btn(text):
            btn = QPushButton(text)
            btn.setStyleSheet(btn_style)
            btn.setCursor(Qt.PointingHandCursor)
            layout.addWidget(btn)
            
            menu = QMenu(self)
            
            # Use asset path for checkmark if available
            check_path = get_asset_path("check.png").replace("\\", "/")
            
            menu.setStyleSheet(f"""
                QMenu {{ 
                    background-color: #333; 
                    color: white; 
                    border: 1px solid #111; 
                }} 
                QMenu::item {{
                    padding: 8px 30px 8px 30px; /* More padding for indicator */
                }}
                QMenu::item:selected {{ 
                    background-color: {THEME_COLOR}; 
                }}
                /* Removed indicator styling since we use manual icons now */
            """)
            btn.setMenu(menu)
            return menu

        # 1. Sleepy Player
        app_menu = add_menu_btn("Sleepy Player")
        
        about_action = QAction("About", self)
        about_action.triggered.connect(lambda: QMessageBox.information(self, "About", "Sleepy Shows Player v0.3"))
        app_menu.addAction(about_action)
        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        app_menu.addAction(exit_action)
        
        # 2. Sleep Timer
        # MANUAL DROPDOWN (No QMenu)
        # We manually create a button and handle the popup ourselves
        self.btn_sleep_timer = QPushButton("Sleep Timer")
        self.btn_sleep_timer.setStyleSheet(btn_style)
        self.btn_sleep_timer.setCursor(Qt.PointingHandCursor)
        self.btn_sleep_timer.clicked.connect(self.show_sleep_timer_dropdown)
        layout.addWidget(self.btn_sleep_timer)

        layout.addStretch(1) # Push menus to left, fill rest

        # Mode buttons on top-right (replaces Mode dropdown)
        mode_btn_style = f"""
            QPushButton {{
                color: #e0e0e0;
                background: transparent;
                font-size: 14px;
                font-weight: bold;
                padding: 5px 12px;
                border: 1px solid transparent;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background: #444;
            }}
            QPushButton:checked {{
                background: {THEME_COLOR};
                border-color: {THEME_COLOR};
            }}
        """

        self.btn_mode_welcome = QPushButton("HOME")
        self.btn_mode_play = QPushButton("PLAY")
        self.btn_mode_edit = QPushButton("EDIT")
        self.btn_mode_bumps = QPushButton("SETTINGS")

        for btn in (self.btn_mode_welcome, self.btn_mode_play, self.btn_mode_edit, self.btn_mode_bumps):
            btn.setStyleSheet(mode_btn_style)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            layout.addWidget(btn)

        self.btn_mode_welcome.clicked.connect(lambda _=False: self.set_mode(0))
        self.btn_mode_play.clicked.connect(lambda _=False: self.set_mode(2))
        self.btn_mode_edit.clicked.connect(lambda _=False: self.set_mode(1))
        self.btn_mode_bumps.clicked.connect(lambda _=False: self.set_mode(3))
        
        # Ensure status label exists
        if not hasattr(self, 'lbl_sleep_status'):
            self.lbl_sleep_status = QLabel("")
            self.lbl_sleep_status.setStyleSheet("color: white; padding-right: 10px;")
            # status bar might be hidden in full screen, but we add to layout if we want custom
            self.statusBar().addPermanentWidget(self.lbl_sleep_status)

    def show_sleep_timer_dropdown(self, anchor_widget=None):
        # If called from clicked(bool), ignore the boolean argument.
        if isinstance(anchor_widget, bool):
            anchor_widget = None

        # Create a custom popup widget simulate a dropdown
        if hasattr(self, 'sleep_dropdown') and self.sleep_dropdown and self.sleep_dropdown.isVisible():
            self.sleep_dropdown.close()
            return
            
        self.sleep_dropdown = QWidget(self, Qt.Popup)
        self.sleep_dropdown.setStyleSheet("""
            QWidget { background-color: #333; border: 1px solid #111; }
            QPushButton { 
                text-align: left; 
                padding: 8px 15px; 
                color: white; 
                background: transparent; 
                border: none; 
                font-size: 14px;
            }
            QPushButton:hover { background-color: #0e1a77; }
        """)
        
        layout = QVBoxLayout(self.sleep_dropdown)
        layout.setContentsMargins(0, 5, 0, 5)
        layout.setSpacing(0)
        
        # State Checking
        is_active = bool(self.sleep_timer_active)
        active_mins = int(self.current_sleep_minutes) if hasattr(self, 'current_sleep_minutes') else 0
        
        # Cache icons so update_sleep_menu_state() can reuse them.
        self._sleep_check_icon = QIcon(get_asset_path("check.png"))
        self._sleep_empty_icon = QIcon()

        check_icon = self._sleep_check_icon
        empty_icon = self._sleep_empty_icon

        # Track buttons so we can update checkmarks while the dropdown is open.
        self._sleep_dropdown_buttons = {}
        
        # Helper to add item
        def add_item(text, is_checked, callback, minutes_value=None):
            btn = QPushButton(text)
            if is_checked:
                btn.setIcon(check_icon)
            else:
                btn.setIcon(empty_icon) # Keep alignment

            if minutes_value is not None:
                self._sleep_dropdown_buttons[int(minutes_value)] = btn

            # clicked(bool) -> ignore the bool
            btn.clicked.connect(lambda _=False: callback())
            btn.clicked.connect(lambda _=False: self.sleep_dropdown.close())
            layout.addWidget(btn)

        # 1. Off
        add_item("Off", not is_active, lambda: self.cancel_sleep_timer(), minutes_value=0)
        
        # 2. Durations
        durations = [30, 60, 90, 120, 180]
        for mins in durations:
            if mins == 90:
                label = "1.5 Hours"
            elif mins % 60 == 0:
                h = mins // 60
                label = f"{h} Hour" if h == 1 else f"{h} Hours"
            else:
                label = f"{mins} Minutes"
                
            is_checked = is_active and (mins == active_mins)
            add_item(label, is_checked, lambda m=mins: self.start_sleep_timer(m), minutes_value=mins)
            
        # Position it
        anchor = anchor_widget if anchor_widget is not None else self.btn_sleep_timer
        self.sleep_dropdown.resize(200, self.sleep_dropdown.sizeHint().height())

        global_bottom_left = anchor.mapToGlobal(anchor.rect().bottomLeft())
        global_top_left = anchor.mapToGlobal(anchor.rect().topLeft())

        # Default: open downward. If it would go off-screen, open upward.
        screen = anchor.screen() if hasattr(anchor, 'screen') else None
        available = screen.availableGeometry() if screen is not None else QApplication.primaryScreen().availableGeometry()

        popup_w = self.sleep_dropdown.width()

        popup_h = self.sleep_dropdown.height()

        x = global_bottom_left.x()
        y = global_bottom_left.y()

        if y + popup_h > available.bottom():
            y = global_top_left.y() - popup_h

        # Clamp to screen bounds
        if x + popup_w > available.right():
            x = max(available.left(), available.right() - popup_w)
        if x < available.left():
            x = available.left()
        if y < available.top():
            y = available.top()
        if y + popup_h > available.bottom():
            y = max(available.top(), available.bottom() - popup_h)

        self.sleep_dropdown.move(x, y)
        self.sleep_dropdown.show()

    def cycle_sleep_timer_quick(self):
        """Cycle sleep timer duration on single press.

        Order: 3h -> 2h -> 1.5h -> 1h -> 30m -> OFF -> (back to 3h)
        The dropdown picker remains available from the top menu.
        """
        try:
            # If the dropdown is open (from the top menu), close it.
            if hasattr(self, 'sleep_dropdown') and self.sleep_dropdown and self.sleep_dropdown.isVisible():
                try:
                    self.sleep_dropdown.close()
                except Exception:
                    pass

            steps = [180, 120, 90, 60, 30, 0]
            is_active = bool(getattr(self, 'sleep_timer_active', False))
            cur = int(getattr(self, 'current_sleep_minutes', 0) or 0)

            if not is_active:
                nxt = steps[0]
            else:
                if cur in steps:
                    i = steps.index(cur)
                    nxt = steps[(i + 1) % len(steps)]
                else:
                    # If current is non-standard, choose the next lower standard step.
                    nxt = 0
                    for m in steps:
                        if m == 0:
                            continue
                        if cur > m:
                            nxt = m
                            break

            if nxt <= 0:
                self.cancel_sleep_timer()
            else:
                self.start_sleep_timer(nxt)
        except Exception as e:
            print(f"DEBUG: cycle_sleep_timer_quick failed: {e}")

    # Old method removed as logic is now inside show_sleep_timer_dropdown
    def update_sleep_menu_state(self):
        try:
            # Only relevant while the dropdown is visible.
            if not hasattr(self, 'sleep_dropdown') or not self.sleep_dropdown or not self.sleep_dropdown.isVisible():
                return
            if not hasattr(self, '_sleep_dropdown_buttons') or not isinstance(self._sleep_dropdown_buttons, dict):
                return

            check_icon = getattr(self, '_sleep_check_icon', None)
            empty_icon = getattr(self, '_sleep_empty_icon', None)
            if check_icon is None or empty_icon is None:
                return

            is_active = bool(getattr(self, 'sleep_timer_active', False))
            active_mins = int(getattr(self, 'current_sleep_minutes', 0) or 0)

            for mins, btn in list(self._sleep_dropdown_buttons.items()):
                if btn is None:
                    continue

                if mins == 0:
                    is_checked = not is_active
                else:
                    is_checked = is_active and (mins == active_mins)

                btn.setIcon(check_icon if is_checked else empty_icon)
        except Exception:
            return

    def _ensure_sleep_status_label(self):
        if hasattr(self, 'lbl_sleep_status') and self.lbl_sleep_status is not None:
            return

        self.lbl_sleep_status = QLabel("")
        self.lbl_sleep_status.setStyleSheet("color: white; padding-right: 10px;")
        self.statusBar().addPermanentWidget(self.lbl_sleep_status)

    def _is_show_playing(self):
        try:
            if not hasattr(self, 'player') or not self.player or not self.player.mpv:
                return False
            mpv = self.player.mpv
            paused = bool(getattr(mpv, 'pause', True))
            core_idle = bool(getattr(mpv, 'core_idle', True))
            # Keep this consistent with existing UI logic (show_controls/hide_controls).
            return (not paused) and (not core_idle)
        except Exception:
            return False

    def _sleep_remaining_minutes(self):
        if not self.sleep_timer_active or self.sleep_remaining_ms <= 0:
            return 0
        # Show remaining as minutes (ceiling)
        return max(1, int((self.sleep_remaining_ms + 59999) // 60000))

    def _update_sleep_timer_ui(self):
        self._ensure_sleep_status_label()

        if not self.sleep_timer_active:
            self.lbl_sleep_status.setText("")
            if hasattr(self, 'play_mode_widget') and hasattr(self.play_mode_widget, 'btn_sleep_timer'):
                self.play_mode_widget.btn_sleep_timer.setText("SLEEP\nOFF")
            return

        remaining_min = self._sleep_remaining_minutes()
        self.lbl_sleep_status.setText(f"Sleep in {remaining_min}m")
        if hasattr(self, 'play_mode_widget') and hasattr(self.play_mode_widget, 'btn_sleep_timer'):
            self.play_mode_widget.btn_sleep_timer.setText(f"SLEEP\n{remaining_min}m")

    def _pause_sleep_countdown(self):
        self._sleep_last_tick = None
        if self.sleep_countdown_timer.isActive():
            self.sleep_countdown_timer.stop()

    def _resume_sleep_countdown_if_needed(self):
        if not self.sleep_timer_active or self.sleep_remaining_ms <= 0:
            self._pause_sleep_countdown()
            return
        if not self._is_show_playing():
            self._pause_sleep_countdown()
            return
        if not self.sleep_countdown_timer.isActive():
            self._sleep_last_tick = time.monotonic()
            self.sleep_countdown_timer.start()

    def _on_sleep_countdown_tick(self):
        if not self.sleep_timer_active:
            self._pause_sleep_countdown()
            self._update_sleep_timer_ui()
            return

        if not self._is_show_playing():
            self._pause_sleep_countdown()
            self._update_sleep_timer_ui()
            return

        now = time.monotonic()
        if self._sleep_last_tick is None:
            self._sleep_last_tick = now
            self._update_sleep_timer_ui()
            return

        elapsed_ms = int((now - self._sleep_last_tick) * 1000)
        self._sleep_last_tick = now
        if elapsed_ms <= 0:
            self._update_sleep_timer_ui()
            return

        self.sleep_remaining_ms = max(0, int(self.sleep_remaining_ms) - elapsed_ms)
        self._update_sleep_timer_ui()
        if self.sleep_remaining_ms <= 0:
            self.on_sleep_timer()

    def start_sleep_timer(self, minutes):
        try:
            minutes = int(minutes) if minutes is not None else 0
            if minutes <= 0:
                minutes = int(getattr(self, 'sleep_timer_default_minutes', 180))

            self.current_sleep_minutes = minutes
            self.sleep_timer_active = True

            # Exposure scoring: sleep timer ON enables diminishing episode deltas.
            try:
                self.playlist_manager.set_sleep_timer_active_for_exposure(True)
            except Exception:
                pass
            self.sleep_remaining_ms = int(minutes * 60 * 1000)
            self._sleep_last_tick = None
            
            print(f"DEBUG: Start Timer {minutes}m")

            self._update_sleep_timer_ui()

            # Nullify any prior countdown and restart from the new duration.
            self._pause_sleep_countdown()
            self._resume_sleep_countdown_if_needed()
            
            # Immediate sync of menu state
            self.update_sleep_menu_state()

            # Sync Welcome Screen Toggle (if started from Menu)
            if hasattr(self, 'welcome_screen'):
                if not self.welcome_screen.is_sleep_on:
                     self.welcome_screen.is_sleep_on = True
                     self.welcome_screen.update_checkbox(self.welcome_screen.btn_sleep_check, True)
        except Exception as e:
            print(f"Error starting timer: {e}")

    def cancel_sleep_timer(self):
        try:
            self.sleep_timer_active = False

            # Exposure scoring: sleep timer OFF => constant episode deltas.
            try:
                self.playlist_manager.set_sleep_timer_active_for_exposure(False)
            except Exception:
                pass
            self.sleep_remaining_ms = 0
            self._pause_sleep_countdown()
            self._update_sleep_timer_ui()
            
            # Immediate sync
            self.update_sleep_menu_state()
            
            if hasattr(self, 'welcome_screen'):
                if self.welcome_screen.is_sleep_on:
                     self.welcome_screen.is_sleep_on = False
                     self.welcome_screen.update_checkbox(self.welcome_screen.btn_sleep_check, False)
        except Exception as e:
            print(f"Error cancelling timer: {e}")
            if hasattr(self, 'welcome_screen') and self.welcome_screen.is_sleep_on:
                self.welcome_screen.is_sleep_on = False
                self.welcome_screen.update_checkbox(self.welcome_screen.btn_sleep_check, False)

    def set_mode(self, index):
        self.mode_stack.setCurrentIndex(index)
        self.update_menu_mode_state()

        # Ensure native playback surfaces (mpv video container) and bump cards never
        # bleed through non-player screens.
        try:
            self._sync_playback_surface_visibility_for_mode()
        except Exception:
            pass
        
        # If switching to Play mode (Index 2), refresh list
        if index == 2:
            self.play_mode_widget.refresh_episode_list()
            # If no playlist is loaded, ensure menu is open so user can pick one
            if not self.playlist_manager.current_playlist and not self.play_mode_widget.sidebar_visible:
                 self.play_mode_widget.toggle_sidebar()

    def _sync_playback_surface_visibility_for_mode(self):
        """Prevent mpv/bump widgets from overlaying non-player modes."""
        try:
            on_player = int(self.mode_stack.currentIndex()) == 2
        except Exception:
            on_player = False

        if not on_player:
            # Hide all playback surfaces explicitly. This is important on Windows where
            # native child windows can draw above other Qt widgets.
            try:
                if hasattr(self, 'overlay_label'):
                    self.overlay_label.setVisible(False)
            except Exception:
                pass
            try:
                if hasattr(self, 'play_mode_widget') and self.play_mode_widget is not None:
                    self.play_mode_widget.controls_widget.setVisible(False)
            except Exception:
                pass
            try:
                if hasattr(self, 'video_container') and self.video_container is not None:
                    self.video_container.setVisible(False)
            except Exception:
                pass
            try:
                if hasattr(self, 'bump_widget') and self.bump_widget is not None:
                    self.bump_widget.setVisible(False)
            except Exception:
                pass
            return

        # On player: allow the stack to decide what is visible, but ensure both
        # stack pages are eligible to paint.
        try:
            if hasattr(self, 'video_container') and self.video_container is not None:
                self.video_container.setVisible(True)
        except Exception:
            pass
        try:
            if hasattr(self, 'bump_widget') and self.bump_widget is not None:
                self.bump_widget.setVisible(True)
        except Exception:
            pass

        # Controls visibility is managed by show_controls/hide_controls.
        try:
            self.show_controls()
        except Exception:
            pass

    def go_to_welcome(self):
        self.set_mode(0)

    def update_menu_mode_state(self):
        if not hasattr(self, 'mode_stack'):
            return
        idx = int(self.mode_stack.currentIndex())

        if hasattr(self, 'btn_mode_welcome'):
            self.btn_mode_welcome.setChecked(idx == 0)
        if hasattr(self, 'btn_mode_edit'):
            self.btn_mode_edit.setChecked(idx == 1)
        if hasattr(self, 'btn_mode_play'):
            self.btn_mode_play.setChecked(idx == 2)
        if hasattr(self, 'btn_mode_bumps'):
            self.btn_mode_bumps.setChecked(idx == 3)

        if hasattr(self, 'bumps_mode_widget'):
            try:
                self.bumps_mode_widget.refresh_status()
            except Exception:
                pass
        
    # --- Mode Logic ---
    
    def add_source_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add Shows Directory")
        if folder:
            structure = self.playlist_manager.add_source(folder)
            self.populate_library_cumulative(structure)

    def populate_library_cumulative(self, full_structure):
        self.edit_mode_widget.library_tree.clear()
        
        for source_path, groups in full_structure.items():
            source_root = QTreeWidgetItem(self.edit_mode_widget.library_tree)
            base = os.path.basename(source_path)
            parent = os.path.basename(os.path.dirname(source_path))
            if base.lower() in ('episodes', 'episodesl') and parent:
                source_name = f"{parent}/{base}"
            else:
                source_name = base or source_path
            source_root.setText(0, f"[{source_name}]")
            
            for group, items in groups.items():
                if group == "Root":
                    parent = source_root
                else:
                    parent = QTreeWidgetItem(source_root)
                    parent.setText(0, group)
                    
                for item in items:
                    child = QTreeWidgetItem(parent)
                    child.setText(0, item['name'])
                    child.setData(0, Qt.UserRole, item['path'])
                    
        self.edit_mode_widget.library_tree.expandAll()

    def clear_library(self):
        self.playlist_manager.clear_library()
        self.edit_mode_widget.library_tree.clear()

    def choose_interstitial_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Interludes Directory")
        if folder:
            self._set_interstitials_folder(folder, persist=True)
            QMessageBox.information(self, "Interludes", f"Found {len(self.playlist_manager.interstitials)} items.")

    def set_interludes_folder(self, folder: str):
        """Set the global interludes folder (persisted in user settings)."""
        try:
            folder = str(folder or '').strip()
        except Exception:
            folder = ''
        if not folder:
            return
        try:
            self._set_interstitials_folder(folder, persist=True)
        except Exception:
            return
        try:
            if hasattr(self, 'bumps_mode_widget'):
                self.bumps_mode_widget.refresh_status()
        except Exception:
            pass

    def _set_interstitials_folder(self, folder: str, *, persist: bool):
        """Scan + set interludes folder, and update the filesystem watcher."""
        try:
            folder = str(folder or '').strip()
        except Exception:
            folder = ''
        if not folder:
            return

        try:
            self.playlist_manager.scan_interstitials(folder)
        except Exception:
            return

        try:
            # Keep legacy field for internal use.
            self._interstitials_dir = folder
        except Exception:
            pass

        if persist:
            try:
                # Save both names for backward compatibility.
                self._settings['interlude_folder'] = str(folder)
                self._settings['interstitial_folder'] = str(folder)
                self._save_user_settings()
            except Exception:
                pass

        # Update watcher paths (watch exactly this folder).
        try:
            existing = list(self._interstitial_watcher.directories())
            if existing:
                self._interstitial_watcher.removePaths(existing)
        except Exception:
            pass
        try:
            if os.path.isdir(folder):
                self._interstitial_watcher.addPath(folder)
        except Exception:
            pass

    def _on_interstitials_dir_changed(self, folder: str):
        # Re-scan so interstitial count and queue stay current.
        try:
            folder = str(folder or '').strip()
        except Exception:
            folder = ''
        if not folder:
            return
        try:
            self.playlist_manager.scan_interstitials(folder)
        except Exception:
            pass

    def choose_bump_scripts(self):
        # Scripts are loaded from the app-local `bumps/` folder (like `playlists/`).
        folder = getattr(self, 'bump_scripts_dir', None) or get_local_bumps_scripts_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass

        self.playlist_manager.bump_manager.load_bumps(folder)
        # Persist any one-time exposure seeds immediately.
        try:
            if bool(getattr(self.playlist_manager.bump_manager, '_script_exposure_seeded_last_changed', False)):
                self.playlist_manager._exposure_dirty = True
                self.playlist_manager._save_exposure_scores(force=True)
        except Exception:
            pass
        QMessageBox.information(
            self,
            "Bumps",
            f"Loaded {len(self.playlist_manager.bump_manager.bump_scripts)} scripts from:\n{folder}\n\n"
            "Drop your script files into that folder and click this button again to reload.",
        )
        if hasattr(self, 'bumps_mode_widget'):
            self.bumps_mode_widget.refresh_status()
            
    def choose_bump_music(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Bump Music Directory")
        if folder:
            self.playlist_manager.bump_manager.scan_music(folder)
            # Persist any one-time exposure seeds immediately.
            try:
                if bool(getattr(self.playlist_manager.bump_manager, '_music_exposure_seeded_last_changed', False)):
                    self.playlist_manager._exposure_dirty = True
                    self.playlist_manager._save_exposure_scores(force=True)
            except Exception:
                pass
            QMessageBox.information(self, "Bumps", f"Found {len(self.playlist_manager.bump_manager.music_files)} music files.")
            if hasattr(self, 'bumps_mode_widget'):
                self.bumps_mode_widget.refresh_status()

    def choose_bump_images(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Bump Images Directory")
        if not folder:
            return

        self.bump_images_dir = folder
        try:
            self._settings['bump_images_dir'] = folder
            self._save_user_settings()
        except Exception:
            pass

        try:
            self.playlist_manager.bump_manager.bump_images_dir = folder
        except Exception:
            pass

        # Reload scripts so any <img ...> tags re-resolve against the new base dir.
        try:
            self._reload_bump_scripts_for_assets()
        except Exception:
            pass

        if hasattr(self, 'bumps_mode_widget'):
            try:
                self.bumps_mode_widget.refresh_status()
            except Exception:
                pass

    def choose_bump_audio_fx(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Bump Audio FX Directory")
        if not folder:
            return

        self.bump_audio_fx_dir = folder
        try:
            self._settings['bump_audio_fx_dir'] = folder
            self._save_user_settings()
        except Exception:
            pass

        try:
            self.playlist_manager.bump_manager.bump_audio_fx_dir = folder
        except Exception:
            pass

        # Reload scripts so any <sound ...> tags re-resolve against the new base dir.
        try:
            self._reload_bump_scripts_for_assets()
        except Exception:
            pass

        if hasattr(self, 'bumps_mode_widget'):
            try:
                self.bumps_mode_widget.refresh_status()
            except Exception:
                pass

    def set_bumps_enabled(self, enabled):
        self.bumps_enabled = bool(enabled)

        # Interludes are only meaningful when TV Vibes is on.
        try:
            if hasattr(self, 'edit_mode_widget') and hasattr(self.edit_mode_widget, 'chk_interstitials'):
                w = self.edit_mode_widget.chk_interstitials
                try:
                    w.setEnabled(bool(self.bumps_enabled))
                except Exception:
                    pass
                if not bool(self.bumps_enabled):
                    # Avoid confusion: if Vibes are OFF, force Interludes OFF.
                    try:
                        w.setChecked(False)
                    except Exception:
                        pass
        except Exception:
            pass

    def _reload_bump_scripts_for_assets(self):
        """Re-parse bump scripts so <img> and <sound> tags resolve correctly.

        Image/audio-FX paths are resolved at script parse time. If the user (or
        auto-config) sets `bump_images_dir` / `bump_audio_fx_dir` after scripts
        have already been loaded, previously-parsed cards can keep stale/relative
        paths (e.g. "campfire.png") that won't render/play.
        """
        bump_mgr = getattr(getattr(self, 'playlist_manager', None), 'bump_manager', None)
        if not bump_mgr:
            return

        scripts_dir = getattr(self, 'bump_scripts_dir', None)
        if not scripts_dir:
            try:
                scripts_dir = get_local_bumps_scripts_dir()
            except Exception:
                scripts_dir = None
        if not scripts_dir:
            return
        try:
            if not os.path.isdir(str(scripts_dir)):
                return
        except Exception:
            return

        # Clear image cache so newly-resolved absolute paths can be loaded.
        try:
            if hasattr(self, '_bump_prefetch_lock') and hasattr(self, '_bump_prefetch_images'):
                with self._bump_prefetch_lock:
                    self._bump_prefetch_images = {}
        except Exception:
            pass

        try:
            bump_mgr.load_bumps(str(scripts_dir))
        except Exception:
            return

        # Persist any one-time script exposure seeds immediately.
        try:
            if bool(getattr(bump_mgr, '_script_exposure_seeded_last_changed', False)):
                self.playlist_manager._exposure_dirty = True
                self.playlist_manager._save_exposure_scores(force=True)
        except Exception:
            pass

        if hasattr(self, 'bumps_mode_widget'):
            try:
                self.bumps_mode_widget.refresh_status()
            except Exception:
                pass

    def add_dropped_items(self, items):
        candidates = []
        def recurse(tree_item):
            path = tree_item.data(0, Qt.UserRole)
            if path:
                candidates.append(path)
            else:
                for i in range(tree_item.childCount()):
                    recurse(tree_item.child(i))

        for item in items:
            recurse(item)
            
        for path in candidates:
            # Basic append
            self.playlist_manager.current_playlist.append({'type': 'video', 'path': path})
            
        self.edit_mode_widget.refresh_playlist_list()

    def add_selected_to_playlist(self):
        items = self.edit_mode_widget.library_tree.selectedItems()
        self.add_dropped_items(items)

    def remove_from_playlist(self):
        lst = self.edit_mode_widget.playlist_list
        rows = sorted([lst.row(item) for item in lst.selectedItems()], reverse=True)
        for row in rows:
            if row < len(self.playlist_manager.current_playlist):
                del self.playlist_manager.current_playlist[row]
        self.edit_mode_widget.refresh_playlist_list()

    def show_playlist_context_menu(self, pos):
        # Shown on Edit Widget
        menu = QMenu()
        rem = QAction("Remove", self)
        rem.triggered.connect(self.remove_from_playlist)
        menu.addAction(rem)
        menu.exec(self.edit_mode_widget.playlist_list.mapToGlobal(pos))

    def generate_playlist_logic(self, shuffle_mode, interstitials, bumps):
        self.current_playlist_filename = None
        try:
            self.playlist_manager.clear_frequency_settings()
        except Exception:
            pass

        # Guardrail: Interludes (interstitials) only inject if we have scanned files.
        # Interludes are a GLOBAL setting (not per-playlist).
        try:
            if bool(interstitials) and not list(getattr(self.playlist_manager, 'interstitials', []) or []):
                folder = str(getattr(self.playlist_manager, 'interstitial_folder', '') or '').strip()
                hint = (
                    "Interludes are ON, but no interlude videos were found.\n\n"
                    "Set the Interludes Folder in Settings (or use 'Set Interludes Folder'), then Generate again.\n"
                    "Supported video types include .mp4/.mkv/.avi/.webm." 
                )
                if folder:
                    hint = hint + f"\n\nCurrent folder: {folder}"
                QMessageBox.information(self, "Interludes", hint)
        except Exception:
            pass

        # Generate playlist contents (injections are decided here).
        self.playlist_manager.generate_playlist(None, False, interstitials, bumps)
        self.playlist_manager.reset_playback_state()
        self.set_shuffle_mode(shuffle_mode)

        try:
            self._persist_resume_state(force=True, reason='generate_playlist')
        except Exception:
            pass
        
        self.edit_mode_widget.refresh_playlist_list()
        
        if self.playlist_manager.current_playlist and not self.player.mpv.core_idle:
             pass 

    def set_shuffle_mode(self, mode, update_ui=True):
        # Rebuild queue (watched episodes become effectively "unwatched") but do not change current episode.
        self.playlist_manager.set_shuffle_mode(mode, current_index=self.playlist_manager.current_index, rebuild=True)

        if update_ui:
            label = mode.upper() if mode != 'standard' else 'STANDARD'
            self.play_mode_widget.btn_shuffle.setText(label)
            if hasattr(self, 'edit_mode_widget') and hasattr(self.edit_mode_widget, 'btn_shuffle_mode'):
                self.edit_mode_widget.shuffle_mode = mode
                self.edit_mode_widget.btn_shuffle_mode.setText(f"Shuffle: {mode.title()}")

    def cycle_shuffle_mode(self):
        self.set_shuffle_mode(_next_shuffle_mode(self.playlist_manager.shuffle_mode))

    def save_playlist(self):
        if not self.playlist_manager.current_playlist:
            QMessageBox.warning(self, "Cannot Save", "Playlist is empty.")
            return

        files_dir = get_local_playlists_dir()
        os.makedirs(files_dir, exist_ok=True)
        filename, _ = QFileDialog.getSaveFileName(self, "Save Playlist", files_dir, "Sleepy Playlist (*.json)")
        if filename:
            if not filename.lower().endswith(".json"):
                filename += ".json"
            
            data = {
                'playlist': [
                    item for item in self.playlist_manager.current_playlist
                    if not (isinstance(item, dict) and item.get('type') == 'bump')
                ],
                # Backward-compatible boolean (standard shuffle == True)
                'shuffle_default': (self.playlist_manager.shuffle_mode != 'off'),
                # Preferred persisted value
                'shuffle_mode': self.playlist_manager.shuffle_mode,
                'frequency_settings': self.playlist_manager.get_frequency_settings_for_save(),
            }
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
            self.current_playlist_filename = filename
            QMessageBox.information(self, "Success", "Playlist saved!")
            self.play_mode_widget.refresh_playlists()
            try:
                self.edit_mode_widget.refresh_saved_playlists_list()
            except Exception:
                pass

            # Clear the working playlist after save so the user can build a new one.
            # If media is currently loaded in the player, don't clear to avoid breaking playback.
            try:
                core_idle = bool(getattr(getattr(self.player, 'mpv', None), 'core_idle', True))
            except Exception:
                core_idle = True
            if core_idle:
                self.clear_playlist()

    def load_playlist(self, filename=False, auto_play=False):
        if not filename:
            files_dir = get_local_playlists_dir()
            os.makedirs(files_dir, exist_ok=True)
            filename, _ = QFileDialog.getOpenFileName(self, "Load Playlist", files_dir, "Sleepy Playlist (*.json)")

        # Resolve local path if possible.
        try:
            if filename:
                playlist_io.reject_url_source(str(filename))
                filename = resolve_playlist_path(filename)
        except Exception:
            pass

        playlist_source = filename

        if playlist_source and os.path.exists(str(playlist_source)):
            try:
                result = playlist_io.load_playlist_json(str(playlist_source))
                data = result.data

                # Portable-mode correctness: if this is an auto-generated playlist
                # and the external-drive files have changed (deletions/additions),
                # rebuild it against the current on-disk folder before loading.
                try:
                    is_portable = (str(getattr(self, 'playback_mode', 'portable') or 'portable').strip().lower() == 'portable')
                except Exception:
                    is_portable = False

                if is_portable:
                    try:
                        auto_generated = bool((data or {}).get('auto_generated', False))
                    except Exception:
                        auto_generated = False
                    try:
                        src_folder = str((data or {}).get('source_folder', '') or '').strip()
                    except Exception:
                        src_folder = ''

                    if auto_generated and src_folder and os.path.isdir(src_folder):
                        try:
                            pl_name = os.path.basename(str(playlist_source))
                        except Exception:
                            pl_name = ''
                        if pl_name:
                            try:
                                updated = _write_auto_playlist_json(
                                    pl_name,
                                    src_folder,
                                    default_shuffle_mode='standard',
                                    prefer_existing_playlist_paths=False,
                                )
                            except Exception:
                                updated = False

                            if updated:
                                try:
                                    result = playlist_io.load_playlist_json(str(playlist_source))
                                    data = result.data
                                except Exception:
                                    pass

                self.current_playlist_filename = filename

                try:
                    self.playlist_manager.set_frequency_settings_from_playlist_data(data)
                except Exception:
                    pass
                
                self.playlist_manager.current_playlist = data.get('playlist', [])
                self.playlist_manager.reset_playback_state()

                # Restore shuffle mode (string preferred, bool fallback)
                mode = data.get('shuffle_mode', None)
                if mode is None:
                    shuffle_default = data.get('shuffle_default', False)
                    mode = 'standard' if shuffle_default else 'off'
                self.set_shuffle_mode(mode)

                # Don't force start at index 0; let shuffle decide the first episode.
                # Keep current_index at -1 until the user starts playback (or auto_play starts it).
                self.playlist_manager.current_index = -1
                self.playlist_manager.rebuild_queue(current_index=self.playlist_manager.current_index)

                # Arm auto-resume for this playlist (no prompts). It triggers only
                # when the user starts playback for the default start index.
                try:
                    self._arm_auto_resume_for_playlist(str(playlist_source or ''))
                except Exception:
                    pass

                try:
                    self._persist_resume_state(force=True, reason='load_playlist')
                except Exception:
                    pass
                
                self.edit_mode_widget.refresh_playlist_list()
                self.play_mode_widget.refresh_episode_list()
                
                self.set_mode(2) # Switch to play
                
                if auto_play:
                    start_idx = 0
                    if self.playlist_manager.shuffle_mode != 'off':
                        start_idx = self.playlist_manager.get_next_index()
                        if start_idx == -1:
                            start_idx = 0
                    self.play_index(start_idx)

                    if self.play_mode_widget.sidebar_visible:
                        self.play_mode_widget.toggle_sidebar()
                
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load: {e}")

    def load_playlist_into_editor(self, filename=False):
        if not filename:
            files_dir = get_local_playlists_dir()
            os.makedirs(files_dir, exist_ok=True)
            filename, _ = QFileDialog.getOpenFileName(self, "Load Playlist", files_dir, "Sleepy Playlist (*.json)")

        try:
            if filename:
                playlist_io.reject_url_source(str(filename))
                filename = resolve_playlist_path(filename)
        except Exception:
            pass

        playlist_source = filename

        if playlist_source and os.path.exists(str(playlist_source)):
            try:
                result = playlist_io.load_playlist_json(str(playlist_source))
                data = result.data

                self.current_playlist_filename = filename

                try:
                    self.playlist_manager.set_frequency_settings_from_playlist_data(data)
                except Exception:
                    pass

                self.playlist_manager.current_playlist = data.get('playlist', [])
                self.playlist_manager.reset_playback_state()

                mode = data.get('shuffle_mode', None)
                if mode is None:
                    shuffle_default = data.get('shuffle_default', False)
                    mode = 'standard' if shuffle_default else 'off'
                self.set_shuffle_mode(mode, update_ui=True)

                self.playlist_manager.current_index = -1
                self.playlist_manager.rebuild_queue(current_index=self.playlist_manager.current_index)

                try:
                    self._persist_resume_state(force=True, reason='load_playlist_editor')
                except Exception:
                    pass

                self.edit_mode_widget.refresh_playlist_list()
                try:
                    self.edit_mode_widget.refresh_saved_playlists_list()
                except Exception:
                    pass

            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load: {e}")

    def persist_current_playlist_frequency_settings(self):
        """Persist frequency settings into the playlist file if we have one."""
        filename = getattr(self, 'current_playlist_filename', None)
        if not filename:
            return
        if not os.path.exists(filename):
            return

        try:
            with open(filename, 'r') as f:
                data = json.load(f)
        except Exception:
            data = {}

        try:
            data['frequency_settings'] = self.playlist_manager.get_frequency_settings_for_save()
        except Exception:
            return

        # Keep file consistent with current editor state.
        try:
            data['playlist'] = [
                item for item in self.playlist_manager.current_playlist
                if not (isinstance(item, dict) and item.get('type') == 'bump')
            ]
            data['shuffle_default'] = (self.playlist_manager.shuffle_mode != 'off')
            data['shuffle_mode'] = self.playlist_manager.shuffle_mode
        except Exception:
            pass

        try:
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def show_clear_viewing_history_dialog(self):
        playlists = []
        try:
            playlists = list(self.playlist_manager.list_saved_playlists() or [])
        except Exception:
            playlists = []

        choices = ["Clear All"] + sorted([p for p in playlists if isinstance(p, str)], key=natural_sort_key)
        choice, ok = QInputDialog.getItem(self, "Clear Viewing History", "Choose what to clear:", choices, 0, False)
        if not ok or not choice:
            return

        if choice == "Clear All":
            resp = QMessageBox.question(self, "Confirm", "Clear viewing history for ALL episodes?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                return
            self.playlist_manager.clear_episode_exposure_scores_all()
            QMessageBox.information(self, "History Cleared", "Cleared episode viewing history.")
            return

        playlist_path = os.path.join('playlists', choice)
        if not os.path.exists(playlist_path):
            QMessageBox.warning(self, "Not Found", f"Playlist not found: {choice}")
            return

        try:
            with open(playlist_path, 'r') as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read playlist: {e}")
            return

        paths = []
        for it in list(data.get('playlist', []) or []):
            if isinstance(it, dict):
                if it.get('type', 'video') == 'video':
                    p = it.get('path')
                    if p:
                        paths.append(p)
            elif isinstance(it, str):
                paths.append(it)

        if not paths:
            QMessageBox.information(self, "Nothing To Clear", "That playlist has no episodes.")
            return

        resp = QMessageBox.question(self, "Confirm", f"Clear viewing history for episodes in '{choice}'?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resp != QMessageBox.Yes:
            return

        removed = self.playlist_manager.clear_episode_exposure_scores_for_paths(paths)
        QMessageBox.information(self, "History Cleared", f"Cleared history for {removed} episode(s).")

    def clear_playlist(self):
        self.playlist_manager.current_playlist = []
        self.playlist_manager.reset_playback_state()
        try:
            self.playlist_manager.clear_frequency_settings()
        except Exception:
            pass
        self.current_playlist_filename = None
        self.edit_mode_widget.refresh_playlist_list()
        if hasattr(self, 'play_mode_widget'):
            try:
                self.play_mode_widget.refresh_episode_list()
            except Exception:
                pass

    # --- Playback Control ---

    def play_from_library(self, item, column):
        path = item.data(0, Qt.UserRole)
        if path:
            self.stop_playback()
            self.current_playlist_filename = None
            try:
                self.playlist_manager.clear_frequency_settings()
            except Exception:
                pass
            self.playlist_manager.current_playlist = [{'type': 'video', 'path': path}]
            self.playlist_manager.current_index = 0
            self.edit_mode_widget.refresh_playlist_list()
            self.play_mode_widget.refresh_episode_list()
            self.set_mode(1) # Switch to play

            # Auto-resume if this is the same single-file target.
            try:
                if self._maybe_auto_resume_for_target(str(path)):
                    return
            except Exception:
                pass

            self.play_index(0)

            if self.play_mode_widget.sidebar_visible:
                self.play_mode_widget.toggle_sidebar()

    def _should_suppress_auto_nav_ui(self) -> bool:
        try:
            if bool(getattr(self, '_advancing_from_eof', False)):
                return True
        except Exception:
            pass
        try:
            if bool(getattr(self, '_advancing_from_bump_end', False)):
                return True
        except Exception:
            pass
        return False

    def play_index(self, index, record_history=True, bypass_bump_gate=False, *, suppress_ui: bool = False):
        pm = self.playlist_manager

        # If the user is starting the same show again, silently resume from disk
        # for the default start index (no prompts).
        try:
            if not bypass_bump_gate:
                if self._maybe_auto_resume_on_first_play(int(index)):
                    return
        except Exception:
            pass

        try:
            suppress_ui = bool(suppress_ui) or self._should_suppress_auto_nav_ui()
        except Exception:
            suppress_ui = bool(suppress_ui)
        try:
            if index is not None and int(index) != int(getattr(pm, 'current_index', -1)):
                self._maybe_apply_episode_skip_penalty()
        except Exception:
            pass

        # New track start: clear per-start penalty bookkeeping.
        self._skip_penalty_applied_for_start = None
        if 0 <= index < len(pm.current_playlist):
            # Cut off startup ambient audio as soon as playback begins.
            self._stop_startup_ambient()
            self.stop_bump_playback()
            pm.current_index = index
            item = pm.current_playlist[index]

            # Global bump gate: when enabled, play a bump before any episode playback,
            # including the first episode (even if we're starting mid-queue).
            if (
                self.bumps_enabled
                and not bypass_bump_gate
                and self.video_stack.currentIndex() != 1
                and getattr(self, '_pending_next_index', None) is None
            ):
                try:
                    is_episode = pm.is_episode_item(item)
                except Exception:
                    is_episode = False

                # Only gate real episodes (not interstitials/explicit bump items).
                if is_episode:
                    bump_item = None
                    try:
                        bump_item = pm.bump_manager.get_next_bump()
                    except Exception:
                        bump_item = None

                    # If no eligible bump exists (e.g., no music long enough), just play.
                    if bump_item:
                        self._pending_next_index = int(index)
                        self._pending_next_record_history = bool(record_history)
                        self._play_bump_with_optional_interstitial(bump_item)
                        return

            # Global bumps are optional. If disabled, skip bump items.
            if isinstance(item, dict) and item.get('type') == 'bump' and not self.bumps_enabled:
                QTimer.singleShot(0, self.play_next)
                return

            # Reset EOF watchdog state for the new track.
            self._handled_eof_for_index = None

            # Reset progress/stall timers for the new track.
            try:
                now = float(time.monotonic())
                self._time_pos_last_update_mono = now
                self._time_pos_last_progress_mono = now
                self._time_pos_last_value = None
            except Exception:
                pass
            self._play_start_monotonic = time.monotonic()
            self._played_since_start = False

            # Record playback history for Prev navigation.
            if record_history:
                try:
                    pm.record_playback_index(index)
                except Exception:
                    pass

            # Record episode start for shuffle avoidance.
            try:
                pm.mark_episode_started(index, sleep_timer_on=bool(getattr(self, 'sleep_timer_active', False)))
            except Exception:
                pass
            
            # Highlight in lists
            # We can't easily highlight the sidebar list without refreshing or sophisticated mapping, 
            # so just refresh the sidebar list to show ">" indicator
            self.play_mode_widget.refresh_episode_list()
            self.play_mode_widget.episode_list_widget.setCurrentRow(index)
            
            if isinstance(item, dict):
                itype = item.get('type', 'video')
                if itype == 'video' or itype == 'interstitial':
                    path = item['path']
                    self.video_stack.setCurrentIndex(0)
                    try:
                        if (not suppress_ui) and (not self.isFullScreen()):
                            self._show_episode_overlay(auto_hide_seconds=4.0)
                    except Exception:
                        pass
                    target = self._resolve_video_play_target(path)
                    try:
                        exists = None
                        try:
                            # Don't do expensive checks for non-local targets.
                            t = str(target or '')
                            if t and (len(t) > 2) and (t[1:3] == ':\\' or t.startswith('\\\\')):
                                exists = os.path.exists(t)
                        except Exception:
                            exists = None
                        print(f"DEBUG: play_index video kind={itype} index={index} src={path} target={target} exists={exists}")
                    except Exception:
                        pass
                    try:
                        self._last_play_target = str(target or '')
                        self._last_play_source_path = str(path or '')
                    except Exception:
                        self._last_play_target = None
                        self._last_play_source_path = None
                    # Skip slow network file existence check - let mpv handle missing files
                    # In web mode with manifest, we trust the file exists
                    self.player.play(target)
                    self._played_since_start = True
                    prefix = "[IL]" if itype == 'interstitial' else ""
                    self.setWindowTitle(f"Sleepy Shows - {prefix} {os.path.basename(path)}")
                    try:
                        self._log_event('play_start', kind=itype, source_path=str(path or ''), target=str(target or ''), index=int(index))
                    except Exception:
                        pass
                    self._sync_keep_awake()
                    self._resume_sleep_countdown_if_needed()
                    QTimer.singleShot(200, self._resume_sleep_countdown_if_needed)
                elif itype == 'bump':
                    try:
                        self._activate_prefetched_bump_assets(int(index))
                    except Exception:
                        pass
                    self._play_bump_with_optional_interstitial(item)
            else:
                 # Legacy
                 self.video_stack.setCurrentIndex(0)
                 try:
                     if (not suppress_ui) and (not self.isFullScreen()):
                         self._show_episode_overlay(auto_hide_seconds=4.0)
                 except Exception:
                     pass
                 target = self._resolve_video_play_target(item)
                 try:
                     exists = None
                     try:
                         t = str(target or '')
                         if t and (len(t) > 2) and (t[1:3] == ':\\' or t.startswith('\\\\')):
                             exists = os.path.exists(t)
                     except Exception:
                         exists = None
                     print(f"DEBUG: play_index video kind=video index={index} src={item} target={target} exists={exists}")
                 except Exception:
                     pass
                 try:
                     self._last_play_target = str(target or '')
                     self._last_play_source_path = str(item or '')
                 except Exception:
                     self._last_play_target = None
                     self._last_play_source_path = None
                 # IMPORTANT: Avoid synchronous filesystem existence checks here.
                 # On network mounts these can block UI for many seconds.
                 # Let mpv attempt the open and report errors via errorOccurred.
                 self.player.play(target)
                 self._played_since_start = True
                 self.setWindowTitle(f"Sleepy Shows - {os.path.basename(item)}")
                 try:
                     self._log_event('play_start', kind='video', source_path=str(item or ''), target=str(target or ''), index=int(index))
                 except Exception:
                     pass
                 self._sync_keep_awake()
                 self._resume_sleep_countdown_if_needed()
                 QTimer.singleShot(200, self._resume_sleep_countdown_if_needed)
        
        if not suppress_ui:
            self.show_controls()

    def play_bump(self, bump_item):
        # Safety: if a prior interstitial preroll left a pending bump behind,
        # clear it now so future prerolls don't get blocked.
        try:
            self._pending_bump_item = None
        except Exception:
            pass

        # Bumps are not episodes; always hide the episode title overlay.
        self._hide_episode_overlay()
        script = bump_item.get('script')
        audio = bump_item.get('audio')
        video = bump_item.get('video')
        try:
            video_inclusive = bool(bump_item.get('video_inclusive', False))
        except Exception:
            video_inclusive = False

        # Exposure scoring: bumps are transient, but their components accrue exposure.
        try:
            self.playlist_manager.note_bump_played(bump_item)
        except Exception:
            pass

        # Ensure bump manager eventually has a list of outro sounds.
        # Do NOT block the UI thread on filesystem probing here.
        try:
            self._ensure_outro_sounds_loaded()
        except Exception:
            pass

        # Track the chosen outro sound for this bump (if any).
        try:
            self._current_bump_outro_audio_path = bump_item.get('outro_audio_path')
        except Exception:
            self._current_bump_outro_audio_path = None

        # Treat bumps as non-episode playback for sleep timer purposes.
        self._pause_sleep_countdown()

        # Bump-gate: mark that we are inside a bump so play_next() doesn't trigger another bump.
        try:
            self._in_bump_playback = True
        except Exception:
            pass

        # Reset bump-video state.
        try:
            self._current_bump_is_video = False
            self._current_bump_video_inclusive = False
            self._current_bump_video_path = None
            self._post_video_bump_script = None
            self._bump_video_overlay_text = None
            self._bump_video_overlay_ms = None
            self._bump_video_overlay_play_audio = False
            self._bump_video_overlay_scheduled = False
        except Exception:
            pass
        try:
            self._hide_bump_video_overlay()
        except Exception:
            pass

        # Video bump: play a bump video asset (optionally with inclusive outro overlay).
        try:
            vpath = str(video or '').strip()
        except Exception:
            vpath = ''
        if vpath:
            self._current_bump_is_video = True
            self._current_bump_video_inclusive = bool(video_inclusive)
            self._current_bump_video_path = str(vpath)

            # Critical: bump-video inclusive overlay scheduling uses _last_time_pos.
            # Reset it here so we don't accidentally schedule using the prior episode's
            # time-pos (which would make the outro appear immediately and then vanish).
            try:
                self._last_time_pos = 0.0
            except Exception:
                pass
            try:
                self.total_duration = 0
            except Exception:
                pass

            # Reset EOF watchdog state for bump-video playback.
            try:
                self._handled_eof_for_bump_key = None
            except Exception:
                pass
            try:
                self._play_start_monotonic = time.monotonic()
                self._played_since_start = False
            except Exception:
                pass

            try:
                self._log_event(
                    'bump_video_start',
                    video=str(vpath or ''),
                    inclusive=bool(video_inclusive),
                    has_script=bool(script),
                )
            except Exception:
                pass

            # Ensure we're on the normal video surface.
            try:
                self.video_stack.setCurrentIndex(0)
            except Exception:
                pass

            self.setWindowTitle("Sleepy Shows - [BV]")

            # Reset any lingering FX from a prior bump.
            self._stop_bump_fx()

            # Reset per-bump audio control state.
            self._bump_music_cut_active = False
            self._bump_music_cut_prev_mute = None
            self._bump_outro_audio_exclusive = False

            # Play bump video.
            try:
                target = self._resolve_video_play_target(vpath)
            except Exception:
                target = vpath
            try:
                self._last_play_target = str(target or '')
                self._last_play_source_path = str(vpath or '')
            except Exception:
                self._last_play_target = None
                self._last_play_source_path = None

            try:
                self.player.play(target)
                self._played_since_start = True
                try:
                    self._log_event('play_start', kind='bump_video', source_path=str(vpath or ''), target=str(target or ''), index=int(getattr(self.playlist_manager, 'current_index', -1) or -1))
                except Exception:
                    pass
                self._sync_keep_awake()
            except Exception:
                pass

            # Inclusive mode: overlay the outro card during the final N ms of the video.
            if script and bool(video_inclusive):
                try:
                    cards = script.get('cards', []) if isinstance(script, dict) else []
                except Exception:
                    cards = []
                outro_card = None
                for c in reversed(list(cards or [])):
                    if isinstance(c, dict) and bool(c.get('is_outro', False)):
                        outro_card = c
                        break
                if outro_card is not None:
                    try:
                        self._bump_video_overlay_text = str(outro_card.get('text', '') or '')
                    except Exception:
                        self._bump_video_overlay_text = ''
                    try:
                        self._bump_video_overlay_ms = int(outro_card.get('duration', 800) or 800)
                    except Exception:
                        self._bump_video_overlay_ms = 800
                    try:
                        self._bump_video_overlay_play_audio = bool(outro_card.get('outro_audio', False))
                    except Exception:
                        self._bump_video_overlay_play_audio = False

                    # If duration is already cached, schedule immediately; otherwise update_duration() will schedule.
                    try:
                        dur_ms = self._lookup_bump_video_duration_ms(vpath)
                    except Exception:
                        dur_ms = None
                    if dur_ms is not None:
                        try:
                            self._schedule_inclusive_bump_video_overlay(int(dur_ms))
                        except Exception:
                            pass
            elif script and not bool(video_inclusive):
                # Non-inclusive: run the bump script after the video ends.
                try:
                    self._post_video_bump_script = script
                except Exception:
                    self._post_video_bump_script = script

            # While this bump is playing, prefetch/stage assets for the next bump.
            try:
                QTimer.singleShot(0, self._schedule_prefetch_next_bump_assets)
            except Exception:
                pass
            return
        
        self.video_stack.setCurrentIndex(1) # Bump View
        self.setWindowTitle("Sleepy Shows - [AS]")

        # Reset any lingering FX from a prior bump.
        self._stop_bump_fx()

        # Reset per-bump audio control state.
        self._bump_music_cut_active = False
        self._bump_music_cut_prev_mute = None
        self._bump_outro_audio_exclusive = False
        
        if audio:
            target = self._maybe_staged_path(audio)
            self.player.play(target)
            try:
                self._log_event('play_start', kind='bump', source_path=str(audio or ''), target=str(target or ''), index=int(getattr(self.playlist_manager, 'current_index', -1) or -1))
            except Exception:
                pass
            self._sync_keep_awake()
            
        if script:
            self.current_bump_script = script.get('cards', [])
            self.current_card_index = 0
            self.advance_bump_card()

        # While this bump is playing, prefetch/stage assets for the next bump.
        try:
            QTimer.singleShot(0, self._schedule_prefetch_next_bump_assets)
        except Exception:
            pass

    def _interstitial_chance_per_bump(self) -> float:
        """Chance that a bump gets an interstitial preroll.

        Frequency rule:
        - Let N be the number of available interlude/interstitial video files.
            - Choose a per-bump probability so that over ~100 bumps we'd expect to
                see about 4*(100/N) prerolls (example: N=20 => 20% chance per bump).

        Cap:
        - Never exceed 80% chance, even for very small N.
        """
        try:
            n = int(len(getattr(self.playlist_manager, 'interstitials', []) or []))
        except Exception:
            n = 0
        if n <= 0:
            return 0.0
        try:
            # Example: N=20 => 0.10 (10%).
            return float(min(0.8, 4.0 / float(n)))
        except Exception:
            return 0.0

    def _play_preroll_interstitial(self, path: str) -> bool:
        try:
            p = str(path or '').strip()
        except Exception:
            p = ''
        if not p:
            return False

        # Ensure we're not in bump view.
        try:
            self.stop_bump_playback()
        except Exception:
            pass
        try:
            self.video_stack.setCurrentIndex(0)
        except Exception:
            pass

        # Exposure scoring.
        try:
            self.playlist_manager.note_interstitial_played(p)
        except Exception:
            pass

        # Reset EOF watchdog state for this transient playback.
        try:
            self._handled_eof_for_index = None
            self._play_start_monotonic = time.monotonic()
            self._played_since_start = False
        except Exception:
            pass

        try:
            target = self._resolve_video_play_target(p)
        except Exception:
            return False
        try:
            self._last_play_target = str(target or '')
            self._last_play_source_path = str(p or '')
        except Exception:
            self._last_play_target = None
            self._last_play_source_path = None

        try:
            self.player.play(target)
            self._played_since_start = True
        except Exception:
            return False

        try:
            self.setWindowTitle(f"Sleepy Shows - [IL] {os.path.basename(p)}")
        except Exception:
            pass
        try:
            self._log_event('play_start', kind='interstitial_preroll', source_path=str(p or ''), target=str(target or ''), index=int(getattr(self.playlist_manager, 'current_index', -1) or -1))
        except Exception:
            pass

        try:
            self._sync_keep_awake()
        except Exception:
            pass
        try:
            self._resume_sleep_countdown_if_needed()
            QTimer.singleShot(200, self._resume_sleep_countdown_if_needed)
        except Exception:
            pass

        return True

    def _play_bump_with_optional_interstitial(self, bump_item: dict):
        """Play an interstitial before a bump (best-effort) when TV Vibes is on.

        When TV Vibes is enabled and interludes exist, we always try to play an
        interlude before each bump.
        """

        # Interstitials only play when TV Vibes (bumps) are enabled.
        if not bool(getattr(self, 'bumps_enabled', False)):
            return self.play_bump(bump_item)

        # Don't stack prerolls.
        try:
            if getattr(self, '_pending_bump_item', None) is not None:
                return self.play_bump(bump_item)
        except Exception:
            pass

        # If a bump is actively playing, don't try to interrupt it with a preroll.
        try:
            if bool(getattr(self, '_in_bump_playback', False)):
                return self.play_bump(bump_item)
        except Exception:
            pass

        try:
            inters = list(getattr(self.playlist_manager, 'interstitials', []) or [])
        except Exception:
            inters = []
        if not inters:
            return self.play_bump(bump_item)

        inter_path = None
        try:
            inter_path = self.playlist_manager.get_next_interstitial_path()
        except Exception:
            inter_path = None
        if not inter_path:
            try:
                inter_path = random.choice(inters)
            except Exception:
                inter_path = None
        if not inter_path:
            return self.play_bump(bump_item)

        # Play interstitial now; when it ends, start this bump.
        try:
            self._pending_bump_item = bump_item
        except Exception:
            self._pending_bump_item = bump_item

        ok = self._play_preroll_interstitial(str(inter_path))
        if not ok:
            try:
                self._pending_bump_item = None
            except Exception:
                pass
            return self.play_bump(bump_item)
        return None

    def _app_cache_dir(self):
        """Prefer an app-local cache dir, with a safe fallback."""
        try:
            # Running as a script or a frozen executable.
            base = None
            try:
                base = os.path.dirname(os.path.abspath(sys.argv[0]))
            except Exception:
                base = None
            if not base:
                base = os.getcwd()
            p = os.path.join(base, '_cache')
            os.makedirs(p, exist_ok=True)
            return p
        except Exception:
            try:
                p = os.path.join(tempfile.gettempdir(), 'sleepyshows_cache')
                os.makedirs(p, exist_ok=True)
                return p
            except Exception:
                return None

    def _maybe_staged_path(self, path: str):
        try:
            p = str(path or '').strip()
        except Exception:
            p = ''
        if not p:
            return p
        try:
            with self._bump_prefetch_lock:
                return str(self._bump_staged_audio_map.get(p, p))
        except Exception:
            return p

    def _clear_active_bump_assets(self):
        # Volatile-memory purge only. Do not delete cache files; they are re-used.
        try:
            with self._bump_prefetch_lock:
                self._active_bump_index = None
                self._bump_staged_audio_map = {}
                self._bump_prefetch_images = {}
                self._bump_prefetch_files = set()
        except Exception:
            pass

    def _clear_next_bump_prefetch(self):
        # Volatile-memory purge only. Do not delete cache files; they are re-used.
        try:
            with self._bump_prefetch_lock:
                self._next_prefetched_for_bump_index = None
                self._next_bump_staged_audio_map = {}
                self._next_bump_prefetch_images = {}
                self._next_bump_prefetch_files = set()
        except Exception:
            pass

    def _activate_prefetched_bump_assets(self, bump_index: int):
        try:
            bidx = int(bump_index)
        except Exception:
            return

        with self._bump_prefetch_lock:
            if self._next_prefetched_for_bump_index != bidx:
                return

            # Swap next -> active.
            self._active_bump_index = bidx
            self._bump_staged_audio_map = dict(self._next_bump_staged_audio_map or {})
            self._bump_prefetch_images = dict(self._next_bump_prefetch_images or {})
            self._bump_prefetch_files = set(self._next_bump_prefetch_files or set())

            # Clear next.
            self._next_prefetched_for_bump_index = None
            self._next_bump_staged_audio_map = {}
            self._next_bump_prefetch_images = {}
            self._next_bump_prefetch_files = set()

    def _clear_bump_prefetch(self):
        # Backward-compatible alias: this clears the NEXT prefetch buffer.
        self._clear_next_bump_prefetch()

    def _find_next_bump_index(self, start_index: int):
        pm = self.playlist_manager
        try:
            start = int(start_index)
        except Exception:
            start = -1
        if start < 0:
            start = 0
        try:
            items = pm.current_playlist or []
        except Exception:
            items = []

        for i in range(start, len(items)):
            it = items[i]
            if isinstance(it, dict) and it.get('type') == 'bump':
                return i
        return None

    def _ensure_outro_sounds_loaded(self):
        """Populate bump_manager.outro_sounds from the filesystem (cached)."""
        return self._ensure_outro_sounds_loaded_force(force=False)

    def _ensure_outro_sounds_loaded_force(self, *, force: bool = False):
        """Populate bump_manager.outro_sounds from the filesystem.

        If force=True, refresh even if bump_manager already has an outro list.
        """
        pm = self.playlist_manager
        bump_mgr = getattr(pm, 'bump_manager', None)
        if not bump_mgr:
            return

        try:
            already = bool(getattr(bump_mgr, 'outro_sounds', []) or [])
        except Exception:
            already = False
        if already and not force:
            return

        # Never probe the filesystem synchronously here (called during playback).
        # If we don't already have a cached list (or force=True), refresh in background.
        try:
            cached = getattr(self, '_outro_sounds_cache', None)
        except Exception:
            cached = None

        if isinstance(cached, list) and cached and not force:
            try:
                bump_mgr.set_outro_sounds(list(cached))
            except Exception:
                pass
            return

        # Avoid launching multiple refresh threads.
        if bool(getattr(self, '_outro_sounds_refresh_running', False)):
            return
        self._outro_sounds_refresh_running = True

        def _refresh_worker():
            try:
                files = self._list_outro_sounds()
            except Exception:
                files = []

            def _apply():
                try:
                    self._outro_sounds_cache = list(files or [])
                except Exception:
                    self._outro_sounds_cache = []
                try:
                    bump_mgr.set_outro_sounds(list(self._outro_sounds_cache or []))
                except Exception:
                    pass
                try:
                    self._outro_sounds_refresh_running = False
                except Exception:
                    pass

            try:
                QTimer.singleShot(0, _apply)
            except Exception:
                _apply()

        threading.Thread(target=_refresh_worker, daemon=True).start()

    def _invalidate_outro_sounds_cache(self):
        try:
            self._outro_sounds_cache = None
        except Exception:
            pass
        try:
            self._outro_sound_queue = []
        except Exception:
            pass
        try:
            self._recent_outro_sound_basenames = []
        except Exception:
            pass

    def _schedule_probe_bump_video_durations(self):
        """Probe bump video durations in background and cache into bump_manager."""
        try:
            if bool(getattr(self, '_bump_video_probe_running', False)):
                return
        except Exception:
            pass

        try:
            vdir = str(getattr(self, 'bump_videos_dir', '') or '').strip()
        except Exception:
            vdir = ''
        if not vdir or not os.path.isdir(vdir):
            return

        self._bump_video_probe_running = True

        def _worker():
            bm = getattr(getattr(self, 'playlist_manager', None), 'bump_manager', None)
            try:
                if bm is not None:
                    bm.scan_bump_videos(
                        vdir,
                        recursive=True,
                        max_files=10000,
                        max_depth=None,
                        time_budget_s=None,
                        probe_durations=True,
                    )
            finally:
                def _done():
                    try:
                        self._bump_video_probe_running = False
                    except Exception:
                        pass
                try:
                    QTimer.singleShot(0, _done)
                except Exception:
                    _done()

        threading.Thread(target=_worker, daemon=True).start()

    def _lookup_bump_video_duration_ms(self, path: str) -> int | None:
        try:
            p = str(path or '').strip()
        except Exception:
            p = ''
        if not p:
            return None

        bm = getattr(getattr(self, 'playlist_manager', None), 'bump_manager', None)
        if bm is None:
            return None

        try:
            ap = os.path.abspath(p)
        except Exception:
            ap = p
        try:
            k = bm._norm_path_key(ap)
        except Exception:
            k = ap
        if not k:
            return None
        try:
            v = (getattr(bm, 'video_durations_ms', {}) or {}).get(k)
        except Exception:
            v = None
        if v is None:
            return None
        try:
            vv = int(v)
        except Exception:
            return None
        return vv if vv > 0 else None

    def _list_outro_sounds_cached(self):
        try:
            cached = self._outro_sounds_cache
        except Exception:
            cached = None
        # IMPORTANT: Do not validate cached paths with os.path.exists() here.
        # On SMB/NFS this can block the UI thread (bump transitions).
        if isinstance(cached, list) and cached:
            return cached

        files = self._list_outro_sounds()
        try:
            self._outro_sounds_cache = list(files or [])
        except Exception:
            self._outro_sounds_cache = None
        return self._outro_sounds_cache or []

    def _list_outro_sounds(self):
        # Prefer an explicitly configured folder, then probe common external-drive layouts.
        folders = []
        try:
            p = str(getattr(self, '_outro_sounds_dir', '') or '').strip()
            if p and os.path.isdir(p):
                folders.append(p)
        except Exception:
            pass

        def _probe_mount(mount_root: str):
            if not mount_root or not os.path.isdir(mount_root):
                return

            data_root = os.path.join(mount_root, 'Sleepy Shows Data')
            roots_to_probe = []
            if os.path.isdir(data_root):
                roots_to_probe.append(data_root)
            roots_to_probe.append(mount_root)

            for root in roots_to_probe:
                direct = os.path.join(root, 'TV Vibe', 'outro sounds')
                if os.path.isdir(direct):
                    folders.append(direct)
                    continue

                tv_vibe_dir = _find_child_dir_case_insensitive(root, 'TV Vibe')
                if not tv_vibe_dir:
                    continue
                outro_dir = _find_child_dir_case_insensitive(tv_vibe_dir, 'outro sounds')
                if outro_dir and os.path.isdir(outro_dir):
                    folders.append(outro_dir)

        try:
            label = str(getattr(self, 'auto_config_volume_label', '') or '').strip()
        except Exception:
            label = ''

        for mount_root in _iter_mount_roots_for_label(label) or []:
            _probe_mount(mount_root)
        for mount_root in _iter_mount_roots_fallback() or []:
            _probe_mount(mount_root)

        audio_exts = {'.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.opus', '.webm', '.mp4'}

        out = []
        for folder in folders:
            try:
                for name in os.listdir(folder):
                    full = os.path.join(folder, name)
                    if not os.path.isfile(full):
                        continue
                    if os.path.splitext(name)[1].lower() in audio_exts:
                        out.append(full)
            except Exception:
                continue

        # De-dupe while preserving order.
        seen = set()
        uniq = []
        for p in out:
            try:
                key = os.path.normpath(str(p))
            except Exception:
                key = str(p)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(p)

        return uniq

    def _pick_random_outro_sound(self):
        files = self._list_outro_sounds_cached()
        if not files:
            return None

        # Rebuild queue if empty or stale.
        try:
            if not isinstance(self._outro_sound_queue, list):
                self._outro_sound_queue = []
            if not self._outro_sound_queue:
                q = list(range(len(files)))
                random.shuffle(q)

                # Best-effort: keep recently used outros out of the first N picks.
                recent_set = set((self._recent_outro_sound_basenames or [])[-int(self._outro_recent_n):])
                if recent_set:
                    non_recent = []
                    recent = []
                    for idx in q:
                        try:
                            name = os.path.basename(str(files[int(idx)] or '')).lower()
                        except Exception:
                            name = ''
                        if name in recent_set:
                            recent.append(idx)
                        else:
                            non_recent.append(idx)
                    q = non_recent + recent

                self._outro_sound_queue = q
        except Exception:
            self._outro_sound_queue = []

        # Pop next.
        try:
            idx = self._outro_sound_queue.pop(0)
            p = str(files[int(idx)] or '')
            if p:
                try:
                    name = os.path.basename(p).lower()
                    if name:
                        self._recent_outro_sound_basenames.append(name)
                        self._recent_outro_sound_basenames = self._recent_outro_sound_basenames[-int(self._outro_recent_n):]
                except Exception:
                    pass
            return p
        except Exception:
            return None

        return None

    def _stage_small_file(self, src_path: str):
        """Copy a small file into the app-local cache dir and return the staged path."""
        try:
            src = str(src_path or '').strip()
        except Exception:
            src = ''
        if not src or not os.path.exists(src):
            return None

        cache_dir = self._app_cache_dir()
        if not cache_dir:
            return None

        try:
            base = os.path.basename(src)
            root, ext = os.path.splitext(base)
            h = hashlib.md5(src.encode('utf-8', errors='ignore')).hexdigest()[:10]
            dst = os.path.join(cache_dir, f"{root}__{h}{ext}")
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            return dst
        except Exception:
            return None

    def _schedule_prefetch_next_bump_assets(self):
        """Prefetch next bump's images and stage its audio files."""
        pm = self.playlist_manager

        # Determine where playback will resume after this bump.
        pending = getattr(self, '_pending_next_index', None)
        if pending is not None:
            start = int(pending) + 1
        else:
            start = int(getattr(pm, 'current_index', -1) or -1) + 1

        bump_idx = self._find_next_bump_index(start)
        if bump_idx is None:
            return

        # Avoid re-prefetching the same bump.
        try:
            with self._bump_prefetch_lock:
                if self._next_prefetched_for_bump_index == int(bump_idx):
                    return
        except Exception:
            pass

        # Clear previous NEXT-buffer prefetch to keep memory bounded.
        self._clear_next_bump_prefetch()

        try:
            bump_item = pm.current_playlist[int(bump_idx)]
        except Exception:
            bump_item = None
        if not isinstance(bump_item, dict) or bump_item.get('type') != 'bump':
            return

        t = threading.Thread(
            target=self._prefetch_next_bump_assets_worker,
            args=(int(bump_idx), bump_item),
            daemon=True,
        )
        t.start()

    def _prefetch_next_bump_assets_worker(self, bump_idx: int, bump_item: dict):
        try:
            script = bump_item.get('script') if isinstance(bump_item, dict) else None
            cards = (script.get('cards') if isinstance(script, dict) else None) or []

            # --- Stage audio ---
            staged_map = {}
            staged_files = set()

            def _stage(path: str):
                if not path:
                    return
                dst = self._stage_small_file(path)
                if dst:
                    staged_map[str(path)] = str(dst)
                    staged_files.add(str(dst))

            try:
                _stage(str(bump_item.get('audio') or ''))
            except Exception:
                pass

            try:
                _stage(str(bump_item.get('outro_audio_path') or ''))
            except Exception:
                pass

            for c in list(cards):
                if not isinstance(c, dict):
                    continue
                sound = c.get('sound')
                if isinstance(sound, dict):
                    try:
                        _stage(str(sound.get('path') or ''))
                    except Exception:
                        pass

            # --- Prefetch images (decoded) ---
            images = {}
            for c in list(cards):
                if not isinstance(c, dict):
                    continue
                img_info = c.get('image') if isinstance(c.get('image'), dict) else None
                if not img_info:
                    continue
                img_path = str(img_info.get('path') or '').strip()
                if not img_path or img_path in images:
                    continue
                try:
                    qimg = QImage(img_path)
                    if not qimg.isNull():
                        images[img_path] = qimg
                except Exception:
                    continue

            with self._bump_prefetch_lock:
                self._next_prefetched_for_bump_index = int(bump_idx)
                self._next_bump_staged_audio_map = staged_map
                self._next_bump_prefetch_files = staged_files
                self._next_bump_prefetch_images = images
        except Exception:
            return

    def _remaining_bump_ms(self):
        try:
            cards = self.current_bump_script or []
            idx = int(getattr(self, 'current_card_index', 0) or 0)
            if idx < 0:
                idx = 0
            total = 0
            for c in cards[idx:]:
                try:
                    total += int(c.get('duration', 0) or 0)
                except Exception:
                    continue
            return max(0, int(total))
        except Exception:
            return 0

    def _stop_bump_fx(self):
        try:
            if hasattr(self, '_bump_fx_stop_timer') and self._bump_fx_stop_timer.isActive():
                self._bump_fx_stop_timer.stop()
        except Exception:
            pass

        # Restore FX volume if it was temporarily adjusted (e.g. outro attenuation).
        try:
            prev_vol = getattr(self, '_bump_fx_prev_volume', None)
            if prev_vol is not None and hasattr(self, 'fx_player') and self.fx_player:
                try:
                    self.fx_player.set_volume(float(prev_vol))
                except Exception:
                    pass
        except Exception:
            pass

        self._bump_fx_prev_volume = None

        try:
            if hasattr(self, 'fx_player') and self.fx_player:
                self.fx_player.stop()
        except Exception:
            pass

        # Restore bump music mute state if we interrupted (unless a permanent cut is active).
        try:
            if not bool(getattr(self, '_bump_music_cut_active', False)):
                prev = getattr(self, '_bump_fx_interrupt_prev_mute', None)
                if prev is not None and hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                    try:
                        self.player.mpv.mute = bool(prev)
                    except Exception:
                        pass
        except Exception:
            pass

        self._bump_fx_interrupt_prev_mute = None
        self._bump_fx_active = False
        self._bump_fx_policy = None

    def _play_outro_audio(self, *, duration_ms: int | None = None):
        path = None
        try:
            path = str(getattr(self, '_current_bump_outro_audio_path', None) or '').strip()
        except Exception:
            path = None
        if not path:
            path = self._pick_random_outro_sound()

        path = self._maybe_staged_path(path)
        if not path:
            return

        # Outro audio is always a CUT and trumps all other audio:
        # - stop any other FX
        # - mute bump music for the remainder of the bump
        # - block subsequent FX so this is the only sound playing
        try:
            self._stop_bump_fx()
        except Exception:
            pass

        self._bump_outro_audio_exclusive = True
        try:
            if hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                if not bool(getattr(self, '_bump_music_cut_active', False)):
                    self._bump_music_cut_prev_mute = bool(getattr(self.player.mpv, 'mute', False))
                self._bump_music_cut_active = True
                self.player.mpv.mute = True
        except Exception:
            pass

        try:
            if hasattr(self, 'fx_player') and self.fx_player:
                # Reduce outro sounds by 3 dB.
                # mpv volume is a linear scale (0..100), so multiply by 10^(-3/20).
                try:
                    prev_vol = None
                    if getattr(self.fx_player, 'mpv', None) is not None:
                        prev_vol = getattr(self.fx_player.mpv, 'volume', None)
                    if prev_vol is not None:
                        self._bump_fx_prev_volume = float(prev_vol)
                        atten = 10 ** (-3.0 / 20.0)
                        self.fx_player.set_volume(float(prev_vol) * float(atten))
                except Exception:
                    self._bump_fx_prev_volume = None
                # Avoid os.path.exists() checks on network paths; let mpv handle missing files.
                self.fx_player.play(path)
        except Exception:
            self._stop_bump_fx()
            return

        self._bump_fx_active = True
        self._bump_fx_policy = 'ms'
        try:
            dms = None
            try:
                dms = int(duration_ms) if duration_ms is not None else None
            except Exception:
                dms = None
            if dms is None or dms <= 0:
                dms = 800
            self._bump_fx_stop_timer.start(int(dms))
        except Exception:
            pass

    def _play_bump_fx_for_card(self, card, card_duration_ms):
        try:
            # If outro-audio exclusivity is active, don't play any other FX.
            if bool(getattr(self, '_bump_outro_audio_exclusive', False)):
                return
            if not isinstance(card, dict):
                return
            sound = card.get('sound')
            if not isinstance(sound, dict):
                return

            path = str(sound.get('path') or '').strip()
            path = self._maybe_staged_path(path)
            if not path:
                return

            mix = str(sound.get('mix') or 'add').strip().lower()
            play_for = str(sound.get('play_for') or 'card').strip().lower()

            remaining_ms = int(self._remaining_bump_ms())
            if remaining_ms <= 0:
                return

            limit_ms = None
            if play_for == 'duration':
                # Play the file, but never longer than the remaining bump length.
                limit_ms = remaining_ms
            elif play_for == 'ms':
                try:
                    limit_ms = int(sound.get('ms', 0) or 0)
                except Exception:
                    limit_ms = 0
                if limit_ms <= 0:
                    limit_ms = int(card_duration_ms)
                limit_ms = min(limit_ms, remaining_ms)
            else:
                # 'card'
                limit_ms = min(int(card_duration_ms), remaining_ms)

            # Starting a new FX replaces any currently playing FX.
            self._stop_bump_fx()

            if mix == 'cut':
                # Permanently mute bump music for the remainder of the bump.
                try:
                    if hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                        if not bool(getattr(self, '_bump_music_cut_active', False)):
                            self._bump_music_cut_prev_mute = bool(getattr(self.player.mpv, 'mute', False))
                        self._bump_music_cut_active = True
                        self.player.mpv.mute = True
                except Exception:
                    pass

            if mix == 'interrupt':
                try:
                    if hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                        self._bump_fx_interrupt_prev_mute = bool(getattr(self.player.mpv, 'mute', False))
                        self.player.mpv.mute = True
                except Exception:
                    self._bump_fx_interrupt_prev_mute = None

            try:
                if hasattr(self, 'fx_player') and self.fx_player:
                    self.fx_player.play(path)
            except Exception:
                # Ensure we don't leave bump music muted if FX couldn't play.
                self._stop_bump_fx()
                return

            self._bump_fx_active = True
            self._bump_fx_policy = play_for

            try:
                if limit_ms is not None and int(limit_ms) > 0:
                    self._bump_fx_stop_timer.start(int(limit_ms))
            except Exception:
                pass
        except Exception:
            return

    def advance_bump_card(self):
         if not self.current_bump_script:
             return

         # If script is finished, cut off music and advance to the next item.
         if self.current_card_index >= len(self.current_bump_script):
             self.lbl_bump_text.setText("")
             self.stop_bump_playback()
             pending = getattr(self, '_pending_next_index', None)
             if pending is not None:
                 idx = int(pending)
                 record_history = bool(getattr(self, '_pending_next_record_history', True))
                 self._pending_next_index = None
                 self._pending_next_record_history = True
                 self._advancing_from_bump_end = True
                 try:
                     self.play_index(idx, record_history=record_history, bypass_bump_gate=True, suppress_ui=True)
                 finally:
                     self._advancing_from_bump_end = False
                 return

             # Bump completion is an automatic transition; keep controls/overlay hidden.
             self._advancing_from_bump_end = True
             try:
                 self.play_next()
             finally:
                 self._advancing_from_bump_end = False
             return

         # Card ended: stop any card-scoped FX before showing the next card.
         if getattr(self, '_bump_fx_policy', None) == 'card' and getattr(self, '_bump_fx_active', False):
             self._stop_bump_fx()

         card = self.current_bump_script[self.current_card_index]
         # Card durations are stored in milliseconds.
         duration = int(card.get('duration', 1200))

         # Play any sound FX for this card.
         self._play_bump_fx_for_card(card, duration)

         def _hide_all_bump_widgets():
             self.lbl_bump_text.hide()
             self.lbl_bump_text_top.hide()
             self.bump_image_view.hide()
             self.lbl_bump_text_bottom.hide()
             self.lbl_bump_text.setText("")
             self.lbl_bump_text_top.setText("")
             self.lbl_bump_text_bottom.setText("")
             try:
                 self.lbl_bump_text_top.setMaximumHeight(16777215)
                 self.lbl_bump_text_bottom.setMaximumHeight(16777215)
                 self.lbl_bump_text_top.setMinimumHeight(0)
                 self.lbl_bump_text_bottom.setMinimumHeight(0)
             except Exception:
                 pass
             try:
                 self.bump_image_view.clear()
             except Exception:
                 pass

         ctype = card.get('type', 'text')
         if ctype == 'text':
             try:
                 self._bump_safe_vpad_ratio = 0.15
                 self._apply_bump_safe_padding_now()
             except Exception:
                 pass
             _hide_all_bump_widgets()
             self.lbl_bump_text.setTextFormat(Qt.PlainText)
             self.lbl_bump_text.setText(card.get('text', ''))
             self.lbl_bump_text.show()
             if bool(card.get('outro_audio', False)):
                 self._play_outro_audio(duration_ms=int(duration))
         elif ctype == 'pause':
             try:
                 self._bump_safe_vpad_ratio = 0.15
                 self._apply_bump_safe_padding_now()
             except Exception:
                 pass
             _hide_all_bump_widgets()
         elif ctype == 'img_char':
             try:
                 # Char-inline images always include text; keep safe padding.
                 self._bump_safe_vpad_ratio = 0.15
                 self._apply_bump_safe_padding_now()
             except Exception:
                 pass
             _hide_all_bump_widgets()
             img_info = card.get('image') if isinstance(card.get('image'), dict) else {}
             img_path = str(img_info.get('path') or '')
             pm = QPixmap()
             if img_path:
                 try:
                     # Prefer loading pixmap directly on the GUI thread.
                     # This avoids issues with QImage objects decoded in background threads.
                     pm = QPixmap(img_path)
                     if pm.isNull():
                         with self._bump_prefetch_lock:
                             qimg = self._bump_prefetch_images.get(img_path)
                         if qimg is None:
                             # If this bump wasn't prefetched (common for the global bump gate),
                             # load on-demand so <img> cards can still display.
                             try:
                                 qimg_try = QImage(img_path)
                                 if not qimg_try.isNull():
                                     with self._bump_prefetch_lock:
                                         self._bump_prefetch_images[img_path] = qimg_try
                                     qimg = qimg_try
                             except Exception:
                                 qimg = None
                         if qimg is not None:
                             # QImage objects can be created in a background thread during prefetch.
                             # Make a deep copy before converting to QPixmap on the GUI thread.
                             try:
                                 qimg_use = qimg.copy()
                             except Exception:
                                 qimg_use = qimg
                             pm = QPixmap.fromImage(qimg_use)
                         else:
                             pm = QPixmap()
                 except Exception:
                     pm = QPixmap()
             if pm.isNull():
                 try:
                     print(f"DEBUG: Bump image failed to load (img_char): {img_path} exists={os.path.exists(img_path) if img_path else False}")
                 except Exception:
                     pass
                 # Fallback: render without image.
                 self.lbl_bump_text.setTextFormat(Qt.PlainText)
                 self.lbl_bump_text.setText(str(card.get('template', '')).replace('[[IMG]]', ''))
                 self.lbl_bump_text.show()
             else:
                 template = str(card.get('template', ''))
                 parts = template.split('[[IMG]]')
                 before = html.escape(parts[0]) if parts else ''
                 after = html.escape(parts[1]) if len(parts) > 1 else ''
                 # Preserve line breaks.
                 before = before.replace('\n', '<br>')
                 after = after.replace('\n', '<br>')
                 font_px = max(1, int(self.lbl_bump_text.fontMetrics().height()))
                 try:
                     img_url = QUrl.fromLocalFile(img_path).toString()
                 except Exception:
                     img_url = str(img_path)
                 img_html = f'<img src="{html.escape(img_url)}" style="height:{font_px}px;" />'
                 html_body = f'<div align="center">{before}{img_html}{after}</div>'
                 self.lbl_bump_text.setTextFormat(Qt.RichText)
                 self.lbl_bump_text.setText(html_body)
                 self.lbl_bump_text.show()
         elif ctype == 'img':
             _hide_all_bump_widgets()
             img_info = card.get('image') if isinstance(card.get('image'), dict) else {}
             img_path = str(img_info.get('path') or '')
             pm = QPixmap()
             if img_path:
                 try:
                     # Prefer loading pixmap directly on the GUI thread.
                     # This avoids issues with QImage objects decoded in background threads.
                     pm = QPixmap(img_path)
                     if pm.isNull():
                         with self._bump_prefetch_lock:
                             qimg = self._bump_prefetch_images.get(img_path)
                         if qimg is None:
                             # If this bump wasn't prefetched (common for the global bump gate),
                             # load on-demand so <img> cards can still display.
                             try:
                                 qimg_try = QImage(img_path)
                                 if not qimg_try.isNull():
                                     with self._bump_prefetch_lock:
                                         self._bump_prefetch_images[img_path] = qimg_try
                                     qimg = qimg_try
                             except Exception:
                                 qimg = None
                         if qimg is not None:
                             # QImage objects can be created in a background thread during prefetch.
                             # Make a deep copy before converting to QPixmap on the GUI thread.
                             try:
                                 qimg_use = qimg.copy()
                             except Exception:
                                 qimg_use = qimg
                             pm = QPixmap.fromImage(qimg_use)
                         else:
                             pm = QPixmap()
                 except Exception:
                     pm = QPixmap()
             if pm.isNull():
                 try:
                     print(f"DEBUG: Bump image failed to load (img): {img_path} exists={os.path.exists(img_path) if img_path else False} mode={img_info.get('mode')}")
                 except Exception:
                     pass
                 # Fallback: show text only.
                 combined = "\n".join([str(card.get('text_before') or ''), str(card.get('text_after') or '')]).strip()
                 self.lbl_bump_text.setTextFormat(Qt.PlainText)
                 self.lbl_bump_text.setText(combined)
                 self.lbl_bump_text.show()
             else:
                 mode = str(img_info.get('mode') or 'default')
                 percent = None
                 try:
                     percent = float(img_info.get('percent')) if 'percent' in img_info else None
                 except Exception:
                     percent = None

                 # Note: do NOT strip here. For <img ... lines> cards, the bump parser
                 # uses NBSP lines to preserve explicit blank lines (<\s>), and stripping
                 # would delete them (causing image jumping between cards).
                 before = str(card.get('text_before') or '')
                 after = str(card.get('text_after') or '')

                 # Disable the bump safe padding only for image-only cards.
                 # This allows default <img filename> cards (like bsod.png) to truly
                 # fill the bump view and max out width/height.
                 try:
                     def _blankish(s):
                         ss = str(s or '')
                         ss = ss.replace('\u00A0', ' ').strip()
                         return ss == ''

                     is_image_only = (mode == 'default') and _blankish(before) and _blankish(after)
                     self._bump_safe_vpad_ratio = 0.0 if is_image_only else 0.15
                     self._apply_bump_safe_padding_now()
                 except Exception:
                     pass

                 # If "lines" mode, reserve space for the explicit number of text lines.
                 if mode == 'lines':
                     fm = self.lbl_bump_text.fontMetrics()
                     line_h = int(fm.lineSpacing())
                     top_lines = int(card.get('before_lines', 0) or 0)
                     bot_lines = int(card.get('after_lines', 0) or 0)

                     # Always reserve the computed height, even if the text is visually blank.
                     # This keeps the image stable when spacer lines (<\s>) are used.
                     if top_lines > 0:
                         self.lbl_bump_text_top.setTextFormat(Qt.PlainText)
                         self.lbl_bump_text_top.setText(before if before else '\u00A0')
                         self.lbl_bump_text_top.setFixedHeight(max(0, top_lines * line_h))
                         self.lbl_bump_text_top.show()
                     if bot_lines > 0:
                         self.lbl_bump_text_bottom.setTextFormat(Qt.PlainText)
                         self.lbl_bump_text_bottom.setText(after if after else '\u00A0')
                         self.lbl_bump_text_bottom.setFixedHeight(max(0, bot_lines * line_h))
                         self.lbl_bump_text_bottom.show()
                 else:
                     if before:
                         self.lbl_bump_text_top.setTextFormat(Qt.PlainText)
                         self.lbl_bump_text_top.setText(before)
                         self.lbl_bump_text_top.show()
                     if after:
                         self.lbl_bump_text_bottom.setTextFormat(Qt.PlainText)
                         self.lbl_bump_text_bottom.setText(after)
                         self.lbl_bump_text_bottom.show()

                 self.bump_image_view.set_image(pm, mode=mode, percent=percent)
                 self.bump_image_view.show()
         else:
             _hide_all_bump_widgets()
             
         self.current_card_index += 1
         # MPV audio is running independently. We just time the cards.
         self.bump_timer.start(max(1, duration))

    def stop_bump_playback(self):
        self.bump_timer.stop()
        was_in_bump_view = False
        try:
            was_in_bump_view = self.video_stack.currentIndex() == 1
        except Exception:
            was_in_bump_view = False

        was_bump = False
        try:
            was_bump = bool(was_in_bump_view) or bool(getattr(self, '_in_bump_playback', False))
        except Exception:
            was_bump = bool(was_in_bump_view)

        # If we were playing a bump (audio or video), cut it off cleanly.
        if was_bump:
            try:
                self.player.stop()
            except Exception:
                pass
            try:
                self._set_stop_reason('stop_bump_playback')
            except Exception:
                pass
            self._sync_keep_awake()

        try:
            self._hide_bump_video_overlay()
        except Exception:
            pass

        # Stop any active FX and restore bump music mute.
        self._stop_bump_fx()

        # Restore bump music mute state if a "cut" was applied.
        try:
            prev = getattr(self, '_bump_music_cut_prev_mute', None)
            if prev is not None and hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                try:
                    self.player.mpv.mute = bool(prev)
                except Exception:
                    pass
        except Exception:
            pass

        self._bump_music_cut_active = False
        self._bump_music_cut_prev_mute = None
        self._bump_outro_audio_exclusive = False
        self._current_bump_outro_audio_path = None

        # Clear bump state flags.
        try:
            self._in_bump_playback = False
            self._current_bump_is_video = False
            self._current_bump_video_inclusive = False
            self._current_bump_video_path = None
            self._post_video_bump_script = None
        except Exception:
            pass

        self.current_bump_script = None
        try:
            if hasattr(self, 'lbl_bump_text'):
                self.lbl_bump_text.setText("")
            if hasattr(self, 'lbl_bump_text_top'):
                self.lbl_bump_text_top.setText("")
            if hasattr(self, 'lbl_bump_text_bottom'):
                self.lbl_bump_text_bottom.setText("")
            if hasattr(self, 'bump_image_view'):
                self.bump_image_view.clear()
        except Exception:
            pass

        # Purge active bump assets after bump playback ends.
        try:
            self._clear_active_bump_assets()
        except Exception:
            pass

    def toggle_play(self):
        # If player is idle but we have a playlist, start playing
        idle = False
        try:
             idle = self.player.mpv.idle_active
        except:
             idle = True # assume
             
        if idle and self.playlist_manager.current_playlist:
            # If we haven't started yet, pick a start index based on shuffle mode.
            if self.playlist_manager.current_index is None or self.playlist_manager.current_index < 0:
                start_idx = 0
                if self.playlist_manager.shuffle_mode != 'off':
                    start_idx = self.playlist_manager.get_next_index()
                    if start_idx == -1:
                        start_idx = 0
                self.play_index(start_idx)
            else:
                self.play_index(self.playlist_manager.current_index)
        else:
            try:
                self._user_pause_toggle = True
            except Exception:
                pass
            self.player.toggle_pause()

    def _maybe_apply_episode_skip_penalty(self):
        """Apply a small penalty when an episode is skipped/cut off."""
        try:
            if bool(getattr(self, '_advancing_from_eof', False)):
                return False
        except Exception:
            pass

        pm = getattr(self, 'playlist_manager', None)
        if pm is None:
            return False

        try:
            idx = int(getattr(pm, 'current_index', -1))
        except Exception:
            idx = -1
        if idx < 0:
            return False

        try:
            item = pm.current_playlist[idx]
        except Exception:
            return False

        try:
            if not pm.is_episode_item(item):
                return False
        except Exception:
            return False

        # If the player is idle (no active file), don't apply a penalty.
        try:
            if hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                if bool(getattr(self.player.mpv, 'core_idle', True)):
                    return False
        except Exception:
            pass

        # Only apply when we're in the normal video view (not bump view).
        try:
            if hasattr(self, 'video_stack') and self.video_stack.currentIndex() != 0:
                return False
        except Exception:
            return False

        # Avoid double-applying for the same playback start.
        try:
            start_key = (idx, getattr(self, '_play_start_monotonic', None))
            if getattr(self, '_skip_penalty_applied_for_start', None) == start_key:
                return False
        except Exception:
            start_key = None

        # If we're at the end (or extremely close), don't treat it as a skip.
        # If duration is unknown, we still treat manual navigation/stop as a cut-off.
        try:
            pos = self._last_time_pos
            if pos is None and hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                pos = getattr(self.player.mpv, 'time_pos', None)
            dur = self.total_duration
            if (dur is None or float(dur) <= 0) and hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                dur = getattr(self.player.mpv, 'duration', None)

            if pos is not None and dur is not None and float(dur) > 0:
                if float(pos) >= (float(dur) - 0.15):
                    return False
        except Exception:
            pass

        try:
            delta = pm.apply_episode_skip_penalty(idx, points=1.0)
        except Exception:
            return False

        try:
            self._skip_penalty_applied_for_start = start_key
        except Exception:
            pass
        return bool(delta)

    def stop_playback(self):
        try:
            self._maybe_apply_episode_skip_penalty()
        except Exception:
            pass
        try:
            self._pending_bump_item = None
        except Exception:
            pass

        try:
            if hasattr(self, '_resume_recover_timer'):
                self._resume_recover_timer.stop()
        except Exception:
            pass

        try:
            self._persist_resume_state(force=True, reason='stop_playback')
        except Exception:
            pass
        self.player.stop()
        try:
            self._set_stop_reason('stop_playback')
        except Exception:
            pass
        self.show_controls()
        self._pause_sleep_countdown()
        self._sync_keep_awake()

    def play_next(self):
        pm = self.playlist_manager

        # One-shot bypass used when a bump has just ended and we want to advance
        # without immediately triggering the global bump gate again.
        bypass_bump_gate_once = False
        try:
            bypass_bump_gate_once = bool(getattr(self, '_bypass_bump_gate_once', False))
        except Exception:
            bypass_bump_gate_once = False
        if bypass_bump_gate_once:
            try:
                self._bypass_bump_gate_once = False
            except Exception:
                pass

        suppress_ui = False
        try:
            suppress_ui = self._should_suppress_auto_nav_ui()
        except Exception:
            suppress_ui = False

        # Manual forward navigation mid-episode counts as a skip/cut-off.
        try:
            self._maybe_apply_episode_skip_penalty()
        except Exception:
            pass

        # If a bump-gated next is pending and the user hits Next again, skip the bump.
        if getattr(self, '_pending_next_index', None) is not None and self.video_stack.currentIndex() == 1:
            idx = int(self._pending_next_index)
            record_history = bool(getattr(self, '_pending_next_record_history', True))
            self._pending_next_index = None
            self._pending_next_record_history = True
            self.stop_bump_playback()
            # User explicitly skipped the bump; do not suppress UI.
            self.play_index(idx, record_history=record_history, bypass_bump_gate=True, suppress_ui=False)
            return

        next_idx = -1
        record_history = True

        # If the user went backward, honor forward history first.
        try:
            if getattr(pm, 'playback_history_pos', -1) < len(getattr(pm, 'playback_history', [])) - 1:
                idx = pm.step_forward_in_history()
                if idx is not None and idx >= 0:
                    next_idx = idx
                    record_history = False
        except Exception:
            pass

        # Otherwise advance using the queue.
        if next_idx == -1:
            next_idx = pm.get_next_index()
            record_history = True

        if next_idx == -1:
            try:
                self._set_stop_reason('end_of_playlist')
            except Exception:
                pass
            self.stop_playback()
            return

        # If the next item is itself a bump, just play it (no double-bump).
        try:
            next_item = pm.current_playlist[next_idx]
            if isinstance(next_item, dict) and next_item.get('type') == 'bump':
                self.play_index(next_idx, record_history=record_history, bypass_bump_gate=True, suppress_ui=bool(suppress_ui))
                return
        except Exception:
            pass

        # Global bump gate: any forward move plays a bump first when enabled.
        if (
            self.bumps_enabled
            and (not bypass_bump_gate_once)
            and self.video_stack.currentIndex() != 1
            and not bool(getattr(self, '_in_bump_playback', False))
        ):
            bump_item = None
            try:
                bump_item = pm.bump_manager.get_random_bump()
            except Exception:
                bump_item = None

            if bump_item:
                self._pending_next_index = int(next_idx)
                self._pending_next_record_history = bool(record_history)
                self.stop_bump_playback()
                self._play_bump_with_optional_interstitial(bump_item)
                return

        self.play_index(next_idx, record_history=record_history, suppress_ui=bool(suppress_ui))

    def _fallback_previous_episode_index(self) -> int:
        pm = getattr(self, 'playlist_manager', None)
        if pm is None:
            return -1
        try:
            anchor = pm._anchor_episode_index(getattr(pm, 'current_index', -1))
        except Exception:
            anchor = -1
        if anchor < 0:
            return -1
        # Best-effort chronological previous episode.
        try:
            ordered = pm._chronological_episode_indices()
        except Exception:
            ordered = []
        if not ordered or anchor not in ordered:
            return -1
        pos = ordered.index(anchor)
        if pos <= 0:
            return -1
        return int(ordered[pos - 1])

    def skip_to_next_episode(self):
        """Manual Next: always skip to the next episode (no interludes/bumps).

        Interludes/bumps only play during natural EOF auto-advance.
        """
        pm = getattr(self, 'playlist_manager', None)
        if pm is None:
            return

        # Manual forward navigation mid-episode counts as a skip/cut-off.
        try:
            self._maybe_apply_episode_skip_penalty()
        except Exception:
            pass

        # If we were in bump playback or a bump-gated transition, cancel it.
        try:
            self._pending_next_index = None
            self._pending_next_record_history = True
        except Exception:
            pass
        try:
            self.stop_bump_playback()
        except Exception:
            pass

        next_idx = -1
        # If user had navigated back, honor forward history first (episode-only).
        try:
            next_idx = pm.step_forward_in_history_to_episode()
        except Exception:
            next_idx = -1

        if next_idx == -1:
            try:
                next_idx = pm.get_next_episode_index_episode_only(current_index=getattr(pm, 'current_index', -1))
            except Exception:
                next_idx = -1

        if next_idx == -1:
            try:
                self._set_stop_reason('end_of_playlist')
            except Exception:
                pass
            try:
                self.stop_playback()
            except Exception:
                pass
            return

        # Manual skip should not trigger bump-gating.
        try:
            self.play_index(int(next_idx), record_history=True, bypass_bump_gate=True, suppress_ui=False)
        except Exception:
            return

    def skip_to_previous_episode(self):
        """Manual Previous: always go to the previous episode (no interludes/bumps)."""
        pm = getattr(self, 'playlist_manager', None)
        if pm is None:
            return

        # First press goes to start of current episode if we're not near the beginning.
        try:
            pos = self._last_time_pos
            if pos is None and hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                pos = getattr(self.player.mpv, 'time_pos', None)
            if pos is not None and float(pos) > 3.0:
                self.player.seek(0)
                return
        except Exception:
            pass

        # If we were in bump playback or a bump-gated transition, cancel it.
        try:
            self._pending_next_index = None
            self._pending_next_record_history = True
        except Exception:
            pass
        try:
            self.stop_bump_playback()
        except Exception:
            pass

        prev_idx = -1
        try:
            prev_idx = pm.step_back_in_history_to_episode()
        except Exception:
            prev_idx = -1

        if prev_idx == -1:
            try:
                prev_idx = int(self._fallback_previous_episode_index())
            except Exception:
                prev_idx = -1

        if prev_idx == -1:
            return

        # History navigation should not append new history entries.
        try:
            self.play_index(int(prev_idx), record_history=False, bypass_bump_gate=True, suppress_ui=False)
        except Exception:
            return

    def play_previous(self):
        # 1) First press goes to start of current episode if we're not near the beginning.
        try:
            pos = self._last_time_pos
            if pos is None and hasattr(self, 'player') and self.player and getattr(self.player, 'mpv', None):
                pos = getattr(self.player.mpv, 'time_pos', None)
            if pos is not None and float(pos) > 3.0:
                self.player.seek(0)
                return
        except Exception:
            pass

        # 2) Otherwise, step back through actual playback history.
        try:
            self._maybe_apply_episode_skip_penalty()
        except Exception:
            pass
        pm = self.playlist_manager
        idx = -1
        try:
            idx = pm.step_back_in_history()
        except Exception:
            idx = -1

        if idx is not None and idx >= 0:
            self.play_index(idx, record_history=False)
        else:
            self.play_index(0, record_history=False)

    def on_playback_finished(self):
        # Ignore mpv EOF during bump scripts; bump_timer controls the bump sequence.
        try:
            if self.video_stack.currentIndex() == 1 and self.current_bump_script:
                return
        except Exception:
            pass

        # Audio-only bump EOF handling (no script controlling progression).
        # If we don't clear bump state here, `_in_bump_playback` can stay True and
        # effectively disable future interlude prerolls.
        try:
            if bool(getattr(self, '_in_bump_playback', False)) and not bool(getattr(self, '_current_bump_is_video', False)):
                try:
                    self._in_bump_playback = False
                    self.current_bump_script = None
                    self.current_card_index = 0
                except Exception:
                    pass

                # Honor bump-gated pending next index first.
                pending = getattr(self, '_pending_next_index', None)
                if pending is not None:
                    idx = int(pending)
                    record_history = bool(getattr(self, '_pending_next_record_history', True))
                    self._pending_next_index = None
                    self._pending_next_record_history = True
                    try:
                        self.play_index(idx, record_history=record_history, bypass_bump_gate=True, suppress_ui=True)
                    except Exception:
                        pass
                    return

                # No explicit pending index: advance, but do NOT immediately trigger another bump gate.
                self._advancing_from_bump_end = True
                try:
                    try:
                        self._bypass_bump_gate_once = True
                    except Exception:
                        pass
                    self.play_next()
                    return
                finally:
                    self._advancing_from_bump_end = False
        except Exception:
            pass

        # Video bump EOF handling.
        try:
            if bool(getattr(self, '_in_bump_playback', False)) and bool(getattr(self, '_current_bump_is_video', False)):
                # Non-inclusive: after video ends, run the bump script as a normal bump view.
                post_script = getattr(self, '_post_video_bump_script', None)
                inclusive = bool(getattr(self, '_current_bump_video_inclusive', False))
                if not inclusive:
                    cards = None
                    try:
                        if isinstance(post_script, dict):
                            cards = post_script.get('cards', [])
                        elif isinstance(post_script, list):
                            cards = post_script
                    except Exception:
                        cards = None

                if (not inclusive) and isinstance(cards, list) and cards:
                    try:
                        self._post_video_bump_script = None
                    except Exception:
                        pass
                    try:
                        self.video_stack.setCurrentIndex(1)
                    except Exception:
                        pass
                    try:
                        self.setWindowTitle("Sleepy Shows - [AS]")
                    except Exception:
                        pass
                    try:
                        self.current_bump_script = list(cards)
                        self.current_card_index = 0
                        try:
                            self._log_event('bump_video_eof', action='start_post_script')
                        except Exception:
                            pass
                        self.advance_bump_card()
                        return
                    except Exception:
                        # If bump script fails, fall through to normal advance.
                        pass

                try:
                    if (not inclusive) and (not cards):
                        self._log_event('bump_video_eof', action='no_post_script_cards')
                except Exception:
                    pass

                # Inclusive (or no post-script): bump is complete at video EOF.
                try:
                    self._hide_bump_video_overlay()
                except Exception:
                    pass

                try:
                    self._log_event('bump_video_eof', action='complete')
                except Exception:
                    pass

                # Clear bump state before advancing.
                try:
                    self._in_bump_playback = False
                    self._current_bump_is_video = False
                    self._current_bump_video_inclusive = False
                    self._current_bump_video_path = None
                    self._post_video_bump_script = None
                except Exception:
                    pass

                # Honor bump-gated pending next index first.
                pending = getattr(self, '_pending_next_index', None)
                if pending is not None:
                    idx = int(pending)
                    record_history = bool(getattr(self, '_pending_next_record_history', True))
                    self._pending_next_index = None
                    self._pending_next_record_history = True
                    try:
                        self.play_index(idx, record_history=record_history, bypass_bump_gate=True, suppress_ui=True)
                    except Exception:
                        pass
                    return

                # No explicit pending index: advance, but do NOT immediately trigger
                # another bump gate. Also, do not switch away from the video surface
                # here; on Linux/X11 hiding the mpv native window can cause mpv to exit
                # ("window destroyed").
                self._advancing_from_bump_end = True
                try:
                    try:
                        self._bypass_bump_gate_once = True
                    except Exception:
                        pass
                    self.play_next()
                    return
                finally:
                    self._advancing_from_bump_end = False
        except Exception:
            pass

        # If an interstitial preroll just ended, start the pending bump instead of advancing.
        pending_bump = getattr(self, '_pending_bump_item', None)
        if pending_bump is not None:
            try:
                self._pending_bump_item = None
            except Exception:
                pass
            try:
                self.play_bump(pending_bump)
                return
            except Exception:
                # Fall through to normal advance.
                try:
                    self._pending_bump_item = None
                except Exception:
                    pass

        # Auto advance
        try:
            self._set_stop_reason('mpv_eof')
        except Exception:
            pass
        self._sync_keep_awake()
        self._advancing_from_eof = True
        try:
            self.play_next()
        finally:
            self._advancing_from_eof = False

    def on_player_error(self, msg):
        print(f"Player Error: {msg}")

        try:
            self._set_stop_reason('player_error', message=str(msg or ''))
        except Exception:
            pass
        self._sync_keep_awake()

        try:
            self._persist_resume_state(force=True, reason='player_error')
        except Exception:
            pass

        # If this looks like a transient media disappearance (e.g., USB disconnect),
        # start a best-effort recovery loop that waits for the file to reappear.
        try:
            self._maybe_start_missing_media_recovery(reason='player_error')
        except Exception:
            pass

        # In packaged builds, stdout/stderr isn't visible. Show a helpful, non-spammy
        # dialog when playback fails so users understand what's wrong.
        try:
            # Only show one dialog per unique target per run.
            target = str(getattr(self, '_last_play_target', '') or '').strip()
            if not target:
                return

            last_shown = getattr(self, '_player_error_last_dialog_target', None)
            if last_shown == target:
                return
            self._player_error_last_dialog_target = target

            mode = 'portable'
            try:
                mode = str(getattr(self, 'playback_mode', 'portable') or 'portable').strip().lower()
            except Exception:
                mode = 'portable'

            # Provide a specific hint when it looks like the user is trying to play from
            # a configured network mount root (common: /mnt/shows) but it's not mounted.
            wfr = ''
            try:
                wfr = str(getattr(self, 'web_files_root', '') or '').strip()
            except Exception:
                wfr = ''

            hint = ''
            try:
                if wfr and (target == wfr or target.startswith(wfr.rstrip('/\\') + os.sep)):
                    hint = (
                        "\n\nIt looks like your library is on a mounted path that isn't available right now.\n"
                        f"Web Files Root (from Settings): {wfr}\n\n"
                        "If you're using a network share, make sure it is mounted in your OS, or switch to Settings → Web mode so the app can warn you when the mount is missing."
                    )
            except Exception:
                hint = ''

            src_path = str(getattr(self, '_last_play_source_path', '') or '').strip()
            shown_path = src_path if src_path else target

            title = 'Playback Error'
            body = (
                "Sleepy Shows couldn't open the media file.\n\n"
                f"Path: {shown_path}\n\n"
                "Common causes:\n"
                "- The drive/share isn't mounted\n"
                "- The file was moved/renamed\n"
                "- Permissions prevent reading the file"
                f"{hint}"
            )

            # Don't block playback flow with repeated popups.
            QMessageBox.warning(self, title, body)
        except Exception:
            return
        
    def on_player_paused(self, paused):
        suppress_ui = False
        try:
            suppress_ui = self._should_suppress_auto_nav_ui()
        except Exception:
            suppress_ui = False

        # Don't force controls visible on unpause. mpv often emits pause=False when a new
        # file starts; showing controls there causes the "pop".
        if bool(paused):
            self.show_controls()
        else:
            try:
                if (not suppress_ui) and bool(getattr(self, '_user_pause_toggle', False)):
                    self.show_controls()
            except Exception:
                pass

        try:
            self._user_pause_toggle = False
        except Exception:
            pass

        if not paused:
            self._played_since_start = True

        # Sleep timer only counts down while actively playing.
        if paused:
            self._pause_sleep_countdown()
        else:
            self._resume_sleep_countdown_if_needed()

        try:
            self._log_event('paused', paused=bool(paused))
        except Exception:
            pass

        try:
            if bool(paused):
                self._persist_resume_state(force=True, reason='paused')
        except Exception:
            pass
        self._sync_keep_awake()

    def on_mpv_end_file_reason(self, reason: str):
        try:
            r = str(reason or '').strip()
        except Exception:
            r = ''

        try:
            target = str(getattr(self, '_last_play_target', '') or '')
        except Exception:
            target = ''

        try:
            print(f"DEBUG: mpv end-file reason={r} target={target}")
        except Exception:
            pass

        self._log_event('mpv_end_file', reason=r, target=target)

        # If this wasn't EOF, capture it as a stop reason for overnight debugging.
        try:
            if r and r.lower() != 'eof':
                self._set_stop_reason(f'mpv_end_file:{r}')
        except Exception:
            pass

        try:
            if r and r.lower() != 'eof':
                self._persist_resume_state(force=True, reason=f'mpv_end_file:{r}')
        except Exception:
            pass

        try:
            if r and r.lower() != 'eof':
                self._maybe_start_missing_media_recovery(reason=f'mpv_end_file:{r}')
        except Exception:
            pass
        self._sync_keep_awake()

        # Bump-video reliability: mpv often reports missing/failed loads via end-file
        # reason=error (or similar) and does NOT emit playbackFinished.
        # Treat any non-EOF end-file as "finished" for bump videos so we can still
        # show the outro/post-script and/or advance past the bump gate.
        try:
            if r and r.lower() != 'eof':
                if bool(getattr(self, '_in_bump_playback', False)) and bool(getattr(self, '_current_bump_is_video', False)):
                    try:
                        vpath = str(getattr(self, '_current_bump_video_path', '') or '').strip()
                    except Exception:
                        vpath = ''

                    try:
                        self._log_event('bump_video_end_file', reason=str(r), video=str(vpath or ''), target=str(target or ''))
                    except Exception:
                        pass

                    # One warning per failed target per run.
                    try:
                        key = str(target or vpath or '').strip()
                        last = getattr(self, '_bump_video_error_last_dialog_target', None)
                        if key and last != key:
                            self._bump_video_error_last_dialog_target = key
                            try:
                                QMessageBox.warning(
                                    self,
                                    'Bump Video Failed',
                                    (
                                        "Sleepy Shows couldn't play the bump video.\n\n"
                                        f"Reason: {str(r)}\n"
                                        f"Video: {vpath or '(unknown)'}\n\n"
                                        f"Debug log: {getattr(self, '_playback_log_path', '')}"
                                    ),
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass

                    try:
                        QTimer.singleShot(0, self.on_playback_finished)
                    except Exception:
                        try:
                            self.on_playback_finished()
                        except Exception:
                            pass
        except Exception:
            pass

        # Episode title overlay behavior:
        # - Windowed: shown briefly on episode start (see play_index)
        # - Fullscreen: shown only while controls are visible (see show_controls/hide_controls)

    # Control Proxies
    def set_volume(self, value):
        self.player.set_volume(value)

    def on_seek_start(self):
        self.is_seeking = True
        
    def on_seek_end(self):
        val = self.play_mode_widget.slider_seek.value()
        # Calculate target time
        if self.total_duration > 0:
            pct = max(0.0, min(100.0, float(val)))
            target = (pct / 100.0) * float(self.total_duration)
            self.player.seek(target)
        self.is_seeking = False

    def seek_relative(self, offset):
        if self.player:
            self.player.seek_relative(offset)

    def seek_video(self, val):
        # Called by sliderMoved (dragging and our ClickableSlider click-to-position).
        # Seek immediately so timeline clicks work.
        try:
            if self.total_duration > 0:
                pct = max(0.0, min(100.0, float(val)))
                target = (pct / 100.0) * float(self.total_duration)
                self.player.seek(target)
        except Exception:
            return

    def update_seeker(self, time_pos):
        # If the sleep timer is enabled, start/resume countdown only once
        # playback is actually progressing.
        if getattr(self, 'sleep_timer_active', False):
            self._resume_sleep_countdown_if_needed()

        self._last_time_pos = time_pos

        # Progress tracking for missing-media stall detection.
        try:
            now = float(time.monotonic())
            self._time_pos_last_update_mono = now
        except Exception:
            now = 0.0
        try:
            prev = getattr(self, '_time_pos_last_value', None)
        except Exception:
            prev = None
        try:
            cur = float(time_pos) if time_pos is not None else None
        except Exception:
            cur = None
        try:
            self._time_pos_last_value = cur
        except Exception:
            pass
        try:
            if now and cur is not None:
                if prev is None or abs(float(cur) - float(prev)) >= 0.25:
                    self._time_pos_last_progress_mono = now
        except Exception:
            pass

        try:
            self._persist_resume_state(force=False, reason='time_pos')
        except Exception:
            pass

        if not self.is_seeking and self.total_duration > 0:
            dur = float(self.total_duration)
            pos = float(time_pos) if time_pos is not None else 0.0

            # mpv often reports time-pos that never exactly equals duration,
            # so snap to 100% when we're effectively at the end.
            if pos >= max(0.0, dur - 0.10):
                percent = 100
            else:
                ratio = 0.0 if dur <= 0 else max(0.0, min(1.0, pos / dur))
                percent = int(round(ratio * 100.0))

            self.play_mode_widget.slider_seek.setValue(percent)
            
        self.update_time_label(time_pos, self.total_duration)

    def _check_playback_end(self):
        # Runs on the Qt thread.
        try:
            if not hasattr(self, 'player') or not self.player or not getattr(self.player, 'mpv', None):
                return

            # Don't auto-advance during bump scripts; bump_timer controls progression.
            try:
                if self.video_stack.currentIndex() == 1 and self.current_bump_script:
                    return
            except Exception:
                pass

            if not self.playlist_manager.current_playlist:
                return

            # Bump-video playback isn't tied to playlist indices. Track EOF separately.
            is_bump_video = False
            try:
                is_bump_video = bool(getattr(self, '_in_bump_playback', False)) and bool(getattr(self, '_current_bump_is_video', False))
            except Exception:
                is_bump_video = False

            idx = self.playlist_manager.current_index
            if (idx is None or idx < 0) and (not is_bump_video):
                return

            if not is_bump_video:
                # Only handle EOF once per index.
                if self._handled_eof_for_index == idx:
                    return
            else:
                try:
                    vpath = str(getattr(self, '_current_bump_video_path', '') or '').strip()
                except Exception:
                    vpath = ''
                try:
                    key = (str(vpath), float(getattr(self, '_play_start_monotonic', 0.0) or 0.0))
                except Exception:
                    key = (str(vpath), 0.0)
                try:
                    if getattr(self, '_handled_eof_for_bump_key', None) == key:
                        return
                except Exception:
                    pass

            # Avoid firing immediately after starting a file.
            if self._play_start_monotonic is not None:
                if (time.monotonic() - self._play_start_monotonic) < 0.75:
                    return

            mpv = self.player.mpv

            # Missing-media stall detection: if playback appears to be active but
            # time-pos isn't advancing and the current target disappeared, enter
            # recovery immediately (avoid permanent gray screen).
            try:
                # Don't interfere while we are already waiting for reconnect.
                if getattr(self, '_missing_media_waiting_for_target', None):
                    pass
                else:
                    core_idle_now = bool(getattr(mpv, 'core_idle', False))
                    paused_now = bool(getattr(mpv, 'pause', True))
                    if (not core_idle_now) and (not paused_now) and (not is_bump_video):
                        last_prog = float(getattr(self, '_time_pos_last_progress_mono', 0.0) or 0.0)
                        if last_prog:
                            stall_s = float(time.monotonic()) - last_prog
                            if stall_s > float(getattr(self, '_missing_media_stall_timeout_s', 2.5) or 2.5):
                                target = str(getattr(self, '_last_play_target', '') or '').strip()
                                if target:
                                    try:
                                        if not os.path.exists(target):
                                            self._maybe_start_missing_media_recovery(reason='watchdog_stall')
                                    except Exception:
                                        pass
            except Exception:
                pass

            # Prefer mpv's eof flag if available.
            eof_reached = False
            try:
                eof_reached = bool(getattr(mpv, 'eof_reached'))
            except Exception:
                eof_reached = False

            # Fallback: if time-pos is basically duration and mpv is idle/paused.
            pos = self._last_time_pos
            dur = self.total_duration
            if not dur:
                try:
                    dur = float(getattr(mpv, 'duration') or 0)
                except Exception:
                    dur = 0

            core_idle = False
            paused = True
            try:
                core_idle = bool(getattr(mpv, 'core_idle'))
            except Exception:
                core_idle = False
            try:
                paused = bool(getattr(mpv, 'pause'))
            except Exception:
                paused = True

            pos_at_end = False
            if dur and pos is not None:
                try:
                    pos_at_end = float(pos) >= (float(dur) - 0.15)
                except Exception:
                    pos_at_end = False

            should_advance = False
            if eof_reached:
                should_advance = True
            elif self._played_since_start and pos_at_end and (core_idle or paused):
                should_advance = True

            if should_advance:
                if not is_bump_video:
                    self._handled_eof_for_index = idx
                else:
                    try:
                        self._handled_eof_for_bump_key = key
                    except Exception:
                        pass
                QTimer.singleShot(0, self.on_playback_finished)
        except Exception:
            return

    def update_duration(self, duration):
        self.total_duration = duration
        self.update_time_label(0, duration) # Reset current? or keep

        try:
            self._persist_resume_state(force=False, reason='duration')
        except Exception:
            pass

        # Inclusive bump-video outro alignment: schedule overlay once we know the video length.
        try:
            if bool(getattr(self, '_current_bump_is_video', False)) and bool(getattr(self, '_current_bump_video_inclusive', False)):
                vpath = str(getattr(self, '_current_bump_video_path', '') or '').strip()
                if vpath:
                    dur_ms = None
                    try:
                        dur_ms = int(round(float(duration) * 1000.0)) if duration is not None else None
                    except Exception:
                        dur_ms = None
                    if dur_ms is not None and dur_ms > 0:
                        # Cache it for future starts.
                        try:
                            bm = getattr(getattr(self, 'playlist_manager', None), 'bump_manager', None)
                            if bm is not None:
                                try:
                                    ap = os.path.abspath(vpath)
                                except Exception:
                                    ap = vpath
                                k = bm._norm_path_key(ap)
                                if k and (getattr(bm, 'video_durations_ms', None) is not None):
                                    bm.video_durations_ms[k] = int(dur_ms)
                        except Exception:
                            pass

                        try:
                            self._schedule_inclusive_bump_video_overlay(int(dur_ms))
                        except Exception:
                            pass
        except Exception:
            pass

    def update_time_label(self, current, total):
        def fmt(s):
            m, s = divmod(int(s), 60)
            h, m = divmod(m, 60)
            if h > 0: return f"{h}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"
        self.play_mode_widget.lbl_current_time.setText(f"{fmt(current)} / {fmt(total)}")

    def on_sleep_timer(self):
        try:
            self._set_stop_reason('sleep_timer_fired')
        except Exception:
            pass
        self.stop_playback()
        self.set_mode(0) # Go to Welcome

        # Ensure internal state + UI reflects Off after timer fires.
        self.cancel_sleep_timer()

    def closeEvent(self, event):
        try:
            self._log_event('app_close')
        except Exception:
            pass

        try:
            self._persist_resume_state(force=True, reason='app_close')
        except Exception:
            pass

        try:
            if hasattr(self, '_resume_recover_timer'):
                self._resume_recover_timer.stop()
        except Exception:
            pass
        try:
            self._keep_awake.disable()
        except Exception:
            pass
        try:
            self._keep_awake_active = False
        except Exception:
            pass
        return super().closeEvent(event)


if __name__ == "__main__":
    import locale
    locale.setlocale(locale.LC_NUMERIC, 'C')

    # On Windows, ensure the process is DPI-aware so Qt sees the real screen
    # geometry and our percent-of-screen sizing matches the user's resolution.
    try:
        if platform.system().lower().startswith('win'):
            import ctypes
            try:
                # Per-monitor v2 DPI awareness (best on modern Windows).
                ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                try:
                    # Fallback for older Windows.
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
    except Exception:
        pass

    app = QApplication(sys.argv)

    loading = StartupLoadingScreen()
    loading.set_progress(0, "Starting...")
    loading.show()

    # Let the splash paint before constructing the main window.
    try:
        app.processEvents()
    except Exception:
        pass

    # The splash is intentionally an *artificial* load screen.
    # Keep the UI responsive but block the app from constructing until it hits 100%.
    try:
        loading.run_blocking_fake_load(app, min_seconds=2.0, max_seconds=4.0)
    except Exception:
        pass

    window = MainWindow()

    # Show the main window, then close the splash immediately.
    window.show()

    # Log startup sizing to a file (useful for packaged EXE where stdout isn't visible).
    try:
        QTimer.singleShot(0, lambda: _append_startup_geometry_log(window))
    except Exception:
        pass

    # Optional debug: print startup geometry/sizing to stdout.
    if os.getenv('SLEEPYSHOWS_DEBUG_GEOMETRY', '').strip() == '1':
        def _print_startup_geometry():
            try:
                avail = getattr(window, '_startup_available_size', None)
                req = getattr(window, '_startup_requested_size', None)
                sz = window.size()
                print(f"DEBUG: startup available={avail} requested={req} actual={(sz.width(), sz.height())} maximized={window.isMaximized()}")
            except Exception:
                pass
        try:
            QTimer.singleShot(0, _print_startup_geometry)
        except Exception:
            pass

    try:
        loading.close()
    except Exception:
        pass

    # Start ambient only after the splash is gone.
    try:
        QTimer.singleShot(0, window._start_startup_ambient)
    except Exception:
        pass
    sys.exit(app.exec())
