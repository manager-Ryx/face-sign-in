# Adaptive behaviour

Face Unlock adapts in a few narrow, measurable ways. Each adaptation below
states its benefit, its trade-off, the evidence it needs, its bounds, and how
to undo it. Nothing here is an open-ended optimiser: user preferences never
change on their own, safeguards never change silently, and every automatic
change is listed on the *Learning* page with its reason.

Status key: **Active** (on in the shipped configuration), **Evidence-gated**
(turns itself on only after measurements justify it), **Optional** (off until
you turn it on), **Recommend-only** (suggests; you decide).

## 1. Learning your current appearance – Active (when "Learn" is on)

- **Benefit.** Gradual changes – beard, glasses, haircut, seasonal light – stay
  recognisable without re-enrolling.
- **What happens.** Only after Windows *confirms* a face sign-in
  (`ReportResult` success from the tile) is the single most useful frame of that
  attempt added. "Useful" balances image quality (exposure, size, sharpness,
  facing the camera) against novelty (distance from everything stored).
- **Never learned from.** Failed attempts, presence checks, recognition tests,
  PIN/password sign-ins, frames with more than one face, dark/blurred/small
  faces, or frames nearly identical to a stored sample. The earlier option to
  learn from near-miss frames of failed attempts was removed.
- **Drift protection.** A candidate must match one of your *original*
  enrollment photos at least 0.10 more closely than the unlock threshold.
  Learned samples never validate other learned samples, so there is no chain
  of slowly drifting samples. The check is repeated at commit time.
- **Callbacks.** Each staged sample has a random 144-bit id. A missing report
  expires after 2 minutes, a failed report discards it, a repeated or late
  report finds nothing to commit – the profile can't be changed twice or by a
  stale message. Writes are atomic (temp file, fsync, replace).
- **Bounds.** 1 sample per sign-in (setting allows 2); at most 300 learned
  samples in addition to the original enrollment, which is never evicted.
  When full, the newest 20 % are always kept and the most redundant of the
  rest is removed first, so distinct looks (glasses on/off) survive.
- **Storage.** A 512-number face signature per sample; photos only if you
  enable "keep a small photo" (off by default, capped at 50).
- **Undo.** Learning page: *Undo the last 24 hours*, *Reset learned data*
  (original enrollment kept), pause with the switch. Profile backups are made
  before any replacement.

## 2. Recognition strictness – Recommend-only (automatic only if you allow it)

- **Benefit.** The strictest match that still recognises you consistently.
- **Evidence.** Held-out samples that are never used for matching: your
  recognition tests (complete five-frame sequences), 1-in-7 photos from a new
  enrollment, and one spare frame per confirmed sign-in (marked as possibly
  optimistic because only successful sign-ins contribute). Requires 60+
  frames, from 5+ days, in 2+ lighting conditions.
- **Rule.** A stricter value must keep a conservative (Wilson 95 %) lower bound
  of ≥ 90 % per-frame acceptance overall and in each lighting condition with
  15+ samples, and – when 10+ recorded tests exist – ≥ 95 % of tests replayed
  through the real two-frame rule must still unlock.
- **Bounds.** One step of at most 0.02; never below 0.30; never looser than
  your own setting; never loosened because of failures (poor recognition
  produces advice to add your current appearance instead).
- **Rollback.** After an automatic change, the next 12 lock-screen sessions in
  which your face was seen are monitored; if face sign-in success falls more
  than 15 points below the rate before the change, the previous value is
  restored automatically and recorded under *Changes*. *Use my own setting
  again* clears it at any time.
- **Limit.** Genuine samples show how often *you* are recognised. They cannot
  show how often someone else would be accepted, and no threshold guarantees
  every other face is rejected.

## 3. Progressive frames on the lock screen – Active

- **Benefit.** Faster decisions with fewer camera frames: no face → three
  detector-only looks (~10 ms each) then stop; clear mismatch → stop after two;
  match → one confirming frame; uncertain or poor image → up to five.
- **Kept the same.** Two matching frames are required, as with the previous
  "two of three" vote. A one-frame rule exists but is disabled; evaluation has
  not justified it.
- **Correlation.** Confirming frames must be at least 0.1 s apart; frames from
  one attempt are still strongly correlated, so the second match guards
  against a glitched frame rather than being an independent identity check.
