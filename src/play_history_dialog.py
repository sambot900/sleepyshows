"""Play history distribution chart and export dialog."""

from __future__ import annotations

import os
import re
import math
import datetime

from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QFontMetrics
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QWidget, QSizePolicy, QFileDialog, QMessageBox, QToolTip,
)

# Season colour palette — Material Design 300 level, visually distinct on dark backgrounds.
_PALETTE = [
    QColor('#4FC3F7'),  # Light Blue 300
    QColor('#FF8A65'),  # Deep Orange 300
    QColor('#81C784'),  # Green 300
    QColor('#F06292'),  # Pink 300
    QColor('#7986CB'),  # Indigo 300
    QColor('#4DB6AC'),  # Teal 300
    QColor('#FFB74D'),  # Orange 300
    QColor('#BA68C8'),  # Purple 300
    QColor('#A1887F'),  # Brown 300
    QColor('#90A4AE'),  # Blue Grey 300
]

_EP_RE = re.compile(r'[Ss](\d+)[Ee](\d+)\s*(.*)')


def _parse_episode(path: str) -> tuple[int, int, str]:
    """Return (season, episode, title) parsed from the filename."""
    base = os.path.splitext(os.path.basename(path))[0]
    m = _EP_RE.search(base)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(3).strip()
    return 0, 0, base


def _nice_max(val: int) -> int:
    """Round up to the next 'human-friendly' tick value for the Y-axis."""
    if val <= 0:
        return 5
    for step in [5, 10, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200, 250, 300, 400, 500]:
        if val <= step:
            return step
    return int(math.ceil(val / 100) * 100)


def _build_records(pm) -> list[dict]:
    """Build sorted per-episode records from PlaylistManager state."""
    records = []
    for item in list(pm.current_playlist or []):
        if not isinstance(item, dict):
            continue
        if item.get('type', 'video') != 'video':
            continue
        path = str(item.get('path') or '')
        if not path:
            continue
        season, episode, title = _parse_episode(path)
        key = pm._norm_path_key(path)
        play_count = int((pm.episode_play_counts or {}).get(key, 0) or 0)
        exposure = float((pm.episode_exposure_scores or {}).get(key, 0.0) or 0.0)
        try:
            offset = float(pm._effective_episode_offset(path))
        except Exception:
            offset = 0.0
        try:
            factor = float(pm._effective_episode_factor(path))
        except Exception:
            factor = 1.0
        records.append({
            'season': season,
            'episode': episode,
            'title': title,
            'path': path,
            'key': key,
            'play_count': play_count,
            'exposure': exposure,
            'offset': offset,
            'factor': factor,
        })
    records.sort(key=lambda r: (r['season'], r['episode']))
    return records


