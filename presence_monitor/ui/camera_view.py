"""Seeing the camera: the live preview, lighting meter and head-turn hint used by face
capture, and the "Is this you?" camera check on first-run setup's *Get ready* step.

Every window that opens the camera first borrows it from the background service
through a renewable lease (``pause_camera`` / ``renew_camera`` / ``resume_camera``),
never opens it if the hand-over is refused, and gives it back when it stops,
closes, or Windows locks.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import cv2
from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from face_service.camera import Camera, CameraError
from face_service.detector import FaceDetector
from face_service.exposure import expose_for_face
from face_service.quality import assess, to_gray

from . import theme
from .capture import METER_ZONES, Lighting, Observation, lighting
from .icons import G, GlyphLabel
from .widgets import StatusLine, button, fade_in, label, motion_enabled

if TYPE_CHECKING:
    from ..app import AppController

log = logging.getLogger(__name__)

LEASE_S = 120
RENEW_MS = 30_000
EXPOSE_EVERY_S = 1.0


def mirrored_image(frame) -> QImage:
    rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    return QImage(rgb.data, width, height, 3 * width, QImage.Format.Format_RGB888).copy()


class CameraLease(QObject):
    """Borrow the camera from the background service, renewing until released."""

    granted = Signal()
    refused = Signal()
    lost = Signal()        # a renewal was refused: stop using the camera now

    def __init__(self, controller: AppController, owner: str, parent: QObject | None = None):
        super().__init__(parent)
        self.controller = controller
        self.owner = owner
        self.held = False
        self._asking = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._renew)

    def acquire(self) -> None:
        if self.held or self._asking:
            return
        self._asking = True

        def done(reply) -> None:
            self._asking = False
            if not reply or not reply.get("ok"):
                self.refused.emit()
                return
            self.held = True
            self._timer.start(RENEW_MS)
            self.granted.emit()

        self.controller.client.request({"cmd": "pause_camera", "owner": self.owner,
                                        "seconds": LEASE_S}, done, timeout_s=12.0)

    def _renew(self) -> None:
        def done(reply) -> None:
            if self.held and (not reply or not reply.get("ok")):
                self.release()
                self.lost.emit()
        self.controller.client.request({"cmd": "renew_camera", "owner": self.owner,
                                        "seconds": LEASE_S}, done)

    def release(self) -> None:
        self._timer.stop()
        if self.held:
            self.held = False
            self.controller.client.request({"cmd": "resume_camera", "owner": self.owner})


class PreviewWorker(QThread):
    """Shows what the camera sees, with the face and its lighting (takes no photos)."""

    frame = Signal(QImage, object)     # mirrored image, Observation
    failed = Signal(str)

    FPS = 15

    def __init__(self, controller: AppController):
        super().__init__()
        self.cfg = controller.cfg

    def run(self) -> None:
        camera = Camera.from_config(self.cfg, allow_slow_backend=True, open_budget_s=20.0)
        try:
            camera.open()
            if self.cfg.camera_focus:
                camera.fix_focus(self.cfg.camera_focus)
            camera.settle()
        except CameraError as exc:
            log.warning("camera check could not start the camera: %s", exc)
            self.failed.emit(str(exc))
            camera.close()
            return
        detector = FaceDetector()
        last_emit = last_exposed = 0.0
        try:
            while not self.isInterruptionRequested():
                frame = camera.read_frame()
                if self.cfg.camera_face_exposure and time.monotonic() - last_exposed >= EXPOSE_EVERY_S:
                    last_exposed = time.monotonic()
                    frame = expose_for_face(camera, frame, detector).frame
                now = time.monotonic()
                if now - last_emit < 1.0 / self.FPS:
                    continue
                last_emit = now
                faces = detector.detect(frame)
                quality = assess(to_gray(frame), faces[0] if faces else None,
                                 face_count=len(faces))
                light = lighting(quality)
                # The face's brackets say "found"; the lighting meter beside it gives hints.
                observation = Observation(
                    "" if faces else "Looking for your face…",
                    "good" if faces and light.tone == "good" else "issue" if faces else "idle",
                    False, None, faces[0] if len(faces) == 1 else None, quality)
                self.frame.emit(mirrored_image(frame), observation)
        except CameraError as exc:
            self.failed.emit(str(exc))
        finally:
            camera.close()


class Preview(QWidget):
    """Live, mirrored camera view: an oval guide, the face that was found, a message,
    and a soft flash whenever a photo is taken."""

    def __init__(self, minimum: tuple[int, int] = (560, 420), *, oval: bool = True) -> None:
        super().__init__()
        self.image: QImage | None = None
        self.observation: Observation | None = None
        self.message = "Starting the camera…"
        self.oval = oval
        self._flash = 0.0
        self._flash_animation = QPropertyAnimation(self, b"flash", self)
        self._flash_animation.setDuration(320)
        self._flash_animation.setEasingCurve(QEasingCurve.Type.OutQuad)
        self.setMinimumSize(*minimum)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Camera preview")

    def _get_flash(self) -> float:
        return self._flash

    def _set_flash(self, value: float) -> None:
        self._flash = value
        self.update()

    flash = Property(float, _get_flash, _set_flash)

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
        self.setAccessibleDescription(message)
        self.update()

    def snap(self) -> None:
        """A photo was taken: a quick white flash, like a camera."""
        if not motion_enabled():
            return
        self._flash_animation.stop()
        self._flash_animation.setStartValue(1.0)
        self._flash_animation.setEndValue(0.0)
        self._flash_animation.start()

    def _image_rect(self, area: QRectF) -> QRectF | None:
        if self.image is None:
            return None
        scale = max(area.width() / self.image.width(), area.height() / self.image.height())
        w, h = self.image.width() * scale, self.image.height() * scale
        return QRectF((area.width() - w) / 2, (area.height() - h) / 2, w, h)

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
        target = self._image_rect(area)
        tone = self.observation.tone if self.observation else "idle"
        color = {"good": QColor(t.accent), "issue": QColor("#F7B500")}.get(tone, QColor("white"))
        if target is not None:
            painter.drawImage(target, self.image)
            box = self.observation.box if self.observation else None
            if box is not None:
                self._draw_face(painter, target, box, color)
        if self.oval:
            oval = QRectF(area.center().x() - area.width() * 0.2, area.height() * 0.14,
                          area.width() * 0.4, area.height() * 0.68)
            painter.setPen(QPen(color, 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(oval)
        if self.message:
            painter.setFont(theme.font(15 if area.width() >= 480 else 13, strong=True))
            metrics = painter.fontMetrics()
            width = min(area.width() - 24, metrics.horizontalAdvance(self.message) + 36)
            height = metrics.height() + 16
            pill = QRectF((area.width() - width) / 2, area.height() - height - 14, width, height)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawRoundedRect(pill, height / 2, height / 2)
            painter.setPen(QColor("white"))
            painter.drawText(pill.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignCenter,
                             metrics.elidedText(self.message, Qt.TextElideMode.ElideRight,
                                                round(pill.width()) - 24))
        if self._flash > 0:
            painter.fillRect(area, QColor(255, 255, 255, round(170 * self._flash)))
        painter.end()

    def _draw_face(self, painter: QPainter, target: QRectF, box, color: QColor) -> None:
        """Corner brackets around the face that was found (mirrored, like the image)."""
        scale = target.width() / self.image.width()
        x = target.left() + (self.image.width() - box.x - box.w) * scale
        rect = QRectF(x, target.top() + box.y * scale, box.w * scale, box.h * scale)
        arm = min(rect.width(), rect.height()) * 0.22
        pen = QPen(color, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        for cx, cy, dx, dy in ((rect.left(), rect.top(), 1, 1), (rect.right(), rect.top(), -1, 1),
                               (rect.left(), rect.bottom(), 1, -1),
                               (rect.right(), rect.bottom(), -1, -1)):
            painter.drawLine(QPointF(cx, cy), QPointF(cx + dx * arm, cy))
            painter.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * arm))


class LightMeter(QWidget):
    """How well lit your face is: a bar from dark to bright, a needle, and a hint."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Transparent")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(GlyphLabel(G.SUN, 14, tone="muted"))
        top.addWidget(label("Light on your face", "Secondary"))
        top.addStretch(1)
        layout.addLayout(top)
        self.bar = _MeterBar()
        layout.addWidget(self.bar)
        self.hint = label("", "Secondary", wrap=True)
        layout.addWidget(self.hint)
        self.setAccessibleName("Lighting meter")
        self.reading: Lighting | None = None

    def show_lighting(self, reading: Lighting) -> None:
        changed = self.reading is None or reading.hint != self.reading.hint
        self.reading = reading
        self.bar.set_level(reading.level, reading.tone)
        if changed:
            self.hint.setText(reading.hint)
            self.setAccessibleDescription(reading.hint)


