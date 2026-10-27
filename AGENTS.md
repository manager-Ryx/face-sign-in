# Instructions for AI agents

These instructions are for AI coding agents (Claude Code, Codex, Cursor,
Copilot and others). Most people install Face Unlock by asking their agent to
do it, so **Part A** is a runbook for installing and setting it up for
someone. **Part B** covers working on the code.

Face Unlock signs a person in to Windows when their webcam recognises them,
and locks the PC when they walk away. It stores their Windows password
(encrypted for their account) and pictures of their face. Treat everything
below about privacy and security as hard rules, not suggestions.

---

## Part A – installing Face Unlock for someone

### Your job

Do as much as you can yourself, and tell the person before each moment that
needs them: say exactly what they will see and what to do. They should never
have to work out a step on their own.

| You do this | Only the person can do this |
|---|---|
| Check the PC is suitable | Choose **Yes** at the Windows permission prompt (UAC) |
| Download, verify and run the installer | Type their Windows password into Face Unlock's setup window |
| Start the background service and the app | Sit in front of the camera for about a minute while it captures their face |
| Open first-run setup | Pick their preferences (auto-lock, learning) |
| Check everything works and explain what's left | Press Windows+L to try it (only when they choose to) |
| Read the logs if something goes wrong | Change Windows security settings, if one is in the way |

### Hard rules

1. **Never ask for, type, store or repeat the person's Windows password.**
   They type it into Face Unlock's own setup window and nowhere else. If they
   paste it into the chat, don't use it. Tell them it should only go into the
   app, and suggest they change it if the chat is stored somewhere.
2. **Never read, copy, print or upload their face data or stored password.**
   That means `%USERPROFILE%\.face-unlock\credentials.bin`, `embeddings.npz`,
   and the `enroll\`, `learned-photos\` and `backups\` folders. The logs
   (`service.log`, `presence.log`) and `metrics\` hold no images or passwords:
   you may read those.
3. **Never send the `unlock` command to the `\\.\pipe\FaceUnlock` pipe.** Its
   reply contains the password, and it is meant only for the Windows sign-in
   screen. `ping` and `status` are safe.
4. **Don't lock, sign out, restart or shut down the PC**, and don't close the
   person's windows, unless they've asked you to or agreed first.
5. **Don't change Windows security settings yourself.** That includes UAC,
   Windows Hello, Defender, SmartScreen, group policy and the machine-wide
   execution policy. Explain what the setting does, and let the person decide
   and change it.
6. **Don't run the installer from an elevated ("Run as administrator")
   shell.** Start it normally. Windows then asks the person, and the
   background tasks are registered for *their* account. If they enter a
   different administrator account's credentials at the prompt, that is fine.
7. Only install from this repository's
   [Releases](https://github.com/manager-Ryx/face-sign-in/releases), and only
   a file whose SHA-256 matches its published `.sha256`. `tools/install.ps1`
   checks this for you.

### Step 1 – Check the PC and tell the person the plan

Requirements: 64-bit Windows 10 or 11, a webcam, about 2 GB of free disk
space, an internet connection the first time (the face model is about
130 MB), and permission to approve an administrator prompt. The person's
Windows account must have a **password**: a PIN alone isn't enough. A
Microsoft account has one even if they normally use a PIN.

Tell them, in your own words, before you start:

> I'll download Face Unlock and check it's the genuine file. Windows will ask
> for permission once – please choose **Yes**. Then a setup window opens: it
> needs your Windows account password (typed into that window, not to me),
> about a minute in front of your camera, and a few choices. Your PIN and
> password keep working as before.

### Step 2 – Install

Download the two helper scripts and read them before you run them:

```powershell
$dir = Join-Path $env:TEMP 'FaceUnlockAgent'
New-Item -ItemType Directory -Path $dir -Force | Out-Null
foreach ($f in 'install.ps1', 'check_install.ps1') {
    Invoke-WebRequest "https://raw.githubusercontent.com/manager-Ryx/face-sign-in/main/tools/$f" -OutFile "$dir\$f" -UseBasicParsing
}
```

Then run the installer script, as the person, not elevated:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\FaceUnlockAgent\install.ps1"
```

What the script does:

1. Checks the PC: 64-bit Windows 10/11, a camera, disk space, and the
   Hello-only setting.
2. Downloads the newest release (`-Version v0.3.0-beta.1` picks a specific
   one) and checks its SHA-256.
3. Runs Setup silently. Windows shows a permission prompt, which may flash
   in the taskbar rather than come to the front.
4. Starts the background service and tray app, and opens first-run setup.

It can take several minutes, so use a long timeout or run it in the
background.

Options: `-DownloadOnly` downloads and verifies without installing.
`-SkipChecks` installs even if a check fails.

Before you run it, tell the person: "A Windows permission prompt is about to
appear. It may only flash in the taskbar. Please choose **Yes**."

- **"Setup did not finish":** they most likely chose No at the prompt, or
  closed it. Ask, then run the script again: it reuses the verified download.
- **If your shell can't show windows on their desktop** (a sandbox, a remote
  session, or a container), you can't run Setup there. Give them the two
  commands above to paste into a normal PowerShell window, and continue from
  Step 3 once it has finished.

### Step 3 – Guide them through first-run setup

The setup window opens by itself after installing. If it doesn't, run
`"C:\Program Files\WindowsFaceUnlock\face_unlock_tray.exe" --welcome`. Its
pages, and what to tell the person on each:

1. **Welcome:** what is stored on the PC, and their agreement. They read it
   and continue.
