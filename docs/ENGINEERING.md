# Engineering notes

## Measurements (this PC: 28 cores, NearStream V02AF, evening light)

| What | Before | After | How measured |
|---|---|---|---|
| Camera open to first frame | 1.77 s (width/height re-set after open) | 0.64 s | 3 runs each, DirectShow, `tools`-style scripts |
| Fixed warm-up after opening | 10 frames ≈ 0.6 s | stops when stable: 3 frames ≈ 0.1 s | same |
| Cold lock-screen attempt | 2.27 s median (camera 1.9-2.1 s) | measure live: open 0.64-0.80 s + settle 0.1 s + analysis | old code in-process vs new service in-process |
| ArcFace per face | 330-400 ms (1 thread, eager) | 87 ms (4 threads, compiled); 62 ms at 8 | 15-20 repetitions; outputs identical (max element difference 4e-7) |
| Service memory, model loaded | 828 MB in use, 1,098 MB committed | 561 MB in use, 571 MB committed | live service; cost: 96 -> 110 ms per face with TensorFlow's oneDNN kernels off (32 synthetic faces x 2 runs); outputs within 2e-7 cosine |
| Tray memory | 785 MB committed (numpy BLAS reserves a buffer per CPU core) | 45 MB committed | live tray; one BLAS thread in both processes |
| Failed attempt with nobody in view (warm camera) | 3 frames analysed | ~60-75 ms, detector only | new service in-process |
| Lock-screen camera use while away | on continuously (one request ~6 s for up to 25 h) | continuous only when you're likely back soon (learned), otherwise a ~1 s look every few seconds | service log history; pacing and routine tests |
| Returns caught while already scanning (simulated routine) | fixed 30 min window: 43 %, 60 min: 65 % | learned routine: 100 % at 14–15 % of locked time | `tests/test_routine.py` generator, 4 weeks learned / 1 tested, 3 seeds |
| Camera on while the model loads after a restart | 8.2 s (first presence check during warm-up) | 0 s: camera waits for the model | service log 20:33:39–48; start-up gate test |
| Metrics written by the lock screen | one record per request (~1/s scanning, every few s in quick looks) | one record per lock, plus attempts that saw a face | service tests |
| Face brightness in dim light | 46 (30 fps) | 72-90 (15 fps), 94-105 (10 fps) | YuNet face box, 20 frames per run |
| Software brightening (CLAHE) | – | made match distances worse in every run; not used | 15 sampled frames per configuration |
| Media Foundation backend | – | 12.8-17.1 s to open; only used if DirectShow can't see the camera | 1 run each |

Not yet measured live (needs you in front of the camera and a real lock/unlock):
the end-to-end time from waking the screen to signing in, and whether
Windows reports the display-off state on the lock screen as expected (both
are recorded automatically in `metrics\service.jsonl` once the new service
runs).

## Code review – findings and status

From a full read plus an independent review. "Fixed" items have tests.