- **Trade-off.** Up to five frames gives a genuine-but-borderline attempt more
  chances; it also gives a look-alike more chances within one attempt. The
  sign-in tile retries anyway, so the threshold remains the real control.
- **Evidence.** `python -m tools.evaluate_policies` replays recorded tests
  through both strategies. *Settings → Advanced* switches back to "Fixed, 2 of 3".

## 4. Camera warm-up – Active

- **Before.** Re-set width/height after opening (≈1.1 s, measured) and then
  discard 10 frames (≈0.6 s), every time.
- **Now.** The camera is used as delivered (it already gives 640×480) and
  frames are read until whole-frame brightness is stable for three frames
  (exposure settling), within 12 frames / 2 s. Head movement barely changes
  whole-frame brightness, so it can't extend the warm-up; darkness alone is not
  a reason to wait.
- **Learning.** Start-up and settling times are recorded per lighting
  condition and frame rate; after 5 start-ups the budget becomes twice the
  learned settling time plus a margin, never above the configured limits.

## 5. Low-light frame rate – Evidence-gated

- **Benefit.** In dim light, asking the camera for 15 fps lets it expose
  longer. Measured on the NearStream V02AF in evening light: face brightness
  46 → 72–90, match distances improved (median 0.45 → 0.39).
- **Trade-off.** Opening at 15 fps took ~1.9 s instead of 0.64 s; switching an
  open camera ~1.3 s; slightly more motion blur.
- **Evidence gate.** Background presence checks in dim light try the switch and
  measure the gain. After 3 trials the mode turns on only if the median gain is
  ≥ 1.25×, otherwise it turns off with the measured reason. Then the camera
  opens directly at 15 fps when the hour-of-day history predicts dim light.
- **No global changes.** The frame rate belongs to this capture session; no
  hardware controls are written (exposure/gain controls are not exposed by this
  camera through DirectShow anyway).
- **Undo.** *Measure the camera again* resets it; *Advanced → Low-light frame
  rate → Off* disables it.

## 6. Camera use on the lock screen – Active, follows your routine

- **Measured problem.** The tile asks for a scan again after every failure for
  as long as the lock screen is up: one request about every 6 s for 25 hours
  in one logged case, with the camera on throughout.
- **Now.** The sign-in screen either scans continuously (you are signed in the
  moment you sit down) or takes a quick look every `unlock_idle_interval_s`
  seconds with the camera off in between. It scans continuously for a short
  while after the screen locks or wakes, or a face is seen – `unlock_idle_after_s`
  until your routine is known, then 2 minutes – and whenever the learned
  routine says you are likely to be back soon. Pressing a key or clicking the
  tile always scans at once. With *Keep the camera off while the screen is off*
  on, nothing is scanned until the display wakes.
- **Routine learning (Active; *Learn when I usually come back*).** Each lock,
  from locking to signing in again, is folded into counts: returns and locked
  hours per band of time since locking (first six hours; separately for
  weekday/weekend nights, mornings, afternoons and evenings, by when you
  locked) and per half hour of a weekday or weekend day. No list of lock
  times is kept, and the counts halve every three weeks. A band's return rate
  (returns per locked hour) is damped towards your average – or towards zero
  for time of day – while it rests on little data. The camera scans
  continuously when the chance of a return within 15 minutes is at least
  10 %. It needs about 10 locks over 5 days; until then the fixed setting
  applies, and knowledge older than a few months stops counting.
  *Forget my routine* resets it; turning the switch off stops recording.
- **Evidence.** On a simulated regular office routine (four weeks learned, one
  week tested, three random seeds) every return in the test week came while
  the camera was already scanning, with continuous scanning 14–15 % of locked
  time. A fixed 30-minute window caught 43 % of returns (8–9 % of locked
  time) and a fixed 60-minute window 65 % (14–15 %). Real routines are less
  regular: the Learning page shows the live figures – face sign-ins while
  already scanning versus on a quick look, and the share of locked time spent
  scanning.
- **Hold time.** Between active retries the camera stays open for the 90th
  percentile of measured retry gaps plus 1 s (2–15 s); it is released at once
  after success, after background checks, and in idle mode.
