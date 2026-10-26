# Windows Face Unlock — face login + presence auto-lock for Windows 10/11

**An open-source Windows face recognition login and walk-away auto-lock —
the kind of "Howdy for Windows" / Windows Hello alternative that stays on
your machine, runs on any off-the-shelf webcam, and you can actually read
the source of.**

Keywords: *windows face unlock, windows face login, face recognition
windows, webcam login windows, howdy windows, windows hello alternative,
credential provider face, deepface windows, arcface windows, auto lock
when away, presence monitor, walk-away lock, face id for pc.*

| | |
|---|---|
| Platform | Windows 10 / 11 x64 |
| Python   | 3.11 or 3.12 |
| License  | MIT |
| Models   | ArcFace + MiniFASNet (DeepFace) + YuNet (OpenCV Zoo) |

## Features at a glance

- **Log in with your face** from the Windows lock screen via a proper
  Credential Provider tile (C++ DLL), not a user-mode hack.
- **Walk-away auto-lock**: the tray process probes at a configurable interval;
  after the configured uninterrupted absence duration, `LockWorkStation()`
  fires. A successful enrolled-face match restarts that timer.
- **Two presence modes**: strict (must match the enrolled face, blocks
  strangers) or lightweight (any face is enough, replaces old AutoFaceLock
  scripts).
- **Anti-spoofing** (liveness, MiniFASNet) blocks flat photos and videos
  of your face.
- **Remote-session aware**: skips auto-lock when the session is RDP, or
  when TeamViewer / AnyDesk / RustDesk / Parsec / Chrome Remote Desktop /
  Quick Assist / UltraViewer hold an active remote connection.
- **Managed from the tray**: live status dashboard, settings editor,
  guided enrollment wizard with live camera preview + auto-capture, one
  Quit button that actually stops everything.
- **12-language UI** — English, Tiếng Việt, 中文, Español, Français,
  Deutsch, 日本語, 한국어, Русский, Português, العربية, हिन्दी. Switch
  from the tray, applies live.
- **DPAPI-encrypted** Windows password storage (user scope).

## Install (end user)

