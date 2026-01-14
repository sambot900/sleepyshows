import sys
import os
import json
import time
import platform
import html
import random
import glob
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QPushButton, QFileDialog, QTreeWidget, 
                               QTreeWidgetItem, QSplitter, QLabel, QSlider, QTabWidget,
                               QListWidget, QListWidgetItem, QInputDialog, QMessageBox, QMenu, QStackedWidget,
                               QDockWidget, QFrame, QSizePolicy, QToolButton, QStyle, QGridLayout,
                               QStyleOptionButton, QStyleOptionToolButton, QStylePainter, QStyleOptionSlider,
                               QLineEdit)
from PySide6.QtCore import Qt, QTimer, QSize, Signal, QPropertyAnimation, QEasingCurve, QRect, QEvent, QObject, QThread, Slot, QPoint
from PySide6.QtGui import QAction, QActionGroup, QIcon, QFont, QColor, QPalette, QPixmap, QPainter, QBrush, QLinearGradient, QRadialGradient, QPen, QPainterPath, QImage

from player_backend import MpvPlayer, MpvAudioPlayer
from playlist_manager import PlaylistManager, VIDEO_EXTENSIONS, natural_sort_key
from ui_styles import DARK_THEME


THEME_COLOR = "#0e1a77"

# White strokes are intentionally transparent so the global background gradient shows through.
WHITE_STROKE_ALPHA = 0


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
        fit_scale = min(vw / float(iw), vh / float(ih))
        if fit_scale > 1.0:
            scale = min(2.0, fit_scale)
        else:
            scale = fit_scale

        w0 = float(iw) * float(scale)
        h0 = float(ih) * float(scale)

        # If we scaled to 200% and still don't hit either dimension, just center.
        if abs(scale - 2.0) < 1e-6 and w0 < vw and h0 < vh:
            x = int(round((vw - w0) / 2.0))
            y = int(round((vh - h0) / 2.0))
            return QRect(x, y, int(round(w0)), int(round(h0)))

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

        # --- Sound settings ---
        self._check_on_icon = QIcon(get_asset_path("check.png"))
        self._check_off_icon = QIcon(get_asset_path("checkbox.png"))

        def add_toggle_row(label_text, initial_checked, on_toggle):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)

            lbl = QLabel(label_text)
            lbl.setStyleSheet("font-size: 16px; color: white;")

            btn = QPushButton("")
            btn.setCheckable(True)
            btn.setChecked(bool(initial_checked))
            btn.setFixedSize(40, 40)
            btn.setIconSize(QSize(28, 28))
            btn.setStyleSheet("QPushButton { background: transparent; border: none; }")

            def refresh_icon():
                btn.setIcon(self._check_on_icon if btn.isChecked() else self._check_off_icon)

            refresh_icon()
            btn.clicked.connect(lambda _=False: (refresh_icon(), on_toggle(btn.isChecked())))

            row.addWidget(btn)
            row.addWidget(lbl)
            row.addStretch(1)
            layout.addLayout(row)
            return btn

        self.btn_startup_crickets = add_toggle_row(
            "Startup cricket sound",
            getattr(self.main_window, "startup_crickets_enabled", True),
            lambda checked: self.main_window.set_startup_crickets_enabled(checked),
        )

        self.btn_normalize_audio = add_toggle_row(
            "Normalize volume",
            getattr(self.main_window, "normalize_audio_enabled", False),
            lambda checked: self.main_window.set_normalize_audio_enabled(checked),
        )

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

        # Sync toggle icons/checked state from main window settings.
        try:
            if hasattr(self, 'btn_startup_crickets'):
                self.btn_startup_crickets.setChecked(bool(getattr(self.main_window, 'startup_crickets_enabled', True)))
            if hasattr(self, 'btn_normalize_audio'):
                self.btn_normalize_audio.setChecked(bool(getattr(self.main_window, 'normalize_audio_enabled', False)))
            if hasattr(self, '_check_on_icon') and hasattr(self, '_check_off_icon'):
                if hasattr(self, 'btn_startup_crickets'):
                    self.btn_startup_crickets.setIcon(self._check_on_icon if self.btn_startup_crickets.isChecked() else self._check_off_icon)
                if hasattr(self, 'btn_normalize_audio'):
                    self.btn_normalize_audio.setIcon(self._check_on_icon if self.btn_normalize_audio.isChecked() else self._check_off_icon)
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