class _MeterBar(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(14)
        self.setMinimumWidth(160)
        self._level = 0.0
        self._target = 0.0
        self.tone = "idle"
        self._timer = QTimer(self)   # glides the needle rather than jumping
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._glide)

    def set_level(self, level: float, tone: str) -> None:
        self._target = level
        self.tone = tone
        if not motion_enabled():
            self._level = level
            self.update()
        elif not self._timer.isActive():
            self._timer.start()

    def _glide(self) -> None:
        self._level += (self._target - self._level) * 0.25
        if abs(self._target - self._level) < 0.002:
            self._level = self._target
            self._timer.stop()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(6, 4, self.width() - 12, 6)
        colors = {"critical": QColor(t.critical), "caution": QColor(t.caution),
                  "good": QColor(t.success)}
        gradient = QLinearGradient(track.topLeft(), track.topRight())
        previous = 0.0
        for limit, zone in METER_ZONES:
            color = QColor(colors[zone])
            color.setAlphaF(0.55)
            gradient.setColorAt(previous / 255.0, color)
            gradient.setColorAt(min(1.0, limit / 255.0), color)
            previous = limit + 0.01
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(track, 3, 3)
        if self.tone != "idle":
            x = track.left() + track.width() * self._level
            needle = colors.get(self.tone, QColor(t.text))
            painter.setBrush(QColor(t.card))
            painter.setPen(QPen(needle, 3))
            painter.drawEllipse(QPointF(x, track.center().y()), 5.5, 5.5)
        painter.end()


