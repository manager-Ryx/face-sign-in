from __future__ import annotations
import os
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

try:
    import tomli_w  # type: ignore
except ImportError:  # pragma: no cover - optional for read-only usage
    tomli_w = None  # type: ignore

APP_DIR = Path(os.environ.get("FACE_UNLOCK_HOME", Path.home() / ".face-unlock"))
CONFIG_PATH = APP_DIR / "config.toml"
ENROLL_DIR = APP_DIR / "enroll"
EMBED_PATH = APP_DIR / "embeddings.npz"
CREDS_PATH = APP_DIR / "credentials.bin"
LOG_PATH = APP_DIR / "service.log"

PIPE_NAME = r"\\.\pipe\FaceUnlock"

PRESENCE_MODES = ("recognition", "detection")


def _default_language() -> str:
    # Imported here to avoid a circular import (i18n → config is fine,
    # but we only need the detection helper at runtime).
    from .i18n import detect_system_language
    return detect_system_language()


@dataclass
class Config:
    model_name: str = "ArcFace"
    detector_backend: str = "yunet"  # yunet is fast + robust. Alternatives: opencv, retinaface
    distance_metric: str = "cosine"
    threshold: float = 0.45          # ArcFace cosine
    anti_spoofing: bool = True
    camera_device_id: str = ""  # strict USB device identity; no fallback to other cameras
    camera_index: int = 0
    camera_warmup_frames: int = 10   # discard N frames after opening for auto-exposure
    persistent_camera: bool = True   # keep VideoCapture open between requests
    verify_frames: int = 5            # số frame cần đạt ngưỡng
    verify_required: int = 2          # trong đó cần ≥ N khớp (giảm từ 3 để nhanh hơn)
    # Lock-screen verification is separately tunable: two matches out of three
    # gives fast response without reducing the normal identity threshold.
    unlock_verify_frames: int = 3
    unlock_verify_required: int = 2
    # With persistent_camera=false, keep it open only across active lock-screen
    # retries, then release it shortly after the requests stop.
    # Keep the webcam open across the slow (ArcFace) lock-screen retries. A
    # retry takes roughly 6 seconds on this machine; 15 seconds avoids
    # repeatedly reopening and re-warming the camera after a long idle period,
    # while still releasing it shortly after the worker stops.
    unlock_camera_hold_s: float = 15.0
    adaptive_enrollment: bool = False
    adaptive_samples_per_unlock: int = 2
    # During a lock-screen attempt, retain only safe embedding candidates from
    # recent failed votes. They are committed only if a later face attempt is
    # accepted by Windows; raw frames are never retained.
    adaptive_pre_auth_enabled: bool = True
    adaptive_pre_auth_max_frames: int = 24
    adaptive_pre_auth_ttl_s: float = 45.0
    # Adaptive-only cap. Manual anchors are stored in addition to this limit.
    adaptive_max_embeddings: int = 300
    # Adaptive samples older than this rolling window are evicted. Zero keeps
    # them indefinitely while still enforcing adaptive_max_embeddings.
    adaptive_retention_days: float = 14.0
    adaptive_match_margin: float = 0.10
    adaptive_min_diversity: float = 0.025
    adaptive_min_face_confidence: float = 0.90
    adaptive_min_face_area_ratio: float = 0.04
    adaptive_min_blur: float = 40.0
    presence_interval_s: int = 60
    # Elapsed time without a successful enrolled-face match before locking.
    # Zero preserves old configs by deriving the timeout from interval * strikes.
    presence_absence_timeout_s: int = 0
    presence_absent_strikes: int = 2  # vắng mặt liên tiếp trước khi lock
    # "recognition" = DeepFace ArcFace must match enrolled face (stronger; walk-away + strangers)
    # "detection"   = YuNet any-face-in-frame is enough (weaker; mimics old AutoFaceLock)
    presence_mode: str = "recognition"
    warmup_on_start: bool = True
    # UI language code (see face_service.i18n.LANGUAGES). Auto-detected
    # from the system locale on first run if the config file is missing.
    language: str = field(default_factory=_default_language)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            APP_DIR.mkdir(parents=True, exist_ok=True)
            return cls()
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        if tomli_w is None:
            raise RuntimeError("tomli-w is required to save config (pip install tomli-w)")
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            tomli_w.dumps(asdict(self)),
            encoding="utf-8",
        )

    @property
    def effective_presence_absence_timeout_s(self) -> int:
        """Configured absence timeout, with a compatibility fallback."""
        if self.presence_absence_timeout_s > 0:
            return self.presence_absence_timeout_s
        return self.presence_interval_s * self.presence_absent_strikes

    def validate(self) -> None:
        from .i18n import LANG_CODES
        if self.presence_mode not in PRESENCE_MODES:
            raise ValueError(
                f"presence_mode must be one of {PRESENCE_MODES}, got {self.presence_mode!r}"
            )
        if self.presence_interval_s < 5:
            raise ValueError("presence_interval_s must be >= 5")
        if self.presence_absent_strikes < 1:
            raise ValueError("presence_absent_strikes must be >= 1")
        if self.presence_absence_timeout_s < 0:
            raise ValueError("presence_absence_timeout_s must be >= 0")
        if not (0.0 < self.threshold < 2.0):
            raise ValueError("threshold must be in (0, 2)")
        if self.verify_frames < 1 or not (1 <= self.verify_required <= self.verify_frames):
            raise ValueError("verify_required must be between 1 and verify_frames")
        if self.unlock_verify_frames < 1 or not (
            1 <= self.unlock_verify_required <= self.unlock_verify_frames
        ):
            raise ValueError(
                "unlock_verify_required must be between 1 and unlock_verify_frames"
            )
        if not (0.0 <= self.unlock_camera_hold_s <= 30.0):
            raise ValueError("unlock_camera_hold_s must be between 0 and 30")
        if self.adaptive_samples_per_unlock < 0:
            raise ValueError("adaptive_samples_per_unlock must be >= 0")
        if self.adaptive_pre_auth_max_frames < 0:
            raise ValueError("adaptive_pre_auth_max_frames must be >= 0")
        if not (0.0 <= self.adaptive_pre_auth_ttl_s <= 300.0):
            raise ValueError("adaptive_pre_auth_ttl_s must be between 0 and 300")
        if self.adaptive_max_embeddings < 1:
            raise ValueError("adaptive_max_embeddings must be >= 1")
        if self.adaptive_retention_days < 0.0:
            raise ValueError("adaptive_retention_days must be >= 0")
        if not (0.0 <= self.adaptive_match_margin < self.threshold):
            raise ValueError("adaptive_match_margin must be >= 0 and below threshold")
        if not (0.0 <= self.adaptive_min_diversity < 2.0):
            raise ValueError("adaptive_min_diversity must be in [0, 2)")
        if not (0.0 <= self.adaptive_min_face_confidence <= 1.0):
            raise ValueError("adaptive_min_face_confidence must be in [0, 1]")
        if not (0.0 <= self.adaptive_min_face_area_ratio <= 1.0):
            raise ValueError("adaptive_min_face_area_ratio must be in [0, 1]")
        if self.adaptive_min_blur < 0.0:
            raise ValueError("adaptive_min_blur must be >= 0")
        if self.language not in LANG_CODES:
            raise ValueError(
                f"language must be one of {LANG_CODES}, got {self.language!r}"
            )
