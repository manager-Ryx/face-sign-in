"""Reusable, theme-aware building blocks for the Face Unlock window."""
from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolTip,
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
FADE_MS = 160       # how long things take to appear: noticeable, never in the way

# Off for still renders and tests (FACE_UNLOCK_STILL=1). Windows' own "Animation
# effects" switch (Accessibility > Visual effects) turns motion off as well.
MOTION = os.environ.get("FACE_UNLOCK_STILL") != "1"


def motion_enabled() -> bool:
    if not MOTION:
        return False
    try:
        import ctypes
        enabled = ctypes.c_int(1)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
        return bool(enabled.value)   # SPI_GETCLIENTAREAANIMATION
    except (AttributeError, OSError):
        return True


def fade_in(widget: QWidget, ms: int = FADE_MS, *, start: float = 0.0) -> None:
    """Let a widget appear gently (drawn without the effect again once it ends)."""
    if not motion_enabled() or not widget.isVisible():
        return
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(start)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(ms)
    animation.setStartValue(start)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    # Text renders crisper without an effect, so drop it afterwards.
    animation.finished.connect(lambda: widget.graphicsEffect() is effect
                               and widget.setGraphicsEffect(None))
    animation.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)


def fade_out(widget: QWidget, ms: int = FADE_MS, *, then: Callable | None = None) -> None:
    """Fade a widget away, then hide it (and call ``then``)."""
    def finish() -> None:
        widget.hide()
        widget.setGraphicsEffect(None)
        if then is not None:
            then()
    if not motion_enabled() or not widget.isVisible():
        finish()
        return
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(ms)
    animation.setStartValue(1.0)
    animation.setEndValue(0.0)
    animation.finished.connect(finish)
    animation.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)


