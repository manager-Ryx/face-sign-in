"""User configuration: typed loading, validation, and atomic saving.

Every setting carries a :class:`Spec` describing who controls it:

* ``PREFERENCE`` – the user's choice; the app never changes it on its own.
* ``AUTOMATIC``  – the app may adjust it within the stated bounds, based on
  measurements, and must be able to explain and undo the change.
* ``SAFEGUARD``  – a protection that must never change silently.

Loading never trusts the file blindly. Each value is type-checked and
bounds-checked; an invalid value falls back to the last configuration that
was saved successfully (or to the built-in default) and the problem is
reported, so a damaged or tampered file cannot weaken recognition.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import MISSING, asdict, dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, get_type_hints

from .storage import write_text_atomic

try:
    import tomli_w
except ImportError:  # pragma: no cover - optional for read-only usage
    tomli_w = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

APP_DIR = Path(os.environ.get("FACE_UNLOCK_HOME", Path.home() / ".face-unlock"))
CONFIG_PATH = APP_DIR / "config.toml"
LAST_GOOD_CONFIG_PATH = APP_DIR / "config.last-good.toml"
ENROLL_DIR = APP_DIR / "enroll"
EMBED_PATH = APP_DIR / "embeddings.npz"
CREDS_PATH = APP_DIR / "credentials.bin"
LOG_PATH = APP_DIR / "service.log"
PRESENCE_LOG_PATH = APP_DIR / "presence.log"
METRICS_DIR = APP_DIR / "metrics"
STATE_DIR = APP_DIR / "state"
BACKUP_DIR = APP_DIR / "backups"
LEARNED_PHOTOS_DIR = APP_DIR / "learned-photos"

PIPE_NAME = r"\\.\pipe\FaceUnlock"

PRESENCE_MODES = ("recognition", "detection")
UNLOCK_STRATEGIES = ("progressive", "fixed")
THEMES = ("system", "light", "dark")
CLIENT_CHECK_MODES = ("off", "log", "enforce")


class Kind(StrEnum):
    PREFERENCE = "preference"
    AUTOMATIC = "automatic"
    SAFEGUARD = "safeguard"


@dataclass(frozen=True)
class Spec:
    kind: Kind
    summary: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None


def _setting(default: Any, kind: Kind, summary: str, *,
             minimum: float | None = None, maximum: float | None = None,
             choices: tuple[str, ...] | None = None, factory: Any = None) -> Any:
    spec = Spec(kind, summary, minimum, maximum, choices)
    if factory is not None:
        return field(default_factory=factory, metadata={"spec": spec})
    return field(default=default, metadata={"spec": spec})


def _default_language() -> str:
    from .i18n import detect_system_language
    return detect_system_language()


P, A, S = Kind.PREFERENCE, Kind.AUTOMATIC, Kind.SAFEGUARD


@dataclass
class Config:
    # --- recognition -------------------------------------------------------
    model_name: str = _setting(
        "ArcFace", S, "Face-recognition model. Changing it requires re-enrolling.",
        choices=("ArcFace",))
    detector_backend: str = _setting(
        "yunet", S, "Face detector used before recognition.", choices=("yunet",))
    distance_metric: str = _setting(
        "cosine", S, "How face similarity is measured.", choices=("cosine",))
    threshold: float = _setting(
        0.45, S, "Strictest-allowed match distance (lower is stricter). Automatic "
        "tuning may only make recognition stricter than this.", minimum=0.20, maximum=0.68)
    anti_spoofing: bool = _setting(
        True, S, "Check that the face is live (blocks printed photos) before matching.")
    detector_min_confidence: float = _setting(
        0.80, S, "Minimum face-detection score for live frames. Identity matching "
        "is unaffected.", minimum=0.50, maximum=0.95)
    inference_threads: int = _setting(
        4, P, "CPU threads used for each recognition step.", minimum=1, maximum=8)

    # --- camera ------------------------------------------------------------
    camera_device_id: str = _setting(
        "", P, "USB identity of the only camera allowed to recognise you.")
    camera_index: int = _setting(
        0, P, "Legacy camera index, used only when no camera identity is set.",
        minimum=0, maximum=16)
    camera_warmup_frames: int = _setting(
        12, A, "Maximum frames to wait for the image to settle after the camera opens.",
        minimum=1, maximum=60)
    camera_warmup_max_s: float = _setting(
        2.0, A, "Maximum seconds to wait for the image to settle.", minimum=0.3, maximum=6.0)
    camera_low_light_fps: float = _setting(
        15.0, A, "Frame rate requested in dim light so the camera can expose longer "
        "(0 disables).", minimum=0.0, maximum=30.0)
    persistent_camera: bool = _setting(
        False, P, "Keep the camera open all the time (camera light stays on).")
    unlock_camera_hold_s: float = _setting(
        15.0, A, "Longest time the camera stays open between active lock-screen retries.",
        minimum=0.0, maximum=30.0)

    # --- verification ------------------------------------------------------
    verify_frames: int = _setting(
        5, S, "Frames examined by a manual verification check.", minimum=1, maximum=30)
    verify_required: int = _setting(
        2, S, "Matching frames needed by a manual verification check.", minimum=1, maximum=30)
    unlock_strategy: str = _setting(
        "progressive", S, "Lock-screen frame strategy: 'progressive' decides after every "
        "frame and looks at up to five; 'fixed' always uses the frame count below.",
        choices=UNLOCK_STRATEGIES)
    unlock_verify_frames: int = _setting(
        3, S, "Frames per lock-screen attempt with the fixed strategy.", minimum=1, maximum=10)
    unlock_verify_required: int = _setting(
        2, S, "Matching frames needed to unlock.", minimum=1, maximum=5)
    unlock_max_frames: int = _setting(
        5, S, "Most frames one progressive lock-screen attempt may examine.",
        minimum=2, maximum=10)
    unlock_idle_after_s: float = _setting(
        60.0, A, "Seconds of continuous lock-screen scanning after the screen locks or "
        "wakes, or a face is seen. With routine learning on, the learned routine takes over "
        "once enough is known.", minimum=15.0, maximum=7200.0)
    unlock_idle_interval_s: float = _setting(
        10.0, P, "Seconds between quick checks once lock-screen scanning has slowed down.",
        minimum=3.0, maximum=20.0)
    unlock_learn_routine: bool = _setting(
        True, P, "Learn when you usually come back to a locked PC and scan continuously "
        "then (uses only lock and sign-in times, kept as counts on this PC).")
    unlock_pause_when_display_off: bool = _setting(
        True, P, "Stop using the camera while the screen is off on the lock screen.")
    unlock_client_check: str = _setting(
        "enforce", S, "Only the Windows sign-in screen may request your credentials "
        "('enforce'); 'log' lets other programs ask but records them; 'off' skips the check.",
        choices=CLIENT_CHECK_MODES)
    unlock_wait_after_manual_lock: bool = _setting(
        True, S, "After you lock the PC yourself, face sign-in waits until you have stepped "
        "out of view, woken the screen or selected the Face Unlock tile.")

    # --- learning ----------------------------------------------------------
    adaptive_enrollment: bool = _setting(
        False, P, "Learn your current appearance from confirmed face sign-ins.")
    adaptive_samples_per_unlock: int = _setting(
        1, S, "Most face samples learned from one confirmed sign-in.", minimum=0, maximum=2)
    adaptive_max_embeddings: int = _setting(
        300, P, "Most learned samples kept, in addition to your original enrollment.",
        minimum=1, maximum=2000)
    adaptive_retention_days: float = _setting(
        0.0, P, "Forget learned samples older than this many days (0 keeps them).",
        minimum=0.0, maximum=3650.0)
    adaptive_match_margin: float = _setting(
        0.10, S, "A learned sample must match an original enrollment photo this much "
        "more closely than the unlock threshold.", minimum=0.0, maximum=0.30)
    adaptive_min_diversity: float = _setting(
        0.025, S, "Reject learned samples nearly identical to ones already stored.",
        minimum=0.0, maximum=0.5)
    adaptive_min_face_confidence: float = _setting(
        0.90, S, "Minimum detection score for a frame to be learned.", minimum=0.0, maximum=1.0)
    adaptive_min_face_area_ratio: float = _setting(
        0.04, S, "Minimum face size (share of the image) for a frame to be learned.",
        minimum=0.0, maximum=1.0)
    adaptive_min_blur: float = _setting(
        40.0, S, "Minimum sharpness for a frame to be learned.", minimum=0.0, maximum=1000.0)
    adaptive_keep_photos: bool = _setting(
        False, P, "Also keep a small photo of each learned sample (off: only the "
        "numeric face signature is stored).")
    adaptive_photo_limit: int = _setting(
        50, P, "Most learned-sample photos kept when photos are enabled.",
        minimum=1, maximum=500)
    auto_tighten_threshold: bool = _setting(
        False, P, "Let the app make recognition stricter automatically once enough "
        "evidence shows it still recognises you reliably.")

    # --- presence ----------------------------------------------------------
    presence_enabled: bool = _setting(
        True, P, "Lock the PC automatically when you have been away.")
    presence_interval_s: int = _setting(
        300, P, "Seconds between presence checks.", minimum=10, maximum=3600)
    presence_absence_timeout_s: int = _setting(
        0, P, "Lock after roughly this many seconds without seeing you "
        "(0 uses the legacy strike count).", minimum=0, maximum=86400)
    presence_absent_strikes: int = _setting(
        2, P, "Legacy: consecutive absent checks before locking, used only when the "
        "timeout is 0.", minimum=1, maximum=100)
    presence_mode: str = _setting(
        "recognition", P, "'recognition' requires your face; 'detection' accepts any face.",
        choices=PRESENCE_MODES)
    presence_pause_while_locked: bool = _setting(
        True, P, "Skip presence checks while Windows is locked.")
    relock_unconfirmed_s: int = _setting(
        120, S, "After a face sign-in, lock again if you are not seen at the PC within this "
        "many seconds, for example when you only walked past (0 turns this off).",
        minimum=0, maximum=600)
    absence_learning: bool = _setting(
        False, P, "Keep approximate, local away-time statistics to suggest a lock timeout.")
    adaptive_lock: bool = _setting(
        False, P, "Use a shorter lock timeout at times when you are usually away for hours.")
    adaptive_lock_min_timeout_s: int = _setting(
        900, P, "Shortest lock timeout adaptive locking may use.", minimum=300, maximum=86400)

    # --- application -------------------------------------------------------
    warmup_on_start: bool = _setting(
        True, P, "Load the recognition model when the service starts.")
    language: str = _setting(None, P, "Interface language.", factory=_default_language)
    theme: str = _setting("system", P, "Light, dark, or follow Windows.", choices=THEMES)
    notifications: bool = _setting(True, P, "Show desktop notifications.")
    check_for_updates: bool = _setting(
        False, P, "Check the upstream project for new releases at start-up.")
    metrics_retention_days: int = _setting(
        90, P, "Days of local measurements kept for the Learning page.",
        minimum=7, maximum=365)

    # --- loading and saving -------------------------------------------------

    @classmethod
    def load(cls) -> Config:
        """Load the user configuration, falling back safely on bad values."""
        result = load_config()
        for problem in result.problems:
            log.warning("config: %s", problem)
        return result.config

    def save(self) -> None:
        """Validate and atomically write this configuration."""
        save_config(self)

    def problems(self) -> list[str]:
        """Return every validation problem, in plain English."""
        return _field_problems(self) + _cross_field_problems(self)

    def validate(self) -> None:
        problems = self.problems()
        if problems:
            raise ValueError(problems[0])

    @property
    def effective_presence_absence_timeout_s(self) -> int:
        """Configured absence timeout, with a compatibility fallback."""
        if self.presence_absence_timeout_s > 0:
            return self.presence_absence_timeout_s
        return self.presence_interval_s * self.presence_absent_strikes


def spec_of(name: str) -> Spec:
    """Return the :class:`Spec` for a configuration field."""
    return _SPECS[name]


def settings_specs() -> dict[str, Spec]:
    return dict(_SPECS)


_SPECS: dict[str, Spec] = {f.name: f.metadata["spec"] for f in fields(Config)}
_TYPES: dict[str, type] = {name: hint for name, hint in get_type_hints(Config).items()
                           if name in _SPECS}


def _coerce(name: str, value: Any) -> Any:
    """Convert a TOML value to the field's type or raise ``ValueError``."""
    expected = _TYPES[name]
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected is bool and isinstance(value, bool):
        return value
    if expected is int and is_number and float(value).is_integer():
        return int(value)
    if expected is float and is_number:
        return float(value)
    if expected is str and isinstance(value, str):
        return value
    raise ValueError(f"{name} must be a {expected.__name__}, got {value!r}")