| Finding | Status |
|---|---|
| Any program running as you could ask the pipe for your password while your face was visible | **Fixed**: only the Windows sign-in screen, identified as SYSTEM, may request it (`unlock_client_check = enforce`, the default since live sign-ins confirmed the identification; tested). Same-user malware can still decrypt DPAPI directly – inherent to the design. |
| Pipe accept loop could be stalled by one silent client; shutdown hung; client reads had no timeout | **Fixed** (overlapped I/O with deadlines; tested) |
| Config never validated at start-up (e.g. `threshold = 1.9` accepted any face) | **Fixed** (typed, bounded loading with last-known-good fallback; tested) |
| `pause_camera` unbounded (could disable auto-lock forever) | **Fixed** (≤ 10 min, renewable lease; tested) |
| Missing profile caused hourly blind locks | **Fixed** (reported as an error, never as absence; tested) |
| Profile state mutated from several threads without a lock | **Fixed** (immutable profile swapped atomically under a lock) |
| tkinter windows in several threads with several interpreters | **Fixed** (one Qt GUI thread; work on a thread pool) |
| Logs grew without limit (16 MB) | **Fixed** (2 MB × 3 rotation; bounded JSONL metrics) |
| Lock-screen looks recorded one by one would rotate months of measurements out within hours; camera start-up estimates written to disk on every look | **Fixed** (counted per lock; start-up samples at most every 10 min; estimates saved at most once a minute) |
| A presence check at start-up held the camera on for ~8 s while the model loaded | **Fixed** (camera users wait for the model first; tested) |
| Four slightly different copies of the "write a state file safely" code | **Fixed** (one helper, `face_service/storage.py`, with fsync and temp-file clean-up) |
| Settings edited outside the app could be overwritten by the tray's stale copy | **Fixed** (the tray watches `config.toml` and reloads; tested) |
| A face recognised while walking past (screens off) left the PC unlocked and unattended until the hour-long auto-lock | **Fixed** (a face sign-in must be confirmed by a presence check after 30 s; otherwise the PC locks again after 2 min; tested) |
| Windows+L while still facing the camera was undone about 3.6 s later | **Fixed** (after a lock of your own, face sign-in waits until you have stepped away, woken the screen or selected the tile; tested) |
| Presence checks were skipped for 35 s after a sign-in, and the absence timer only started at the next 5-minute check | **Fixed** (the sign-in screen releases the camera as soon as you are signed in) |
| The caller check and request log wrote one entry per lock-screen request (9,186 records and ~900 log lines an hour overnight) | **Fixed** (one caller record per lock, plus any other caller; per-request lines at debug level) |
| Learning from frames of failed attempts | **Removed** (brief: failed attempts never train) |
| Credential Provider: DLL could be unloaded while its worker ran; self-UnAdvise left the tile dead; registration errors ignored; CLSID duplicated as strings | **Fixed in source, compiled to `build-next`, not installed** (`credential_provider\update_provider.ps1`, with rollback) |
| Pipe instance squatting by same-user processes | **Open** – needs a SYSTEM-owned service or client-side server verification in the tile |
| Plain-text password briefly in Python memory | **Accepted limit** of a user-mode service; a commercial design should move the secret out of user mode |
| Updater pointed at the upstream repo and ran installers without a mandatory checksum | **Mitigated**: automatic checks off by default with a warning; still needs fail-closed checksums and Authenticode checks before any release channel |
| Installer created a world-writable data folder and registered tasks for the elevated account | **Fixed in `installer.iss`** (not rebuilt here) |
| Interface translations | **Deferred**: new screens are English; the i18n mechanism remains |

## Performance notes for the future

- **Where the service's memory goes** (measured step by step): Python, numpy
  and OpenCV 37 MB; importing TensorFlow +223 MB; building ArcFace through
  DeepFace +216 MB with oneDNN off (+478 MB with it on) for 130 MB of weights;
  the YuNet detector +27 MB.

- **ONNX Runtime** for ArcFace (and MiniFASNet) would remove TensorFlow and
  PyTorch from the service: faster start (TensorFlow import alone is 2.5-9 s),
  far lower memory, and a much smaller installer. Keep parity tests: embeddings
  must match the existing enrollment.
- **Detection** runs on a padded, down-scaled image inside DeepFace (for
  alignment parity). A native pipeline would detect faces at full resolution,
  but changes embeddings slightly – it requires re-enrollment or migration.

## Before selling it

1. **Model licence.** ArcFace weights downloaded by DeepFace (and MS1M/Glint
   training data) are generally non-commercial. Ship a model you are licensed
   to sell; MiniFASNet weights need checking too.
2. **Signing.** Sign the Credential Provider DLL and both executables (OV/EV
   certificate); LogonUI loading an unsigned DLL is a non-starter for
   businesses.
3. **Unique CLSID** in `credential_provider/guid.h` (now the only place it's
   defined).
4. **Architecture.** For enterprise customers, run the matching service as a
   Windows service, keep the secret out of user mode (or replace password
   replay with certificate-based logon), and verify the pipe server from the
   tile (the service already refuses callers other than the sign-in screen).
5. **Privacy and consent.** Biometric data (GDPR Art. 9, Illinois BIPA, Texas
   CUBI): EULA, privacy notice, explicit consent at setup, retention policy and
   deletion – the app already stores everything locally and offers reset.
6. **Release channel.** Own update server, signed releases, fail-closed
   verification.
7. **Licences of dependencies.** Qt for Python is LGPL-3.0 (dynamic linking,
   provide notices); add a third-party notices file.
