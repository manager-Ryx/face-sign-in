"""First-run setup: from a fresh install to signing in with your face.

Six short steps – welcome and consent; getting the PC ready; your Windows
password; your face; a few preferences; try it. "Getting ready" does the
work itself wherever it can: it starts the background service, picks your
webcam when there's only one real camera, waits for the face model,
installs the sign-in tile (Windows asks for permission once) and makes
Face Unlock start when you sign in. Anything it can't fix is explained, and
nothing here blocks your PIN or password.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QProgressBar,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import system_setup
from .camera_view import CameraPeek
from .dialogs import (
    PASSWORD_REASON,
    microsoft_account,
    open_password_help,
    password_hint,
    store_windows_password,
    windows_account,
)
from .icons import G, GlyphLabel
from .status import download_text
from .widgets import (
    Banner,
    CapsLockWarning,
    Card,
    ProgressRing,
    SettingRow,
    StatusLine,
    StepIndicator,
    SuccessBadge,
    ToggleSwitch,
    VScroll,
    accent_button,
    button,
    fade_in,
    hbox,
    label,
    subtle_button,
)

if TYPE_CHECKING:
    from ..app import AppController

log = logging.getLogger(__name__)

STEPS = ("Welcome", "Get ready", "Password", "Your face", "Preferences", "Try it")
CHECK_EVERY_MS = 2000
LOCAL_CHECK_EVERY = 5      # timer ticks between the slower local checks (registry, tasks)

# "When should the sign-in screen look for you?"
SCANNING = {
    "instant": ("Sign me in the moment I sit down (recommended)",
                "The camera keeps looking for a while after you leave and learns when you "
                "usually come back.",
                {"unlock_learn_routine": True, "unlock_idle_after_s": 1800.0,
                 "unlock_idle_interval_s": 3.0, "unlock_pause_when_display_off": False}),
    "balanced": ("Balanced",
                 "Looks continuously for 5 minutes, then every 5 seconds; rests while the "
                 "screen is off.",
                 {"unlock_learn_routine": True, "unlock_idle_after_s": 300.0,
                  "unlock_idle_interval_s": 5.0, "unlock_pause_when_display_off": True}),
    "battery": ("Save battery",
                "Looks for a minute, then every 10 seconds; rests while the screen is off. "
                "Press a key to sign in sooner.",
                {"unlock_learn_routine": False, "unlock_idle_after_s": 60.0,
                 "unlock_idle_interval_s": 10.0, "unlock_pause_when_display_off": True}),
}
LOCK_AFTER = ((300, "5 minutes"), (900, "15 minutes (recommended)"), (1800, "30 minutes"),
              (3600, "1 hour"))


def preference_changes(*, auto_lock: bool, lock_after_s: int, scanning: str, learning: bool,
                       notifications: bool) -> dict[str, Any]:
    """Settings for the answers on the Preferences step."""
    changes: dict[str, Any] = {
        "presence_enabled": auto_lock,
        "presence_absence_timeout_s": int(lock_after_s),
        # Check often enough that locking happens close to the chosen time.
        "presence_interval_s": int(max(60, min(300, lock_after_s // 5))),
        "adaptive_enrollment": learning,
        "adaptive_learn_after_password": learning,
        "adaptive_learn_while_present": learning,
        "notifications": notifications,
    }
    changes.update(SCANNING[scanning][2])
    return changes


@dataclass
class SystemChecks:
    """What the "Get ready" step checks locally (replaceable in previews and tests)."""

    tile_state: Any = field(default=system_setup.tile_state)
    install_tile: Any = field(default=system_setup.install_tile)
    autostart_state: Any = field(default=system_setup.autostart_state)
    enable_autostart: Any = field(default=system_setup.enable_autostart)
    hello_only: Any = field(default=system_setup.hello_only_sign_in)
    start_service: Any = field(default=system_setup.start_service)


class _Relay(QObject):
    done = Signal(object, object)   # callback, result


class CheckRow(SettingRow):
    """One readiness item: what it is, how it stands, and a button to fix it."""

    def __init__(self, glyph: str, title: str):
        super().__init__(glyph, title, "")
        self.layout().setContentsMargins(16, 8, 16, 8)   # compact: five rows and the camera
        for unused in (self.saved_mark, self.reset_button):   # not settings: no slots needed
            self.layout().removeWidget(unused)
            unused.deleteLater()
        self.mark = GlyphLabel(G.DONE, 18, tone="success")
        self.ring = ProgressRing(18)
        self.action = button("", None)
        for widget in (self.ring, self.mark, self.action):
            self.control_box.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setAccessibleName(f"{title} progress")
        # Its thin strip is kept while hidden, so nothing moves when a download starts.
        policy = self.bar.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.bar.setSizePolicy(policy)
        self.bar.hide()
        self.text_box.addSpacing(4)
        self.text_box.addWidget(self.bar)
        self.ok = False
        self._slot = None

    def show_progress(self, done: int, total: int, text: str) -> None:
        """Working, with a bar: how far it is (or just busy, if the size is unknown)."""
        self.show_state("working", text)
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, round(done * 1000 / total)))
        else:
            self.bar.setRange(0, 0)
        self.bar.show()

    def show_state(self, state: str, text: str, action: str = "", slot=None) -> None:
        """``state``: ok, working, action or problem."""
        became_ok = state == "ok" and not self.ok
        self.ok = state == "ok"
        self.bar.hide()
        self.set_description(text)
        self.mark.setVisible(self.ok)
        if became_ok:
            fade_in(self.mark, 260)   # each item ticks itself off as it's ready
        self.ring.setVisible(state == "working")
        self.action.setVisible(bool(action))
        if action:
            self.action.setText(action)
        if slot is not self._slot:
            if self._slot is not None:
                self.action.clicked.disconnect(self._slot)
            self._slot = slot
            if slot is not None:
                self.action.clicked.connect(slot)


class OnboardingWizard(QDialog):
    def __init__(self, controller: AppController, parent: QWidget | None = None, *,
                 checks: SystemChecks | None = None):
        super().__init__(parent)
        self.controller = controller
        self.checks = checks or SystemChecks()
        self._relay = _Relay(self)
        self._relay.done.connect(lambda callback, result: callback(result))
        self._busy: set[str] = set()
        self._download_start: tuple[float, int] | None = None   # for "minutes left"
        self._camera_listed = False
        self._local: dict | None = None    # tile, Windows Hello-only, autostart
        self._ticks = 0
        self.setWindowTitle("Set up Face Unlock")
        self.setMinimumSize(780, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(14)
        self.steps = StepIndicator(STEPS)
        outer.addWidget(self.steps)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.banner = Banner()
        outer.addWidget(self.banner)
        buttons = QHBoxLayout()
        self.back = button("Back", self._back)
        self.back.setToolTip("The previous step (Alt+Left)")
        self.later = button("Finish later", self.reject)
        self.later.setToolTip("Close setup. You can carry on from the Face Unlock icon in the "
                              "notification area.")
        self.next = accent_button("", self._next)
        self.next.setDefault(True)
        # Why Continue isn't available yet, said next to it (not only on hover).
        self.waiting = label("", "Caption")
        buttons.addWidget(self.back)
        buttons.addStretch(1)
        buttons.addWidget(self.waiting)
        buttons.addWidget(self.later)
        buttons.addWidget(self.next)
        outer.addLayout(buttons)
        QShortcut(QKeySequence("Alt+Left"), self, activated=lambda: self.back.isVisible()
                  and self._back())

        for page in (self._welcome(), self._ready(), self._password(), self._face(),
                     self._preferences(), self._try()):
            self.stack.addWidget(VScroll(page))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        controller.status_changed.connect(self._on_status)
        self.finished.connect(lambda _code: controller.status_changed.disconnect(self._on_status))
        self._go(0)

    def _on_status(self, _status: Any) -> None:
        self._refresh()

    # ---- helpers ---------------------------------------------------------------------

    @staticmethod
    def _page(title: str, intro: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("Transparent")
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(label(title, "PageTitle", wrap=True))
        if intro:
            layout.addWidget(label(intro, "Body2", wrap=True))
        return page, layout

    def _in_background(self, key: str, work, done) -> None:
        """Run blocking work (Windows permission prompts, registry) off the UI thread."""
        if key in self._busy:
            return
        self._busy.add(key)

        def run() -> None:
            try:
                result = work()
            except Exception as exc:
                log.exception("setup step %s failed", key)
                result = (False, str(exc))
            self._relay.done.emit(lambda r: (self._busy.discard(key), done(r)), result)

        threading.Thread(target=run, name=f"setup-{key}", daemon=True).start()

    @property
    def status(self) -> dict:
        return self.controller.client.status or {}

    # ---- pages -------------------------------------------------------------------------

    def _welcome(self) -> QWidget:
        page, layout = self._page(
            "Welcome to Face Unlock",
            "Sign in to Windows just by sitting down at your PC, and have it lock itself when "
            "you walk away. Setting up takes about three minutes.")
        card = Card(padding=(18, 14), spacing=8)
        card.body.addWidget(label("What stays on this PC", "CardTitle"))
        for glyph, text in (
                (G.FACE, "A numeric signature of your face – not photos, unless you choose."),
                (G.KEY, "Your Windows password, encrypted so only your account can read it."),
                (G.LOCK, "Nothing is uploaded. Your PIN and password always keep working.")):
            row = hbox(GlyphLabel(glyph, 16, tone="accent"), spacing=10)
            row.addWidget(label(text, "Body", wrap=True), 1)
            card.body.addLayout(row)
        layout.addWidget(card)
        self.consent = QCheckBox("I understand, and I want to set up Face Unlock on this PC")
        self.consent.toggled.connect(lambda _on: self._update_buttons())
        layout.addWidget(self.consent)
        layout.addStretch(1)
        return page

    def _ready(self) -> QWidget:
        page, layout = self._page(
            "Getting your PC ready",
            "Face Unlock sets up what it can by itself. Windows may ask for your permission "
            "once, to add face sign-in to the lock screen.")
        card = Card(padding=(0, 4), spacing=0)
        self.row_service = CheckRow(G.SETTINGS, "Background service")
        self.row_model = CheckRow(G.FACE, "Face recognition model")
        self.row_camera = CheckRow(G.CAMERA, "Camera")
        self.row_tile = CheckRow(G.LOCK, "Face sign-in on the lock screen")
        self.row_start = CheckRow(G.HOME, "Start Face Unlock when you sign in")
        for row in (self.row_service, self.row_model, self.row_camera, self.row_tile,
                    self.row_start):
            card.body.addWidget(row)
        layout.addWidget(card)
        peek = Card(padding=(16, 14), spacing=0)
        self.peek = CameraPeek(self.controller)
        peek.body.addWidget(self.peek)
        layout.addWidget(peek)
        layout.addStretch(1)
        return page

    def _password(self) -> QWidget:
        page, layout = self._page(
            "Your Windows password",
            "When the camera recognises you, Face Unlock signs you in with your Windows "
            "password, as if you had typed it. It's checked with Windows first, then stored "
            "encrypted so only your account on this PC can read it.")
        user, domain = windows_account()
        email = microsoft_account()
        layout.addWidget(label(f"Microsoft account: {email}" if email
                               else f"Account: {domain}\\{user}", "Secondary"))
        layout.addWidget(label(password_hint(email), "Body2", wrap=True))
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText("Microsoft account password" if email
                                       else "Windows password")
        self.secret.setAccessibleName("Windows password")
        self.secret.returnPressed.connect(self._save_password)
        self.secret.setMinimumWidth(320)
        self.save_button = accent_button("Check and save", self._save_password)
        self.save_button.setEnabled(False)   # until there is something to check
        self.save_button.setToolTip("Type your password first")
        self.secret.textChanged.connect(self._password_typed)
        layout.addLayout(hbox(self.secret, self.save_button, spacing=8, stretch_at=2))
        layout.addWidget(CapsLockWarning(self.secret))
        self.show_secret = QCheckBox("Show password")
        self.show_secret.toggled.connect(lambda shown: self.secret.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password))
        forgot = subtle_button("Forgot it?", lambda: open_password_help(email))
        layout.addLayout(hbox(self.show_secret, forgot, spacing=16, stretch_at=2))
        layout.addWidget(label(PASSWORD_REASON + " If you change your password later, Face "
                               "Unlock will ask for the new one.", "Caption", wrap=True))
        self.password_state = StatusLine()
        layout.addWidget(self.password_state)
        layout.addStretch(1)
        return page

    def _face(self) -> QWidget:
        page, layout = self._page(
            "Your face",
            "The camera takes about 20 photos while you look at it and turn your head a "
            "little. It takes about a minute.")
        tips = Card(padding=(18, 14), spacing=8)
        for glyph, text in ((G.SUN, "Sit where you normally do, facing the screen."),
                            (G.FACE, "Wear what you usually wear, such as glasses."),
                            (G.PERSON, "If an angle won't register, use “Take photo now” "
                                       "or skip it.")):
            tips.body.addLayout(hbox(GlyphLabel(glyph, 16, tone="accent"),
                                     label(text, "Body"), spacing=10, stretch_at=2))
        layout.addWidget(tips)
        self.face_state = StatusLine()
        self.face_button = accent_button("Start the camera", self._enroll)
        layout.addWidget(self.face_state)
        layout.addLayout(hbox(self.face_button, stretch_at=1))
        layout.addStretch(1)
        return page

    def _preferences(self) -> QWidget:
        page, layout = self._page("A few preferences",
                                  "You can change any of these later in Settings.")
        card = Card(padding=(0, 4), spacing=0)
        self.pref_lock = ToggleSwitch()
        self.pref_lock.setChecked(True)
        self.pref_after = QComboBox()
        for seconds, text in LOCK_AFTER:
            self.pref_after.addItem(text, seconds)
        self.pref_after.setCurrentIndex(1)
        self.pref_lock.toggled.connect(self.pref_after.setEnabled)
        lock_row = SettingRow(G.LOCK, "Lock my PC when I walk away",
                              "Checks for you with the camera now and then. Your apps keep "
                              "running.", self.pref_after)
        lock_row.set_control(self.pref_lock)
        card.body.addWidget(lock_row)
        self.pref_learn = ToggleSwitch()
        self.pref_learn.setChecked(True)
        card.body.addWidget(SettingRow(G.BULB, "Keep learning how I look",
                                       "Keeps up with a new haircut, glasses or lighting. "
                                       "Everything learned can be undone.", self.pref_learn))
        self.pref_notify = ToggleSwitch()
        self.pref_notify.setChecked(True)
        card.body.addWidget(SettingRow(G.INFO, "Show notifications",
                                       "For example when your camera is disconnected.",
                                       self.pref_notify))
        layout.addWidget(card)
        layout.addWidget(label("When should the sign-in screen look for you?", "CardTitle"))
        self.pref_scan = QButtonGroup(self)
        for index, (key, (title, text, _)) in enumerate(SCANNING.items()):
            choice = QRadioButton(title)
            choice.setProperty("key", key)
            choice.setChecked(index == 0)
            self.pref_scan.addButton(choice)
            layout.addWidget(choice)
            layout.addWidget(label(text, "Caption", wrap=True))
        layout.addStretch(1)
        return page

    def _try(self) -> QWidget:
        page, layout = self._page(
            "Try it",
            "Look at the camera and test recognition. Then lock your PC (Windows+L), step "
            "away for a moment and come back – you'll be signed in as you sit down.")
        self.test_button = accent_button("Test recognition", self._test)
        self.test_button.setToolTip("Looks for you with the camera for a few seconds")
        self.test_badge = SuccessBadge(32)
        self.test_badge.hide()
        self.test_state = StatusLine()
        test_row = hbox(self.test_button, self.test_badge, spacing=12)
        test_row.addWidget(self.test_state, 1)
        layout.addLayout(test_row)
        lock = button("Lock my PC now", self._lock)
        lock.setToolTip("Locks Windows straight away, like Windows+L, so you can try signing in")
        layout.addLayout(hbox(lock, stretch_at=1))
        tips = Card(padding=(18, 14), spacing=8)
        for glyph, text in (
                (G.FACE, "No need to click anything on the lock screen: just look at the "
                         "camera."),
                (G.KEY, "Your PIN and password still work, under “Sign-in options”."),
                (G.POWER, "After a restart, sign in once with your PIN or password; face "
                          "sign-in takes over from then.")):
            row = hbox(GlyphLabel(glyph, 16, tone="accent"), spacing=10)
            row.addWidget(label(text, "Body", wrap=True), 1)
            tips.body.addLayout(row)
        layout.addWidget(tips)
        self.summary = label("", "Secondary", wrap=True)
        layout.addWidget(self.summary)
        layout.addStretch(1)
        return page

    # ---- navigation ---------------------------------------------------------------------

    def _go(self, index: int) -> None:
        if self.stack.currentIndex() == 1 and index != 1:
            self.peek.stop()   # the camera check is only for the Get ready step
        changed = index != self.stack.currentIndex()
        self.stack.setCurrentIndex(index)
        if changed:
            fade_in(self.stack.currentWidget())
        self.steps.set_step(index)
        self.banner.hide()
        self.back.setVisible(index > 0)
        self.later.setVisible(index < len(STEPS) - 1)
        self.next.setText("Finish" if index == len(STEPS) - 1 else
                          "Get started" if index == 0 else "Continue")
        self.timer.stop()
        if index == 1:
            self.timer.start(CHECK_EVERY_MS)
        if index in (1, 5):
            self._check_local()
        self._refresh()

    def _tick(self) -> None:
        self.controller.client.refresh_status()
        self._ticks += 1
        if self._ticks % LOCAL_CHECK_EVERY == 0:
            self._check_local()

    def _check_local(self) -> None:
        """Registry and task checks take a moment: run them off the UI thread."""
        checks = self.checks

        def work() -> dict:
            return {"tile": checks.tile_state(), "hello": checks.hello_only(),
                    "autostart": checks.autostart_state()}

        def done(result) -> None:
            if isinstance(result, dict):
                self._local = result
            self._refresh()

        self._in_background("local", work, done)

    def _back(self) -> None:
        self._go(max(0, self.stack.currentIndex() - 1))

    def _next(self) -> None:
        index = self.stack.currentIndex()
        if index == 4:
            self._save_preferences()
        if index == len(STEPS) - 1:
            system_setup.mark_onboarding_done()
            self.accept()
            self.controller.show_main()
            return
        self._go(index + 1)

    def _update_buttons(self) -> None:
        reason = self.waiting_for()
        self.next.setEnabled(not reason)
        self.next.setToolTip(reason)
        self.waiting.setText(reason)

    def waiting_for(self) -> str:
        """Why Continue isn't available yet ("" when it is)."""
        index = self.stack.currentIndex()
        status = self.status
        if index == 0 and not self.consent.isChecked():
            return "Tick the box above to continue"
        if index == 1:
            missing = [name for row, name in ((self.row_service, "the background service"),
                                              (self.row_model, "the face model"),
                                              (self.row_camera, "the camera")) if not row.ok]
            if missing:
                return "Waiting for " + (", ".join(missing[:-1]) + " and " + missing[-1]
                                         if len(missing) > 1 else missing[0])
        if index == 2 and not status.get("credentials"):
            return "Save your password to continue"
        if index == 3 and not (status.get("profile") or {}).get("anchors"):
            return "Set up your face to continue"
        return ""

    def reject(self) -> None:
        self.timer.stop()
        self.peek.stop()
        super().reject()

    def pause_for_lock(self) -> None:
        """Windows is locking: the camera check gives the camera back at once."""
        self.peek.pause_for_lock()

    # ---- live state -------------------------------------------------------------------------

    def _refresh(self) -> None:
        index = self.stack.currentIndex()
        if index == 1:
            self._refresh_ready()
        elif index == 2:
            if self.status.get("credentials") and "password" not in self._busy:
                self.password_state.done("success", "Your password is saved.")
        elif index == 3:
            anchors = (self.status.get("profile") or {}).get("anchors", 0)
            if anchors:
                self.face_state.done("success",
                                     f"Your face is set up ({anchors} reference photos).")
            else:
                self.face_state.clear()
            self.face_button.setText("Take them again" if anchors else "Start the camera")
        elif index == 5:
            self._refresh_summary()
        self._update_buttons()

    def _refresh_ready(self) -> None:
        status = self.status
        if not status:
            self.row_service.show_state("action", "Not running yet.", "Start it",
                                        self._start_service)
            for row in (self.row_model, self.row_camera):
                row.show_state("working", "Waiting for the background service…")
        else:
            self.row_service.show_state("ok", "Running.")
            recognition = status.get("recognition") or {}
            download = recognition.get("download")
            if recognition.get("model_loaded"):
                self.row_model.show_state("ok", "Ready.")
            elif download:
                done, total = int(download.get("done", 0)), int(download.get("total", 0))
                self.row_model.show_progress(done, total,
                                             download_text(done, total, self._rate(done)))
            elif recognition.get("model_error"):
                self.row_model.show_state(
                    "problem", "The face model couldn't be prepared "
                    f"({recognition['model_error']}). Check the internet connection, then try "
                    "again.", "Try again", self._prepare_model)
            else:
                self.row_model.show_state(
                    "working", "Preparing…" if recognition.get("model_present") else
                    "Downloading the face model (about 130 MB). This needs the internet and "
                    "can take a few minutes the first time.")
            self._refresh_camera(status)
        local = self._local
        if local is None:
            for row in (self.row_tile, self.row_start):
                row.show_state("working", "Checking…")
            return
        tile = local["tile"]
        if local["hello"]:
            self.row_tile.show_state(
                "problem", "Windows is set to allow only Windows Hello sign-in, which hides "
                "Face Unlock. Turn off “For improved security, only allow Windows Hello "
                "sign-in” in Sign-in options.", "Show me how", self._open_hello)
        elif tile.installed:
            self.row_tile.show_state("ok", "Installed.")
        elif "tile" in self._busy:
            self.row_tile.show_state("working", "Waiting for Windows…")
        else:
            self.row_tile.show_state(
                "action", "Needs your permission once. Without it, auto-lock still works but "
                "you can't sign in with your face.", "Install", self._install_tile)
        if local["autostart"] != "off":
            self.row_start.show_state("ok", "On.")
        else:
            self.row_start.show_state("action", "Off: you'd have to open Face Unlock yourself "
                                      "after every restart.", "Turn on", self._enable_autostart)

    def _refresh_camera(self, status: dict) -> None:
        device = (status.get("camera") or {}).get("device") or {}
        state = device.get("state")
        if state == "ready":
            self.row_camera.show_state("ok", f"Using {device.get('name') or 'your camera'}.")
        elif state == "unpinned" and not self._camera_listed:
            self.row_camera.show_state("working", "Looking for your webcam…")
            self._pick_camera()
        else:
            self.row_camera.show_state(
                "action", device.get("message") or "Choose the webcam that should recognise "
                "you.", "Choose camera", self._choose_camera)

    # ---- actions --------------------------------------------------------------------------------

    def _start_service(self) -> None:
        def work() -> tuple[bool, str]:
            self.checks.start_service()
            return True, ""

        self._in_background("service", work, lambda _r: QTimer.singleShot(
            3000, self.controller.client.refresh_status))
        self.row_service.show_state("working", "Starting…")

    def _pick_camera(self) -> None:
        """With exactly one real camera, use it; otherwise let you choose."""
        self._camera_listed = True

        def listed(reply) -> None:
            cameras = [c for c in (reply or {}).get("cameras", []) if not c.get("virtual")]
            if len(cameras) == 1:
                ok, problem = self.controller.update_config(camera_device_id=cameras[0]["path"])
                if not ok:
                    self.banner.show_message("caution", "Camera not chosen", problem)
            self._refresh()

        self.controller.client.request({"cmd": "camera_list"}, listed, timeout_s=15.0)

    def _choose_camera(self) -> None:
        from .dialogs import CameraDialog
        CameraDialog(self.controller, self).exec()
        self._refresh()

    def _install_tile(self) -> None:
        self.row_tile.action.set_busy(True, "Waiting for Windows…")

        def done(result) -> None:
            self.row_tile.action.set_busy(False)
            ok, message = result
            if not ok:
                self.banner.show_message("caution", "Face sign-in wasn't added", message)
            self._check_local()
        self._in_background("tile", self.checks.install_tile, done)
        self._refresh()

    def _enable_autostart(self) -> None:
        ok, message = self.checks.enable_autostart()
        if not ok:
            self.banner.show_message("caution", "Couldn't turn this on", message)
        self._check_local()

    def _open_hello(self) -> None:
        from .guides import HelloOnlyGuide
        HelloOnlyGuide(self).exec()
        self._check_local()  # see the change straight away

    def _rate(self, done: int) -> float | None:
        """Download speed so far (bytes a second), from the first progress seen."""
        now = time.monotonic()
        if self._download_start is None or done < self._download_start[1]:
            self._download_start = (now, done)
            return None
        started, first = self._download_start
        return (done - first) / (now - started) if now - started >= 4 else None

    def _prepare_model(self) -> None:
        self.controller.client.request({"cmd": "prepare_model"},
                                       lambda _r: self.controller.client.refresh_status())

    def _password_typed(self, text: str) -> None:
        self.save_button.setEnabled(bool(text) and "password" not in self._busy)
        self.save_button.setToolTip("" if text else "Type your password first")

    def _save_password(self) -> None:
        """Checking with Windows can take seconds on work PCs: off the UI thread."""
        user, domain = windows_account()
        secret = self.secret.text()
        if not secret or "password" in self._busy:
            return
        self.secret.clear()
        self.save_button.set_busy(True, "Checking…")
        self.password_state.working("Checking your password with Windows…")

        def done(result) -> None:
            self.save_button.set_busy(False)
            self._password_typed(self.secret.text())
            ok, problem = result
            if not ok:
                self.password_state.done("critical", "Not saved. Type it again below.")
                self.banner.show_message("critical", "Not saved", problem)
                self.secret.setFocus()
                return
            self.banner.hide()
            self.password_state.done("success", "Windows accepted it. Your password is saved.")
            self.controller.client.refresh_status()

        self._in_background("password", lambda: store_windows_password(user, domain, secret),
                            done)

    def _enroll(self) -> None:
        from .enroll import EnrollWizard
        anchors = (self.status.get("profile") or {}).get("anchors") if self.status else None
        # "Add" is safe without a profile too; only a known-empty profile starts afresh.
        wizard = EnrollWizard(self.controller, "replace" if anchors == 0 else "append", self)
        self.controller.capture_window_opened(wizard)  # hands the camera back on Windows+L
        wizard.exec()
        self.controller.client.refresh_status()

    def _save_preferences(self) -> None:
        choice = self.pref_scan.checkedButton()
        ok, problem = self.controller.update_config(**preference_changes(
            auto_lock=self.pref_lock.isChecked(), lock_after_s=self.pref_after.currentData(),
            scanning=choice.property("key") if choice else "instant",
            learning=self.pref_learn.isChecked(), notifications=self.pref_notify.isChecked()))
        if not ok:
            self.banner.show_message("caution", "Some preferences weren't saved", problem)

    def _test(self) -> None:
        self.test_button.set_busy(True, "Looking…")
        self.test_badge.hide()
        self.test_state.working("Look at the camera…")

        def done(reply) -> None:
            self.test_button.set_busy(False)
            if not reply or not reply.get("ok"):
                self.test_state.done("critical", "The test couldn't run: " + str(
                    (reply or {}).get("message") or "Face Unlock isn't responding."))
            elif reply.get("accepted"):
                self.test_state.done("success", "Recognised you. You're all set!")
                self.test_state.icon.hide()
                self.test_badge.show()
                self.test_badge.play()
            else:
                from .status import guidance_text
                self.test_state.done("caution", "Not recognised this time. " + (
                    guidance_text(reply.get("guidance")) or "Look straight at the camera and "
                    "try again."))

        self.controller.client.request({"cmd": "check", "record": True}, done, timeout_s=30.0)

    @staticmethod
    def _lock() -> None:
        ctypes.windll.user32.LockWorkStation()

    def _refresh_summary(self) -> None:
        cfg = self.controller.cfg
        tile = (self._local or {}).get("tile")
        lines = [
            "Face sign-in: " + ("on the lock screen" if tile and tile.installed else
                                "not on the lock screen yet (see the Overview)"),
            "Auto-lock: " + (f"after about {cfg.effective_presence_absence_timeout_s // 60} "
                             "minutes away"
                             if cfg.presence_enabled else "off"),
            "Learning your appearance: " + ("on" if cfg.adaptive_enrollment else "off"),
        ]
        self.summary.setText("  ·  ".join(lines))
