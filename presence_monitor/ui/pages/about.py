"""About and help: how Face Unlock works, its limits, and privacy."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt

from face_service._version import __version__

from ..icons import G, GlyphLabel, app_icon
from ..widgets import Card, Page, hbox, label

if TYPE_CHECKING:
    from ...app import AppController

HOW = (
    (G.FACE, "Signing in", "On the lock screen, the Face Unlock tile asks the background "
     "service to look for you. When two frames match your face profile, Windows signs you in "
     "with your stored password, exactly as if you had typed it."),
    (G.LOCK, "Locking when you leave", "Every few minutes the camera briefly checks that you're "
     "still here. After you've been away for the time you chose, Windows locks; your apps keep "
     "running."),
    (G.BULB, "Keeping up with you", "After Windows confirms a face sign-in, the most useful new "
     "view of your face can be added to your profile, within strict limits."),
)
LIMITS = (
    "After Windows restarts, sign in once with your PIN or password. The background service "
    "starts at sign-in and your stored password can only be decrypted inside your session, so "
    "face sign-in works from the next lock screen.",
    "This is not Windows Hello. An ordinary webcam can't measure depth, and photo-spoof "
    "protection is optional (Advanced). Without it, a good photo of you could unlock your PC.",
    "No setting can guarantee that every other face is always rejected. Stricter settings make "
    "it less likely, at the cost of asking you for your PIN more often.",
    "Your PIN and password always keep working on the sign-in screen.",
)
PRIVACY = (
    "Your reference photos, face profile, settings and measurements are stored only in your "
    "user folder on this PC. Nothing is uploaded. Your Windows password is encrypted with "
    "Windows so only your account on this PC can decrypt it. Measurements never contain images "
    "or face signatures."
)
CREDITS = (
    "Built on DeepFace (MIT) with the ArcFace recognition model, OpenCV and its YuNet face "
    "detector, Qt for Python (LGPL), and pywin32. Check the licences of the recognition model "
    "weights before any commercial distribution."
)


class AboutPage(Page):
    def __init__(self, controller: AppController):
        super().__init__("About and help")
        header = Card(padding=(20, 18), spacing=4)
        logo = label("")
        logo.setPixmap(app_icon().pixmap(48, 48))
        title = label("Face Unlock", "HeroTitle")
        version = label(f"Version {__version__}", "Secondary")
        text = hbox(logo, None, spacing=14)
        from PySide6.QtWidgets import QVBoxLayout
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(title)
        column.addWidget(version)
        text.addLayout(column, 1)
        header.body.addLayout(text)
        self.add(header)

        self.section("How it works")
        how = Card(padding=(20, 16), spacing=12)
        for glyph, heading, body in HOW:
            row = hbox(GlyphLabel(glyph, 18, tone="accent"), spacing=12)
            column = QVBoxLayout()
            column.setSpacing(2)
            column.addWidget(label(heading, "CardTitle"))
            column.addWidget(label(body, "Body2", wrap=True))
            row.addLayout(column, 1)
            row.setAlignment(Qt.AlignmentFlag.AlignTop)
            how.body.addLayout(row)
        self.add(how)

        self.section("Good to know")
        limits = Card(padding=(20, 16), spacing=10)
        for text_ in LIMITS:
            limits.body.addLayout(hbox(GlyphLabel(G.INFO, 16, tone="muted"),
                                       label(text_, "Body2", wrap=True), spacing=10))
        self.add(limits)

        self.section("Privacy")
        privacy = Card(padding=(20, 16))
        privacy.body.addWidget(label(PRIVACY, "Body2", wrap=True))
        self.add(privacy)

        self.section("Credits")
        credits_card = Card(padding=(20, 16))
        credits_card.body.addWidget(label(CREDITS, "Body2", wrap=True))
        self.add(credits_card)
        self.finish()
