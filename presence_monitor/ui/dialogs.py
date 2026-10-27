"""Dialogs: confirmation, stored password, camera choice, and photo review."""
from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from face_service.camera import list_cameras
from face_service.config import ENROLL_DIR

from .icons import G, GlyphLabel
from .widgets import Banner, ProgressRing, accent_button, button, hbox, label

if TYPE_CHECKING:
    from ..app import AppController

log = logging.getLogger(__name__)


class Dialog(QDialog):
    """Fluent-style content dialog: title, body, and right-aligned buttons."""

    def __init__(self, parent: QWidget | None, title: str, width: int = 460):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(width)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(12)
        heading = label(title, "HeroTitle", wrap=True)
        outer.addWidget(heading)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)
        outer.addSpacing(6)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        self.buttons.addStretch(1)
        outer.addLayout(self.buttons)

    def add_buttons(self, accept_text: str, *, cancel_text: str = "Cancel",
                    destructive: bool = False) -> None:
        self.accept_button = accent_button(accept_text, self.accept)
        self.cancel_button = button(cancel_text, self.reject)
        self.buttons.addWidget(self.accept_button)
        self.buttons.addWidget(self.cancel_button)
        self.accept_button.setDefault(not destructive)
        (self.cancel_button if destructive else self.accept_button).setFocus()


def confirm(parent: QWidget | None, title: str, message: str, accept_text: str, *,
            destructive: bool = True) -> bool:
    dialog = Dialog(parent, title)
    dialog.body.addWidget(label(message, "Body2", wrap=True))
    dialog.add_buttons(accept_text, destructive=destructive)
    return dialog.exec() == QDialog.DialogCode.Accepted


def windows_account() -> tuple[str, str]:
    return os.environ.get("USERNAME", ""), os.environ.get("USERDOMAIN", ".")


def verify_windows_password(user: str, domain: str, secret: str) -> tuple[bool, str]:
    """Check the password with Windows before it is stored.

    A wrong stored password would be replayed at every face sign-in, and
    Windows counts each one as a failed sign-in (work PCs may lock the
    account), so only a password Windows has accepted is ever stored.
    """
    import pywintypes
    import win32con
    import win32security
    try:
        token = win32security.LogonUser(user, domain, secret, win32con.LOGON32_LOGON_INTERACTIVE,
                                        win32con.LOGON32_PROVIDER_DEFAULT)
        token.Close()
        return True, ""
    except pywintypes.error as exc:
        if exc.winerror == 1326:  # ERROR_LOGON_FAILURE
            return False, ("Windows didn't accept that password. If you sign in with a "
                           "Microsoft account, use its password, not your PIN.")
        if exc.winerror == 1327:  # ERROR_ACCOUNT_RESTRICTION, e.g. no blank passwords
            return False, "Windows doesn't allow this account to sign in with a password."
        return False, f"Windows couldn't check the password ({exc.strerror})."


def store_windows_password(user: str, domain: str, secret: str) -> tuple[bool, str]:
    """Verify, then store the password encrypted for this Windows account."""
    if not secret:
        return False, "Type your Windows password."
    ok, problem = verify_windows_password(user, domain, secret)
    if not ok:
        return False, problem
    from face_service.credentials import save_password
    try:
        save_password(user, secret, domain)
    except Exception as exc:  # DPAPI or file errors
        log.exception("password not stored")
        return False, f"It couldn't be saved ({exc})."
    log.info("Windows password stored for face sign-in")
    return True, ""


class _Result(QObject):
    ready = Signal(object)