def spinner_pixmap(angle: int, color: str, size: int = 16, dpr: float = 2.0) -> QPixmap:
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    stroke = 2.0
    painter.setPen(QPen(QColor(color), stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawArc(QRectF(stroke, stroke, size - 2 * stroke, size - 2 * stroke),
                    -angle * 16, 100 * 16)
    painter.end()
    return pixmap


class Button(QPushButton):
    """A push button that can show it's working: a small spinner and words such as
    "Checking…", ignoring clicks until the work is done."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._idle: list | None = None    # [text, enabled, icon] to restore afterwards
        self._angle = 0
        self._spin = QTimer(self)
        self._spin.setInterval(40)
        self._spin.timeout.connect(self._turn)

    @property
    def busy(self) -> bool:
        return self._idle is not None

    def set_busy(self, busy: bool, text: str = "") -> None:
        if busy:
            if self._idle is None:
                self._idle = [self.text(), self.isEnabled(), self.icon()]
            if text:
                QPushButton.setText(self, text)
            QPushButton.setEnabled(self, False)
            self.setIconSize(QSize(16, 16))
            self._turn()
            self._spin.start()
            self.setAccessibleDescription("Working")
        elif self._idle is not None:
            self._spin.stop()
            text_, enabled, icon = self._idle
            self._idle = None
            QPushButton.setText(self, text_)
            QPushButton.setEnabled(self, enabled)
            self.setIcon(icon)
            self.setAccessibleDescription("")

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        if self._idle is not None:
            self._idle[0] = text  # shown once the work is done
        else:
            super().setText(text)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt API
        if self._idle is not None:
            self._idle[1] = enabled
        else:
            super().setEnabled(enabled)

    def _turn(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.setIcon(QIcon(spinner_pixmap(self._angle, theme.current().text2)))


def _button(text: str, slot: Callable | None, name: str = "") -> Button:
    widget = Button(text)
    if name:
        widget.setObjectName(name)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setMinimumHeight(BUTTON_HEIGHT)
    if slot:
        widget.clicked.connect(slot)
    return widget


def accent_button(text: str, slot: Callable | None = None) -> Button:
    """The single most important action in a view."""
    return _button(text, slot, "Accent")


def button(text: str, slot: Callable | None = None) -> Button:
    return _button(text, slot)


def subtle_button(text: str, slot: Callable | None = None) -> Button:
    """A low-emphasis, text-style action."""
    return _button(text, slot, "Subtle")


def glyph_button(glyph: str, tip: str, slot: Callable | None = None, *,
                 size: int = 28) -> QPushButton:
    """A small icon-only button; ``tip`` is its tooltip and screen-reader name."""
    widget = QPushButton(glyph)
    widget.setObjectName("Glyph")
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setFixedSize(size, size)
    widget.setToolTip(tip)
    widget.setAccessibleName(tip)
    if slot:
        widget.clicked.connect(slot)
    return widget


def set_unavailable(widget: QWidget, reason: str) -> None:
    """Disable a control and say why on hover; an empty ``reason`` enables it again."""
    widget.setEnabled(not reason)
    widget.setToolTip(reason)


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
    """A status chip: its tooltip explains it, and it fades briefly when it changes."""

    def __init__(self, text: str = "", tone: str = "neutral", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("Pill")
        self.setProperty("tone", tone)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_state(self, text: str, tone: str, tip: str | None = None) -> None:
        changed = bool(self.text()) and (text != self.text() or tone != self.property("tone"))
        self.setText(text)
        if tip is not None:
            self.setToolTip(tip)
            self.setAccessibleName(f"{text}. {tip}" if tip else text)
        if self.property("tone") != tone:
            self.setProperty("tone", tone)
            restyle(self)
        if changed:
            fade_in(self, 240, start=0.2)


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
                 control: QWidget | None = None, parent: QWidget | None = None, *,
                 explain: str = ""):
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
        self.explain = explain
        if explain:
            # "What's this?": the fuller story for a subtle setting, on hover or click.
            self.help_button = glyph_button(G.HELP, "What's this?", self._show_help, size=22)
            self.help_button.setToolTip(explain)
            title_row = QHBoxLayout()
            title_row.setSpacing(4)
            title_row.addWidget(self.title)
            title_row.addWidget(self.help_button)
            title_row.addStretch(1)
            text.addLayout(title_row)
        else:
            text.addWidget(self.title)
        # Always part of the row (hidden while empty): a description set later must
        # appear here, not as a stray top-level window.
        text.addWidget(self.description)
        self.description.setVisible(bool(description))
        layout.addLayout(text, 1)
        # Two small slots before the control, always reserved so controls line up and
        # nothing shifts: a tick confirming a save, and "back to the default".
        self.saved_mark = GlyphLabel(G.CHECK, 14, tone="success")
        self.saved_mark.setToolTip("Saved")
        self.reset_button = glyph_button(G.UNDO, "Back to the default", size=26)
        for slot in (self.saved_mark, self.reset_button):
            policy = slot.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            slot.setSizePolicy(policy)
            slot.hide()
            layout.addWidget(slot, 0, Qt.AlignmentFlag.AlignVCenter)
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        self._saved_timer.timeout.connect(lambda: fade_out(self.saved_mark, 400))
        self.control_box = QHBoxLayout()
        self.control_box.setSpacing(10)
        layout.addLayout(self.control_box)
        if control is not None:
            self.set_control(control)

    def _show_help(self) -> None:
        QToolTip.showText(self.help_button.mapToGlobal(QPoint(0, self.help_button.height())),
                          self.explain, self.help_button)

    def flash_saved(self) -> None:
        """A tick fades in beside the control, then away: the change was saved."""
        self._saved_timer.stop()
        self.saved_mark.setGraphicsEffect(None)
        self.saved_mark.show()
        fade_in(self.saved_mark, 180)
        self._saved_timer.start(1600)

    def show_reset(self, default_text: str | None) -> None:
        """Offer "back to the default" while the value differs (None hides it)."""
        self.reset_button.setVisible(default_text is not None)
        if default_text is not None:
            tip = f"Back to the default: {default_text}"
            self.reset_button.setToolTip(tip)
            self.reset_button.setAccessibleName(tip)

    def matches(self, words: list[str]) -> bool:
        text = " ".join((self.title.text(), self.description.text(), self.explain)).casefold()
        return all(word in text for word in words)

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
        self.rows: list[tuple[QWidget | None, QWidget]] = []   # (hairline above, row)

    def add(self, row: QWidget) -> QWidget:
        divider = None
        if self.rows:
            divider = QWidget()
            divider.setObjectName("Transparent")
            wrapper = QHBoxLayout(divider)
            wrapper.setContentsMargins(16, 0, 16, 0)
            wrapper.addWidget(Divider())
            self.body.addWidget(divider)
        self.body.addWidget(row)
        self.rows.append((divider, row))
        return row

    def filter(self, words: list[str]) -> int:
        """Show only the rows matching every word; returns how many are shown."""
        shown = 0
        for divider, row in self.rows:
            visible = not words or not isinstance(row, SettingRow) or row.matches(words)
            row.setVisible(visible)
            if divider is not None:
                divider.setVisible(visible and shown > 0)
            shown += visible
        self.setVisible(shown > 0)
        return shown


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
        appearing = not self.isVisible()
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
        self.setAccessibleName(f"{title} {message}".strip())
        self.show()
        if appearing:
            fade_in(self)


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
        self._sections: list[tuple[QLabel, list[SettingsGroup]]] = []
        self.search: QLineEdit | None = None
        self.no_results: QLabel | None = None

    def section(self, text: str) -> QLabel:
        heading = label(text, "SectionTitle")
        self.content.addSpacing(8)
        self.content.addWidget(heading)
        self._sections.append((heading, []))
        return heading

    def add(self, widget: QWidget) -> QWidget:
        self.content.addWidget(widget)
        if isinstance(widget, SettingsGroup) and self._sections:
            self._sections[-1][1].append(widget)
        return widget

    def add_search(self, placeholder: str = "Find a setting") -> QLineEdit:
        """A filter box: only settings containing every typed word stay visible."""
        from .icons import glyph_icon
        self.search = QLineEdit()
        self.search.setPlaceholderText(placeholder)
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName(placeholder)
        self.search.setToolTip("Type a word such as camera, lock or learn (Ctrl+F)")
        self.search.addAction(glyph_icon(G.SEARCH, theme.current().text2),
                              QLineEdit.ActionPosition.LeadingPosition)
        self.search.setMaximumWidth(360)
        self.search.textChanged.connect(self.filter_settings)
        self.content.addWidget(self.search)
        self.no_results = label("", "Body2", wrap=True)
        self.content.addWidget(self.no_results)
        self.no_results.hide()
        return self.search

    def filter_settings(self, text: str) -> int:
        words = text.casefold().split()
        total = 0
        for heading, groups in self._sections:
            shown = sum(group.filter(words) for group in groups)
            heading.setVisible(shown > 0 or not groups)
            total += shown
        if self.no_results is not None:
            self.no_results.setText(f"No settings match “{text.strip()}”. Try another "
                                    "word, such as camera, lock or learn.")
            self.no_results.setVisible(bool(words) and total == 0)
        return total

    def finish(self) -> None:
        self.content.addStretch(1)

    def refresh(self) -> None:
        """Called when the page becomes visible or data changes."""


class VScroll(QScrollArea):
    """Scrolls its content vertically when the window is short, keeping wrapped text at
    its natural height instead of squashing rows together."""

    def __init__(self, content: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(content)
        content.installEventFilter(self)   # text changed: fit again

    def _fit(self) -> None:
        content = self.widget()
        layout = content.layout() if content is not None else None
        if layout is None:
            return
        width = self.viewport().width()
        height = (layout.totalHeightForWidth(width) if layout.hasHeightForWidth()
                  else layout.totalSizeHint().height())
        if content.minimumHeight() != height:
            content.setMinimumHeight(height)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if watched is self.widget() and event.type() == QEvent.Type.LayoutRequest:
            QTimer.singleShot(0, self._fit)
        return False

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._fit()
        super().resizeEvent(event)


class Toast(QFrame):
    """A short message floating at the bottom of a window that fades away by itself:
    "Saved", "Copied", "Auto-lock paused for 15 minutes · Undo"."""

    def __init__(self, host: QWidget, text: str, tone: str = "success",
                 action: tuple[str, Callable] | None = None, ms: int = 3500):
        window = host.window()
        super().__init__(window)
        self.setObjectName("Toast")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 8 if action else 16, 8)
        layout.setSpacing(10)
        glyph = {"success": G.DONE, "caution": G.WARNING, "critical": G.ERROR}.get(tone, G.INFO)
        layout.addWidget(GlyphLabel(glyph, 16, tone=tone if tone != "info" else "accent"))
        self.text = label(text, "Body")
        layout.addWidget(self.text)
        if action is not None:
            self.action = subtle_button(action[0], lambda: (action[1](), self.dismiss()))
            layout.addWidget(self.action)
        self.setAccessibleName(text)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._timer.start(ms)
        previous = getattr(window, "_toast", None)
        if previous is not None:
            try:
                previous.deleteLater()
            except RuntimeError:
                pass  # already gone
        window._toast = self
        size = self.sizeHint()
        width = min(size.width(), window.width() - 48)
        self.setGeometry((window.width() - width) // 2, window.height() - size.height() - 24,
                         width, size.height())
        self.show()
        self.raise_()
        fade_in(self, 180)

    def dismiss(self) -> None:
        self._timer.stop()
        fade_out(self, 300, then=self.deleteLater)


def show_toast(host: QWidget, text: str, tone: str = "success",
               action: tuple[str, Callable] | None = None, ms: int = 3500) -> Toast:
    return Toast(host, text, tone, action, ms)


class StepIndicator(QWidget):
    """Numbered steps joined by a line: finished ones get a tick, the current one is
    filled with the accent colour, and the line grows as you go."""

    def __init__(self, steps: tuple[str, ...] | list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.steps = list(steps)
        self.index = 0
        self._shown = 0.0
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._animation = QPropertyAnimation(self, b"progress", self)
        self._animation.setDuration(280)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_progress(self) -> float:
        return self._shown

    def _set_progress(self, value: float) -> None:
        self._shown = value
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def set_step(self, index: int) -> None:
        self.index = index
        self.setAccessibleName(f"Step {index + 1} of {len(self.steps)}: {self.steps[index]}")
        self._animation.stop()
        if motion_enabled() and self.isVisible():
            self._animation.setStartValue(self._shown)
            self._animation.setEndValue(float(index))
            self._animation.start()
        else:
            self._set_progress(float(index))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(600, 30)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        normal, strong = theme.font(12), theme.font(12, strong=True)
        dot, gap = 20.0, 6.0
        painter.setFont(strong)
        widths = [painter.fontMetrics().horizontalAdvance(text) for text in self.steps]
        links = len(self.steps) - 1
        free = self.width() - sum(dot + gap + w for w in widths) - 2
        if free < links * 16:  # too narrow for every name: show only the current one
            widths = [w if i == self.index else -gap for i, w in enumerate(widths)]
            free = self.width() - sum(dot + gap + w for w in widths) - 2
        link = max(10.0, free / max(1, links))
        y = self.height() / 2
        centres, x = [], 1.0
        for width in widths:
            centres.append(x + dot / 2)
            x += dot + gap + width + link
        for i in range(links):   # the line between steps, filled up to the current one
            start = centres[i] + dot / 2 + gap + widths[i] + 6
            end = centres[i + 1] - dot / 2 - 6
            if end <= start:
                continue
            painter.setPen(QPen(QColor(t.divider), 2, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(start, y), QPointF(end, y))
            fill = min(1.0, max(0.0, self._shown - i))
            if fill > 0:
                painter.setPen(QPen(QColor(t.accent), 2, Qt.PenStyle.SolidLine,
                                    Qt.PenCapStyle.RoundCap))
                painter.drawLine(QPointF(start, y), QPointF(start + (end - start) * fill, y))
        for i, text in enumerate(self.steps):
            circle = QRectF(centres[i] - dot / 2, y - dot / 2, dot, dot)
            if i <= self.index:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(t.accent))
                painter.drawEllipse(circle)
            if i < self.index:
                painter.setPen(QPen(QColor(t.on_accent), 1.8, Qt.PenStyle.SolidLine,
                                    Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                tick = QPainterPath(QPointF(circle.left() + 5.5, y + 0.5))
                tick.lineTo(circle.left() + 8.5, y + 3.5)
                tick.lineTo(circle.left() + 14.5, y - 3.5)
                painter.drawPath(tick)
            else:
                if i > self.index:
                    painter.setPen(QPen(QColor(t.text3), 1.2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawEllipse(circle.adjusted(0.6, 0.6, -0.6, -0.6))
                painter.setPen(QColor(t.on_accent if i == self.index else t.text3))
                painter.setFont(strong if i == self.index else normal)
                painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, str(i + 1))
            if widths[i] > 0:
                painter.setFont(strong if i == self.index else normal)
                painter.setPen(QColor(t.text if i <= self.index else t.text3))
                painter.drawText(QRectF(circle.right() + gap, 0, widths[i] + 4, self.height()),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.end()


class SuccessBadge(QWidget):
    """A green circle whose tick draws itself: the satisfying "all done" moment."""

    def __init__(self, size: int = 56, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._drawn = 1.0
        self._animation = QPropertyAnimation(self, b"drawn", self)
        self._animation.setDuration(560)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setAccessibleName("Done")

    def _get_drawn(self) -> float:
        return self._drawn

    def _set_drawn(self, value: float) -> None:
        self._drawn = value
        self.update()

    drawn = Property(float, _get_drawn, _set_drawn)

    def play(self) -> None:
        self._animation.stop()
        if not motion_enabled():
            self._set_drawn(1.0)
            return
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = self.width()
        grow = min(1.0, self._drawn * 2.2)            # the circle pops in first...
        radius = size / 2 * (0.6 + 0.4 * grow)
        fill = QColor(t.success)
        fill.setAlphaF(0.2 + 0.8 * grow)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawEllipse(QPointF(size / 2, size / 2), radius, radius)
        tick = max(0.0, (self._drawn - 0.35) / 0.65)  # ...then the tick draws itself
        if tick > 0:
            a, b, c = (QPointF(size * 0.29, size * 0.52), QPointF(size * 0.44, size * 0.66),
                       QPointF(size * 0.72, size * 0.37))
            first, second = (b - a).manhattanLength(), (c - b).manhattanLength()
            length = (first + second) * tick
            path = QPainterPath(a)
            if length <= first:
                path.lineTo(a + (b - a) * (length / first))
            else:
                path.lineTo(b)
                path.lineTo(b + (c - b) * ((length - first) / second))
            painter.setPen(QPen(QColor("#1B1B1B" if t.dark else "#FFFFFF"), max(2.5, size / 14),
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.end()


class StatusLine(QWidget):
    """A result shown where you're looking: a spinner while working, then a coloured
    icon with the outcome ("Recognised you", "Not saved: …")."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Transparent")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.ring = ProgressRing(16)
        self.icon = GlyphLabel(G.DONE, 16, tone="success")
        self.text = label("", "Body2", wrap=True)
        layout.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.text, 1)
        self.clear()

    def working(self, text: str) -> None:
        self.ring.show()
        self.icon.hide()
        self._say(text)

    def done(self, tone: str, text: str) -> None:
        """``tone``: success, caution, critical or info."""
        self.ring.hide()
        glyph = {"success": G.DONE, "caution": G.WARNING, "critical": G.ERROR}.get(tone, G.INFO)
        self.icon.set_glyph(glyph, tone if tone != "info" else "accent")
        self.icon.show()
        self._say(text)
        fade_in(self.icon, 240)

    def clear(self) -> None:
        self.ring.hide()
        self.icon.hide()
        self.text.setText("")
        self.hide()

    def _say(self, text: str) -> None:
        self.text.setText(text)
        self.setAccessibleName(text)
        if not self.isVisible():
            self.show()
            fade_in(self)


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
