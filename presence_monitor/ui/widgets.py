"""Reusable, theme-aware building blocks for the Face Unlock window."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .icons import G, GlyphLabel


def restyle(widget: QWidget) -> None:
    """Re-apply the style sheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def label(text: str = "", role: str = "", *, wrap: bool = False,
          selectable: bool = False) -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setObjectName(role)
    widget.setWordWrap(wrap)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


BUTTON_HEIGHT = 32  # Windows 11 standard control height


def _button(text: str, slot: Callable | None, name: str = "") -> QPushButton:
    widget = QPushButton(text)
    if name:
        widget.setObjectName(name)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setMinimumHeight(BUTTON_HEIGHT)
    if slot:
        widget.clicked.connect(slot)
    return widget


def accent_button(text: str, slot: Callable | None = None) -> QPushButton:
    """The single most important action in a view."""
    return _button(text, slot, "Accent")


def button(text: str, slot: Callable | None = None) -> QPushButton:
    return _button(text, slot)


def subtle_button(text: str, slot: Callable | None = None) -> QPushButton:
    """A low-emphasis, text-style action."""
    return _button(text, slot, "Subtle")


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None, *, padding: tuple[int, int] = (20, 16),
                 spacing: int = 10):
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding[0], padding[1], padding[0], padding[1])
        self.body.setSpacing(spacing)


class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFrameShape(QFrame.Shape.NoFrame)