- **Bounds.** Every request finishes within 15 s of waiting, well inside the
  tile's 30 s timeout. Settings: *Sign-in screen* section.

## 7. Presence checks while locked – Active

Checks are skipped while Windows is locked (the tile already looks for you),
during remote sessions, while the camera is lent to setup, and while paused.
Skipped or failed checks reset the absence timer: they never count as you
being away. Unlocking restarts the absence window.

## 8. Away-time patterns – Optional (off)

- **Stored.** Only counts: for weekdays/weekends and each hour, how many
  absences fell into "under 10 min … over 4 h". Absences starting with a lock
  or ending with an unlock are timed exactly; camera-detected ones to within a
  check interval. Remote sessions, camera failures and unobserved periods
  discard the absence in progress.
- **Recommendation.** After 14 observed days, a time slot with 8+ absences of
  which ≥ 75 % lasted over 2 hours (Wilson lower bound ≥ 50 %) is reported with
  its counts.
- **Adaptive locking (separate switch).** In such slots only, lock after the
  minimum you choose (default 15 minutes, never under 5) instead of your normal
  timeout; never longer than your normal timeout. The active rule is shown on
  the Overview.

## 9. When to re-enroll instead

The Learning page suggests *Add my current appearance* when matches have
drifted weaker over several weeks, when face sign-in often needed another
method, or when strictness evidence shows test frames outside the current
setting. A short guided capture (about 30 s) is more reliable than letting
learning chase a large change.

## Every setting, classified

Generated from the code (`face_service/config.py`).

