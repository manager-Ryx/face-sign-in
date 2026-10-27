"""Is this PC ready for Face Unlock, and putting right what isn't.

Checked here, outside the service:

* the sign-in tile (the Credential Provider DLL) is registered with Windows –
  installing it needs administrator rights, so it is done through one
  Windows permission prompt (``regsvr32`` run elevated);
* Face Unlock starts when you sign in – the installer's scheduled tasks, or,
  if those are missing, the per-user ``Run`` key (no administrator rights
  needed);
* "Only allow Windows Hello sign-in" is off – when on, Windows hides
  password-based sign-in tiles, including this one;
* first-run setup has been completed.

Everything else (camera, face profile, stored password, model) is reported
by the service itself.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import winreg
from dataclasses import dataclass
from pathlib import Path

from face_service.config import STATE_DIR
from face_service.storage import read_state, write_json_atomic

log = logging.getLogger(__name__)

TILE_CLSID = "{F8A0B4D9-3C7F-4B0A-9E21-8C1B1E2B7C10}"   # credential_provider/guid.h
TILE_DLL = "FaceCredentialProvider.dll"
PROVIDERS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers"
CLSID_KEY = r"SOFTWARE\Classes\CLSID"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PASSWORDLESS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device"
TASKS = ("FaceUnlock-Service", "FaceUnlock-Presence")
RUN_VALUES = {"service": "FaceUnlockService", "tray": "FaceUnlock"}
SERVICE_EXE, TRAY_EXE = "face_service.exe", "face_unlock_tray.exe"
ONBOARDING_PATH = STATE_DIR / "onboarding.json"
ELEVATED_TIMEOUT_S = 120.0
ERROR_CANCELLED = 1223
REPO_ROOT = Path(__file__).resolve().parent.parent


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    return Path(sys.executable).parent if _frozen() else REPO_ROOT


# ---- the sign-in tile ---------------------------------------------------------

@dataclass(frozen=True)
class TileState:
    installed: bool
    dll: str = ""
    problem: str = ""


def _read_default(root: int, path: str) -> str | None:
    try:
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, "")
            return str(value)
    except OSError:
        return None


def tile_state() -> TileState:
    if _read_default(winreg.HKEY_LOCAL_MACHINE, rf"{PROVIDERS_KEY}\{TILE_CLSID}") is None:
        return TileState(False, problem="not installed")
    dll = _read_default(winreg.HKEY_LOCAL_MACHINE, rf"{CLSID_KEY}\{TILE_CLSID}\InprocServer32")
    if not dll:
        return TileState(False, problem="registered without its program file")
    if not Path(os.path.expandvars(dll)).is_file():
        return TileState(False, dll, "its program file is missing")
    return TileState(True, dll)


def bundled_tile_dll() -> Path | None:
    """The tile shipped with this copy of the app."""
    candidates = [app_dir() / "credential_provider" / TILE_DLL]
    if not _frozen():
        candidates += [REPO_ROOT / "credential_provider" / "build-next" / "Release" / TILE_DLL,
                       REPO_ROOT / "credential_provider" / "build" / "Release" / TILE_DLL]
    return next((path for path in candidates if path.is_file()), None)


def run_elevated(program: str, arguments: str, *,
                 timeout_s: float = ELEVATED_TIMEOUT_S) -> tuple[bool, str]:
    """Run a program with administrator rights after Windows asks you (UAC)."""
    import pywintypes
    import win32con
    import win32event
    import win32process
    from win32com.shell import shell, shellcon

    try:
        info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="runas",
                                    lpFile=program, lpParameters=arguments,
                                    nShow=win32con.SW_HIDE)
    except pywintypes.error as exc:
        if exc.winerror == ERROR_CANCELLED:
            return False, "Windows didn't get permission, so nothing was changed."
        return False, f"Windows couldn't start it ({exc.strerror})."
    process = info["hProcess"]
    try:
        if win32event.WaitForSingleObject(process, int(timeout_s * 1000)) != 0:
            return False, "It took too long to finish."
        code = win32process.GetExitCodeProcess(process)
    finally:
        process.Close()
    return code == 0, "" if code == 0 else f"It finished with error {code}."


def install_tile() -> tuple[bool, str]:
    """Register the shipped sign-in tile (asks for administrator permission)."""
    dll = bundled_tile_dll()
    if dll is None:
        return False, "The sign-in tile isn't included with this copy of Face Unlock."
    ok, message = run_elevated("regsvr32.exe", f'/s "{dll}"')
    if ok and not tile_state().installed:
        return False, "Windows didn't keep the registration."
    if ok:
        log.info("sign-in tile installed from %s", dll)
    return ok, message


def hello_only_sign_in() -> bool:
    """Is "Only allow Windows Hello sign-in for Microsoft accounts" on?

    It hides password-based sign-in tiles – including Face Unlock's."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, PASSWORDLESS_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "DevicePasswordLessBuildVersion")
            return int(value) == 2
    except (OSError, ValueError):
        return False


# ---- starting with Windows ------------------------------------------------------

def _task_exists(name: str) -> bool:
    try:
        result = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _run_value(name: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def autostart_state() -> str:
    """"tasks", "run-key", or "off"."""
    if all(_task_exists(name) for name in TASKS):
        return "tasks"
    if all(_run_value(name) for name in RUN_VALUES.values()):
        return "run-key"
    return "off"


def _commands() -> dict[str, list[str]] | None:
    if not _frozen():
        return None
    folder = app_dir()
    return {"service": [str(folder / SERVICE_EXE)], "tray": [str(folder / TRAY_EXE)]}


def enable_autostart() -> tuple[bool, str]:
    """Start Face Unlock at sign-in for this user (no administrator rights needed)."""
    commands = _commands()
    if commands is None:
        return False, "This is a developer copy: run tools\\register_tasks.ps1 instead."
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            for part, value_name in RUN_VALUES.items():
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ,
                                  subprocess.list2cmdline(commands[part]))
    except OSError as exc:
        return False, f"Windows refused the change ({exc.strerror})."
    return True, ""


def start_service() -> None:
    """Start the background service now: its scheduled task, or the program itself."""
    flags = subprocess.CREATE_NO_WINDOW
    if _task_exists(TASKS[0]):
        subprocess.Popen(["schtasks", "/Run", "/TN", TASKS[0]], creationflags=flags)
        return
    commands = _commands()
    if commands is not None:
        subprocess.Popen(commands["service"], cwd=app_dir(),
                         creationflags=flags | subprocess.DETACHED_PROCESS)
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), "-m", "face_service"],
                     cwd=REPO_ROOT, creationflags=flags | subprocess.DETACHED_PROCESS)


# ---- first-run setup ---------------------------------------------------------------

def onboarding_done() -> bool:
    return read_state(ONBOARDING_PATH, {"version": 1, "completed_at": None},
                      what="setup state")["completed_at"] is not None


def mark_onboarding_done() -> None:
    try:
        write_json_atomic(ONBOARDING_PATH, {"version": 1, "completed_at": time.time()})
    except OSError as exc:
        log.warning("setup state not saved: %s", exc)
