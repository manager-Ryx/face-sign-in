"""Guided face setup: get ready, capture, review and save, done.

The wizard borrows the camera from the background service through an
explicit, renewable lease and never opens the camera if the hand-over is
refused. It releases the camera as soon as capture ends, when the window
closes, or when Windows locks. Nothing replaces your current profile until
you select "Save face profile".
"""
from __future__ import annotations

import logging
import secrets
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
from PySide6.QtCore import QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QProgressBar,
    QRadioButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from face_service.camera import Camera, CameraError
from face_service.config import APP_DIR
from face_service.detector import FaceDetector
from face_service.exposure import expose_for_face
from face_service.quality import to_gray

from . import theme
from .capture import CapturePlan, Observation, targets_for
from .icons import G, GlyphLabel
from .widgets import (
    Banner,
    Card,
    ProgressRing,
    accent_button,
    button,
    hbox,
    label,
    subtle_button,
)

if TYPE_CHECKING:
    from ..app import AppController

log = logging.getLogger(__name__)

OWNER = "enrollment"
LEASE_S = 120
RENEW_MS = 30_000
PREVIEW_FPS = 20
EXPOSE_EVERY_S = 1.0     # how often the face's brightness is re-checked while capturing


class CaptureWorker(QThread):
    preview = Signal(QImage, object)     # mirrored image, Observation
    captured = Signal(int, int)          # taken, total
    failed = Signal(str)
    notice = Signal(str)

    def __init__(self, controller: AppController, staging: Path, plan: CapturePlan):
        super().__init__()
        self.cfg = controller.cfg
        self.staging = staging
        self.plan = plan
        # Requests from the window, handled on this thread (the plan is only touched here).
        self.take_requested = False
        self.skip_requested = False

    def _save(self, frame, pose: str, suffix: str = "") -> bool:
        index = self.plan.taken + 1
        path = self.staging / f"face-{index:02d}-{pose}{suffix}.jpg"
        return cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])

    def run(self) -> None:
        camera = Camera.from_config(self.cfg, allow_slow_backend=True, open_budget_s=20.0)
        try:
            report = camera.open()
            if self.cfg.camera_focus:
                camera.fix_focus(self.cfg.camera_focus)
            camera.settle()
        except CameraError as exc:
            log.warning("setup could not start the camera: %s", exc)
            self.failed.emit(str(exc))
            camera.close()
            return
        log.info("setup camera started in %.0f ms", report.open_ms)
        detector = FaceDetector()
        last_emit = last_exposed = 0.0
        try:
            while not self.isInterruptionRequested() and not self.plan.done:
                frame = camera.read_frame()
                if self.cfg.camera_face_exposure and time.monotonic() - last_exposed >= EXPOSE_EVERY_S:
                    last_exposed = time.monotonic()
                    frame = expose_for_face(camera, frame, detector).frame
                faces = detector.detect(frame)
                observation = self.plan.observe(to_gray(frame), faces)
                if self.skip_requested:
                    self.skip_requested = False
                    if self.plan.skip():
                        self.captured.emit(self.plan.taken, self.plan.total)
                if self.take_requested:
                    self.take_requested = False
                    box = faces[0] if len(faces) == 1 else None
                    pose = self.plan.next_pose()
                    if box is None or pose is None:
                        self.notice.emit("Make sure your face, and only yours, is in view.")
                    elif self._save(frame, pose, "-manual"):
                        self.plan.take_anyway(box)
                        self.captured.emit(self.plan.taken, self.plan.total)
                elif observation.capture and observation.pose and observation.box:
                    if self._save(frame, observation.pose):
                        self.plan.record(observation.pose, observation.box)
                        self.captured.emit(self.plan.taken, self.plan.total)
                now = time.monotonic()
                if now - last_emit >= 1.0 / PREVIEW_FPS:
                    last_emit = now
                    mirrored = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
                    height, width = mirrored.shape[:2]
                    image = QImage(mirrored.data, width, height, 3 * width,
                                   QImage.Format.Format_RGB888).copy()
                    self.preview.emit(image, observation)
        except CameraError as exc:
            self.failed.emit(str(exc))
        finally:
            camera.close()


