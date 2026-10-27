"""Pre-download model weights into the build tree, to be bundled.

MiniFASNet (photo protection, Apache-2.0) is always bundled, so it works
offline. ArcFace is bundled only with BUNDLE_ARCFACE=1: its weights are
generally licensed for non-commercial research, so public releases leave
them out and the app downloads them once, on first start (about 130 MB).

Targets:
    models/weights/arcface_weights.h5
    models/weights/2.7_80x80_MiniFASNetV2.pth
    models/weights/4_0_0_80x80_MiniFASNetV1SE.pth
    models/face_detection_yunet_2023mar.onnx  (already bundled by git)
"""
from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_DIR = REPO_ROOT / "models" / "weights"

FILES = [
    # (url, relative_path, expected_min_bytes)
    (
        "https://github.com/serengil/deepface_models/releases/download/v1.0/arcface_weights.h5",
        WEIGHTS_DIR / "arcface_weights.h5",
        130_000_000,
    ),
    (
        "https://github.com/minivision-ai/Silent-Face-Anti-Spoofing/raw/master/resources/anti_spoof_models/2.7_80x80_MiniFASNetV2.pth",
        WEIGHTS_DIR / "2.7_80x80_MiniFASNetV2.pth",
        1_500_000,
    ),
    (
        "https://github.com/minivision-ai/Silent-Face-Anti-Spoofing/raw/master/resources/anti_spoof_models/4_0_0_80x80_MiniFASNetV1SE.pth",
        WEIGHTS_DIR / "4_0_0_80x80_MiniFASNetV1SE.pth",
        1_500_000,
    ),
]


def _fetch(url: str, dest: Path) -> None:
    print(f"  -> {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "wfu-installer-builder"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        seen = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            seen += len(chunk)
            if total:
                pct = seen * 100 // total
                print(f"\r     {seen // 1024:>10} KB / {total // 1024} KB ({pct}%)",
                      end="", flush=True)
        print()


def main() -> int:
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    rc = 0
    bundle_arcface = os.environ.get("BUNDLE_ARCFACE") == "1"
    for url, dest, min_bytes in FILES:
        if dest.name == "arcface_weights.h5" and not bundle_arcface:
            print(f"[skip] {dest.name}: downloaded by the app on first start "
                  "(set BUNDLE_ARCFACE=1 to ship it)")
            continue
        if dest.exists() and dest.stat().st_size >= min_bytes:
            print(f"[skip] {dest.name} already present ({dest.stat().st_size} B)")
            continue
        print(f"[get ] {dest.name}")
        try:
            _fetch(url, dest)
        except Exception as e:
            print(f"ERROR downloading {url}: {e}", file=sys.stderr)
            rc = 1
            continue
        if dest.stat().st_size < min_bytes:
            print(f"ERROR {dest.name} looks truncated ({dest.stat().st_size} B)",
                  file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