def _check_field(name: str, value: Any) -> str | None:
    spec = _SPECS[name]
    if spec.choices is not None and value not in spec.choices:
        return f"{name} must be one of {', '.join(spec.choices)} (got {value!r})"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value:  # NaN
            return f"{name} must be a number"
        if spec.minimum is not None and value < spec.minimum:
            return f"{name} must be at least {spec.minimum:g} (got {value:g})"
        if spec.maximum is not None and value > spec.maximum:
            return f"{name} must be at most {spec.maximum:g} (got {value:g})"
    return None


def _field_problems(cfg: Config) -> list[str]:
    from .i18n import LANG_CODES

    problems = []
    for name in _SPECS:
        value = getattr(cfg, name)
        try:
            _coerce(name, value)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        problem = _check_field(name, value)
        if problem:
            problems.append(problem)
    if cfg.language not in LANG_CODES:
        problems.append(f"language must be one of {', '.join(LANG_CODES)} (got {cfg.language!r})")
    return problems


def _cross_field_problems(cfg: Config) -> list[str]:
    problems = []
    if not 1 <= cfg.verify_required <= cfg.verify_frames:
        problems.append("verify_required must be between 1 and verify_frames")
    if not 1 <= cfg.unlock_verify_required <= cfg.unlock_verify_frames:
        problems.append("unlock_verify_required must be between 1 and unlock_verify_frames")
    if cfg.unlock_verify_required > cfg.unlock_max_frames:
        problems.append("unlock_verify_required must not exceed unlock_max_frames")
    if cfg.adaptive_match_margin >= cfg.threshold:
        problems.append("adaptive_match_margin must be below threshold")
    if cfg.camera_low_light_fps not in (0.0, 10.0, 15.0, 20.0):
        problems.append("camera_low_light_fps must be 0, 10, 15 or 20")
    return problems


