"""Learning and adaptation: what the app learned, measured, and changed – and why."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QComboBox, QGridLayout, QMenu, QVBoxLayout, QWidget

from ..icons import G, GlyphLabel
from ..status import duration, fmt_ms, reason_text, sentence, when
from ..widgets import (
    Card,
    Divider,
    Page,
    Pill,
    SettingRow,
    ToggleSwitch,
    accent_button,
    button,
    hbox,
    label,
    subtle_button,
)

if TYPE_CHECKING:
    from ...app import AppController

LIGHT = {"bright": "good light", "dim": "dim light", "dark": "darkness", "backlit": "backlight",
         "unknown": "unknown light"}


PART_TEXT = {"night": "nights", "morning": "mornings", "afternoon": "afternoons",
             "evening": "evenings"}


def _clock(minute: int) -> str:
    minute %= 24 * 60
    return f"{minute // 60:02d}:{minute % 60:02d}"


def describe_routine(info: dict, interval_s: float) -> str:
    """The learned lock-screen schedule in a few plain sentences."""
    sentences = []
    returns = info.get("usual_returns") or {}
    times = [f"{name} " + ", ".join(f"{_clock(a)}–{_clock(b)}" for a, b in returns[day][:4])
             for day, name in (("weekday", "weekdays"), ("weekend", "weekends"))
             if returns.get(day)]
    if times:
        sentences.append("Scans continuously around the times you usually come back: "
                         + "; ".join(times) + ".")
    after = info.get("after_leaving") or {}
    soon = []
    for day in ("weekday", "weekend"):
        items = []
        for part, text in PART_TEXT.items():
            ranges = after.get(f"{day}-{part}")
            if ranges is None:
                continue
            first = ranges[0] if ranges and ranges[0][0] == 0 else None
            items.append(f"{text} {first[1]} min" if first else f"{text} quick looks only")
        if items:
            soon.append(f"{day}s – " + ", ".join(items))
    if soon:
        sentences.append("After you leave it keeps scanning: " + "; ".join(soon) + ".")
    sentences.append(f"The rest of the time it takes a quick look every {interval_s:g} seconds.")
    return " ".join(sentences)


def routine_outcome(info: dict) -> str:
    caught = info.get("caught") or {}
    instant, quick = caught.get("instant", 0), caught.get("quick-look", 0)
    parts = []
    if instant + quick:
        parts.append(f"Last 30 days: {instant} of {instant + quick} face sign-ins happened while "
                     "it was already scanning" + (f"; {quick} on a quick look." if quick else "."))
    share = info.get("scanning_share")
    if share is not None:
        parts.append(f"Continuous scanning used {share * 100:.0f}% of locked time.")
    return " ".join(parts)


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear(item.layout())


class LearningPage(Page):
    def __init__(self, controller: AppController):
        super().__init__(
            "Learning and adaptation",
            "Face Unlock can keep up with gradual changes – a beard, new glasses, different "
            "lighting – by learning from sign-ins Windows has confirmed. Everything it changes "
            "is listed here and can be undone.")
        self.controller = controller
        self.data: dict[str, Any] | None = None

        roles = Card(padding=(20, 16), spacing=8)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(4)
        grid.addWidget(Pill("You decide", "accent"), 0, 0, Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(Pill("Adjusted automatically, within limits", "neutral"), 0, 1,
                       Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(label("Whether learning is on, lock timing, the camera, keeping photos, "
                             "and the strictest setting allowed.", "Secondary", wrap=True), 1, 0)
        grid.addWidget(label("Which learned samples are kept, camera warm-up and low-light mode, "
                             "when the sign-in screen scans continuously, and – only if you "
                             "allow it – making recognition stricter.",
                             "Secondary", wrap=True), 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        roles.body.addLayout(grid)
        self.add(roles)
        self.notice = label("", "Secondary", wrap=True)
        self.add(self.notice)

        # Reliability
        self.section("How reliably face sign-in works")
        self.reliability = Card(padding=(20, 16), spacing=6)
        self.rel_metric = label("—", "Metric")
        self.rel_caption = label("", "Body2", wrap=True)
        self.reliability.body.addWidget(self.rel_metric)
        self.reliability.body.addWidget(self.rel_caption)
        self.rel_recent = QVBoxLayout()
        self.rel_recent.setSpacing(2)
        self.reliability.body.addLayout(self.rel_recent)
        self.reliability.body.addWidget(label(
            "Counts only times your face was in view of the sign-in screen. It shows how often "
            "you were recognised; it can't show how often someone else would be.", "Caption",
            wrap=True))
        self.add(self.reliability)

        # Learned samples
        self.section("What has been learned")
        learn = Card(padding=(0, 4), spacing=0)
        self.learn_toggle = ToggleSwitch()
        self.learn_toggle.toggled.connect(
            lambda on: self._set(adaptive_enrollment=on))
        learn.body.addWidget(SettingRow(G.BULB, "Learn from successful face sign-ins",
                                        "After Windows confirms a face sign-in, keep the single "
                                        "most useful new view of your face. Failed attempts and "
                                        "routine presence checks are never learned from.",
                                        self.learn_toggle))
        learn.body.addWidget(self._divider())
        body = QWidget()
        body.setObjectName("Transparent")
        self.learn_body = QVBoxLayout(body)
        self.learn_body.setContentsMargins(20, 12, 20, 14)
        self.learn_body.setSpacing(6)
        self.learn_summary = label("", "Body", wrap=True)
        self.learn_body.addWidget(self.learn_summary)
        self.timeline = QVBoxLayout()
        self.timeline.setSpacing(2)
        self.learn_body.addLayout(self.timeline)
        self.skipped = label("", "Secondary", wrap=True)
        self.learn_body.addWidget(self.skipped)
        self.undo_button = button("Undo the last 24 hours", self._undo_day)
        self.reset_button = button("Reset learned data…", self._reset)
        self.learn_body.addLayout(hbox(self.undo_button, self.reset_button, stretch_at=2))
        learn.body.addWidget(body)
        self.add(learn)

        # Strictness
        self.section("Recognition strictness")
        strict = Card(padding=(20, 16), spacing=8)
        self.strict_values = label("", "Body", wrap=True)
        self.strict_status = label("", "Body2", wrap=True)
        self.strict_evidence = label("", "Secondary", wrap=True)
        strict.body.addWidget(self.strict_values)
        strict.body.addWidget(self.strict_status)
        strict.body.addWidget(self.strict_evidence)
        self.apply_button = accent_button("Apply", self._apply_threshold)
        self.own_button = button("Use my own setting again", self._clear_threshold)
        strict.body.addLayout(hbox(self.apply_button, self.own_button, stretch_at=2))
        strict.body.addWidget(Divider())
        self.auto_toggle = ToggleSwitch()
        self.auto_toggle.toggled.connect(lambda on: self._set(auto_tighten_threshold=on))
        auto_row = SettingRow(None, "Make recognition stricter automatically",
                              "Only when the evidence above recommends it: one small step at a "
                              "time, never looser than your own setting, and undone "
                              "automatically if face sign-in becomes less reliable.",
                              self.auto_toggle)
        auto_row.layout().setContentsMargins(0, 8, 0, 0)
        strict.body.addWidget(auto_row)
        self.add(strict)

        # Camera
        self.section("Camera start-up")
        self.camera = Card(padding=(20, 16), spacing=6)
        self.camera_rows = QVBoxLayout()
        self.camera_rows.setSpacing(4)
        self.camera.body.addLayout(self.camera_rows)
        self.low_light = label("", "Body2", wrap=True)
        self.camera.body.addWidget(self.low_light)
        self.camera.body.addLayout(hbox(subtle_button("Measure the camera again",
                                                      self._remeasure), stretch_at=1))
        self.add(self.camera)

        # Lock-screen routine
        self.section("When you usually come back")
        routine = Card(padding=(0, 4), spacing=0)
        self.routine_toggle = ToggleSwitch()
        self.routine_toggle.toggled.connect(lambda on: self._set(unlock_learn_routine=on))
        routine.body.addWidget(SettingRow(
            G.CALENDAR_CLOCK, "Learn when I usually come back",
            "The sign-in screen scans continuously when you're likely to be back soon and takes "
            "quick looks the rest of the time. Uses only when you lock and sign in, kept as "
            "counts on this PC; old patterns fade within a few weeks.", self.routine_toggle))
        routine.body.addWidget(self._divider())
        holder = QWidget()
        holder.setObjectName("Transparent")
        routine_body = QVBoxLayout(holder)
        routine_body.setContentsMargins(20, 10, 20, 12)
        routine_body.setSpacing(6)
        self.routine_summary = label("", "Body2", wrap=True)
        self.routine_outcome = label("", "Secondary", wrap=True)
        routine_body.addWidget(self.routine_summary)
        routine_body.addWidget(self.routine_outcome)
        self.routine_forget = button("Forget my routine…", self._forget_routine)
        routine_body.addLayout(hbox(self.routine_forget, stretch_at=1))
        routine.body.addWidget(holder)
        self.add(routine)

        # Changes
        self.section("Changes")
        changes = Card(padding=(20, 16), spacing=6)
        self.ledger = QVBoxLayout()
        self.ledger.setSpacing(6)
        changes.body.addLayout(self.ledger)
        self.restore_button = button("Restore earlier settings")
        self.restore_menu = QMenu(self.restore_button)
        self.restore_button.setMenu(self.restore_menu)
        changes.body.addLayout(hbox(self.restore_button, stretch_at=1))
        self.add(changes)

        # Away patterns
        self.section("Away-time patterns (optional)")
        away = Card(padding=(0, 4), spacing=0)
        self.away_toggle = ToggleSwitch()
        self.away_toggle.toggled.connect(lambda on: self._set(absence_learning=on))
        away.body.addWidget(SettingRow(
            G.CALENDAR_CLOCK, "Keep approximate away-time statistics",
            "Counts how long you tend to be away by time of day, to the nearest few minutes. "
            "Only counts are stored, on this PC.", self.away_toggle))
        away.body.addWidget(self._divider())
        holder = QWidget()
        holder.setObjectName("Transparent")
        away_body = QVBoxLayout(holder)
        away_body.setContentsMargins(20, 10, 20, 12)
        self.away_summary = label("", "Body2", wrap=True)
        away_body.addWidget(self.away_summary)
        away.body.addWidget(holder)
        away.body.addWidget(self._divider())
        self.adaptive_toggle = ToggleSwitch()
        self.adaptive_toggle.toggled.connect(lambda on: self._set(adaptive_lock=on))
        self.adaptive_min = QComboBox()
        for minutes in (10, 15, 20, 30):
            self.adaptive_min.addItem(f"{minutes} minutes", minutes * 60)
        self.adaptive_min.activated.connect(
            lambda _i: self._set(adaptive_lock_min_timeout_s=self.adaptive_min.currentData()))
        self.adaptive_row = SettingRow(
            G.LOCK, "Lock sooner when I'm usually gone for hours",
            "Uses a shorter timeout only in time slots where the statistics show long absences. "
            "Never longer than your normal timeout.", self.adaptive_min)
        self.adaptive_row.set_control(self.adaptive_toggle)
        away.body.addWidget(self.adaptive_row)
        self.add(away)

        # Privacy
        self.section("Measurements")
        privacy = Card(padding=(20, 14), spacing=6)
        self.retention = label("", "Body2", wrap=True)
        privacy.body.addWidget(self.retention)
        privacy.body.addLayout(hbox(button("Clear measurements…", self._clear_measurements),
                                    stretch_at=1))
        self.add(privacy)
        self.finish()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.load)
        controller.config_changed.connect(lambda _c: self.refresh())

    @staticmethod
    def _divider() -> QWidget:
        holder = QWidget()
        holder.setObjectName("Transparent")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.addWidget(Divider())
        return holder

    # ---- data ------------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.load()
        self._timer.start(30_000)
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        super().hideEvent(event)

    def load(self) -> None:
        def done(reply) -> None:
            self.data = reply if reply and reply.get("ok") else None
            self.refresh()
        self.controller.client.request({"cmd": "insights", "days": 30}, done, timeout_s=15.0)

    def _set(self, **changes) -> None:
        ok, problem = self.controller.update_config(**changes)
        if not ok:
            self.notice.setText(f"Couldn't change that: {problem}")

    # ---- rendering ---------------------------------------------------------------

    def refresh(self) -> None:
        cfg = self.controller.cfg
        for toggle, value in ((self.learn_toggle, cfg.adaptive_enrollment),
                              (self.routine_toggle, cfg.unlock_learn_routine),
                              (self.auto_toggle, cfg.auto_tighten_threshold),
                              (self.away_toggle, cfg.absence_learning),
                              (self.adaptive_toggle, cfg.adaptive_lock)):
            toggle.blockSignals(True)
            toggle.setChecked(value)
            toggle.blockSignals(False)
        index = self.adaptive_min.findData(cfg.adaptive_lock_min_timeout_s)
        self.adaptive_min.setCurrentIndex(max(0, index))
        self.retention.setText(
            f"Local measurements (timings, outcomes and match scores – never images) are kept "
            f"for {cfg.metrics_retention_days} days to produce this page.")
        self._render_away()
        data = self.data
        self._render_routine(data.get("routine") if data else None)
        if data is None:
            self.notice.setText("Measurements are unavailable while Face Unlock isn't running.")
            return
        self.notice.setText("")
        self._render_reliability(data["reliability"])
        self._render_learning(data["learning"])
        self._render_threshold(data.get("threshold"))
        self._render_camera(data["camera"])
        self._render_ledger(data["ledger"], data["snapshots"])

    def _render_reliability(self, rel: dict) -> None:
        if not rel["sessions"]:
            self.rel_metric.setText("No data yet")
            self.rel_caption.setText("This fills in after you've used face sign-in a few times.")
        else:
            self.rel_metric.setText(f"{rel['face_unlocked']} of {rel['sessions']}")
            caption = "times your face signed you in when it was in view (last 30 days)."
            if rel.get("median_time_s") is not None:
                caption += (" Typical time to sign you in once the camera starts looking: "
                            f"{rel['median_time_s']:.1f} s.")
            self.rel_caption.setText(caption)
        _clear(self.rel_recent)
        for item in rel.get("recent", [])[:6]:
            glyph, tone = (G.DONE, "success") if item["face"] else (G.KEY, "muted")
            text = (f"{sentence(when(item['at']))} – signed in with your face" if item["face"]
                    else f"{sentence(when(item['at']))} – signed in another way"
                         + (f" ({reason_text(item['reason']).lower()})" if item.get("reason") else ""))
            self.rel_recent.addLayout(hbox(GlyphLabel(glyph, 14, tone=tone),
                                           label(text, "Secondary"), spacing=6))

    def _render_learning(self, info: dict) -> None:
        learned, cap = info["learned"], info["cap"]
        self.learn_summary.setText(
            f"{info['anchors']} original reference photos (never removed automatically) and "
            f"{learned} learned sample{'s' if learned != 1 else ''} (keeps at most {cap}; the "
            "most redundant go first, and the newest are always kept).")
        _clear(self.timeline)
        for item in info.get("timeline", [])[:8]:
            text = (f"{sentence(when(item['at']))} · learned in {LIGHT.get(item['lighting'], '')}"
                    f" · image quality {item['quality']:.2f}")
            self.timeline.addLayout(hbox(GlyphLabel(G.ADD, 14, tone="accent"),
                                         label(text, "Secondary"), spacing=6))
        skipped = info.get("skipped") or {}
        if skipped:
            parts = ", ".join(f"{reason} ({count})" for reason, count in skipped.items())
            self.skipped.setText(f"Recently not learned, and why: {parts}.")
        else:
            self.skipped.setText("")
        self.undo_button.setEnabled(learned > 0)
        self.reset_button.setEnabled(learned > 0)

    def _render_threshold(self, report: dict | None) -> None:
        if report is None:
            self.strict_values.setText("No face profile yet.")
            self.apply_button.hide()
            self.own_button.hide()
            return
        current, baseline = report["current"], report["baseline"]
        tuned = current < baseline - 1e-9
        self.strict_values.setText(
            f"Your setting: {baseline:.2f}   ·   In use: {current:.2f}"
            + ("   (made stricter automatically)" if tuned else "")
            + "   ·   Lower numbers are stricter.")
        self.strict_status.setText(report["explanation"])
        ev = report["evidence"]
        lights = ", ".join(f"{LIGHT.get(k, k)} {v}" for k, v in ev["lighting"].items()) or "none"
        self.strict_evidence.setText(
            f"Evidence: {ev['samples']} test frames from {ev['days']} day(s); lighting: {lights}; "
            f"{ev['sequences']} recorded tests. Run tests on the Your face page in different "
            "lighting to add evidence.")
        self.apply_button.setVisible(report["status"] == "tighten")
        self.apply_button.setText(f"Apply {report['recommended']:.2f}")
        self._recommended = report["recommended"]
        self.own_button.setVisible(tuned)

    def _render_camera(self, camera: dict) -> None:
        _clear(self.camera_rows)
        rows = camera.get("by_condition") or []
        if not rows:
            self.camera_rows.addWidget(label("No camera start-ups measured yet.", "Body2"))
        for row in rows:
            rate = "low-light rate" if row["fps"] != "default" else "normal rate"
            text = (f"In {LIGHT.get(row['lighting'], row['lighting'])}, {rate}: opens in "
                    f"{fmt_ms(row['open_ms'])}, image settles in {fmt_ms(row['settle_ms'])} "
                    f"({row['n']} start-ups)")
            self.camera_rows.addWidget(label(text, "Body2", wrap=True))
        low = camera.get("low_light") or {}
        state = {"trial": "being measured", "enabled": "on", "disabled": "off"}.get(
            low.get("state"), "unknown")
        self.low_light.setText(
            f"Low-light mode is {state}: {low.get('reason', '')}. Between lock-screen retries the "
            f"camera stays on for about {camera.get('hold_s', 0):.0f} s, based on how quickly "
            "the sign-in screen retries; otherwise it's released straight away.")

    def _render_ledger(self, ledger: list[dict], snapshots: list[dict]) -> None:
        _clear(self.ledger)
        if not ledger:
            self.ledger.addWidget(label("Nothing has been changed yet.", "Body2"))
        for entry in ledger[:10]:
            by = "You" if entry["by"] == "you" else "Automatic"
            text = f"{sentence(when(entry['ts']))} · {by} · {entry['reason']}"
            if entry.get("result"):
                text += f" — {entry['result']}"
            if entry.get("status") in ("reverted", "rolled-back"):
                text += f" ({entry['status'].replace('-', ' ')})"
            self.ledger.addWidget(label(text, "Body2", wrap=True))
        self.restore_menu.clear()
        for snap in snapshots[:10]:
            self.restore_menu.addAction(f"{sentence(when(snap['ts']))} – {snap['label']}",
                                        lambda i=snap["index"]: self._restore(i))
        self.restore_button.setEnabled(bool(snapshots))

    def _render_routine(self, info: dict | None) -> None:
        cfg = self.controller.cfg
        fixed = (f"scans continuously for {duration(cfg.unlock_idle_after_s)} after you leave, "
                 f"then takes a quick look every {cfg.unlock_idle_interval_s:g} seconds")
        if not cfg.unlock_learn_routine:
            self.routine_summary.setText(f"Off. The sign-in screen {fixed}.")
        elif not info or not info.get("learned"):
            needs = (info or {}).get("needs", {"locks": 10, "days": 5})
            locks, days = (info or {}).get("locks", 0), (info or {}).get("days", 0)
            self.routine_summary.setText(
                f"Learning: {locks:.0f} of {needs['locks']:.0f} locks over {days} of "
                f"{needs['days']} days so far. Until then the sign-in screen {fixed}.")
        else:
            self.routine_summary.setText(describe_routine(info, cfg.unlock_idle_interval_s))
        self.routine_outcome.setText(routine_outcome(info) if info else "")
        self.routine_forget.setEnabled(bool(info and info.get("locks")))

    def _render_away(self) -> None:
        cfg = self.controller.cfg
        model = self.controller.absence
        if not cfg.absence_learning:
            self.away_summary.setText("Off. Nothing is being recorded.")
        else:
            days = model.observed_days
            recs = model.recommendations()
            if recs:
                self.away_summary.setText(" ".join(r.text for r in recs[:3]))
            else:
                observed = ("Nothing observed yet." if days == 0 else
                            f"Observed on {days} day{'s' if days != 1 else ''} so far.")
                self.away_summary.setText(
                    f"{observed} Suggestions appear after at least two weeks, and only for "
                    "time slots with a clear pattern.")
        self.adaptive_row.setEnabled(cfg.absence_learning)
        self.adaptive_min.setEnabled(cfg.absence_learning and cfg.adaptive_lock)
        timeout = duration(cfg.effective_presence_absence_timeout_s)
        self.adaptive_row.set_description(
            "Uses a shorter timeout only in time slots where the statistics show long absences. "
            f"Never longer than your normal timeout ({timeout}).")

    # ---- actions -------------------------------------------------------------------

    def _service(self, request: dict, message: str) -> None:
        def done(reply) -> None:
            self.notice.setText(message if reply and reply.get("ok")
                                else "That didn't work: Face Unlock isn't responding.")
            self.load()
            self.controller.client.refresh_status()
        self.controller.client.request(request, done, timeout_s=15.0)

    def _undo_day(self) -> None:
        import time
        self._service({"cmd": "learning_undo", "since": time.time() - 86400},
                      "Learning from the last 24 hours was undone.")

    def _reset(self) -> None:
        from ..dialogs import confirm
        if confirm(self, "Reset learned data?", "All learned samples are removed. Your original "
                   "reference photos stay, and learning continues if it's on.", "Reset"):
            self._service({"cmd": "learning_reset"}, "Learned data was reset.")

    def _apply_threshold(self) -> None:
        from ..dialogs import confirm
        value = getattr(self, "_recommended", None)
        if value is None:
            return
        if confirm(self, f"Use {value:.2f}?", "Recognition becomes a little stricter. If face "
                   "sign-in becomes less reliable, the app switches back automatically and lists "
                   "it under Changes.", "Apply"):
            self._service({"cmd": "threshold_apply", "value": value},
                          f"Recognition strictness is now {value:.2f}.")

    def _clear_threshold(self) -> None:
        self._service({"cmd": "threshold_clear"}, "Your own strictness setting is in use again.")

    def _forget_routine(self) -> None:
        from ..dialogs import confirm
        if confirm(self, "Forget your routine?", "The sign-in screen goes back to your fixed "
                   "setting and starts learning again from your next lock.", "Forget"):
            self._service({"cmd": "routine_reset"}, "Your routine was forgotten.")

    def _remeasure(self) -> None:
        self._service({"cmd": "low_light_reset"}, "Camera measurements will start again.")

    def _restore(self, index: int) -> None:
        from ..dialogs import confirm
        if not confirm(self, "Restore these settings?", "Your current settings are replaced by "
                       "the saved ones. You can restore again afterwards.", "Restore"):
            return

        def done(reply) -> None:
            if not reply or not reply.get("ok"):
                self.notice.setText("Couldn't restore: " + str((reply or {}).get(
                    "message", "Face Unlock isn't responding.")))
                return
            ok, problem = self.controller.update_config(**reply["config"])
            self.notice.setText("Settings restored." if ok else f"Couldn't restore: {problem}")
            self.load()

        self.controller.client.request({"cmd": "snapshot_restore", "index": index}, done)

    def _clear_measurements(self) -> None:
        from ..dialogs import confirm
        if confirm(self, "Clear measurements?", "Removes local timings, outcomes and evaluation "
                   "samples used on this page. Your face profile and settings stay.", "Clear"):
            self.controller.metrics.clear()
            self._service({"cmd": "clear_measurements"}, "Measurements cleared.")
