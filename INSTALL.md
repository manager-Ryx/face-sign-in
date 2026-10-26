# Face Unlock – install and operations guide

## 0. Prerequisites

| Requirement | Tested | Install |
|---|---|---|
| Windows 10/11 x64 | 11 Pro 26200 | – |
| Python 3.11 or 3.12 | 3.12.10 | `winget install Python.Python.3.12` |
| VS Build Tools 2022 (C++ workload) | 17.14 | only for the sign-in tile, see §5 |
| CMake | 3.31 (bundled with Build Tools) | – |

## 1. Python setup

```powershell
.\setup.ps1     # creates .venv, installs requirements.txt, registers the sign-in tasks
```

`requirements.txt` pulls DeepFace, OpenCV, TensorFlow/tf-keras, PyTorch (only
for optional anti-spoofing), pywin32, psutil, tomli-w, cv2-enumerate-cameras
and PySide6-Essentials (the app's interface, LGPL-3.0).

Optional, to avoid a slow first run, pre-download the model weights into
`%USERPROFILE%\.deepface\weights\`: `arcface_weights.h5` (131 MB) and, for
anti-spoofing, the two MiniFASNet files. See `installer\download_weights.py`.

## 2. First run

```powershell
.\.venv\Scripts\pythonw -m presence_monitor --show
```

In the app:

1. **Set up** – a guided capture: good light in front of you, look at the
   camera, then turn your head slightly as asked. About a minute. Nothing
   replaces your profile until you select **Save face profile**.
2. **Store password** – your Windows *account* password (not your PIN). It is
   checked with Windows, then encrypted with DPAPI for your account only.
3. **Settings → Camera** – choose your webcam. Only that physical device is
   ever used; virtual cameras (NVIDIA Broadcast, OBS) can't be chosen.

## 3. Day-to-day behaviour

- **Lock screen.** The tile asks the service to look for you. The first
  minute after the lock screen appears it scans continuously; if nobody is in
  view it then takes a quick look every 10 seconds with the camera off in
  between, and while the display is off it doesn't use the camera at all.
  Pressing a key or clicking the tile scans immediately.
- **Frames.** One frame first; up to five when needed. Two matching frames
  sign you in. "No face" stops after three cheap looks and a clear mismatch
  after two, so nobody is kept waiting by the camera.
- **Camera.** Opened only for a check and released straight away (held for a
  couple of seconds between lock-screen retries). It is used as soon as its
  image is stable instead of after a fixed number of frames.
- **Auto-lock.** A check every 5 minutes; Windows locks after about an hour
  without seeing you. Checks are skipped while Windows is locked, during
  remote sessions, and when the camera is lent to the setup window; a skipped
  or failed check never counts as you being away.

## 4. Data folder (`%USERPROFILE%\.face-unlock`)

| Path | Contents |
|---|---|
| `config.toml`, `config.last-good.toml` | Your settings and the last copy that loaded cleanly |
| `embeddings.npz` | Face profile (numeric face signatures; original enrollment plus learned samples) |
| `credentials.bin` | Your Windows password, DPAPI-encrypted |
| `enroll\` | Your reference photos |
| `state\` | Automatic tuning (`adaptation.json`), camera measurements, held-out evaluation samples, optional away-time counts |
| `metrics\` | Local measurements for the Learning page (rotated; never images) |
| `backups\` | Copies made before replacing photos or the profile |
| `service.log`, `presence.log` | Logs, rotated at 2 MB (3 kept) |

## 5. The sign-in tile (Credential Provider)

The registered DLL is loaded by the sign-in screen as SYSTEM. **Do not run
`cmake --build build` in place on a machine where the tile is registered from
`credential_provider\build\Release`** – that silently replaces the live tile.

```powershell
cd credential_provider
cmake -S . -B build-next -A x64 -G "Visual Studio 17 2022"
cmake --build build-next --config Release
# Elevated PowerShell, when you're ready (keeps the old DLL for rollback):
.\update_provider.ps1
.\update_provider.ps1 -Rollback     # if anything looks wrong
```

First-time registration on a new machine: `.\register.ps1 -Action register`
(elevated). Generate your own GUID in `guid.h` before distributing a build.

## 6. Restarting after an update

```powershell
powershell -ExecutionPolicy Bypass -File tools\clean_restart.ps1
```

This stops both tasks and their Python processes and starts them again.
Face sign-in is unavailable for the ~10 seconds the model takes to load.

## 7. Restoring the previous version

- Code: `git switch master` (the redesign lives on `redesign-adaptive-ui`), or
  the full copy in `..\windows-face-unlock-backups\<timestamp>\source`. That
  folder also holds `pip-freeze.txt` (Python packages), the two scheduled-task
  definitions, a copy of the installed sign-in tile DLL, and
  `uncommitted.patch` (work that was uncommitted before the redesign).
- Settings and profile: `%USERPROFILE%\.face-unlock\backups\before-redesign-<timestamp>\`
  holds the `config.toml` and `embeddings.npz` from before the redesign;
  later settings changes leave a `config-before-*.toml` copy beside it.
- In-app: *Learning → Changes → Restore earlier settings* returns to a saved
  snapshot; *Undo the last 24 hours* and *Reset learned data* undo learning
  without touching your original photos; *Forget my routine* clears what the
  sign-in screen learned about when you come back.

## 8. Troubleshooting

| Symptom | What to do |
|---|---|
| "Your camera is on a different USB port" | Confirm it on the Overview page. Nothing switches cameras on its own. |
| "Your face is too dark" | Face a light; low-light mode turns itself on only after measuring that it helps your camera. |
| Face sign-in is slow after a long time locked | Expected: the camera starts fresh (about 0.6 s, longer in dim light). |
| Tile shows "Waiting for a verified face" | Use another sign-in option; check the app's Overview for the reason. |
| Stuck at the lock screen | "Sign-in options" → PIN or Password always works. Safe Mode disables third-party tiles. |

## 9. Uninstall

```powershell
# Elevated PowerShell
.\credential_provider\register.ps1 -Action unregister
Unregister-ScheduledTask -TaskName 'FaceUnlock-Service' -Confirm:$false
Unregister-ScheduledTask -TaskName 'FaceUnlock-Presence' -Confirm:$false
Remove-Item -Recurse -Force "$env:USERPROFILE\.face-unlock"
```