# Cross-field rules name the fields they constrain so a violation can fall
# back as a group instead of leaving an inconsistent mixture.
_CROSS_FIELD_GROUPS: tuple[tuple[str, ...], ...] = (
    ("verify_frames", "verify_required"),
    ("unlock_verify_frames", "unlock_verify_required", "unlock_max_frames"),
    ("threshold", "adaptive_match_margin"),
    ("camera_low_light_fps",),
)

# Keys written by earlier versions that are intentionally ignored. Learning
# from frames of *failed* attempts was removed: only the attempt that
# Windows confirmed may contribute a sample.
_RETIRED_KEYS = frozenset({"adaptive_pre_auth_enabled", "adaptive_pre_auth_max_frames",
                           "adaptive_pre_auth_ttl_s"})


@dataclass
class LoadResult:
    config: Config
    problems: list[str]
    source: str  # "file", "defaults"


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path.name} could not be read: {exc}") from exc


def _apply(data: dict[str, Any], base: Config, problems: list[str]) -> Config:
    values = asdict(base)
    for key, raw in data.items():
        if key not in _SPECS:
            if key not in _RETIRED_KEYS:
                problems.append(f"unknown setting {key!r} ignored")
            continue
        try:
            value = _coerce(key, raw)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        problem = _check_field(key, value)
        if problem:
            problems.append(problem)
            continue
        values[key] = value
    return Config(**values)


