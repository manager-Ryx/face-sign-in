"""Short animated how-to guides for Windows settings the app can't change itself.

The first one turns off "only allow Windows Hello sign-in", which hides Face
Unlock on the lock screen. Each guide plays an animation from ``assets/guides``
(an animated WebP or GIF), shows the steps in words, and watches the setting
so it can say "Done" the moment the person has changed it. Until an animation
is added, a placeholder stands in for it.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from .. import system_setup
from .dialogs import Dialog
from .icons import G, GlyphLabel
from .widgets import ProgressRing, accent_button, button, hbox, label

GUIDES_DIR = Path(__file__).resolve().parent / "assets" / "guides"
ANIMATION_SIZE = QSize(560, 315)   # 16:9; animations are scaled to fit
CHECK_EVERY_MS = 1500


def animation_file(name: str, folder: Path = GUIDES_DIR) -> Path | None:
    """The guide's animation (WebP preferred, then GIF), if one has been added."""
    for suffix in (".webp", ".gif"):
        path = folder / f"{name}{suffix}"
        if path.is_file():
            return path
    return None


class Animation(QFrame):
    """Plays a looping animation, or shows a friendly placeholder without one."""

    def __init__(self, path: Path | None, placeholder: list[tuple[str, str]],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setFixedSize(ANIMATION_SIZE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.movie: QMovie | None = None
        if path is not None:
            self.movie = QMovie(str(path))
            self.movie.setScaledSize(ANIMATION_SIZE)
            screen = QLabel()
            screen.setAlignment(Qt.AlignmentFlag.AlignCenter)
            screen.setMovie(self.movie)
            screen.setAccessibleName("Animated guide")
            layout.addWidget(screen)
            self.movie.start()
            return
        # Placeholder: the path through Settings as a row of steps.
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        trail = []
        for index, (glyph, text) in enumerate(placeholder):
            if index:
                trail.append(GlyphLabel(G.CHEVRON_RIGHT, 12, tone="muted"))
            chip = QVBoxLayout()
            chip.setSpacing(6)
            chip.addWidget(GlyphLabel(glyph, 28, tone="accent"), 0, Qt.AlignmentFlag.AlignHCenter)
            chip.addWidget(label(text, "Caption"), 0, Qt.AlignmentFlag.AlignHCenter)
            holder = QWidget()
            holder.setLayout(chip)
            trail.append(holder)
        layout.addLayout(hbox(None, *trail, spacing=14, stretch_at=len(trail) + 1))
        layout.addSpacing(18)
        note = label("Animated guide coming soon", "Secondary")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(note)
        layout.addStretch(1)

    def stop(self) -> None:
        if self.movie is not None:
            self.movie.stop()


class HelloOnlyGuide(Dialog):
    """How to turn off "only allow Windows Hello sign-in", watching for it to happen."""

    def __init__(self, parent: QWidget | None = None, *,
                 hello_only: Callable[[], bool] = system_setup.hello_only_sign_in,
                 animation: Path | None | bool = True):
        super().__init__(parent, "Let Face Unlock appear on your lock screen", 600)
        self._hello_only = hello_only
        self.body.addWidget(label(
            "Windows is set to offer only Windows Hello on the lock screen, which hides Face "
            "Unlock. One switch changes that. Your PIN keeps working as before.",
            "Body2", wrap=True))
        path = animation_file("hello-only") if animation is True else (animation or None)
        self.animation = Animation(path, [(G.SETTINGS, "Settings"), (G.PERSON, "Accounts"),
                                          (G.KEY, "Sign-in options"), (G.CHECK, "Turn it off")])
        self.body.addWidget(self.animation, 0, Qt.AlignmentFlag.AlignHCenter)
        steps = (
            "Open Sign-in options (the button below takes you straight there).",
            "Scroll down to “Additional settings”.",
            "Turn off “For improved security, only allow Windows Hello sign-in for "
            "Microsoft accounts on this device”.",
        )
        for number, text in enumerate(steps, 1):
            badge = label(str(number), "CardTitle")
            badge.setFixedWidth(22)
            row = hbox(badge, spacing=8)
            row.addWidget(label(text, "Body", wrap=True), 1)
            self.body.addLayout(row)
        self.ring = ProgressRing(16)
        self.done_mark = GlyphLabel(G.DONE, 16, tone="success")
        self.state = label("", "Body2", wrap=True)
        status = hbox(self.ring, self.done_mark, spacing=8)
        status.addWidget(self.state, 1)
        self.body.addSpacing(4)
        self.body.addLayout(status)
        self.open_button = accent_button("Open Sign-in options", self._open_settings)
        self.close_button = button("Close", self.accept)
        self.buttons.addWidget(self.open_button)
        self.buttons.addWidget(self.close_button)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(CHECK_EVERY_MS)
        self.refresh()

    def refresh(self) -> None:
        """Watch the setting: say so as soon as it's off."""
        still_on = self._hello_only()
        self.ring.setVisible(still_on)
        self.done_mark.setVisible(not still_on)
        if still_on:
            self.state.setText("Waiting for the switch to be turned off…")
            return
        self.timer.stop()
        self.state.setText("Done! Face Unlock can now appear on your lock screen.")
        self.close_button.setText("Done")
        self.close_button.setDefault(True)
        self.open_button.hide()

    @staticmethod
    def _open_settings() -> None:
        os.startfile("ms-settings:signinoptions")  # type: ignore[attr-defined]

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        self.timer.stop()
        self.animation.stop()
        super().done(result)
