# Face Unlock for Windows

Sign in to Windows by looking at your webcam, and have your PC lock itself
when you walk away. Everything runs locally; nothing is uploaded.

| | |
|---|---|
| Platform | Windows 10 / 11 x64 |
| Python   | 3.11 or 3.12 |
| Licence  | MIT (see the model-licence note under *Commercial use*) |
| Models   | ArcFace via DeepFace, YuNet face detector, optional MiniFASNet liveness |

## Install

1. Download `WindowsFaceUnlock-Setup-<version>.exe` from
   [Releases](https://github.com/manager-Ryx/face-sign-in/releases).
2. Run it. The installer isn't code-signed yet, so SmartScreen may say
   "Windows protected your PC": choose **More info → Run anyway**. Setup asks
   for administrator rights, because it adds the face sign-in tile to the lock
   screen.
3. When it finishes, first-run setup opens and walks you through the rest:
   camera, your Windows account password (not your PIN), about a minute of
   face capture, and a few preferences. The first start downloads the face
   model once (about 130 MB), so be online for it.
4. Try it: press Windows+L, step away for a moment, come back and look at the
   camera.

Your PIN and password always keep working. To remove it: *Settings → Apps →
Windows Face Unlock → Uninstall*. Starting Windows in Safe Mode also turns
off third-party sign-in tiles.

**Installing with an AI agent?** Ask it something like *"Install Face Unlock
from https://github.com/manager-Ryx/face-sign-in on this PC, following its
AGENTS.md."* [AGENTS.md](AGENTS.md) tells Claude Code, Codex and other agents
how to do everything they can by themselves. It also tells them to guide you
through the few steps only you can do (the Windows permission prompt, your
password, your face) and never to ask for your password.
[`tools/check_install.ps1`](tools/check_install.ps1) checks an installation
and says what's left to do.

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
  most useful new view of your face can be added to your profile. If it failed
  and you used your password, it learns the views it missed; while you sit at
  the PC it fills in times of day it hasn't seen. All within strict limits,
  explained and undoable on the *Learning* page.
- **Copes with a bright window behind you.** It raises the camera's
  brightness for your face (and puts it back afterwards), and can hold a
  fixed focus for your desk.
- **A Windows 11 style app** (Qt) with an overview, a guided face setup with a
  live preview, a recognition test, plain-English settings, and a *Learning
  and adaptation* page that shows what was learned, measured and changed.
- **Friendly setup.** First-run setup shows a live "Is this you?" camera
  check, so you can see the right webcam was picked (and switch). Face
  capture shows which way to turn with a little animated face, flashes and
  clicks softly for each photo (mute it with one click), and a lighting meter
  says when it's a bit dark or too bright behind you.
- **Feedback everywhere.** Buttons show when they're working ("Checking…"),
  results appear right where you clicked, each saved setting gets a tick,
  settings can be searched (Ctrl+F) and put back to their default, and
  undoable actions offer **Undo**. The window remembers its size and place.
- **Notifications that lead to the fix.** Select "Windows didn't accept your
  saved password" and the password window opens; the same for adding face
  sign-in to the lock screen and the Windows Hello-only setting. A cheerful
  "Signed in by face in 0.8 s" after your first face sign-in, and at most a
  weekly "saved you 12 PIN entries" (both follow the notifications setting).
- **Copy diagnostics** (Advanced, About, or the tray menu): one report for
  getting help, copied and saved as a zip in Downloads. Never your password,
  face data or photos; your account name, email, PC name and camera path are
  masked.

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
  Photo protection (a liveness check for photos and screens) is on by
  default; if it turns you away in poor light you can switch it off in
  Settings, but then a good photo of you could unlock the PC.
- **No setting can guarantee every other face is rejected.** Stricter
  matching makes it less likely at the cost of asking for your PIN more often.
  Evidence collected from *you* can show you are still recognised; it cannot
  measure how often someone else would be accepted.

## Setting up (developer install)

```powershell
.\setup.ps1                                  # .venv, dependencies, sign-in tasks
.\.venv\Scripts\python -m presence_monitor --show
```

First-run setup opens by itself and walks through the rest: it starts the
service, picks your webcam, adds face sign-in to the lock screen (one
Windows permission prompt), stores your password, captures your face and
asks a few preferences. See [INSTALL.md](INSTALL.md) and
[credential_provider/README.md](credential_provider/README.md).

## Development

```powershell
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m unittest discover -s tests     # 300+ tests, no camera needed
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
- [docs/ROADMAP.md](docs/ROADMAP.md) – what's needed before a public release,
  and ranked ideas for features, tweaks, systems and UI.

## Commercial use

The code is MIT-licensed, but the ArcFace weights DeepFace downloads, and the
datasets they were trained on, are generally licensed for non-commercial
research. A paid product needs a recognition model you are licensed to ship,
code signing for the DLL and executables, a unique Credential Provider GUID,
and a privacy notice and consent flow for biometric data. See
[docs/ENGINEERING.md](docs/ENGINEERING.md).

## Credits

Grew from [caochitam/windows-face-unlock](https://github.com/caochitam/windows-face-unlock)
by Cao Chí Tâm. Built with
[serengil/deepface](https://github.com/serengil/deepface),
[OpenCV / YuNet](https://github.com/opencv/opencv_zoo),
[Qt for Python](https://doc.qt.io/qtforpython/) (LGPL-3.0),
[boltgolt/howdy](https://github.com/boltgolt/howdy) for the architecture idea,
and Microsoft's
[SampleCredentialProvider](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/CredentialProvider).

## Licence

MIT – see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