Grab the latest installer from the
[**Releases page**](https://github.com/caochitam/windows-face-unlock/releases)
and double-click it. Requires Windows 10/11 x64, admin rights, any webcam.

- ~350–500 MB installer — it bundles Python, TensorFlow CPU, PyTorch CPU,
  DeepFace, OpenCV, YuNet + MiniFASNet models. No other downloads.
- Not yet code-signed, so SmartScreen will say **"Unknown publisher"**.
  Click **More info → Run anyway**. Signing application to SignPath is in
  progress — when approved the signed installer will replace unsigned.
- On first launch, open the tray icon → **Enroll face** and capture ~15
  photos, then **Set Windows password** to enable lock-screen unlock.
- Updates are checked automatically against GitHub Releases; a prompt
  appears when a new version is available. You can also trigger a check
  from the tray menu (*Check for updates…*).

Building the installer from source: see
[`installer/README.md`](installer/README.md).

## Architecture

An open-source, auditable replacement for closed-source webcam-login utilities
(like `facewinunlock-tauri`). Three cooperating components:

| Component             | Language | Runs as                 | Role                                                                 |
|-----------------------|----------|-------------------------|----------------------------------------------------------------------|
| `face_service`        | Python   | User session (always)   | Camera + DeepFace + liveness + DPAPI; exposes a named pipe.          |
| `presence_monitor`    | Python   | User session (tray)     | Periodically probe presence; lock after the elapsed absence timeout. |
| `credential_provider` | C++      | LogonUI (SYSTEM)        | Windows Credential Provider tile that calls the service on unlock.   |

Plus two CLI tools: `tools.enroll` (capture reference photos) and
`tools.set_password` (store your Windows password encrypted with DPAPI).

## Why three pieces?

Windows lock-screen authentication runs in an isolated session as `SYSTEM`,
which cannot comfortably load TensorFlow / open the webcam. The C++
Credential Provider is therefore a thin shim that communicates with the
heavyweight Python service over a local named pipe. This is the same pattern
Howdy uses on Linux with PAM.

## Features

- **ArcFace (DeepFace) face recognition** with cosine-distance thresholding
- **Anti-spoofing (liveness)** via DeepFace's built-in `anti_spoofing=True`
  (MiniFASNet) — blocks flat photos of your face
- **DPAPI-protected** Windows password storage (user scope)
- **Multi-frame voting** for unlock: N out of M frames must match
- **Presence auto-lock** at a configurable interval, with *remote-context exclusion*:
  skips when the session is RDP or when TeamViewer / AnyDesk / RustDesk /
  Parsec / Chrome Remote Desktop / Quick Assist / UltraViewer are actively
  connected. Also skips when input was received recently or when paused
  from the tray icon.
- **Two presence modes** (`presence_mode` in config):
  - `recognition` (default) — enrolled face must match (strong; walk-away +
    stranger detection).
  - `detection` — *any* face in frame is enough, via YuNet (weaker; replaces
    the old standalone AutoFaceLock script).
- **System tray management UI** (pystray + tkinter) — Status dashboard,
  Settings editor, Enroll / Set-password shortcuts, Pause/Resume, and a
  single Quit that stops both service and tray cleanly.

## Requirements

- Windows 10 / 11 x64
- Python 3.11 or 3.12
- Webcam
- (For Credential Provider) Visual Studio 2022 + CMake

## Install (Python parts)

```powershell
# From this folder, in PowerShell
.\setup.ps1
```

This creates `.\.venv`, installs dependencies, writes a default config to
`%USERPROFILE%\.face-unlock\config.toml`, and registers Task Scheduler jobs
that run `face_service` and `presence_monitor` at logon.

## Enroll your face + store password

```powershell
.\.venv\Scripts\python -m tools.enroll capture --count 15
.\.venv\Scripts\python -m tools.set_password
```

Rebuild embeddings any time with:

```powershell
.\.venv\Scripts\python -m tools.enroll build
```

## (Optional) Enable the lock-screen tile

See [credential_provider/README.md](credential_provider/README.md). You will
need Visual Studio 2022. Without this, the presence-lock still works — you
just unlock with your password like usual.

## Quick test (no Credential Provider needed)

```powershell
# Terminal 1
.\.venv\Scripts\python -m face_service

# Terminal 2
.\.venv\Scripts\python -m presence_monitor
```

Trigger a manual unlock probe:

```powershell
# Named-pipe one-shot client from PowerShell
$p = New-Object IO.Pipes.NamedPipeClientStream('.', 'FaceUnlock', 'InOut')
$p.Connect(5000)
$w = New-Object IO.StreamWriter($p); $w.AutoFlush = $true
$r = New-Object IO.StreamReader($p)
$w.Write('{"cmd":"verify"}'); $p.WaitForPipeDrain()
$r.ReadToEnd()
```

## Configuration reference

See [config.example.toml](config.example.toml). Key knobs:

- `threshold` — cosine distance cutoff. Lower = stricter. Tune after enrolling.
- `verify_frames` / `verify_required` — multi-frame voting.
- `unlock_verify_frames` / `unlock_verify_required` — the faster lock-screen
  vote (default two matches from at most three frames). Requests are serialized,
  with a 100 ms to 1 second bounded retry delay, so there is no fixed FPS.
- `unlock_camera_hold_s` — when the normal camera is non-persistent, retain its
  warm handle across the slow lock-screen retries; the supplied configuration
  uses 15 seconds and releases it shortly after the retry stream stops.
- `adaptive_enrollment` — opt into bounded post-unlock embedding updates. It
  stores no new photos, preserves every original manual enrollment anchor, and
  keeps up to 300 adaptive samples in addition to the anchors. The final
  configuration sets `adaptive_retention_days = 0`, so age never removes a
  sample; when the cap is reached, newer accepted samples replace the oldest
  adaptive samples. Both values are configurable with
  `adaptive_max_embeddings` and `adaptive_retention_days`. An ArcFace
  profile at the default cap is roughly 0.6 MiB; comparisons are vectorized,
  so camera capture and face inference remain the meaningful costs.
- `adaptive_pre_auth_enabled` — retain safe embedding candidates from recent
  failed lock-screen votes in memory only. They are capped by
  `adaptive_pre_auth_max_frames` (final: 24) and
  `adaptive_pre_auth_ttl_s` (final: 45 seconds), then discarded. If a later
  face attempt is accepted by Windows, the candidates may be committed through
  the same immutable-anchor quality gates; failed/unreported attempts never
  train the profile.
- `presence_interval_s` — how often to probe (default 60).
- `presence_absence_timeout_s` — minimum uninterrupted time without a
  successful enrolled-face match before locking. Successful matches restart
  the monotonic timer; failed/skipped probes reset it rather than locking
  blindly. Set to `0` only for legacy `interval * strikes` behaviour.
- `persistent_camera = false` — release the webcam after every request. The
  recognition model remains idle between probes and is not run continuously.
- Selecting the Face Unlock tile or pressing Enter while it has no ready
  credential re-arms one fresh worker, closes a stale request/camera lease, and
  retries with a one-second debounce. This is the low-resource wake path; no
  separate motion detector is needed.

## Security notes

Read these before trusting the CP for daily unlock:

1. The stored Windows password is encrypted with **DPAPI user-scope**. That
   protects it from other users and from offline disk inspection, but **not**
   from malware running as you. If your attacker model includes that, use a
   smart card or Windows Hello proper.
2. The pipe DACL allows only the enrolled Windows user and LocalSystem.
   Plaintext credentials still cross that local pipe briefly after a verified
   match, so protect the user session from same-user malware.
3. Anti-spoofing blocks 2-D photos but **not 3-D masks or deepfake screens**.
4. The credential provider skeleton uses a hard-coded GUID from this repo —
   **generate your own** before sharing builds.
5. Adaptive enrollment is deliberately conservative: it requires exactly one
   sharp, sufficiently large, high-confidence face; a match materially inside
   the threshold against an immutable manual anchor; and a non-duplicate
   embedding. Samples are staged before credential serialization and committed
   atomically only if Credential Provider `ReportResult` reports success.
   Failed attempts, password/PIN unlocks, and unacknowledged face attempts add
   nothing. Some successful Windows flows may omit that callback, in which case
   the staged sample expires rather than risking profile poisoning. Adaptive
   samples roll out after 14 days by default; original manual anchors never do.

## Credits / prior art

This project draws on ideas from:

- [boltgolt/howdy](https://github.com/boltgolt/howdy) — Linux/PAM face login
- [serengil/deepface](https://github.com/serengil/deepface) — recognition + liveness
- [ageitgey/face_recognition](https://github.com/ageitgey/face_recognition) — dlib wrapper
- Microsoft's [SampleCredentialProvider](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/CredentialProvider)
  — reference implementation for `ICredentialProvider`

## Contributing

Issues and PRs are welcome. Before sending a PR please:

- Run `python -m tools.bench` if you touched the recognizer / camera paths.
- Keep new UI strings translatable — add keys to
  [`face_service/i18n.py`](face_service/i18n.py) under all 12 languages
  (English fallback is automatic if a key is missing).
- Generate your own GUID in `credential_provider/guid.h` if you're going
  to register the CP DLL on your machine.

## License

MIT — see [LICENSE](LICENSE).