class PasswordDialog(Dialog):
    """Store the Windows password used by face sign-in (DPAPI-encrypted)."""

    def __init__(self, controller: AppController, parent: QWidget | None = None):
        super().__init__(parent, "Store your Windows password", 500)
        self.controller = controller
        self.user, self.domain = windows_account()
        self.body.addWidget(label(
            "Face sign-in hands this password to Windows when it recognises you. It's encrypted "
            "with Windows (DPAPI) so only your account on this PC can read it. If you change your "
            "Windows password, update it here too.", "Body2", wrap=True))
        self.body.addWidget(label(f"Account: {self.domain}\\{self.user}", "Secondary"))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Windows password")
        self.password.setAccessibleName("Windows password")
        self.repeat = QLineEdit()
        self.repeat.setEchoMode(QLineEdit.EchoMode.Password)
        self.repeat.setPlaceholderText("Type it again")
        self.repeat.setAccessibleName("Confirm Windows password")
        self.show_box = QCheckBox("Show password")
        self.show_box.toggled.connect(self._show)
        self.body.addWidget(self.password)
        self.body.addWidget(self.repeat)
        self.body.addWidget(self.show_box)
        self.body.addWidget(label("This is your Windows account password, not your PIN.",
                                  "Caption"))
        self.banner = Banner()
        self.body.addWidget(self.banner)
        self.add_buttons("Save", destructive=False)
        self.accept_button.clicked.disconnect()
        self.accept_button.clicked.connect(self._save)
        self.password.setFocus()

    def _show(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.password.setEchoMode(mode)
        self.repeat.setEchoMode(mode)

    def _save(self) -> None:
        secret, again = self.password.text(), self.repeat.text()
        if secret != again:
            self.banner.show_message("caution", "", "The two passwords don't match.")
            return
        # Windows checks the password first, which can take a while on a work PC
        # whose domain controller is out of reach: keep the window responsive.
        self.accept_button.setEnabled(False)
        self.banner.show_message("info", "", "Checking the password with Windows…")
        result = _Result(self)
        result.ready.connect(self._saved)
        user, domain = self.user, self.domain

        def run() -> None:
            try:
                outcome = store_windows_password(user, domain, secret)
            except Exception as exc:  # noqa: BLE001
                outcome = (False, str(exc))
            try:
                result.ready.emit(outcome)
            except RuntimeError:
                pass  # the window was closed meanwhile

        threading.Thread(target=run, name="store-password", daemon=True).start()
        self.password.clear()
        self.repeat.clear()

    def _saved(self, outcome: tuple[bool, str]) -> None:
        ok, problem = outcome
        self.accept_button.setEnabled(True)
        if not ok:
            self.banner.show_message("critical", "", problem)
            return
        self.controller.client.request({"cmd": "reload_config"},
                                       lambda _r: self.controller.client.refresh_status())
        self.accept()


class CameraDialog(Dialog):
    """Choose the one camera allowed to recognise you."""

    def __init__(self, controller: AppController, parent: QWidget | None = None):
        super().__init__(parent, "Choose your camera", 520)
        self.controller = controller
        self.body.addWidget(label(
            "Face Unlock uses only the camera you choose here and never switches to another one "
            "on its own. Virtual cameras can show any video, so they can't be used to sign in.",
            "Body2", wrap=True))
        self.group = QButtonGroup(self)
        self.options: dict[int, str] = {}
        current = controller.cfg.camera_device_id.casefold().split("#{", 1)[0]
        try:
            cameras = list_cameras()
        except Exception as exc:
            cameras = []
            self.body.addWidget(label(f"Cameras couldn't be listed: {exc}", "Body2", wrap=True))
        for index, camera in enumerate(cameras):
            radio = QRadioButton(camera.name)
            radio.setEnabled(not camera.virtual)
            radio.setChecked(camera.identity == current)
            hint = "Virtual camera" if camera.virtual else (
                "In use now" if camera.identity == current else "")
            self.group.addButton(radio, index)
            self.options[index] = camera.path
            self.body.addLayout(hbox(radio, label(hint, "Caption"), spacing=10, stretch_at=1))
        if not cameras:
            self.body.addWidget(label("No cameras were found.", "Body2"))
        self.add_buttons("Use this camera", destructive=False)
        self.accept_button.clicked.disconnect()
        self.accept_button.clicked.connect(self._save)

    def _save(self) -> None:
        index = self.group.checkedId()
        if index < 0:
            return
        ok, problem = self.controller.update_config(camera_device_id=self.options[index])
        if ok:
            self.accept()
        else:
            self.body.addWidget(label(problem, "Body2", wrap=True))


def confirm_moved_camera(controller: AppController, parent: QWidget | None) -> None:
    status = controller.client.status or {}
    device = (status.get("camera") or {}).get("device") or {}
    path, name = device.get("path"), device.get("name", "your camera")
    if not path:
        CameraDialog(controller, parent).exec()
        return
    if confirm(parent, "Use this camera?",
               f"{name} is connected to a different USB port than before. If this is the same "
               "camera you've been using, keep using it. Otherwise choose your camera in "
               "Settings.", "Use this camera", destructive=False):
        controller.update_config(camera_device_id=path)


class ProfileReviewDialog(Dialog):
    """Show reference photos that look unlike the rest, so you can remove them."""

    def __init__(self, controller: AppController, parent: QWidget | None = None):
        super().__init__(parent, "Review reference photos", 640)
        self.controller = controller
        self.status = label("Checking your reference photos… this can take a minute.", "Body2",
                            wrap=True)
        self.ring = ProgressRing(22)
        self.body.addLayout(hbox(self.ring, self.status, spacing=10))
        self.grid = QGridLayout()
        self.grid.setSpacing(10)
        holder = QWidget()
        holder.setObjectName("Transparent")
        holder.setLayout(self.grid)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        scroll.setMinimumHeight(260)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.body.addWidget(scroll)
        self.add_buttons("Remove selected", cancel_text="Close")
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.disconnect()
        self.accept_button.clicked.connect(self._remove)
        self.boxes: list[tuple[QCheckBox, int]] = []
        controller.client.request({"cmd": "map_anchor_files"}, self._mapped, timeout_s=180.0)

    def _mapped(self, _reply) -> None:
        self.controller.client.request({"cmd": "profile_health"}, self._show, timeout_s=30.0)

    def _show(self, reply) -> None:
        self.ring.hide()
        if not reply or not reply.get("ok"):
            self.status.setText("The photos couldn't be checked because Face Unlock isn't "
                                "responding.")
            return
        outliers = reply.get("outliers", [])
        if not outliers:
            self.status.setText(f"All {reply.get('anchors', 0)} reference photos look "
                                "consistent with each other.")
            return
        self.status.setText(
            f"{len(outliers)} of {reply['anchors']} photos look quite different from the rest – "
            "for example a sideways view, blur, or someone else. Removing poor photos can make "
            "recognition stricter where it matters. Removed photos are moved to a backup folder.")
        for position, item in enumerate(outliers):
            card = QVBoxLayout()
            thumb = label("")
            path = ENROLL_DIR / item["file"] if item.get("file") else None
            if path and path.exists():
                thumb.setPixmap(QPixmap(str(path)).scaled(
                    QSize(160, 120), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            else:
                thumb.setText("Photo not found")
            box = QCheckBox(f"Unusual ({item['typical_distance']:.2f})")
            box.toggled.connect(self._selection_changed)
            card.addWidget(thumb)
            card.addWidget(box)
            self.grid.addLayout(card, position // 3, position % 3)
            self.boxes.append((box, item["index"]))

    def _selection_changed(self) -> None:
        self.accept_button.setEnabled(any(box.isChecked() for box, _ in self.boxes))

    def _remove(self) -> None:
        indices = [index for box, index in self.boxes if box.isChecked()]
        if not indices or not confirm(self, "Remove these photos?",
                                      f"{len(indices)} photo(s) will no longer be used to "
                                      "recognise you. They're moved to a backup folder.",
                                      "Remove"):
            return

        def done(reply) -> None:
            self.status.setText(f"Removed {reply.get('removed', 0)} photo(s)." if reply and
                                reply.get("ok") else "Nothing was removed.")
            self.accept_button.setEnabled(False)
            for box, _ in self.boxes:
                box.setEnabled(False)

        self.controller.client.request({"cmd": "remove_anchors", "indices": indices}, done)


def confirm_quit(controller: AppController, parent: QWidget | None) -> None:
    if confirm(parent, "Quit Face Unlock?",
               "Face sign-in and auto-lock stop until you sign in to Windows again (or start "
               "Face Unlock from the Start menu). Your PIN and password keep working.", "Quit"):
        controller.quit()


__all__ = ["confirm", "PasswordDialog", "CameraDialog", "ProfileReviewDialog",
           "confirm_moved_camera", "confirm_quit", "GlyphLabel", "G"]