2. **Get ready:** done automatically. It starts the service, picks the
   webcam, waits for the face model download, adds face sign-in to the lock
   screen and turns on start-at-sign-in. If it asks to add the lock-screen
   tile, Windows asks for permission again. Virtual cameras (NVIDIA
   Broadcast, OBS) are never chosen. If several real cameras are listed, they
   pick the one in front of them.
3. **Password:** their Windows *account* password, not their PIN. They type
   it here themselves. The app checks it with Windows, then encrypts it for
   their account only.
4. **Your face:** about a minute, looking at the camera and turning their head
   slowly as shown. Good light on their face helps; a bright window behind
   them makes it harder. If an angle won't register, they can press **Take
   photo now** or **Skip this angle**.
5. **Preferences:** auto-lock and how long to wait, how eagerly the lock
   screen looks for them, learning, and notifications. The defaults are
   sensible.
6. **Try it:** a recognition test. *Lock my PC now* is for them to press,
   not you.

### Step 4 – Check it and explain what's left

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\FaceUnlockAgent\check_install.ps1"
```

It is read-only. It checks:

- the start-at-sign-in tasks;
- the running processes;
- the lock-screen tile;
- the Hello-only setting;
- the service's own status.

It prints `OK`/`TODO` lines and a plain-language "Still to do" list. It
exits with 0 when face sign-in is ready. Relay that list to the person.
Then suggest they try it: press Windows+L, step away for a moment, come back
and look at the camera.

### Troubleshooting

| Symptom | What to do |
|---|---|
| No Face Unlock tile on the lock screen | Run `check_install.ps1`. If it says the tile isn't installed: Face Unlock → Overview → **Add to lock screen**. If Hello-only sign-in is on, the person turns off *Settings → Accounts → Sign-in options → "For improved security, only allow Windows Hello sign-in"*. Explain that this is their choice: it's a security setting |
| Tile says "Waiting for a verified face" | They can use their PIN or password. Read `%USERPROFILE%\.face-unlock\service.log` for the reason (`no_face`, `mismatch`, `just-locked`, ...) |
| "Windows didn't accept your saved password" | Their Windows password changed. They store the new one in Face Unlock → Overview |
| Not recognised in dim light | Better light on their face. Or turn off *photo protection* in Settings: it can reject real faces in poor light. Tell them it then can't tell a photo from a face |
| First sign-in after a restart needs the PIN | Expected: the password can only be decrypted once they've signed in |
| Locked while they were at the PC | Settings → auto-lock timeout, or "Anyone in front of the camera". The Learning page shows what happened |
| Several cameras, or a camera "on a different USB port" | They confirm or choose it in the app. Face Unlock never switches cameras by itself |
| Stuck at the lock screen | *Sign-in options* → PIN or password always works. Safe Mode turns off all third-party sign-in tiles |
| Work or school PC | An organisation's policy may block third-party sign-in tiles, or require Windows Hello. The person should ask their IT department; don't work around policy |

Restart the app's background parts (tell the person first: face sign-in is
unavailable for about 20 seconds):

```powershell
Stop-ScheduledTask FaceUnlock-Presence; Stop-ScheduledTask FaceUnlock-Service
Start-ScheduledTask FaceUnlock-Service; Start-ScheduledTask FaceUnlock-Presence
```

### Uninstalling

*Settings → Apps → Windows Face Unlock → Uninstall*, or run
`"C:\Program Files\WindowsFaceUnlock\unins000.exe"`. The uninstaller asks
whether to delete their face data and stored password. That's the person's
answer to give, not yours.

### Installing from source (developers)

Only if they want to run it from a clone. This needs Python 3.11 or 3.12, and
Visual Studio Build Tools 2022 with the C++ workload for the tile.

```powershell
.\setup.ps1                                   # .venv, dependencies, start-at-sign-in tasks
.\.venv\Scripts\python -m presence_monitor --show
```

The lock-screen tile is built and registered separately; see
[INSTALL.md §5](INSTALL.md) and
[credential_provider/README.md](credential_provider/README.md). Registering it
needs an elevated prompt, which the person approves.

---

## Part B – working on the code

- **Layout.**
  - `face_service/`: Python background service (camera, recognition,
    learning, the DPAPI-encrypted password, the pipe server).
  - `presence_monitor/`: Qt tray app, setup and walk-away locking.
  - `credential_provider/`: C++ sign-in tile, loaded by LogonUI as SYSTEM.
  - `installer/`: PyInstaller and Inno Setup.
  - `docs/`: design notes ([ADAPTATION](docs/ADAPTATION.md),
    [ENGINEERING](docs/ENGINEERING.md), [ROADMAP](docs/ROADMAP.md)).
- **Checks before you finish:**
  `.\.venv\Scripts\python -m unittest discover -s tests` and
  `.\.venv\Scripts\ruff check .`. The tests use a temporary data folder and
  their own pipe name: they never touch a real profile, camera or running
  service. Keep it that way.
- **Settings:** after changing a setting in `face_service/config.py`, run
  `python -m tools.settings_table --write`. A test checks that the docs
  match.
- **The tile:**
  - Never build into `credential_provider\build` on a PC where the tile is
    registered from there: that silently replaces the live sign-in tile.
    Build into another folder (for example `build-next`).
  - Installing a tile build needs the person's agreement and an elevated
    prompt.
  - The service must keep working with older tiles: new request fields are
    optional, and their absence is handled.
- **Never** call the pipe's `unlock` command from tools or tests run against a
  real service.
- **UI previews:** `python -m tools.ui_preview out\` renders every screen to
  PNG without a camera or service.
- **Releases:** pushing a `v*` tag runs `.github/workflows/release.yml`. It
  builds the installer on GitHub and publishes it; a tag with a suffix (such
  as `-beta.1`) publishes a pre-release. ArcFace weights aren't bundled (see
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
