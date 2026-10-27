"""Icons: Windows' own Segoe Fluent Icons glyphs, plus the app and tray marks."""
from __future__ import annotations

import winreg

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QWidget

from . import theme


class G:
    """Segoe Fluent Icons code points used by the app."""

    HOME = ""
    FACE = ""
    CHART = ""
    SETTINGS = ""
    SLIDERS = ""
    INFO = ""
    CAMERA = ""
    VIDEO = ""
    LOCK = ""
    UNLOCK = ""
    WARNING = ""
    ERROR = ""
    DONE = ""
    CHECK = ""
    CLOSE = ""
    REFRESH = ""
    UNDO = ""
    HISTORY = ""
    DELETE = ""
    PERSON = ""
    SHIELD = ""
    SUN = ""
    MOON = ""
    PAUSE = ""
    PLAY = ""
    POWER = ""
    FOLDER = ""
    KEY = ""
    BULB = ""
    CLOCK = ""
    GAUGE = ""
    SCAN = ""
    EYE_OFF = ""
    ADD = ""
    SYNC = ""
    STOPWATCH = ""
    REPORT = ""
    CALENDAR_CLOCK = ""
    CHECKLIST = ""
    DIAGNOSTIC = ""
    NEW_WINDOW = ""
    CHEVRON_RIGHT = ""
    SEARCH = ""
    COPY = ""
    HELP = ""
    VOLUME = ""
    MUTE = ""
    DOWNLOAD = ""


def glyph_pixmap(glyph: str, color: str | QColor, size: int, dpr: float = 1.0) -> QPixmap:
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setPen(QColor(color))
    painter.setFont(theme.icon_font(size * 0.9))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return pixmap


def glyph_icon(glyph: str, color: str | QColor | None = None, size: int = 16) -> QIcon:
    color = color or theme.current().text
    icon = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0):
        icon.addPixmap(glyph_pixmap(glyph, color, size, dpr))
    return icon


class GlyphLabel(QLabel):
    """A single icon glyph that follows the theme colours."""

    def __init__(self, glyph: str, size: int = 20, *, tone: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(glyph, parent)
        self._size = size
        self._tone = tone
        self.setFont(theme.icon_font(size))
        self.setFixedSize(size + 8, size + 8)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName("")  # decorative; text nearby carries the meaning
        self.refresh()

    def set_glyph(self, glyph: str, tone: str | None = None) -> None:
        self.setText(glyph)
        self._tone = tone
        self.refresh()

    def refresh(self) -> None:
        t = theme.current()
        color = {"success": t.success, "caution": t.caution, "critical": t.critical,
                 "accent": t.accent_text, "muted": t.text2}.get(self._tone or "", t.text)
        # The app style sheet sets a text font on every QLabel; a widget-level
        # rule is the only way to keep the icon font.
        self.setStyleSheet(f'color: {color}; background: transparent; '
                           f'font-family: "{theme.font_family(theme.ICON_FONT)}"; '
                           f"font-size: {round(self._size * 0.9)}px;")


def taskbar_is_light() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except OSError:
        return False


def _draw_face_scan(painter: QPainter, rect: QRectF, color: QColor, stroke: float) -> None:
    """Corner brackets around a simple face: the app's mark."""
    pen = QPen(color, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    inset = stroke / 2 + rect.width() * 0.04
    r = rect.adjusted(inset, inset, -inset, -inset)
    arm = r.width() * 0.28
    radius = r.width() * 0.12
    path = QPainterPath()
    for (cx, cy, dx, dy) in ((r.left(), r.top(), 1, 1), (r.right(), r.top(), -1, 1),
                             (r.left(), r.bottom(), 1, -1), (r.right(), r.bottom(), -1, -1)):
        path.moveTo(cx, cy + dy * arm)
        path.lineTo(cx, cy + dy * radius)
        path.quadTo(cx, cy, cx + dx * radius, cy)
        path.lineTo(cx + dx * arm, cy)
    painter.drawPath(path)
    w = r.width()
    eye = max(stroke * 0.9, w * 0.07)
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    for ex in (0.36, 0.64):
        painter.drawEllipse(QPointF(r.left() + w * ex, r.top() + w * 0.42), eye / 1.2, eye / 1.2)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    smile = QRectF(r.left() + w * 0.33, r.top() + w * 0.42, w * 0.34, w * 0.26)
    painter.drawArc(smile, 200 * 16, 140 * 16)


def tray_icon(state: str) -> QIcon:
    """``ready``, ``paused``, ``attention`` or ``off``; drawn for the taskbar theme."""
    base = QColor("#1B1B1B") if taskbar_is_light() else QColor("#FFFFFF")
    if state == "off":
        base.setAlphaF(0.45)
    badge = {"paused": QColor("#F7B500"), "attention": QColor("#E74856")}.get(state)
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _draw_face_scan(painter, QRectF(0, 0, size, size), base, max(1.4, size / 11))
        if badge is not None:
            d = size * 0.42
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#000000" if not taskbar_is_light() else "#FFFFFF"))
            painter.drawEllipse(QRectF(size - d - 1, size - d - 1, d + 2, d + 2))
            painter.setBrush(badge)
            painter.drawEllipse(QRectF(size - d, size - d, d, d))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def app_icon() -> QIcon:
    """The window icon: the face mark on the accent colour."""
    accent = QColor(theme.current().accent)
    on_accent = QColor(theme.current().on_accent)
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        margin = size * 0.2
        _draw_face_scan(painter, QRectF(margin, margin, size - 2 * margin, size - 2 * margin),
                        on_accent, max(1.2, size / 16))
        painter.end()
        icon.addPixmap(pixmap)
    return icon
