"""Point the app at a throwaway data folder before any test imports it.

Every test module imports this first, so no test can ever read or modify the
real profile, credentials, or settings in ``%USERPROFILE%\\.face-unlock``.
"""
import atexit
import os
import shutil
import tempfile

_home = os.environ.get("FACE_UNLOCK_HOME", "")
if not _home or not os.path.abspath(_home).startswith(os.path.abspath(tempfile.gettempdir())):
    _home = tempfile.mkdtemp(prefix="face-unlock-tests-")
    os.environ["FACE_UNLOCK_HOME"] = _home
    atexit.register(shutil.rmtree, _home, ignore_errors=True)

HOME = _home