def get_local_bumps_scripts_dir():
    # Store bump scripts alongside the app, similar to the local `playlists/` folder.
    if getattr(sys, 'frozen', False):
        # In a frozen build, sys._MEIPASS points to the unpacked internal bundle.
        # Use the executable directory so the folder is user-visible and persistent.
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, 'bumps')


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
        for key in ("King of the Hill", "Bob's Burgers"):
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
            os.path.join('King of the Hill', 'Episodes'),
            # Older/fallback layouts
            os.path.join('King of the Hill', 'King of the Hill'),
            os.path.join('King of the Hill'),
        ]),
        ("Bob's Burgers", [
            # Preferred layout (note: "Episodesl" per user)
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
            os.path.join('King of the Hill', 'Episodes'),
            os.path.join('King of the Hill', 'King of the Hill'),
            os.path.join('King of the Hill'),
        ]),
        ("Bob's Burgers", [
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
            # Stop early if we've found both.
            if len(found) >= 2:
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


def _scan_episode_files(folder_path):
    """Return naturally sorted full paths of video files under folder_path."""
    results = []
    if not folder_path or not os.path.isdir(folder_path):
        return results

    try:
        for root, dirs, files in os.walk(folder_path):
            dirs.sort(key=natural_sort_key)
            files.sort(key=natural_sort_key)
            for f in files:
                if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                    results.append(os.path.join(root, f))
    except Exception:
        return []

    return results


def _write_auto_playlist_json(playlist_filename, episode_folder, default_shuffle_mode='standard'):
    """Create/update a playlist JSON under playlists/ for a given episodes folder.

    Regeneration policy:
    - If the playlist file does not exist, create it.
    - If the playlist exists and its stored 'source_folder' still exists on disk, do nothing.
      (This allows users to freely change settings like shuffle mode without triggering rewrites.)
    - If the playlist exists but its 'source_folder' is missing (e.g. drive letter changed),
      rewrite it using the newly detected episode_folder and preserve user settings when possible.
    """
    try:
        files_dir = os.path.join(os.getcwd(), 'playlists')
        os.makedirs(files_dir, exist_ok=True)
        playlist_path = os.path.join(files_dir, playlist_filename)

        existing = None
        if os.path.exists(playlist_path):
            try:
                with open(playlist_path, 'r') as f:
                    existing = json.load(f)
            except Exception:
                existing = None

        # If the existing playlist still points at a valid folder, don't touch it.
        try:
            existing_source = (existing or {}).get('source_folder', '')
            if existing_source and os.path.isdir(existing_source):
                return False
        except Exception:
            pass

        eps = _scan_episode_files(episode_folder)
        if not eps:
            return False

        # Preserve user settings if the file already existed.
        shuffle_mode = None
        interstitial_folder = ''
        try:
            if isinstance(existing, dict):
                interstitial_folder = existing.get('interstitial_folder', '') or ''
                shuffle_mode = existing.get('shuffle_mode', None)
                if shuffle_mode is None:
                    shuffle_default = bool(existing.get('shuffle_default', False))
                    shuffle_mode = 'standard' if shuffle_default else 'off'
        except Exception:
            shuffle_mode = None

        if shuffle_mode not in ('off', 'standard', 'season'):
            shuffle_mode = default_shuffle_mode

        data = {
            'playlist': [{'type': 'video', 'path': p} for p in eps],
            'interstitial_folder': interstitial_folder,
            'shuffle_default': (shuffle_mode != 'off'),
            'shuffle_mode': shuffle_mode,
            'auto_generated': True,
            'source_folder': episode_folder,
        }
        with open(playlist_path, 'w') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


class AutoConfigWorker(QObject):
    finished = Signal(object)

    def __init__(self, volume_label='T7'):
        super().__init__()
        self.volume_label = volume_label

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
        }

        try:
            show_folders = auto_detect_show_folders(volume_label=self.volume_label)
            result['tv_vibe_scripts_dir'] = auto_detect_tv_vibe_scripts_dir(volume_label=self.volume_label)
            result['tv_vibe_music_dir'] = auto_detect_tv_vibe_music_dir(volume_label=self.volume_label)
            result['tv_vibe_images_dir'] = auto_detect_tv_vibe_images_dir(volume_label=self.volume_label)
            result['tv_vibe_audio_fx_dir'] = auto_detect_tv_vibe_audio_fx_dir(volume_label=self.volume_label)
            sources = []
            for key in ("King of the Hill", "Bob's Burgers"):
                p = show_folders.get(key)
                if p:
                    sources.append(p)

            result['show_folders'] = show_folders
            result['sources'] = sources

            # Build library scan in the worker so the UI thread stays responsive.
            if sources:
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
                ) or updated
            if show_folders.get("King of the Hill"):
                updated = _write_auto_playlist_json(
                    "King of the Hill.json",
                    show_folders["King of the Hill"],
                    default_shuffle_mode='standard',
                ) or updated
            result['playlists_updated'] = bool(updated)
        except Exception:
            # Best-effort only.
            pass

        self.finished.emit(result)