| Setting | Who controls it | Default | Bounds | What it does |
|---|---|---|---|---|
| `model_name` | Safeguard (never changes silently) | `ArcFace` | ArcFace | Face-recognition model. Changing it requires re-enrolling. |
| `detector_backend` | Safeguard (never changes silently) | `yunet` | yunet | Face detector used before recognition. |
| `distance_metric` | Safeguard (never changes silently) | `cosine` | cosine | How face similarity is measured. |
| `threshold` | Safeguard (never changes silently) | `0.45` | 0.2-0.68 | Strictest-allowed match distance (lower is stricter). Automatic tuning may only make recognition stricter than this. |
| `anti_spoofing` | Safeguard (never changes silently) | `True` |  | Check that the face is live (blocks printed photos) before matching. |
| `detector_min_confidence` | Safeguard (never changes silently) | `0.8` | 0.5-0.95 | Minimum face-detection score for live frames. Identity matching is unaffected. |
| `inference_threads` | You decide | `4` | 1-8 | CPU threads used for each recognition step. |
| `camera_device_id` | You decide | `` |  | USB identity of the only camera allowed to recognise you. |
| `camera_index` | You decide | `0` | 0-16 | Legacy camera index, used only when no camera identity is set. |
| `camera_warmup_frames` | Measured, automatic within bounds | `12` | 1-60 | Maximum frames to wait for the image to settle after the camera opens. |
| `camera_warmup_max_s` | Measured, automatic within bounds | `2.0` | 0.3-6 | Maximum seconds to wait for the image to settle. |
| `camera_low_light_fps` | Measured, automatic within bounds | `15.0` | 0-30 | Frame rate requested in dim light so the camera can expose longer (0 disables). |
| `persistent_camera` | You decide | `False` |  | Keep the camera open all the time (camera light stays on). |
| `unlock_camera_hold_s` | Measured, automatic within bounds | `15.0` | 0-30 | Longest time the camera stays open between active lock-screen retries. |
| `verify_frames` | Safeguard (never changes silently) | `5` | 1-30 | Frames examined by a manual verification check. |
| `verify_required` | Safeguard (never changes silently) | `2` | 1-30 | Matching frames needed by a manual verification check. |
| `unlock_strategy` | Safeguard (never changes silently) | `progressive` | progressive, fixed | Lock-screen frame strategy: 'progressive' decides after every frame and looks at up to five; 'fixed' always uses the frame count below. |
| `unlock_verify_frames` | Safeguard (never changes silently) | `3` | 1-10 | Frames per lock-screen attempt with the fixed strategy. |
| `unlock_verify_required` | Safeguard (never changes silently) | `2` | 1-5 | Matching frames needed to unlock. |
| `unlock_max_frames` | Safeguard (never changes silently) | `5` | 2-10 | Most frames one progressive lock-screen attempt may examine. |
| `unlock_idle_after_s` | Measured, automatic within bounds | `60.0` | 15-7200 | Seconds of continuous lock-screen scanning after the screen locks or wakes, or a face is seen. With routine learning on, the learned routine takes over once enough is known. |
| `unlock_idle_interval_s` | You decide | `10.0` | 3-20 | Seconds between quick checks once lock-screen scanning has slowed down. |
| `unlock_learn_routine` | You decide | `True` |  | Learn when you usually come back to a locked PC and scan continuously then (uses only lock and sign-in times, kept as counts on this PC). |
| `unlock_pause_when_display_off` | You decide | `True` |  | Stop using the camera while the screen is off on the lock screen. |
| `unlock_client_check` | Safeguard (never changes silently) | `log` | off, log, enforce | Verify that only the Windows sign-in screen can request your credentials ('log' records the caller, 'enforce' refuses others). |
| `adaptive_enrollment` | You decide | `False` |  | Learn your current appearance from confirmed face sign-ins. |
| `adaptive_samples_per_unlock` | Safeguard (never changes silently) | `1` | 0-2 | Most face samples learned from one confirmed sign-in. |
| `adaptive_max_embeddings` | You decide | `300` | 1-2000 | Most learned samples kept, in addition to your original enrollment. |
| `adaptive_retention_days` | You decide | `0.0` | 0-3650 | Forget learned samples older than this many days (0 keeps them). |
| `adaptive_match_margin` | Safeguard (never changes silently) | `0.1` | 0-0.3 | A learned sample must match an original enrollment photo this much more closely than the unlock threshold. |
| `adaptive_min_diversity` | Safeguard (never changes silently) | `0.025` | 0-0.5 | Reject learned samples nearly identical to ones already stored. |
| `adaptive_min_face_confidence` | Safeguard (never changes silently) | `0.9` | 0-1 | Minimum detection score for a frame to be learned. |
| `adaptive_min_face_area_ratio` | Safeguard (never changes silently) | `0.04` | 0-1 | Minimum face size (share of the image) for a frame to be learned. |
| `adaptive_min_blur` | Safeguard (never changes silently) | `40.0` | 0-1000 | Minimum sharpness for a frame to be learned. |
| `adaptive_keep_photos` | You decide | `False` |  | Also keep a small photo of each learned sample (off: only the numeric face signature is stored). |
| `adaptive_photo_limit` | You decide | `50` | 1-500 | Most learned-sample photos kept when photos are enabled. |
| `auto_tighten_threshold` | You decide | `False` |  | Let the app make recognition stricter automatically once enough evidence shows it still recognises you reliably. |
| `presence_enabled` | You decide | `True` |  | Lock the PC automatically when you have been away. |
| `presence_interval_s` | You decide | `300` | 10-3600 | Seconds between presence checks. |
| `presence_absence_timeout_s` | You decide | `0` | 0-86400 | Lock after roughly this many seconds without seeing you (0 uses the legacy strike count). |
| `presence_absent_strikes` | You decide | `2` | 1-100 | Legacy: consecutive absent checks before locking, used only when the timeout is 0. |
| `presence_mode` | You decide | `recognition` | recognition, detection | 'recognition' requires your face; 'detection' accepts any face. |
| `presence_pause_while_locked` | You decide | `True` |  | Skip presence checks while Windows is locked. |
| `absence_learning` | You decide | `False` |  | Keep approximate, local away-time statistics to suggest a lock timeout. |
| `adaptive_lock` | You decide | `False` |  | Use a shorter lock timeout at times when you are usually away for hours. |
| `adaptive_lock_min_timeout_s` | You decide | `900` | 300-86400 | Shortest lock timeout adaptive locking may use. |
| `warmup_on_start` | You decide | `True` |  | Load the recognition model when the service starts. |
| `language` | You decide | `from Windows` |  | Interface language. |
| `theme` | You decide | `system` | system, light, dark | Light, dark, or follow Windows. |
| `notifications` | You decide | `True` |  | Show desktop notifications. |
| `check_for_updates` | You decide | `False` |  | Check the upstream project for new releases at start-up. |
| `metrics_retention_days` | You decide | `90` | 7-365 | Days of local measurements kept for the Learning page. |