class _BarChart(QWidget):
    """QPainter-rendered bar chart: episodes on X, play count on Y, coloured by season."""

    _ML = 60   # left margin (Y-axis labels)
    _MR = 20   # right margin
    _MT = 20   # top margin
    _MB = 70   # bottom margin (season labels + padding)

    def __init__(self, records: list[dict], parent=None):
        super().__init__(parent)
        self._records = records
        self._bar_rects: list[QRect] = []
        self.setMouseTracking(True)
        self.setMinimumSize(400, 250)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_records(self, records: list[dict]):
        self._records = records
        self._bar_rects = []
        self.update()

    def _chart_rect(self) -> QRect:
        w, h = self.width(), self.height()
        return QRect(self._ML, self._MT, w - self._ML - self._MR, h - self._MT - self._MB)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.fillRect(self.rect(), QColor('#2b2b2b'))

        recs = self._records
        if not recs:
            painter.setPen(QColor('#888888'))
            painter.drawText(self.rect(), Qt.AlignCenter, 'No episodes loaded')
            painter.end()
            return

        cr = self._chart_rect()
        n = len(recs)
        max_count = max((r['play_count'] for r in recs), default=0)
        y_max = _nice_max(max_count)

        small_font = QFont()
        small_font.setPointSize(8)
        painter.setFont(small_font)
        fm = QFontMetrics(small_font)
        text_color = QColor('#cccccc')
        grid_pen = QPen(QColor('#444444'))
        grid_pen.setWidth(1)
        axis_pen = QPen(QColor('#888888'))
        axis_pen.setWidth(1)

        # --- Y axis grid lines and labels ---
        tick_count = 5
        for i in range(tick_count + 1):
            frac = i / tick_count
            y_val = int(frac * y_max)
            y_px = cr.bottom() - int(frac * cr.height())
            if i > 0:
                painter.setPen(grid_pen)
                painter.drawLine(cr.left(), y_px, cr.right(), y_px)
            label = str(y_val)
            lw = fm.horizontalAdvance(label)
            painter.setPen(text_color)
            painter.drawText(cr.left() - lw - 6, y_px + fm.ascent() // 2, label)

        # --- Axis lines ---
        painter.setPen(axis_pen)
        painter.drawLine(cr.left(), cr.top(), cr.left(), cr.bottom() + 1)
        painter.drawLine(cr.left(), cr.bottom() + 1, cr.right(), cr.bottom() + 1)

        # --- Bars ---
        slot_w = cr.width() / n
        bar_w = max(1, slot_w - 1)
        unique_seasons = sorted(set(r['season'] for r in recs))
        season_idx = {s: i for i, s in enumerate(unique_seasons)}
        season_starts: dict[int, int] = {}
        season_ends: dict[int, int] = {}
        bar_rects: list[QRect] = []

        for i, rec in enumerate(recs):
            sn = rec['season']
            x_px = cr.left() + i * slot_w
            if sn not in season_starts:
                season_starts[sn] = int(x_px)
            season_ends[sn] = int(x_px + slot_w)

            bar_h = (rec['play_count'] / y_max * cr.height()) if y_max > 0 else 0
            bar_h = max(0, bar_h)
            y_px = cr.bottom() - bar_h
            rect = QRect(int(x_px), int(y_px), max(1, int(bar_w)), int(bar_h + 0.5))
            bar_rects.append(rect)
            painter.fillRect(rect, _PALETTE[season_idx[sn] % len(_PALETTE)])

        self._bar_rects = bar_rects

        # --- Season separators and X-axis labels ---
        season_font = QFont()
        season_font.setPointSize(9)
        season_font.setBold(True)
        season_fm = QFontMetrics(season_font)
        painter.setFont(season_font)
        sep_pen = QPen(QColor('#666666'))
        sep_pen.setWidth(1)
        label_y = cr.bottom() + 18 + season_fm.ascent()

        for sn in unique_seasons:
            sx = season_starts[sn]
            ex = season_ends[sn]
            cx = (sx + ex) // 2
            lbl = f'S{sn}'
            lw = season_fm.horizontalAdvance(lbl)
            painter.setPen(text_color)
            painter.drawText(cx - lw // 2, label_y, lbl)

            if sn != unique_seasons[0]:
                painter.setPen(sep_pen)
                painter.drawLine(sx, cr.top(), sx, cr.bottom() + 1)

        # --- Legend (top-right, only when multiple seasons) ---
        if len(unique_seasons) > 1:
            leg_font = QFont()
            leg_font.setPointSize(8)
            leg_fm = QFontMetrics(leg_font)
            leg_entry_h = 16
            leg_pad = 7
            leg_swatch = 10
            leg_gap = 5
            max_lbl_w = max(leg_fm.horizontalAdvance(f'Season {s}') for s in unique_seasons)
            leg_w = leg_pad * 2 + leg_swatch + leg_gap + max_lbl_w
            leg_h = leg_pad * 2 + len(unique_seasons) * leg_entry_h
            leg_x = cr.right() - leg_w - 5
            leg_y = cr.top() + 5

            painter.fillRect(leg_x, leg_y, leg_w, leg_h, QColor(30, 30, 30, 210))
            painter.setPen(QColor('#555555'))
            painter.drawRect(leg_x, leg_y, leg_w - 1, leg_h - 1)
            painter.setFont(leg_font)
            for i, sn in enumerate(unique_seasons):
                ey = leg_y + leg_pad + i * leg_entry_h
                painter.fillRect(leg_x + leg_pad, ey + 3, leg_swatch, leg_swatch - 2,
                                 _PALETTE[season_idx[sn] % len(_PALETTE)])
                painter.setPen(text_color)
                painter.drawText(leg_x + leg_pad + leg_swatch + leg_gap,
                                 ey + leg_fm.ascent(), f'Season {sn}')

        painter.end()

    def mouseMoveEvent(self, event):
        pos = event.pos()
        cr = self._chart_rect()
        for i, rect in enumerate(self._bar_rects):
            # Widen hit column to the full chart height for easier hovering.
            col = QRect(rect.left(), cr.top(), rect.width() + 1, cr.height())
            if col.contains(pos):
                rec = self._records[i]
                tip = (f"S{rec['season']:02d}E{rec['episode']:02d} {rec['title']}\n"
                       f"Plays: {rec['play_count']}")
                QToolTip.showText(self.mapToGlobal(pos), tip, self)
                return
        QToolTip.hideText()

    def sizeHint(self) -> QSize:
        return QSize(860, 400)


class PlayHistoryDialog(QDialog):
    """Show play-count distribution chart for the currently loaded show."""

    def __init__(self, pm, show_name: str, parent=None):
        super().__init__(parent)
        self._pm = pm
        self._show_name = show_name
        self._records = _build_records(pm)

        self.setWindowTitle(f'Play History — {show_name}')
        self.resize(960, 580)
        self.setMinimumSize(500, 350)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        title = QLabel(f'Play History — {show_name}')
        title.setStyleSheet('font-size: 18px; font-weight: bold; color: white;')
        root.addWidget(title)

        self._chart = _BarChart(self._records, self)
        root.addWidget(self._chart, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_export = QPushButton('Export…')
        btn_export.clicked.connect(self._export)
        btn_close = QPushButton('Close')
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_export)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    def _export(self):
        safe_name = re.sub(r'[^\w\s\-.]', '', self._show_name).strip()
        default = f'{safe_name} Play History.txt'
        path, _ = QFileDialog.getSaveFileName(
            self, 'Export Play History', default, 'Text Files (*.txt);;All Files (*)'
        )
        if not path:
            return
        try:
            recs = self._records
            lines = [
                f'Play History — {self._show_name}',
                f'Generated: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}',
                f'Total episodes: {len(recs)}',
                '',
            ]
            ep_col = max(
                len('Episode'),
                max((len(f"S{r['season']:02d}E{r['episode']:02d} {r['title']}") for r in recs), default=0),
            )
            hdr = (f"{'Episode':<{ep_col}}  {'Plays':>6}  "
                   f"{'Exposure Score':>16}  {'Offset':>8}  {'Factor':>8}")
            lines += [hdr, '-' * len(hdr)]
            for r in recs:
                ep_label = f"S{r['season']:02d}E{r['episode']:02d} {r['title']}"
                lines.append(
                    f"{ep_label:<{ep_col}}  {r['play_count']:>6}  "
                    f"{r['exposure']:>16.2f}  {r['offset']:>8.2f}  {r['factor']:>8.2f}"
                )
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
        except Exception as e:
            QMessageBox.critical(self, 'Export Failed', str(e))
