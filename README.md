# Face Unlock for Windows

Sign in to Windows by looking at your webcam, and have your PC lock itself
when you walk away. Everything runs locally; nothing is uploaded.

| | |
|---|---|
| Platform | Windows 10 / 11 x64 |
| Python   | 3.11 or 3.12 |
| Licence  | MIT (see the model-licence note under *Commercial use*) |
| Models   | ArcFace via DeepFace, YuNet face detector, optional MiniFASNet liveness |

## What it does

- **Face sign-in on the lock screen** through a Windows Credential Provider
  tile. When two frames match your face profile, Windows signs you in with
  your stored (DPAPI-encrypted) password. Your PIN and password always keep
  working.
- **Ready when you come back.** The sign-in screen learns when you usually
  return (from lock and sign-in times only, kept as counts) and scans
  continuously then, so you're signed in as you sit down; the rest of the time
  it takes a quick look every few seconds with the camera off in between. If
  it only caught you walking past, the PC locks again after two minutes; after
  you press Windows+L it waits until you've actually left.
- **Auto-lock when you leave.** A brief camera check every few minutes; after
  you have been away for the time you chose (one hour by default) Windows
  locks. Apps keep running. Checks pause while Windows is already locked.
- **Keeps up with you.** After Windows *confirms* a face sign-in, the single
  most useful new view of your face can be added to your profile – within
  strict limits, explained and undoable on the *Learning* page.
- **A Windows 11 style app** (Qt) with an overview, a guided face setup with a
  live preview, a recognition test, plain-English settings, and a *Learning
  and adaptation* page that shows what was learned, measured and changed.

## How it fits together

| Component | Language | Runs as | Role |
|---|---|---|---|
| `face_service` | Python | your session, at sign-in | Camera, recognition, profile, learning, DPAPI credential; answers a local named pipe |
| `presence_monitor` | Python + Qt | your session, at sign-in | Tray icon, windows, setup wizard, walk-away locking |
| `credential_provider` | C++ | LogonUI (SYSTEM) | The sign-in tile; asks the service to look for you |

The sign-in screen runs as SYSTEM in an isolated session, so the C++ tile is a
thin client that talks to the Python service over `\\.\pipe\FaceUnlock`
(similar to Howdy's PAM module on Linux).

## Important limits

- **First sign-in after a restart needs your PIN or password.** The service
  starts when you sign in, and your stored password can only be decrypted
  inside your session (DPAPI user scope). Running before sign-in would mean
  storing a secret any administrator could recover, so it is not done.
- **This is not Windows Hello.** A normal webcam cannot measure depth.
  Anti-spoofing (photo/screen detection) is optional and off in the shipped
  personal configuration; without it a good photo of you could unlock the PC.
- **No setting can guarantee every other face is rejected.** Stricter
  matching makes it less likely at the cost of asking for your PIN more often.
  Evidence collected from *you* can show you are still recognised; it cannot
  measure how often someone else would be accepted.

## Setting up (developer install)

```powershell
.\setup.ps1                                  # .venv, dependencies, sign-in tasks
.\.venv\Scripts\python -m presence_monitor --show
```

Then, in the app: **Set up** (guided face capture) and **Store password**. To
enable the lock-screen tile, build and register the Credential Provider – see
[INSTALL.md](INSTALL.md) and [credential_provider/README.md](credential_provider/README.md).

## Development

```powershell
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m unittest discover -s tests     # 200+ tests, no camera needed
.\.venv\Scripts\ruff check .
.\.venv\Scripts\python -m tools.ui_preview out\          # render every screen to PNG
.\.venv\Scripts\python -m tools.bench 5                  # time recognition checks (camera)
.\.venv\Scripts\python -m tools.evaluate_policies        # compare frame strategies
```

Tests run against a throwaway data folder and never touch your real profile,
camera, or the running service.

## Documentation

- [docs/ADAPTATION.md](docs/ADAPTATION.md) – every adaptive behaviour: benefit,
  trade-off, evidence, bounds, rollback; and every setting classified.
- [docs/ENGINEERING.md](docs/ENGINEERING.md) – measurements, the code review
  and what was fixed, open items, and commercialisation blockers.
- [INSTALL.md](INSTALL.md) – full install, configuration, troubleshooting.

## Commercial use

The code is MIT-licensed, but the ArcFace weights DeepFace downloads, and the
datasets they were trained on, are generally licensed for non-commercial
research. A paid product needs a recognition model you are licensed to ship,
code signing for the DLL and executables, a unique Credential Provider GUID,
and a privacy notice and consent flow for biometric data. See
[docs/ENGINEERING.md](docs/ENGINEERING.md).

## Credits

[serengil/deepface](https://github.com/serengil/deepface),
[OpenCV / YuNet](https://github.com/opencv/opencv_zoo),
[Qt for Python](https://doc.qt.io/qtforpython/) (LGPL-3.0),
[boltgolt/howdy](https://github.com/boltgolt/howdy) for the architecture idea,
and Microsoft's
[SampleCredentialProvider](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/CredentialProvider).

## Licence

MIT – see [LICENSE](LICENSE).