# --- Custom Widgets ---

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
        self.setup_ui()
        
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
            btn = QPushButton()
            
            # Store original pixmap for resizing later
            path = get_asset_path(icon_name)
            pix = QPixmap(path)
            btn.setProperty("original_pixmap", pix)
            
            # Initial Setup - Icons will be set in resizeEvent
            # Use Fixed size mode initially, updated in resizeEvent
            # We set a placeholder size
            btn.setFixedSize(220, 320)
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
        shows_layout.addWidget(self.btn_koth)
        self.show_btns.append(self.btn_koth)
        
        # Bobs Burgers
        self.btn_bobs = create_show_btn("bobs-icon.png", lambda: self.load_show_playlist("Bob's Burgers"))
        shows_layout.addWidget(self.btn_bobs)
        self.show_btns.append(self.btn_bobs)
        
        main_layout.addLayout(shows_layout)
        main_layout.addStretch(1) # Balanced vertical centering
        
        # 3. Footer Bar
        footer_widget = QWidget()
        footer_widget.setFixedHeight(80)
        footer_widget.setStyleSheet("background-color: rgba(40, 40, 90, 200);")
        footer_layout = QHBoxLayout(footer_widget)
        footer_layout.setContentsMargins(40, 5, 40, 5)
        
        # Helper for image buttons
        def create_img_btn(filename, callback):
            btn = QPushButton()
            path = get_asset_path(filename)
            pix = QPixmap(path)
            
            if not pix.isNull():
                h = 50
                w = int(pix.width() * (h / pix.height())) if pix.height() > 0 else 50
                btn.setIcon(QIcon(pix))
                btn.setIconSize(QSize(w, h))
                btn.setFixedSize(w + 10, h + 10) 
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
             layout.setContentsMargins(5,5,5,5)
             layout.setSpacing(10)
             
             # Checkbox (Custom Button)
             chk = QPushButton()
             chk.setFlat(True)
             chk.setStyleSheet("border: none; background: transparent;")
             chk.setFixedSize(40, 40)
             chk.clicked.connect(toggle_callback)
             setattr(self, check_btn_ref_name, chk) # Save ref
             layout.addWidget(chk)
             
             # Text Label (Image Button)
             path = get_asset_path(text_img_name)
             pix = QPixmap(path)
             
             txt_btn = QPushButton()
             if not pix.isNull():
                h = 50
                w = int(pix.width() * (h / pix.height())) if pix.height() > 0 else 50
                txt_btn.setIcon(QIcon(pix))
                txt_btn.setIconSize(QSize(w, h))
                txt_btn.setFixedSize(w + 10, h + 10)
             txt_btn.setFlat(True)
             txt_btn.setStyleSheet("border: none; background: transparent;")
             txt_btn.clicked.connect(toggle_callback)
             layout.addWidget(txt_btn)
             
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
        
        # 4. Clouds (Absolute Positioned, Top)
        self.lbl_clouds = QLabel(self)
        self.lbl_clouds.setProperty("original_pixmap", QPixmap(get_asset_path("clouds.png")))
        self.lbl_clouds.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.lbl_clouds.setStyleSheet("background: transparent;") # Crucial for gradient visibility
        self.lbl_clouds.setGeometry(0, 0, 1200, 150)
        
        # 5. Logo (Absolute Positioned, Top Center)
        self.lbl_logo = QLabel(self)
        self.lbl_logo.setProperty("original_pixmap", QPixmap(get_asset_path("sleepy-shows-logo.png")))
        self.lbl_logo.setStyleSheet("background: transparent;")
        self.lbl_logo.setAlignment(Qt.AlignCenter)
        
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
        elif show_name == "Bob's Burgers":
            btn = getattr(self, 'btn_bobs', None)

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
                 # Scale logo much bigger
                 logo_w = 600 # Was 400
                 logo_h = 225
                 scaled_logo = orig_logo.scaled(logo_w, logo_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                 self.lbl_logo.setPixmap(scaled_logo)
                 # Center X, Top Y (e.g. 20px down)
                 x_pos = (w - scaled_logo.width()) // 2
                 self.lbl_logo.setGeometry(x_pos, 20, scaled_logo.width(), scaled_logo.height())

        # 3. Scale Show Buttons
        # Scale based on window height
        target_h = int(h * 0.45) 
        
        for btn in self.show_btns:
            orig = btn.property("original_pixmap")
            if orig and not orig.isNull():
                 scaled_pix = orig.scaledToHeight(target_h, Qt.SmoothTransformation)
                 btn.setIcon(QIcon(scaled_pix))
                 btn.setIconSize(scaled_pix.size())
                 # Explicitly set Fixed Size to match icon so hover area matches image exactly
                 # BARELY surrounding: If FixedSize == IconSize, it is exact.
                 # If border-radius cuts off corners, we reduce padding? 
                 # Actually QIcon fills the rect.
                 btn.setFixedSize(scaled_pix.size())

                 overlay = getattr(btn, '_pending_overlay', None)
                 if overlay is not None:
                     overlay.setGeometry(btn.rect())
        
        super().resizeEvent(event)

        
    def update_checkbox(self, btn, checked):
        base = QPixmap(get_asset_path("checkbox.png"))
        if base.isNull(): return
        
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
        
        btn.setIcon(QIcon(result))
        btn.setIconSize(base.size())
        btn.setFixedSize(base.size())

    def load_show_playlist(self, show_name):
        filename = os.path.join("playlists", f"{show_name}.json")
        if os.path.exists(filename):
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
        self.update_checkbox(self.btn_vibes_check, self.is_vibes_on)
        self.main_window.set_bumps_enabled(self.is_vibes_on)

    def toggle_sleep(self):
        self.is_sleep_on = not self.is_sleep_on
        self.update_checkbox(self.btn_sleep_check, self.is_sleep_on)
        
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
        lib_layout.addWidget(QLabel("Library"))
        
        self.library_tree = QTreeWidget()
        self.library_tree.setHeaderLabel("Episodes")
        self.library_tree.setDragEnabled(True)
        self.library_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.library_tree.itemDoubleClicked.connect(self.main_window.play_from_library)
        
        lib_layout.addWidget(self.library_tree)
        
        # Library Controls
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
        lib_layout.addLayout(lib_controls)
        
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
        
        self.chk_interstitials = QPushButton("Interstitials: OFF")
        self.chk_interstitials.setCheckable(True)
        self.chk_interstitials.toggled.connect(lambda c: self.chk_interstitials.setText(f"Interstitials: {'ON' if c else 'OFF'}"))
        h_gen.addWidget(self.chk_interstitials)
        gen_controls.addLayout(h_gen)
        
        self.btn_set_interstitial = QPushButton("Set Interstitial Folder")
        self.btn_set_interstitial.clicked.connect(self.main_window.choose_interstitial_folder)
        gen_controls.addWidget(self.btn_set_interstitial)
        
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
    
    def add_dropped_items(self, items):
        self.main_window.add_dropped_items(items)

    def add_selected_to_playlist(self):
        self.main_window.add_selected_to_playlist()

    def generate_playlist(self):
        # Gather settings from local buttons and call main window
        self.main_window.generate_playlist_logic(
            shuffle_mode=self.shuffle_mode,
            interstitials=self.chk_interstitials.isChecked(),
            bumps=self.main_window.bumps_enabled
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
                    self.playlist_list.addItem(f"{i+1}. [INT] {name}")
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
        
        # Buttons Row
        btns_layout = QHBoxLayout()
        btns_layout.setContentsMargins(10, 0, 10, 0)
        
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
        btns_layout.addWidget(self.btn_menu)
        
        btns_layout.addStretch(1) # Stretch to center the middle group
        
        # --- Center Group: Playback Controls ---
        self.btn_seek_back = TriStrokeButton("-20s")
        self.btn_seek_back.setFixedSize(100, button_height)
        self.btn_seek_back.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_seek_back.clicked.connect(lambda: self.main_window.seek_relative(-20))
        btns_layout.addWidget(self.btn_seek_back)
        
        self.btn_prev = TriStrokeButton("<<")
        self.btn_prev.setText("")
        self.btn_prev.setIcon(self._tint_icon(self.style().standardIcon(QStyle.SP_MediaSkipBackward), QSize(32, 32), Qt.white))
        self.btn_prev.setIconSize(QSize(32, 32))
        self.btn_prev.setFixedSize(100, button_height)
        self.btn_prev.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_prev.clicked.connect(self.main_window.play_previous)
        btns_layout.addWidget(self.btn_prev)
        
        # Static play/pause icon button (doesn't change dynamically)
        self.btn_play = TriStrokeButton()
        self.btn_play.setIcon(self._make_play_slash_pause_icon(icon_h=40))
        self.btn_play.setIconSize(QSize(90, 40))
        self.btn_play.setFixedSize(140, button_height)
        self.btn_play.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_play.clicked.connect(self.main_window.toggle_play)
        btns_layout.addWidget(self.btn_play)
        
        self.btn_next = TriStrokeButton(">>")
        self.btn_next.setText("")
        self.btn_next.setIcon(self._tint_icon(self.style().standardIcon(QStyle.SP_MediaSkipForward), QSize(32, 32), Qt.white))
        self.btn_next.setIconSize(QSize(32, 32))
        self.btn_next.setFixedSize(100, button_height)
        self.btn_next.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_next.clicked.connect(self.main_window.play_next)
        btns_layout.addWidget(self.btn_next)
        
        self.btn_seek_fwd = TriStrokeButton("+20s")
        self.btn_seek_fwd.setFixedSize(100, button_height)
        self.btn_seek_fwd.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        self.btn_seek_fwd.clicked.connect(lambda: self.main_window.seek_relative(20))
        btns_layout.addWidget(self.btn_seek_fwd)
        
        btns_layout.addStretch(1) # Stretch to push right group to end
        
        # --- Right Group: Shuffle, Vol, Fullscreen ---

        # Sleep Timer Button (shows remaining minutes)
        self.btn_sleep_timer = TriStrokeButton("SLEEP\nOFF")
        self.btn_sleep_timer.setFixedSize(120, button_height)
        self.btn_sleep_timer.setStyleSheet(font_style + " background: transparent; border: none; color: white;")
        # Single-press cycle (menu dropdown is still available from the top menu).
        self.btn_sleep_timer.clicked.connect(self.main_window.cycle_sleep_timer_quick)
        btns_layout.addWidget(self.btn_sleep_timer)

        btns_layout.addSpacing(10)
        
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
        btns_layout.addWidget(self.btn_shuffle)
        
        btns_layout.addSpacing(10)
        
        self.lbl_volume = QLabel("Vol:")
        self.lbl_volume.setStyleSheet("font-size: 14pt; color: white;")
        btns_layout.addWidget(self.lbl_volume)
        
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
        btns_layout.addWidget(self.slider_vol)

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
        btns_layout.addWidget(self.btn_fullscreen)
        
        controls_layout.addLayout(btns_layout)
        
        # Assemble Video Area
        self.video_layout.addWidget(self.video_placeholder, 1) # This will be replaced
        self.video_layout.addWidget(self.controls_widget)
        
        self.layout.addWidget(self.video_area, 1) # Expand
        
        self.sidebar_visible = True
        self.sidebar_container.setVisible(True)
        self.refresh_playlists()

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
        else:
            self.controls_widget.setParent(self.video_area) # Make child of video area again
            self.video_layout.addWidget(self.controls_widget)
            self.controls_widget.setStyleSheet("background-color: #1a1a1a;") # Solid
            
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
        self.resize(1200, 800)
        
        # Data
        self.playlist_manager = PlaylistManager()

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
        self.auto_config_volume_label = str(self._settings.get('auto_config_volume_label', 'T7') or 'T7').strip() or 'T7'

        try:
            self.playlist_manager.bump_manager.bump_images_dir = self.bump_images_dir
        except Exception:
            pass

        try:
            self.playlist_manager.bump_manager.bump_audio_fx_dir = self.bump_audio_fx_dir
        except Exception:
            pass

        # Startup ambient audio
        self._startup_ambient_playing = False
        self._startup_ambient_path = get_asset_path("crickets.mp3")

        # Global bumps toggle (controlled from Welcome)
        self.bumps_enabled = False
        
        # Timers
        self.sleep_timer_default_minutes = 180
        # Single source of truth for timer duration
        self.current_sleep_minutes = self.sleep_timer_default_minutes
        # Manual flag to ensure UI sync reliably (do not rely on QTimer.isActive())
        self.sleep_timer_active = False

        # Sleep timer countdown is paused unless a show is actively playing.
        self.sleep_remaining_ms = 0
        self._sleep_last_tick = None
        self.sleep_countdown_timer = QTimer()
        self.sleep_countdown_timer.setInterval(1000)
        self.sleep_countdown_timer.timeout.connect(self._on_sleep_countdown_tick)
        
        # Mouse Hover Timer
        self.hover_timer = QTimer()
        self.hover_timer.setInterval(2500) # 2.5s hide
        self.hover_timer.setSingleShot(True)
        self.hover_timer.timeout.connect(self.hide_controls)
        self.setMouseTracking(True) # Track mouse without buttons

        self.bump_timer = QTimer()
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
        self._handled_eof_for_index = None
        self._play_start_monotonic = None
        self._played_since_start = False
        self.was_maximized = False # Track window state for fullscreen toggle
        self.last_activity_time = time.time()

        # Playback watchdog: some MPV setups do not reliably deliver end-file events.
        # This ensures we still auto-advance when a file reaches EOF.
        self.playback_watchdog = QTimer()
        self.playback_watchdog.setInterval(500)
        self.playback_watchdog.timeout.connect(self._check_playback_end)
        self.playback_watchdog.start()
        
        # Failsafe timer for fullscreen
        self.failsafe_timer = QTimer()
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
        self._bump_fx_stop_timer = QTimer()
        self._bump_fx_stop_timer.setSingleShot(True)
        self._bump_fx_stop_timer.timeout.connect(self._stop_bump_fx)
        self._bump_fx_active = False
        self._bump_fx_policy = None  # None | 'duration' | 'card' | 'ms'
        self._bump_fx_interrupt_prev_mute = None

        # Bump music cut: when True, bump music is muted for the remainder of the bump.
        self._bump_music_cut_active = False
        self._bump_music_cut_prev_mute = None

        # Outro audio exclusivity: when True, block other bump FX so outro audio is the only sound.
        self._bump_outro_audio_exclusive = False

        # Optional outro audio (<outro ... audio>): pick a random sound from this folder.
        self._outro_sounds_dir = os.path.join('/media', 'tyler', 'T7', 'Sleepy Shows Data', 'TV Vibe', 'outro sounds')

        # Apply audio normalization as early as possible.
        try:
            self.player.set_audio_normalization(self.normalize_audio_enabled)
        except Exception:
            pass
        
        # Overlay for Episode Title
        self.overlay_label = QLabel(self.video_container)
        self.overlay_label.setAlignment(Qt.AlignCenter)
        self.overlay_label.setStyleSheet("background-color: rgba(0, 0, 0, 150); color: white; padding: 10px; font-size: 18px; font-weight: bold;")
        self.overlay_label.setVisible(False)
        self.overlay_label.setAttribute(Qt.WA_TransparentForMouseEvents) # Let clicks pass through
        
        # We need to manually position this because it's an overlay
        self.video_container.installEventFilter(self)

        self.player.positionChanged.connect(self.update_seeker)
        self.player.durationChanged.connect(self.update_duration)
        self.player.playbackFinished.connect(self.on_playback_finished)
        self.player.errorOccurred.connect(self.on_player_error)
        self.player.playbackPaused.connect(self.on_player_paused)
        self.player.mouseMoved.connect(self.on_mouse_move)
        # Handle fullscreen requests from MPV
        self.player.fullscreenRequested.connect(self.toggle_fullscreen)
        self.player.escapePressed.connect(self.on_escape_pressed)
        
        # Create Bump View
        self.bump_widget = QWidget()
        self.bump_widget.setStyleSheet("background-color: black;")
        bump_layout = QVBoxLayout(self.bump_widget)
        self._bump_layout = bump_layout
        self._bump_safe_vpad_ratio = 0.15
        self.lbl_bump_text = QLabel("")
        self.lbl_bump_text.setAlignment(Qt.AlignCenter)
        self.lbl_bump_text.setWordWrap(True)
        self.lbl_bump_text.setFont(QFont("Arial", 28, QFont.Bold))
        self.lbl_bump_text.setStyleSheet("color: white;")

        self.lbl_bump_text_top = QLabel("")
        self.lbl_bump_text_top.setAlignment(Qt.AlignCenter)
        self.lbl_bump_text_top.setWordWrap(True)
        self.lbl_bump_text_top.setFont(QFont("Arial", 28, QFont.Bold))
        self.lbl_bump_text_top.setStyleSheet("color: white;")

        self.bump_image_view = BumpImageView(self.bump_widget)
        self.bump_image_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.lbl_bump_text_bottom = QLabel("")
        self.lbl_bump_text_bottom.setAlignment(Qt.AlignCenter)
        self.lbl_bump_text_bottom.setWordWrap(True)
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

        # Start ambient audio after the event loop begins.
        QTimer.singleShot(0, self._start_startup_ambient)

        # Best-effort: auto-populate library from an external drive (e.g. "T7").
        # If nothing is found, the user can still add a folder manually.
        QTimer.singleShot(0, self._try_auto_populate_library)

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

        # Apply immediately to MPV.
        try:
            self.player.set_audio_normalization(self.normalize_audio_enabled)
        except Exception:
            pass

        try:
            if hasattr(self, 'bumps_mode_widget'):
                self.bumps_mode_widget.refresh_status()
        except Exception:
            pass

    def _try_auto_populate_library(self):
        try:
            if getattr(self, '_auto_config_running', False):
                return
            self._auto_config_running = True

            # Show pending overlay on the show cards while we probe external storage.
            if hasattr(self, 'welcome_screen'):
                try:
                    self.welcome_screen.set_show_pending("King of the Hill", True)
                    self.welcome_screen.set_show_pending("Bob's Burgers", True)
                except Exception:
                    pass

            self._auto_config_thread = QThread(self)
            self._auto_config_worker = AutoConfigWorker(volume_label=getattr(self, 'auto_config_volume_label', 'T7'))
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
            # Remove pending overlays.
            if hasattr(self, 'welcome_screen'):
                try:
                    self.welcome_screen.set_show_pending("King of the Hill", False)
                    self.welcome_screen.set_show_pending("Bob's Burgers", False)
                except Exception:
                    pass

            # If user already added sources while we were scanning, don't override.
            if getattr(self.playlist_manager, 'source_folders', None) and len(self.playlist_manager.source_folders) > 0:
                return

            sources = (result or {}).get('sources', [])
            if sources:
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

            # Auto-detect bumps scripts/music on the same external drive.
            tv_vibe_scripts_dir = (result or {}).get('tv_vibe_scripts_dir', None)
            tv_vibe_music_dir = (result or {}).get('tv_vibe_music_dir', None)
            tv_vibe_images_dir = (result or {}).get('tv_vibe_images_dir', None)
            tv_vibe_audio_fx_dir = (result or {}).get('tv_vibe_audio_fx_dir', None)
            if tv_vibe_scripts_dir and os.path.isdir(tv_vibe_scripts_dir):
                try:
                    default_scripts_dir = get_local_bumps_scripts_dir()
                    bump_mgr = getattr(self.playlist_manager, 'bump_manager', None)

                    # Only override if we're still on the default local folder and nothing is loaded yet.
                    scripts_loaded = bool(getattr(bump_mgr, 'bump_scripts', []) or []) if bump_mgr else False
                    music_loaded = bool(getattr(bump_mgr, 'music_files', []) or []) if bump_mgr else False

                    if getattr(self, 'bump_scripts_dir', None) in (None, '', default_scripts_dir) and not scripts_loaded:
                        self.bump_scripts_dir = tv_vibe_scripts_dir
                        try:
                            bump_mgr.load_bumps(tv_vibe_scripts_dir)
                        except Exception:
                            pass

                    if tv_vibe_music_dir and os.path.isdir(tv_vibe_music_dir) and not music_loaded:
                        try:
                            self.bump_music_dir = tv_vibe_music_dir
                        except Exception:
                            pass
                        try:
                            bump_mgr.scan_music(tv_vibe_music_dir)
                        except Exception:
                            pass

                    if tv_vibe_images_dir and os.path.isdir(tv_vibe_images_dir):
                        # Only override if the user hasn't configured one yet.
                        if not getattr(self, 'bump_images_dir', None):
                            self.bump_images_dir = tv_vibe_images_dir
                            try:
                                self._settings['bump_images_dir'] = tv_vibe_images_dir
                                self._save_user_settings()
                            except Exception:
                                pass
                        try:
                            bump_mgr.bump_images_dir = self.bump_images_dir
                        except Exception:
                            pass

                    if tv_vibe_audio_fx_dir and os.path.isdir(tv_vibe_audio_fx_dir):
                        # Only override if the user hasn't configured one yet.
                        if not getattr(self, 'bump_audio_fx_dir', None):
                            self.bump_audio_fx_dir = tv_vibe_audio_fx_dir
                            try:
                                self._settings['bump_audio_fx_dir'] = tv_vibe_audio_fx_dir
                                self._save_user_settings()
                            except Exception:
                                pass
                        try:
                            bump_mgr.bump_audio_fx_dir = self.bump_audio_fx_dir
                        except Exception:
                            pass

                    if hasattr(self, 'bumps_mode_widget'):
                        try:
                            self.bumps_mode_widget.refresh_status()
                        except Exception:
                            pass
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
                 
             # Positioning Controls in Fullscreen Overlay Mode
             if self.isFullScreen() and hasattr(self, 'play_mode_widget'):
                 ctrls = self.play_mode_widget.controls_widget
                 # If controls are reparented to video_container (happens in toggle_fullscreen)
                 if ctrls.parent() == self.video_container:
                     cw = w
                     ch = 180 # Fixed height
                     ctrls.setGeometry(0, h - ch, cw, ch)

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
            self.on_mouse_move()
        elif event.type() == QEvent.KeyPress:
             if event.key() == Qt.Key_Escape and self.isFullScreen():
                 self.toggle_fullscreen()
             elif event.key() == Qt.Key_F:
                 self.toggle_fullscreen()
        return super().eventFilter(obj, event)

    def on_escape_pressed(self):
        if self.isFullScreen():
            self.toggle_fullscreen()

    def on_mouse_move(self):
        # Update activity timestamp
        self.last_activity_time = time.time()
        
        # If in Play Mode
        if self.mode_stack.currentIndex() == 2:
            self.show_controls()

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
        if self.isFullScreen():
            # Exiting Fullscreen
            if self.was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
            
            self.failsafe_timer.stop()
            
            # Delay UI restoration to avoid "zoom in" effect during OS animation
            QTimer.singleShot(200, self.restore_ui_after_fullscreen)
            
        else:
            # Entering Fullscreen
            self.was_maximized = self.isMaximized()
            self.showFullScreen()
            
            # Hide sidebar
            self.play_mode_widget.sidebar_container.setVisible(False)
            
            self.play_mode_widget.btn_fullscreen.setChecked(True)
            self._update_fullscreen_button_icon()
            self.play_mode_widget.set_controls_overlay(True)
            # Hide both native and custom menu bars while fullscreen.
            self.menuBar().setVisible(False)
            if hasattr(self, 'menu_bar_widget') and self.menu_bar_widget is not None:
                self.menu_bar_widget.setVisible(False)
            self.statusBar().setVisible(False)
            
            self.failsafe_timer.start()
            
            # Trigger resize to position controls
            self.video_container.resizeEvent(QResizeEvent(self.video_container.size(), self.video_container.size()))

    def restore_ui_after_fullscreen(self):
        if not self.isFullScreen():
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

    def setup_ui(self):
        self.create_menu()

        central_widget = GradientBackgroundWidget()
        central_widget.setObjectName('gradient_background')
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.mode_stack = QStackedWidget()
        main_layout.addWidget(self.mode_stack)
        
        # 0. Welcome Screen
        self.welcome_screen = WelcomeScreen(self)
        self.mode_stack.addWidget(self.welcome_screen)
        
        # 1. Edit Mode
        self.edit_mode_widget = EditModeWidget(self)
        self.mode_stack.addWidget(self.edit_mode_widget)
        
        # 2. Play Mode
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
        
        # If switching to Play mode (Index 2), refresh list
        if index == 2:
            self.play_mode_widget.refresh_episode_list()
            # If no playlist is loaded, ensure menu is open so user can pick one
            if not self.playlist_manager.current_playlist and not self.play_mode_widget.sidebar_visible:
                 self.play_mode_widget.toggle_sidebar()

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
        folder = QFileDialog.getExistingDirectory(self, "Select Interstitials Directory")
        if folder:
            self.playlist_manager.scan_interstitials(folder)
            QMessageBox.information(self, "Interstitials", f"Found {len(self.playlist_manager.interstitials)} items.")

    def choose_bump_scripts(self):
        # Scripts are loaded from the app-local `bumps/` folder (like `playlists/`).
        folder = getattr(self, 'bump_scripts_dir', None) or get_local_bumps_scripts_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass

        self.playlist_manager.bump_manager.load_bumps(folder)
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

        if hasattr(self, 'bumps_mode_widget'):
            try:
                self.bumps_mode_widget.refresh_status()
            except Exception:
                pass

    def set_bumps_enabled(self, enabled):
        self.bumps_enabled = bool(enabled)

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

    def generate_playlist_logic(self, shuffle, interstitials, bumps):
        # We don't shuffle physically anymore, but we record the preference.
        # But wait, this method updates the CURRENT play session in memory.
        # The user just clicked "Generate".
        # We should set the runtime shuffle mode immediately if they checked "Default: ON".
        
        pass

    def generate_playlist_logic(self, shuffle_mode, interstitials, bumps):
        # Generate playlist contents (injections are decided here).
        self.playlist_manager.generate_playlist(None, False, interstitials, bumps)
        self.playlist_manager.reset_playback_state()
        self.set_shuffle_mode(shuffle_mode)
        
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

        files_dir = os.path.join(os.getcwd(), "playlists")
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
                'interstitial_folder': self.playlist_manager.interstitial_folder,
                # Backward-compatible boolean (standard shuffle == True)
                'shuffle_default': (self.playlist_manager.shuffle_mode != 'off'),
                # Preferred persisted value
                'shuffle_mode': self.playlist_manager.shuffle_mode
            }
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
            QMessageBox.information(self, "Success", "Playlist saved!")
            self.play_mode_widget.refresh_playlists()

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
            files_dir = os.path.join(os.getcwd(), "playlists")
            os.makedirs(files_dir, exist_ok=True)
            filename, _ = QFileDialog.getOpenFileName(self, "Load Playlist", files_dir, "Sleepy Playlist (*.json)")
        
        if filename and os.path.exists(filename):
            try:
                with open(filename, 'r') as f:
                    data = json.load(f)
                
                if 'interstitial_folder' in data:
                    self.playlist_manager.scan_interstitials(data['interstitial_folder'])
                    
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

    def clear_playlist(self):
        self.playlist_manager.current_playlist = []
        self.playlist_manager.reset_playback_state()
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
            self.playlist_manager.current_playlist = [{'type': 'video', 'path': path}]
            self.playlist_manager.current_index = 0
            self.edit_mode_widget.refresh_playlist_list()
            self.play_mode_widget.refresh_episode_list()
            self.set_mode(1) # Switch to play
            self.play_index(0)

            if self.play_mode_widget.sidebar_visible:
                self.play_mode_widget.toggle_sidebar()

    def play_index(self, index, record_history=True, bypass_bump_gate=False):
        pm = self.playlist_manager
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
                        bump_item = pm.bump_manager.get_random_bump()
                    except Exception:
                        bump_item = None

                    # If no eligible bump exists (e.g., no music long enough), just play.
                    if bump_item:
                        self._pending_next_index = int(index)
                        self._pending_next_record_history = bool(record_history)
                        self.play_bump(bump_item)
                        return

            # Global bumps are optional. If disabled, skip bump items.
            if isinstance(item, dict) and item.get('type') == 'bump' and not self.bumps_enabled:
                QTimer.singleShot(0, self.play_next)
                return

            # Reset EOF watchdog state for the new track.
            self._handled_eof_for_index = None
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
                pm.mark_episode_started(index)
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
                    self.player.play(path)
                    self._played_since_start = True
                    prefix = "[INT]" if itype == 'interstitial' else ""
                    self.setWindowTitle(f"Sleepy Shows - {prefix} {os.path.basename(path)}")
                    self._resume_sleep_countdown_if_needed()
                    QTimer.singleShot(200, self._resume_sleep_countdown_if_needed)
                elif itype == 'bump':
                    self.play_bump(item)
            else:
                 # Legacy
                 self.video_stack.setCurrentIndex(0)
                 self.player.play(item)
                 self._played_since_start = True
                 self.setWindowTitle(f"Sleepy Shows - {os.path.basename(item)}")
                 self._resume_sleep_countdown_if_needed()
                 QTimer.singleShot(200, self._resume_sleep_countdown_if_needed)
        
        self.show_controls()

    def play_bump(self, bump_item):
        script = bump_item.get('script')
        audio = bump_item.get('audio')

        # Treat bumps as non-episode playback for sleep timer purposes.
        self._pause_sleep_countdown()
        
        self.video_stack.setCurrentIndex(1) # Bump View
        self.setWindowTitle("Sleepy Shows - [AS]")

        # Reset any lingering FX from a prior bump.
        self._stop_bump_fx()

        # Reset per-bump audio control state.
        self._bump_music_cut_active = False
        self._bump_music_cut_prev_mute = None
        self._bump_outro_audio_exclusive = False
        
        if audio:
            self.player.play(audio)
            
        if script:
            self.current_bump_script = script.get('cards', [])
            self.current_card_index = 0
            self.advance_bump_card()

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

    def _pick_random_outro_sound(self):
        # Prefer the default T7 location; fall back to any mounted drive under /media/tyler.
        folders = []
        try:
            p = str(getattr(self, '_outro_sounds_dir', '') or '').strip()
            if p and os.path.isdir(p):
                folders.append(p)
        except Exception:
            pass

        try:
            pattern = os.path.join('/media', 'tyler', '*', 'Sleepy Shows Data', 'TV Vibe', 'outro sounds')
            for p in glob.glob(pattern):
                if os.path.isdir(p):
                    folders.append(p)
        except Exception:
            pass

        audio_exts = {'.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.opus', '.webm', '.mp4'}

        for folder in folders:
            try:
                files = []
                for name in os.listdir(folder):
                    full = os.path.join(folder, name)
                    if not os.path.isfile(full):
                        continue
                    if os.path.splitext(name)[1].lower() in audio_exts:
                        files.append(full)
                if files:
                    return random.choice(files)
            except Exception:
                continue

        return None

    def _play_outro_audio(self):
        path = self._pick_random_outro_sound()
        if not path or not os.path.exists(path):
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
                self.fx_player.play(path)
        except Exception:
            self._stop_bump_fx()
            return

        self._bump_fx_active = True
        self._bump_fx_policy = 'ms'
        try:
            self._bump_fx_stop_timer.start(800)
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
            if not path or not os.path.exists(path):
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
                 self.play_index(idx, record_history=record_history, bypass_bump_gate=True)
                 return

             self.play_next()
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
             _hide_all_bump_widgets()
             self.lbl_bump_text.setTextFormat(Qt.PlainText)
             self.lbl_bump_text.setText(card.get('text', ''))
             self.lbl_bump_text.show()
             if bool(card.get('outro_audio', False)):
                 self._play_outro_audio()
         elif ctype == 'pause':
             _hide_all_bump_widgets()
         elif ctype == 'img_char':
             _hide_all_bump_widgets()
             img_info = card.get('image') if isinstance(card.get('image'), dict) else {}
             img_path = str(img_info.get('path') or '')
             pm = QPixmap(img_path) if img_path else QPixmap()
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
                 img_html = f'<img src="{html.escape(img_path)}" style="height:{font_px}px;" />'
                 html_body = f'<div align="center">{before}{img_html}{after}</div>'
                 self.lbl_bump_text.setTextFormat(Qt.RichText)
                 self.lbl_bump_text.setText(html_body)
                 self.lbl_bump_text.show()
         elif ctype == 'img':
             _hide_all_bump_widgets()
             img_info = card.get('image') if isinstance(card.get('image'), dict) else {}
             img_path = str(img_info.get('path') or '')
             pm = QPixmap(img_path) if img_path else QPixmap()
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

                 before = str(card.get('text_before') or '').strip()
                 after = str(card.get('text_after') or '').strip()

                 # If "lines" mode, reserve space for the explicit number of text lines.
                 if mode == 'lines':
                     fm = self.lbl_bump_text.fontMetrics()
                     line_h = int(fm.lineSpacing())
                     top_lines = int(card.get('before_lines', 0) or 0)
                     bot_lines = int(card.get('after_lines', 0) or 0)

                     if before:
                         self.lbl_bump_text_top.setTextFormat(Qt.PlainText)
                         self.lbl_bump_text_top.setText(before)
                         self.lbl_bump_text_top.setFixedHeight(max(0, top_lines * line_h))
                         self.lbl_bump_text_top.show()
                     if after:
                         self.lbl_bump_text_bottom.setTextFormat(Qt.PlainText)
                         self.lbl_bump_text_bottom.setText(after)
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

        # If we were playing bump audio, cut it off cleanly.
        if was_in_bump_view:
            try:
                self.player.stop()
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
            self.player.toggle_pause()

    def stop_playback(self):
        self.player.stop()
        self.show_controls()
        self._pause_sleep_countdown()

    def play_next(self):
        pm = self.playlist_manager

        # If a bump-gated next is pending and the user hits Next again, skip the bump.
        if getattr(self, '_pending_next_index', None) is not None and self.video_stack.currentIndex() == 1:
            idx = int(self._pending_next_index)
            record_history = bool(getattr(self, '_pending_next_record_history', True))
            self._pending_next_index = None
            self._pending_next_record_history = True
            self.stop_bump_playback()
            self.play_index(idx, record_history=record_history, bypass_bump_gate=True)
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
            self.stop_playback()
            return

        # If the next item is itself a bump, just play it (no double-bump).
        try:
            next_item = pm.current_playlist[next_idx]
            if isinstance(next_item, dict) and next_item.get('type') == 'bump':
                self.play_index(next_idx, record_history=record_history, bypass_bump_gate=True)
                return
        except Exception:
            pass

        # Global bump gate: any forward move plays a bump first when enabled.
        if self.bumps_enabled and self.video_stack.currentIndex() != 1:
            bump_item = None
            try:
                bump_item = pm.bump_manager.get_random_bump()
            except Exception:
                bump_item = None

            if bump_item:
                self._pending_next_index = int(next_idx)
                self._pending_next_record_history = bool(record_history)
                self.stop_bump_playback()
                self.play_bump(bump_item)
                return

        self.play_index(next_idx, record_history=record_history)

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

        # Auto advance
        self.play_next()

    def on_player_error(self, msg):
        print(f"Player Error: {msg}")
        
    def on_player_paused(self, paused):
        self.show_controls()

        if not paused:
            self._played_since_start = True

        # Sleep timer only counts down while actively playing.
        if paused:
            self._pause_sleep_countdown()
        else:
            self._resume_sleep_countdown_if_needed()
        
        if paused and self.playlist_manager.current_playlist:
             # Show Overlay
             idx = self.playlist_manager.current_index
             if idx >= 0 and idx < len(self.playlist_manager.current_playlist):
                 item = self.playlist_manager.current_playlist[idx]
                 name = ""
                 if isinstance(item, dict):
                     path = item.get('path', 'Unknown')
                     name = os.path.splitext(os.path.basename(path))[0]
                 else:
                     name = os.path.splitext(os.path.basename(item))[0]
                     
                 self.overlay_label.setText(name)
                 self.overlay_label.setVisible(True)
                 self.overlay_label.raise_()
        else:
             self.overlay_label.setVisible(False)

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

            idx = self.playlist_manager.current_index
            if idx is None or idx < 0:
                return

            # Only handle EOF once per index.
            if self._handled_eof_for_index == idx:
                return

            # Avoid firing immediately after starting a file.
            if self._play_start_monotonic is not None:
                if (time.monotonic() - self._play_start_monotonic) < 0.75:
                    return

            mpv = self.player.mpv

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
                self._handled_eof_for_index = idx
                QTimer.singleShot(0, self.on_playback_finished)
        except Exception:
            return

    def update_duration(self, duration):
        self.total_duration = duration
        self.update_time_label(0, duration) # Reset current? or keep

    def update_time_label(self, current, total):
        def fmt(s):
            m, s = divmod(int(s), 60)
            h, m = divmod(m, 60)
            if h > 0: return f"{h}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"
        self.play_mode_widget.lbl_current_time.setText(f"{fmt(current)} / {fmt(total)}")

    def on_sleep_timer(self):
        self.stop_playback()
        self.set_mode(0) # Go to Welcome

        # Ensure internal state + UI reflects Off after timer fires.
        self.cancel_sleep_timer()

if __name__ == "__main__":
    import locale
    locale.setlocale(locale.LC_NUMERIC, 'C')
    
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
