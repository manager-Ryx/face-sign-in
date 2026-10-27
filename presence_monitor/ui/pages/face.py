"""Your face: the profile, a recognition test, and the stored Windows password."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from .. import theme
from ..icons import G, GlyphLabel
from ..status import guidance_text, reason_text, when
from ..widgets import (
    Card,
    Page,
    Pill,
    accent_button,
    button,
    fade_in,
    hbox,
    label,
    show_toast,
    subtle_button,
)

if TYPE_CHECKING:
    from ...app import AppController

OUTCOME_TONES = {"match": "success", "uncertain": "caution", "mismatch": "critical",
                 "unusable": "caution", "no_face": "neutral", "spoof": "critical"}
OUTCOME_TEXT = {"match": "Match", "uncertain": "Close", "mismatch": "Not you",
                "unusable": "Unclear", "no_face": "No face", "spoof": "Not live"}


class FrameStrip(QWidget):
    """One coloured cell per analysed frame, with its match distance."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[dict] = []
        self.threshold = 0.5
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(400, 64)

    def show_frames(self, frames: list[dict], threshold: float) -> None:
        self.frames = frames
        self.threshold = threshold
        summary = ", ".join(OUTCOME_TEXT.get(f["outcome"], f["outcome"]) for f in frames)
        self.setAccessibleName(f"Frames: {summary}")
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.frames:
            return
        t = theme.current()
        colors = {"success": (t.success_bg, t.success), "caution": (t.caution_bg, t.caution),
                  "critical": (t.critical_bg, t.critical), "neutral": (t.subtle_hover, t.text2)}
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gap = 8
        width = (self.width() - gap * (len(self.frames) - 1)) / len(self.frames)
        for index, frame in enumerate(self.frames):
            fill, ink = colors[OUTCOME_TONES.get(frame["outcome"], "neutral")]
            rect = QRectF(index * (width + gap), 0, width, self.height() - 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(rect, 6, 6)
            painter.setPen(QColor(ink))
            painter.setFont(theme.font(13, strong=True))
            painter.drawText(rect.adjusted(0, 8, 0, -28), Qt.AlignmentFlag.AlignCenter,
                             OUTCOME_TEXT.get(frame["outcome"], frame["outcome"]))
            distance = frame.get("distance")
            painter.setFont(theme.font(12))
            painter.drawText(rect.adjusted(0, 32, 0, -6), Qt.AlignmentFlag.AlignCenter,
                             "—" if distance is None else f"{distance:.2f}")
        painter.end()


class FacePage(Page):
    def __init__(self, controller: AppController):
        super().__init__("Your face", "Face Unlock compares the camera image with the face "
                         "profile below. Everything stays on this PC.")
        self.controller = controller

        profile = Card(padding=(20, 18), spacing=10)
        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(GlyphLabel(G.FACE, 28, tone="accent"), 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(3)
        self.profile_title = label("", "HeroTitle")
        self.profile_detail = label("", "Body2", wrap=True)
        text.addWidget(self.profile_title)
        text.addWidget(self.profile_detail)
        top.addLayout(text, 1)
        profile.body.addLayout(top)
        self.add_button = accent_button("Add my current appearance",
                                        lambda: controller.open_setup("append"))
        self.add_button.setToolTip("Capture a few photos of how you look today, for example "
                                   "with new glasses or a beard, and add them to your profile.")
        self.restart_button = button("Start over…", self._start_over)
        self.restart_button.setToolTip("Replace all your reference photos with new ones (the "
                                       "old ones are backed up)")
        self.review_button = subtle_button("Review reference photos", self._review)
        self.review_button.setToolTip("Find photos that look unlike the rest, and remove them")
        profile.body.addLayout(hbox(self.add_button, self.restart_button, None,
                                    self.review_button, stretch_at=3))
        self.add(profile)

        self.section("Test face recognition")
        test = Card(padding=(20, 16), spacing=10)
        test.body.addWidget(label("Look at the camera and select Test. Face Unlock checks five "
                                  "frames and shows how closely each one matched.", "Body2",
                                  wrap=True))
        self.test_button = accent_button("Test", self._run_test)
        self.test_button.setToolTip("Looks at the camera for a few seconds and shows how each "
                                    "frame matched")
        self.record = QCheckBox("Use this test to help judge the strictness setting")
        self.record.setChecked(True)
        self.record.setToolTip("Stores the face signatures from this test (no photos) as "
                               "evaluation data. They are never used to sign you in.")
        test.body.addLayout(hbox(self.test_button, self.record, stretch_at=2))
        self.verdict_icon = GlyphLabel(G.DONE, 18, tone="success")
        self.verdict = label("", "CardTitle", wrap=True)
        self.verdict_detail = label("", "Secondary", wrap=True)
        self.strip = FrameStrip()
        verdict_row = hbox(self.verdict_icon, spacing=8)
        verdict_row.addWidget(self.verdict, 1)
        test.body.addLayout(verdict_row)
        for widget in (self.verdict_icon, self.verdict, self.verdict_detail, self.strip):
            widget.hide()
        for widget in (self.verdict_detail, self.strip):
            test.body.addWidget(widget)
        self.add(test)

        self.section("Windows password")
        password = Card(padding=(20, 16), spacing=8)
        self.password_state = Pill()
        self.password_text = label("", "Body2", wrap=True)
        password.body.addLayout(hbox(GlyphLabel(G.KEY, 18, tone="accent"), self.password_text,
                                     self.password_state, spacing=12))
        self.password_button = button("Update stored password…", self._password)
        password.body.addLayout(hbox(self.password_button, stretch_at=1))
        self.add(password)
        self.finish()
        controller.status_changed.connect(lambda _s: self.refresh())

    def refresh(self) -> None:
        status = self.controller.client.status or {}
        profile = status.get("profile") or {}
        anchors, learned = profile.get("anchors", 0), profile.get("learned", 0)
        if anchors:
            self.profile_title.setText(f"{anchors} reference photos")
            learning = status.get("learning") or {}
            detail = f"Plus {learned} sample{'s' if learned != 1 else ''} learned from face sign-ins."
            if learning.get("last"):
                detail += f" Last learned {when(learning['last'])}."
            self.profile_detail.setText(detail)
        else:
            self.profile_title.setText("Not set up yet")
            self.profile_detail.setText("Set up face recognition to sign in with your face.")
        self.add_button.setText("Add my current appearance" if anchors else "Set up")
        self.restart_button.setVisible(bool(anchors))
        self.review_button.setVisible(bool(anchors))
        self.test_button.setEnabled(bool(anchors) and status != {})
        self.test_button.setToolTip(
            "Looks at the camera for a few seconds and shows how each frame matched"
            if anchors and status else "Set up face recognition first" if status
            else "Face Unlock's background service isn't running")
        stored = bool(status.get("credentials"))
        self.password_state.set_state("Stored" if stored else "Not stored",
                                      "success" if stored else "caution")
        self.password_text.setText(
            "Encrypted with Windows so only your account on this PC can read it." if stored else
            "Face sign-in needs your Windows password to sign you in.")
        self.password_button.setText("Update stored password…" if stored else "Store password…")

    def _start_over(self) -> None:
        from ..dialogs import confirm
        if confirm(self, "Start over?", "This replaces your reference photos with new ones and "
                   "clears learned samples. Your current photos are backed up first.",
                   "Start over"):
            self.controller.open_setup("replace")

    def _review(self) -> None:
        from ..dialogs import ProfileReviewDialog
        ProfileReviewDialog(self.controller, self).exec()
        self.controller.client.refresh_status()

    def _password(self) -> None:
        from ..dialogs import PasswordDialog
        if PasswordDialog(self.controller, self).exec():
            show_toast(self, "Password saved. Face sign-in will use it.")
        self.controller.client.refresh_status()

    def _verdict(self, tone: str, text: str) -> None:
        glyph = {"success": G.DONE, "caution": G.WARNING, "critical": G.ERROR}[tone]
        self.verdict_icon.set_glyph(glyph, tone)
        self.verdict_icon.show()
        self.verdict.setText(text)
        fade_in(self.verdict_icon, 240)

    def _run_test(self) -> None:
        self.test_button.set_busy(True, "Testing…")
        self.verdict_icon.hide()
        self.verdict.setText("Looking at the camera…")
        self.verdict.show()
        self.verdict_detail.setText("")
        self.strip.hide()

        def done(reply) -> None:
            self.test_button.set_busy(False)
            self.verdict_detail.show()
            if reply is None:
                self._verdict("critical", "Face Unlock isn't responding right now.")
                return
            if not reply.get("ok"):
                self._verdict("caution", "The test couldn't run.")
                self.verdict_detail.setText(guidance_text(reply.get("reason")) or
                                            reply.get("message", reason_text(reply.get("reason"))))
                return
            frames = reply.get("frames", [])
            matches = sum(1 for f in frames if f["outcome"] == "match")
            if reply.get("accepted"):
                self._verdict("success",
                              f"Recognised you ({matches} of {len(frames)} frames matched)")
            else:
                self._verdict("caution", "Not recognised. " + guidance_text(reply.get("guidance")))
            distances = [f["distance"] for f in frames if f.get("distance") is not None]
            detail = [f"Strictness in use: {reply.get('threshold', 0):.2f} (a frame matches at "
                      "or below this; lower numbers mean a closer match)."]
            if distances:
                detail.append(f"Closest match: {min(distances):.2f}.")
            detail.append(f"Camera start-up {reply.get('camera_open_ms', 0) / 1000:.1f} s, "
                          f"analysis {reply.get('verify_ms', 0) / 1000:.1f} s.")
            if reply.get("recorded"):
                detail.append("Saved as evaluation data.")
            self.verdict_detail.setText(" ".join(detail))
            self.strip.show_frames(frames, reply.get("threshold", 0.5))
            self.strip.show()
            fade_in(self.strip)

        self.controller.client.request({"cmd": "check", "record": self.record.isChecked()},
                                       done, timeout_s=30.0)