class TurnHint(QWidget):
    """A friendly little face that shows which way to turn for the next photo."""

    ARROWS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}

    def __init__(self, size: int = 72, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.pose: str | None = None
        self._phase = 0.0
        self._animation = QPropertyAnimation(self, b"phase", self)
        self._animation.setDuration(1600)
        self._animation.setStartValue(0.0)
        self._animation.setKeyValueAt(0.5, 1.0)
        self._animation.setEndValue(0.0)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.setLoopCount(-1)
        self.setAccessibleName("Which way to turn")

    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, value: float) -> None:
        self._phase = value
        self.update()

    phase = Property(float, _get_phase, _set_phase)

    def set_pose(self, pose: str | None) -> None:
        if pose == self.pose:
            return
        self.pose = pose
        self.setAccessibleDescription({"left": "Turn a little to your left",
                                       "right": "Turn a little to your right",
                                       "up": "Tilt your chin up", "down": "Tilt your chin down",
                                       "center": "Look straight at the camera"}.get(pose or "",
                                                                                     ""))
        if pose and motion_enabled():
            if self._animation.state() != QPropertyAnimation.State.Running:
                self._animation.start()
        else:
            self._animation.stop()
            self._set_phase(0.6 if pose in self.ARROWS else 0.0)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._animation.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.pose and motion_enabled():
            self._animation.start()
        super().showEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = self.width()
        centre = QPointF(size / 2, size / 2)
        radius = size * 0.30
        dx, dy = self.ARROWS.get(self.pose or "", (0, 0))
        ink = QColor(t.accent_text)
        # Head.
        fill = QColor(t.accent)
        fill.setAlphaF(0.16)
        painter.setPen(QPen(ink, 2))
        painter.setBrush(fill)
        if self.pose == "center":
            grow = 1.0 + 0.06 * self._phase   # a gentle "look here" pulse
            painter.drawEllipse(centre, radius * grow, radius * grow)
        else:
            painter.drawEllipse(centre, radius, radius)
        # Eyes, nose and smile slide the way the head turns.
        shift = QPointF(dx, dy) * radius * 0.38 * self._phase
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        for ex in (-0.36, 0.36):
            painter.drawEllipse(centre + QPointF(ex * radius, -0.12 * radius) + shift,
                                radius * 0.09, radius * 0.11)
        painter.setPen(QPen(ink, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        mouth = QRectF(centre.x() - radius * 0.32, centre.y() + radius * 0.02,
                       radius * 0.64, radius * 0.42).translated(shift)
        painter.drawArc(mouth, 200 * 16, 140 * 16)
        if self.pose in self.ARROWS:
            # An arrow just outside the head, nudging in the direction to turn.
            alpha = QColor(ink)
            alpha.setAlphaF(0.45 + 0.55 * self._phase)
            painter.setPen(QPen(alpha, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            direction = QPointF(dx, dy)
            normal = QPointF(-dy, dx)
            tip = centre + direction * (radius + size * 0.14 + 3 * self._phase)
            back = tip - direction * size * 0.10
            painter.drawLine(back - direction * size * 0.04, tip)
            painter.drawLine(tip, back + normal * size * 0.07)
            painter.drawLine(tip, back - normal * size * 0.07)
        painter.end()


class CameraPeek(QWidget):
    """"Is this you?": a small live preview on the *Get ready* step, so you can see the
    right camera was picked (and pick another), with the lighting meter."""

    OWNER = "camera-check"

    def __init__(self, controller: AppController, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Transparent")
        self.controller = controller
        self.worker: PreviewWorker | None = None
        self._retired: list[PreviewWorker] = []
        self._cameras: list[dict] = []
        self.lease = CameraLease(controller, self.OWNER, self)
        self.lease.granted.connect(self._open)
        self.lease.refused.connect(lambda: self._problem(
            "Face Unlock's background service didn't hand over the camera, so it wasn't turned "
            "on. Try again in a moment."))
        self.lease.lost.connect(lambda: self.stop("Paused: the camera hand-over ended."))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.preview = Preview((240, 180), oval=False)
        self.preview.setFixedSize(240, 180)
        self.preview.show_message("Camera off")
        layout.addWidget(self.preview)
        column = QVBoxLayout()
        column.setSpacing(8)
        column.addWidget(label("Is this you?", "CardTitle"))
        column.addWidget(label("Check that this is the camera in front of you. The camera "
                               "light comes on only while the preview runs.", "Secondary",
                               wrap=True))
        self.chooser = QComboBox()
        self.chooser.setAccessibleName("Camera")
        self.chooser.setToolTip("Several cameras were found: choose the one in front of you")
        self.chooser.activated.connect(self._chosen)
        self.chooser.hide()
        column.addWidget(self.chooser)
        self.meter = LightMeter()
        self.meter.hide()
        column.addWidget(self.meter)
        self.state = StatusLine()
        column.addWidget(self.state)
        self.toggle = button("Show camera preview", self._toggle)
        self.toggle.setToolTip("Turns the camera on here until you turn it off or continue")
        row = QHBoxLayout()
        row.addWidget(self.toggle)
        row.addStretch(1)
        column.addLayout(row)
        column.addStretch(1)
        layout.addLayout(column, 1)

    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _toggle(self) -> None:
        if self.running or self.lease.held:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.running:
            return
        self.state.clear()
        self.toggle.set_busy(True, "Turning the camera on…")
        self.preview.show_message("Starting the camera…")
        self.controller.capture_window_opened(self)   # hands the camera back on Windows+L
        self._list_cameras()
        self.lease.acquire()

    def _open(self) -> None:
        self._retire(self.worker)
        self.worker = PreviewWorker(self.controller)
        self.worker.frame.connect(self._on_frame)
        self.worker.failed.connect(self._problem)
        self.worker.start()

    def _on_frame(self, image: QImage, observation: Observation) -> None:
        if self.toggle.busy:
            self.toggle.set_busy(False)
            self.toggle.setText("Turn camera off")
        if self.meter.isHidden():
            self.meter.show()
            fade_in(self.meter)
        self.preview.show_frame(image, observation)
        self.meter.show_lighting(lighting(observation.quality))

    def _problem(self, message: str) -> None:
        self.stop("The camera couldn't be used.")
        self.state.done("critical", message)

    def stop(self, message: str = "Camera off") -> None:
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.requestInterruption()
            worker.wait(3000)
            self._retire(worker)
        self.lease.release()
        self.toggle.set_busy(False)
        self.toggle.setText("Show camera preview")
        self.preview.show_message(message)
        self.meter.hide()

    def pause_for_lock(self) -> None:
        """Windows is locking: give the camera back to the sign-in screen at once."""
        if self.running or self.lease.held:
            self.stop("Paused because your PC was locked.")

    def _retire(self, worker: PreviewWorker | None) -> None:
        """Keep a stopped worker alive until its thread has really ended."""
        if worker is None or not worker.isRunning():
            return
        self._retired.append(worker)
        worker.finished.connect(lambda w=worker: self._retired.remove(w)
                                if w in self._retired else None)

    def _list_cameras(self) -> None:
        def listed(reply) -> None:
            self._cameras = [c for c in (reply or {}).get("cameras", []) if not c.get("virtual")]
            self.chooser.clear()
            current = self.controller.cfg.camera_device_id.casefold().split("#{", 1)[0]
            for index, camera in enumerate(self._cameras):
                self.chooser.addItem(camera.get("name") or f"Camera {index + 1}",
                                     camera.get("path"))
                if str(camera.get("path", "")).casefold().split("#{", 1)[0] == current:
                    self.chooser.setCurrentIndex(index)
            self.chooser.setVisible(len(self._cameras) > 1)
        self.controller.client.request({"cmd": "camera_list"}, listed, timeout_s=15.0)

    def _chosen(self, index: int) -> None:
        path = self.chooser.itemData(index)
        if not path or path == self.controller.cfg.camera_device_id:
            return
        was_running = self.running or self.lease.held
        self.stop("Switching cameras…")
        ok, problem = self.controller.update_config(camera_device_id=path)
        if not ok:
            self.state.done("critical", f"That camera wasn't chosen: {problem}")
            return
        self.state.done("success", f"Using {self.chooser.currentText()}.")
        if was_running:
            QTimer.singleShot(400, self.start)
