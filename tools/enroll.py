"""Command-line enrollment (the app's setup wizard is the recommended way).

Usage:
  python -m tools.enroll build              # rebuild the profile from the enroll folder
  python -m tools.enroll from-dir PATH      # copy photos from PATH, then rebuild

Rebuilding replaces the original enrollment and clears learned samples; the
current profile file is backed up first.
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from face_service.config import BACKUP_DIR, EMBED_PATH, ENROLL_DIR, Config
from face_service.recognizer import IMAGE_SUFFIXES, Recognizer


def backup_profile() -> None:
    if EMBED_PATH.exists():
        target = BACKUP_DIR / f"embeddings-{time.strftime('%Y%m%d-%H%M%S')}.npz"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(EMBED_PATH, target)
        print(f"Previous profile backed up to {target}")


def build() -> None:
    backup_profile()
    count = Recognizer(Config.load()).enroll_from_dir(ENROLL_DIR)
    print(f"Built the face profile from {count} photos.")


def from_dir(source: Path) -> None:
    ENROLL_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    for photo in Path(source).iterdir():
        if photo.suffix.lower() in IMAGE_SUFFIXES:
            shutil.copy2(photo, ENROLL_DIR / photo.name)
            copied += 1
    print(f"Copied {copied} photos into {ENROLL_DIR}")
    build()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build")
    copy = commands.add_parser("from-dir")
    copy.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build()
    else:
        from_dir(args.path)


if __name__ == "__main__":
    main()