class Pill(QLabel):
    def __init__(self, text: str = "", tone: str = "neutral", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("Pill")
        self.setProperty("tone", tone)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_state(self, text: str, tone: str) -> None:
        self.setText(text)
        if self.property("tone") != tone:
            self.setProperty("tone", tone)
            restyle(self)


class ToggleSwitch(QAbstractButton):
    """Windows 11 style on/off switch (Space toggles; focus is visible)."""

    def __init__(self, parent: QWidget | None = None, *, accessible_name: str = ""):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(44, 24)
        self.setAccessibleName(accessible_name)
        self._knob = 0.0
        self._state_label: QLabel | None = None
        self._animation = QPropertyAnimation(self, b"knob", self)
        self._animation.setDuration(120)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def attach_state_label(self, state_label: QLabel) -> None:
        """Show "On"/"Off" next to the switch, kept in sync even with signals blocked."""
        self._state_label = state_label
        self._sync_state_label()

    def _sync_state_label(self) -> None:
        if self._state_label is not None:
            self._state_label.setText("On" if self.isChecked() else "Off")

    def _get_knob(self) -> float:
        return self._knob

    def _set_knob(self, value: float) -> None:
        self._knob = value
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def _animate(self, checked: bool) -> None:
        self._sync_state_label()
        self._animation.stop()
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt API
        super().setChecked(checked)
        self._animation.stop()
        self._knob = 1.0 if checked else 0.0
        self._sync_state_label()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(44, 24)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(2, 2, 40, 20)
        enabled = self.isEnabled()
        if self.isChecked():
            fill = QColor(t.accent if enabled else t.text3)
            if self.underMouse() and enabled:
                fill = QColor(t.accent_hover)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
        else:
            painter.setPen(QPen(QColor(t.text2 if enabled else t.text3), 1))
            painter.setBrush(QColor(t.subtle_hover) if self.underMouse() else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(track, 10, 10)
        size = 14 if self.underMouse() and enabled else 12
        x = track.left() + 4 + self._knob * (track.width() - 8 - size)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t.on_accent if self.isChecked() else
                                (t.text2 if enabled else t.text3)))
        painter.drawEllipse(QRectF(x, track.center().y() - size / 2, size, size))
        if self.hasFocus():
            painter.setPen(QPen(QColor(t.focus), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(0.75, 0.75, 42.5, 22.5), 12, 12)
        painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().leaveEvent(event)


class SettingRow(QWidget):
    """Icon, title and explanation on the left; a control on the right."""

    def __init__(self, glyph: str | None, title: str, description: str = "",
                 control: QWidget | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Transparent")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(14)
        if glyph:
            self.icon = GlyphLabel(glyph, 18)
            layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.text_box = text
        self.title = label(title, "Body")
        self.description = label(description, "Secondary", wrap=True)
        text.addWidget(self.title)
        # Always part of the row (hidden while empty): a description set later must
        # appear here, not as a stray top-level window.
        text.addWidget(self.description)
        self.description.setVisible(bool(description))
        layout.addLayout(text, 1)
        self.control_box = QHBoxLayout()
        self.control_box.setSpacing(10)
        layout.addLayout(self.control_box)
        if control is not None:
            self.set_control(control)

    def set_control(self, control: QWidget) -> None:
        self.control_box.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        if isinstance(control, ToggleSwitch):
            state = label("", "Body2")
            control.attach_state_label(state)
            # "On"/"Off" sits directly to the left of its switch, as in Windows Settings.
            self.control_box.insertWidget(self.control_box.indexOf(control), state, 0,
                                          Qt.AlignmentFlag.AlignVCenter)
            if not control.accessibleName():
                control.setAccessibleName(self.title.text())
        elif not control.accessibleName():
            control.setAccessibleName(self.title.text())

    def set_description(self, text: str) -> None:
        self.description.setText(text)
        self.description.setVisible(bool(text))


class SettingsGroup(Card):
    """A card of setting rows separated by hairlines."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, padding=(0, 4), spacing=0)
        self._rows = 0

    def add(self, row: QWidget) -> QWidget:
        if self._rows:
            divider = Divider()
            wrapper = QHBoxLayout()
            wrapper.setContentsMargins(16, 0, 16, 0)
            wrapper.addWidget(divider)
            self.body.addLayout(wrapper)
        self.body.addWidget(row)
        self._rows += 1
        return row


class Banner(QFrame):
    """An inline message with an icon, optional action, and optional close."""

    closed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Banner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(12)
        self.icon = GlyphLabel("", 16)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = label("", "CardTitle")
        self.message = label("", "Body", wrap=True)
        text.addWidget(self.title)
        text.addWidget(self.message)
        layout.addLayout(text, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        layout.addLayout(self.actions)
        self.hide()

    def show_message(self, tone: str, title: str, message: str,
                     actions: list[tuple[str, Callable]] | None = None) -> None:
        glyph = {"success": "", "caution": "", "critical": ""}.get(tone, "")
        self.icon.set_glyph(glyph, tone if tone != "info" else "accent")
        self.setProperty("tone", tone)
        restyle(self)
        self.title.setText(title)
        self.title.setVisible(bool(title))
        self.message.setText(message)
        while self.actions.count():
            item = self.actions.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for text, slot in actions or []:
            self.actions.addWidget(button(text, slot))
        self.show()


class NavButton(QAbstractButton):
    """A navigation-pane item: glyph, label, and the accent selection pill."""

    def __init__(self, glyph: str, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.glyph = glyph
        self.setText(text)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(220, 38)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(4, 1, -4, -1)
        if self.isChecked() or self.underMouse() or self.isDown():
            painter.setPen(Qt.PenStyle.NoPen)
            fill = t.subtle_pressed if self.isDown() else (
                t.selected if self.isChecked() else t.subtle_hover)
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(rect, 5, 5)
        if self.isChecked():
            painter.setBrush(QColor(t.accent))
            painter.drawRoundedRect(QRectF(rect.left(), rect.center().y() - 8, 3, 16), 1.5, 1.5)
        painter.setPen(QColor(t.text))
        painter.setFont(theme.icon_font(16))
        painter.drawText(QRectF(rect.left() + 12, rect.top(), 20, rect.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.glyph)
        painter.setFont(theme.font(14, strong=self.isChecked()))
        painter.drawText(QRectF(rect.left() + 44, rect.top(), rect.width() - 50, rect.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        if self.hasFocus():
            painter.setPen(QPen(QColor(t.focus), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), 5, 5)
        painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().leaveEvent(event)


class ProgressRing(QWidget):
    """Indeterminate progress ring (Fluent ``ProgressRing``)."""

    def __init__(self, size: int = 20, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self.setAccessibleName("Working")

    def _tick(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        stroke = max(2.0, self.width() / 10)
        rect = QRectF(self.rect()).adjusted(stroke, stroke, -stroke, -stroke)
        pen = QPen(QColor(theme.current().accent), stroke, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, -self._angle * 16, 100 * 16)
        painter.end()


class Page(QScrollArea):
    """A scrolling page with a centred, width-limited column of content."""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("PageContent")
        outer = QHBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        column = QWidget()
        column.setObjectName("Transparent")
        column.setMaximumWidth(980)
        outer.addWidget(column)
        self.content = QVBoxLayout(column)
        self.content.setContentsMargins(36, 26, 36, 36)
        self.content.setSpacing(12)
        self.title = label(title, "PageTitle")
        self.title.setAccessibleName(title)
        self.content.addWidget(self.title)
        if subtitle:
            self.subtitle = label(subtitle, "Body2", wrap=True)
            self.content.addWidget(self.subtitle)
        self.content.addSpacing(6)
        self.setWidget(holder)

    def section(self, text: str) -> QLabel:
        heading = label(text, "SectionTitle")
        self.content.addSpacing(8)
        self.content.addWidget(heading)
        return heading

    def add(self, widget: QWidget) -> QWidget:
        self.content.addWidget(widget)
        return widget

    def finish(self) -> None:
        self.content.addStretch(1)

    def refresh(self) -> None:
        """Called when the page becomes visible or data changes."""


def caps_lock_on() -> bool:
    import ctypes
    return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)  # VK_CAPITAL toggled


class CapsLockWarning(QWidget):
    """"Caps Lock is on", shown under password boxes while one of them has focus:
    the most common reason a correct password is turned down."""

    def __init__(self, *fields: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(GlyphLabel(G.WARNING, 14, tone="caution"))
        layout.addWidget(label("Caps Lock is on", "Caption"))
        layout.addStretch(1)
        self._fields = fields
        for field in fields:
            field.installEventFilter(self)
        self.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut,
                            QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QTimer.singleShot(0, self.update_state)  # after Windows has toggled the key
        return False

    def update_state(self) -> None:
        self.setVisible(any(f.hasFocus() for f in self._fields) and caps_lock_on())


def hbox(*widgets: QWidget | None, spacing: int = 8, stretch_at: int | None = None) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(0, 0, 0, 0)
    for index, widget in enumerate(widgets):
        if stretch_at is not None and index == stretch_at:
            layout.addStretch(1)
        if widget is not None:
            layout.addWidget(widget)
    if stretch_at is not None and stretch_at >= len(widgets):
        layout.addStretch(1)
    return layout