class Preview(QWidget):
    """Live, mirrored camera view with an oval guide and a guidance pill."""

    def __init__(self) -> None:
        super().__init__()
        self.image: QImage | None = None
        self.observation: Observation | None = None
        self.message = "Starting the camera…"
        self.setMinimumSize(560, 420)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Camera preview")

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(640, 480)

    def show_frame(self, image: QImage, observation: Observation) -> None:
        self.image = image
        self.observation = observation
        self.message = observation.guidance
        self.update()

    def show_message(self, message: str) -> None:
        self.image = None
        self.observation = None
        self.message = message
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        area = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(area, 12, 12)
        painter.setClipPath(clip)
        painter.fillRect(area, QColor(t.preview_bg))
        if self.image is not None:
            scale = max(area.width() / self.image.width(), area.height() / self.image.height())
            w, h = self.image.width() * scale, self.image.height() * scale
            target = QRectF((area.width() - w) / 2, (area.height() - h) / 2, w, h)
            painter.drawImage(target, self.image)
        oval = QRectF(area.center().x() - area.width() * 0.2, area.height() * 0.14,
                      area.width() * 0.4, area.height() * 0.68)
        tone = self.observation.tone if self.observation else "idle"
        color = {"good": QColor(t.accent), "issue": QColor("#F7B500")}.get(tone, QColor("white"))
        painter.setPen(QPen(color, 3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(oval)
        if self.message:
            painter.setFont(theme.font(15, strong=True))
            metrics = painter.fontMetrics()
            width = min(area.width() - 32, metrics.horizontalAdvance(self.message) + 36)
            pill = QRectF((area.width() - width) / 2, area.height() - 58, width, 38)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawRoundedRect(pill, 19, 19)
            painter.setPen(QColor("white"))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, self.message)
        painter.end()


class StepHeader(QWidget):
    STEPS = ("Get ready", "Capture", "Review and save", "Done")

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Transparent")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        self.labels = []
        for number, text in enumerate(self.STEPS, 1):
            item = label(f"{number}  {text}", "Secondary")
            self.labels.append(item)
            layout.addWidget(item)
        layout.addStretch(1)

    def set_step(self, index: int) -> None:
        t = theme.current()
        for position, item in enumerate(self.labels):
            if position == index:
                item.setStyleSheet(f"color: {t.accent_text}; font-weight: 600;")
            elif position < index:
                item.setStyleSheet(f"color: {t.text};")
            else:
                item.setStyleSheet(f"color: {t.text3};")


class EnrollWizard(QDialog):
    def __init__(self, controller: AppController, mode: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        status = controller.client.status
        anchors = (status.get("profile") or {}).get("anchors") if status else None
        # Only a service that says there is no profile turns "add" into "replace":
        # an unknown status (not answered yet) must never discard what was learned.
        self.has_profile = anchors is None or bool(anchors)
        self.mode = mode if self.has_profile else "replace"
        self.staging: Path | None = None
        self.worker: CaptureWorker | None = None
        # Stopped workers whose thread hasn't ended yet. Qt deletes a running
        # QThread whose last Python reference goes away, which ends the process.
        self._retired: list[CaptureWorker] = []
        self._requesting = False
        self.plan: CapturePlan | None = None
        self._leased = False
        self.setWindowTitle("Set up face recognition")
        self.setMinimumSize(760, 720)
        self.renew_timer = QTimer(self)
        self.renew_timer.timeout.connect(self._renew)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(14)
        self.steps = StepHeader()
        outer.addWidget(self.steps)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.banner = Banner()
        outer.addWidget(self.banner)
        self.buttons = QHBoxLayout()
        outer.addLayout(self.buttons)
        self.primary = accent_button("", self._primary)
        self.secondary = button("", self._secondary)
        self.cancel = button("Cancel", self.reject)
        self.buttons.addStretch(1)
        self.buttons.addWidget(self.secondary)
        self.buttons.addWidget(self.primary)
        self.buttons.addWidget(self.cancel)

        self.stack.addWidget(self._intro_page())
        self.stack.addWidget(self._capture_page())
        self.stack.addWidget(self._review_page())
        self.stack.addWidget(self._done_page())
        self._go(0)

    # ---- pages ---------------------------------------------------------------

    def _intro_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Transparent")
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(label("Add your current appearance" if self.mode == "append"
                               else "Set up face recognition", "PageTitle"))
        layout.addWidget(label(
            "The camera takes a few photos while you look at it and turn your head slightly. "
            "It takes about a minute. Your photos stay on this PC.", "Body2", wrap=True))
        tips = Card(padding=(18, 14), spacing=8)
        for glyph, text in ((G.SUN, "Face a light source; avoid a bright window behind you."),
                            (G.PERSON, "Sit at your usual distance from the screen."),
                            (G.FACE, "Wear what you normally wear, such as your glasses.")):
            tips.body.addLayout(hbox(GlyphLabel(glyph, 16, tone="accent"),
                                     label(text, "Body"), spacing=10, stretch_at=2))
        layout.addWidget(tips)
        if self.has_profile:
            self.add_choice = QRadioButton(
                "Add to my current profile (recommended): keeps your existing photos")
            self.replace_choice = QRadioButton(
                "Start over: replaces your photos (they're backed up) and clears learned samples")
            (self.add_choice if self.mode == "append" else self.replace_choice).setChecked(True)
            layout.addWidget(self.add_choice)
            layout.addWidget(self.replace_choice)
        layout.addStretch(1)
        return page

    def _capture_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Transparent")
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        self.preview = Preview()
        layout.addWidget(self.preview, 1)
        self.guidance = label("", "CardTitle", wrap=True)
        self.guidance.setAccessibleName("Guidance")
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Photos taken")
        self.progress_text = label("", "Secondary")
        layout.addLayout(hbox(self.guidance, self.progress_text, spacing=12, stretch_at=1))
        layout.addWidget(self.progress)
        # Fail-safes for when the automatic capture won't trigger.
        self.take_button = button("Take photo now", self._take_now)
        self.take_button.setToolTip("Use this view for the angle being asked for, even in poor "
                                    "light.")
        self.skip_button = subtle_button("Skip this angle", self._skip_angle)
        layout.addLayout(hbox(self.take_button, self.skip_button, stretch_at=2))
        return page

    def _review_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Transparent")
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(label("Review and save", "PageTitle"))
        self.review_ring = ProgressRing(22)
        self.review_text = label("", "Body2", wrap=True)
        layout.addLayout(hbox(self.review_ring, self.review_text, spacing=10))
        self.review_detail = label("", "Secondary", wrap=True)
        layout.addWidget(self.review_detail)
        layout.addStretch(1)
        return page

    def _done_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Transparent")
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        self.done_icon = GlyphLabel(G.DONE, 40, tone="success")
        layout.addWidget(self.done_icon)
        self.done_title = label("", "PageTitle", wrap=True)
        self.done_text = label("", "Body2", wrap=True)
        layout.addWidget(self.done_title)
        layout.addWidget(self.done_text)
        layout.addStretch(1)
        return page

    def _go(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.steps.set_step(index)
        self.banner.hide()
        texts = {0: ("Start camera", ""), 1: ("", "Pause"), 2: ("Save face profile", "Retake"),
                 3: ("Test it now", "")}
        primary, secondary = texts[index]
        self.primary.setText(primary)
        self.primary.setVisible(bool(primary))
        self.secondary.setText(secondary)
        self.secondary.setVisible(bool(secondary))
        self.cancel.setText("Close" if index == 3 else "Cancel")

    # ---- actions -------------------------------------------------------------

    def _primary(self) -> None:
        index = self.stack.currentIndex()
        if index == 0:
            if self.has_profile:
                self.mode = "append" if self.add_choice.isChecked() else "replace"
            self._start_capture()
        elif index == 2:
            self._save()
        elif index == 3:
            self.controller.show_main("face")
            self.accept()

    def _secondary(self) -> None:
        index = self.stack.currentIndex()
        if index == 1:
            if self.worker is not None and self.worker.isRunning():
                self._stop_camera()
                self.secondary.setText("Resume")
                self.preview.show_message("Paused. The camera is off.")
            else:
                self._start_capture(resume=True)
                self.secondary.setText("Pause")
        elif index == 2:
            self._discard_staging()
            self._go(0)

    def _start_capture(self, resume: bool = False) -> None:
        if self._requesting or (self.worker is not None and self.worker.isRunning()):
            return  # a double click: the camera is already on, or being handed over

        def leased(reply) -> None:
            self._requesting = False
            if not reply or not reply.get("ok"):
                self.banner.show_message(
                    "critical", "The camera isn't available",
                    "Face Unlock's background service didn't hand over the camera, so it wasn't "
                    "opened. Make sure Face Unlock is running and try again.")
                self._go(0)
                return
            self._leased = True
            self.renew_timer.start(RENEW_MS)
            if not resume or self.plan is None:
                self.staging = APP_DIR / "staging" / secrets.token_hex(6)
                self.staging.mkdir(parents=True, exist_ok=True)
                self.plan = CapturePlan(targets_for(self.mode))
            self._go(1)
            self.secondary.setText("Pause")
            self.preview.show_message("Starting the camera…")
            self._update_progress(self.plan.taken, self.plan.total)
            self._retire(self.worker)
            self.worker = CaptureWorker(self.controller, self.staging, self.plan)
            self.worker.preview.connect(self._on_preview)
            self.worker.captured.connect(self._update_progress)
            self.worker.failed.connect(self._on_failed)
            self.worker.notice.connect(self.guidance.setText)
            self.worker.finished.connect(self._on_worker_finished)
            self.worker.start()

        self._requesting = True
        self.controller.client.request({"cmd": "pause_camera", "owner": OWNER,
                                        "seconds": LEASE_S}, leased, timeout_s=12.0)

    def _retire(self, worker: CaptureWorker | None) -> None:
        """Keep a stopped worker alive until its thread has really ended."""
        if worker is None or not worker.isRunning():
            return
        self._retired.append(worker)
        worker.finished.connect(lambda w=worker: self._retired.remove(w)
                                if w in self._retired else None)

    def _renew(self) -> None:
        def done(reply) -> None:
            if not reply or not reply.get("ok"):
                # Without a valid lease the service may use the camera again: stop ours.
                self._stop_camera()
                self.preview.show_message("Paused: the camera hand-over ended.")
                self.secondary.setText("Resume")
        self.controller.client.request({"cmd": "renew_camera", "owner": OWNER,
                                        "seconds": LEASE_S}, done)

    def _on_preview(self, image: QImage, observation: Observation) -> None:
        self.preview.show_frame(image, observation)
        text = observation.guidance
        if observation.box is not None and (observation.stuck or observation.tone == "issue"):
            text += (" Or press “Take photo now” to use this view anyway"
                     + (", or skip this angle." if observation.wanted not in (None, "center")
                        else "."))
        self.guidance.setText(text)
        self.take_button.setEnabled(observation.box is not None)
        self.skip_button.setEnabled(observation.wanted not in (None, "center"))

    def _take_now(self) -> None:
        if self.worker is not None:
            self.worker.take_requested = True

    def _skip_angle(self) -> None:
        if self.worker is not None:
            self.worker.skip_requested = True

    def _update_progress(self, taken: int, total: int) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(taken)
        self.progress_text.setText(f"{taken} of {total} photos")

    def _on_failed(self, message: str) -> None:
        self.preview.show_message("The camera couldn't be used.")
        self.banner.show_message("critical", "Camera problem", message)

    def _on_worker_finished(self) -> None:
        if self.plan is not None and self.plan.done:
            self._release_camera()
            self._review()

    def _stop_camera(self) -> None:
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.requestInterruption()
            worker.wait(3000)
            self._retire(worker)
        self._release_camera()

    def _release_camera(self) -> None:
        self.renew_timer.stop()
        if self._leased:
            self._leased = False
            self.controller.client.request({"cmd": "resume_camera", "owner": OWNER})

    def pause_for_lock(self) -> None:
        """Windows is locking: give the camera back to the sign-in screen at once."""
        if self.worker is not None and self.worker.isRunning():
            self._stop_camera()
            self.preview.show_message("Paused because your PC was locked.")
            self.secondary.setText("Resume")

    def _review(self) -> None:
        self._go(2)
        self.primary.setEnabled(False)
        self.review_ring.show()
        self.review_text.setText("Checking your photos…")
        self.review_detail.setText("")

        def checked(reply) -> None:
            self.review_ring.hide()
            if not reply or not reply.get("ok"):
                reason = (reply or {}).get("message") or "Face Unlock isn't responding."
                self.review_text.setText("The photos couldn't be checked. " + reason)
                return
            accepted, rejected = reply["accepted"], reply["rejected"]
            text = f"{accepted} of {accepted + rejected} photos are usable."
            reasons = {}
            for item in reply.get("results", []):
                if not item["accepted"]:
                    reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
            detail = []
            if reasons:
                detail.append("Not used: " + ", ".join(f"{r} ({n})" for r, n in reasons.items())
                              + ".")
            consistency = reply.get("consistency")
            if self.mode == "append" and consistency is not None:
                if consistency > self.controller.cfg.threshold:
                    detail.append("These photos look quite different from your current profile. "
                                  "Only save them if this is you.")
                else:
                    detail.append("These photos match your current profile.")
            if self.mode == "replace":
                detail.append("Saving replaces your reference photos (they're backed up first) "
                              "and clears learned samples.")
            self.review_text.setText(text)
            self.review_detail.setText(" ".join(detail))
            self.primary.setEnabled(accepted >= 3)

        self.controller.client.request(
            {"cmd": "build_enrollment", "mode": self.mode, "staging": self.staging.name,
             "dry_run": True, "allow_poor_light": self._poor_light_ok()}, checked,
            timeout_s=120.0)

    def _save(self) -> None:
        self.primary.setEnabled(False)
        self.review_ring.show()
        self.review_text.setText("Saving your face profile…")

        def saved(reply) -> None:
            self.review_ring.hide()
            if not reply or not reply.get("ok"):
                self.primary.setEnabled(True)
                self.banner.show_message("critical", "Not saved",
                                         (reply or {}).get("message") or
                                         "Face Unlock isn't responding. Your previous profile "
                                         "is unchanged.")
                return
            self.staging = None  # the service moved the photos into the profile folder
            self._go(3)
            self.done_title.setText("Face profile saved")
            self.done_text.setText(
                f"Face Unlock now has {reply['anchors']} reference photos"
                + (f" and {reply['learned']} learned samples" if reply.get("learned") else "")
                + ". It uses them straight away on the lock screen.")
            self.controller.client.refresh_status()

        self.controller.client.request(
            {"cmd": "build_enrollment", "mode": self.mode, "staging": self.staging.name,
             "allow_poor_light": self._poor_light_ok()}, saved, timeout_s=120.0)

    def _poor_light_ok(self) -> bool:
        """Photos you took yourself are kept even in poor light: you chose them."""
        return self.plan is not None and self.plan.manual > 0

    def _discard_staging(self) -> None:
        if self.staging is not None:
            shutil.rmtree(self.staging, ignore_errors=True)
            self.staging = None
        self.plan = None

    def reject(self) -> None:
        """Cancel, Close, Esc and the window's close button all end here.

        (Qt's dialog close handling calls ``reject``; this used to call
        ``close`` again, so Qt refused the close and Cancel did nothing.)
        """
        index = self.stack.currentIndex()
        if index in (1, 2) and self.plan is not None and self.plan.taken:
            from .dialogs import confirm
            if not confirm(self, "Stop setting up?", "The photos taken so far are discarded and "
                           "your current profile stays as it is.", "Stop"):
                return
        self._stop_camera()
        self._discard_staging()
        super().reject()