def load_config(path: Path = CONFIG_PATH,
                last_good_path: Path = LAST_GOOD_CONFIG_PATH) -> LoadResult:
    """Load ``path`` with per-field validation and a safe fallback.

    Invalid values are replaced by the last successfully saved value, or the
    built-in default when no last-good copy exists. Cross-field violations
    fall back as a group.
    """
    problems: list[str] = []
    fallback = Config()
    try:
        last_good = _read_toml(last_good_path)
    except ValueError:
        last_good = None
    if last_good:
        ignored: list[str] = []
        candidate = _apply(last_good, Config(), ignored)
        if not candidate.problems():
            fallback = candidate

    try:
        data = _read_toml(path)
    except ValueError as exc:
        problems.append(f"{exc}; using your last working settings")
        return LoadResult(fallback, problems, "defaults")
    if data is None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        return LoadResult(fallback, problems, "defaults")

    cfg = _apply(data, fallback, problems)
    from .i18n import LANG_CODES
    if cfg.language not in LANG_CODES:
        problems.append(f"language {cfg.language!r} is not available; using {fallback.language}")
        cfg.language = fallback.language
    for group in _CROSS_FIELD_GROUPS:
        before = _cross_field_problems(cfg)
        if not before:
            break
        trial = replace(cfg, **{name: getattr(fallback, name) for name in group})
        if len(_cross_field_problems(trial)) < len(before):
            problems.append(f"{', '.join(group)}: inconsistent values replaced by your "
                            "last working settings")
            cfg = trial
    remaining = _cross_field_problems(cfg)
    if remaining:
        problems.extend(remaining)
        cfg = fallback
    return LoadResult(cfg, problems, "file")


def dumps_config(cfg: Config) -> str:
    if tomli_w is None:
        raise RuntimeError("tomli-w is required to save the configuration")
    return tomli_w.dumps(asdict(cfg))


def save_config(cfg: Config, path: Path = CONFIG_PATH,
                last_good_path: Path = LAST_GOOD_CONFIG_PATH) -> None:
    """Validate, then atomically write the config and a last-good copy."""
    cfg.validate()
    text = dumps_config(cfg)
    write_text_atomic(path, text)
    write_text_atomic(last_good_path, text)


def default_for(name: str) -> Any:
    f = next(f for f in fields(Config) if f.name == name)
    if f.default is not MISSING:
        return f.default
    return f.default_factory()  # type: ignore[misc]
