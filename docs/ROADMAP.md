# Roadmap

What is still needed before a public release, then ideas ranked by value
for effort. "Done" items are listed at the end for reference.

## Before a public release (must)

1. **A recognition model you may sell.** DeepFace's ArcFace weights (trained
   on MS1M/Glint-style data) are generally licensed for non-commercial use.
   Ship a commercially licensed model, ideally as ONNX (see *Systems*), with a
   one-time migration that rebuilds every profile from the enrollment photos.
2. **Identity and signing.** The installer still names the original author
   ("Cao Chi Tam") as publisher and uses a placeholder CLSID in
   `credential_provider/guid.h`. Set your company name, generate a new CLSID
   (keep `presence_monitor/system_setup.py` in step – a test checks this),
   and sign the DLL and both executables (OV/EV certificate). Windows
   SmartScreen and LogonUI both trust signed binaries far more.
3. **A clean-machine test.** Build the installer (`installer/build.py`, with
   `installer/download_weights.py` so the model ships inside), install on a
   fresh Windows 11 VM with no internet, and walk through first-run setup:
   tile installed, tasks registered, model available offline, face sign-in
   and auto-lock working after a restart.
4. **Release channel.** Own update server, signed releases, fail-closed
   checksum and Authenticode checks (the updater still points at the
   upstream GitHub project and is off by default).
5. **Legal.** EULA, privacy notice (biometric data: GDPR art. 9, Illinois
   BIPA, Texas CUBI), and a support address. The consent wording is in the
   first-run *Welcome* step; deletion is in the uninstaller and the app.
6. **Translations** of the new screens (the old translation tables remain).

## New features

| Idea | Why | Effort |
|---|---|---|
| Lock in seconds when you stand up and walk away | Faster than the timeout: track face size and position over a few checks while you're at the PC | Medium |
| "Someone is looking over your shoulder" alert | Detection already counts faces; notify or blur when a second face appears behind you | Small |
| Infrared (Windows Hello) camera support | Works in the dark and resists photos; Media Foundation exposes the IR stream | Large |
| Photo-spoof protection on by default | MiniFASNet (ONNX) plus a blink/motion check; measure the cost first | Medium |
| Several Windows users on one PC | A profile and password per account; the tile shows for whoever is selected | Medium |
| Face for app and admin prompts (CredUI) | Convenient, but a new attack surface: needs its own threat review | Large |
| Bluetooth phone as a second factor | Combine with Windows Dynamic Lock for high-security users | Medium |
| "Copy diagnostics" | One click zips logs and measurements (never images or the password) for support | Small |

## Tweaks to what exists

- **Presence checks** could come more often at the times the routine says
  you usually leave, and less often otherwise.
- **Routine**: show a day-by-day heat map of when the sign-in screen scans
  continuously, so the schedule is easy to understand.
- **Learning**: a gallery of learned views (when photos are kept) with
  "forget this one".
- **Exposure**: also use frame rate and gain where a camera's driver offers
  them; learn per time of day as well as per kind of light.
- **Camera**: remember settings per camera for laptops used with and without
  an external webcam.
- **Strictness**: suggest a stricter setting through a notification once the
  evidence is there, instead of only on the Learning page.
- **Notifications** with buttons ("Update password", "Add to lock screen").

## Systems

- **ONNX Runtime** instead of TensorFlow: service memory from ~570 MB to an
  estimated ~150 MB, start-up in about a second, installer a fraction of the
  size. Keep the parity tests.
- **A real Windows service** for matching, with the secret out of user mode
  (or certificate-based logon instead of password replay) – the commercial
  architecture.
- **Verify the pipe server from the tile** (refuse a look-alike pipe created
  by another process).
- **End-to-end tests** on a VM that lock and unlock through LogonUI.
- **Crash reports** kept locally, opt-in to send.

## UI and onboarding polish

- A short animation on the *Your face* step showing the head turns.
- A lock-screen preview on the Overview: "this is what you'll see".
- Screen-reader and keyboard-only pass over every screen; high-contrast test.
- Sound or haptic cue when a photo is taken during setup.

## Done in this release

First-run setup (consent, automatic readiness with fixes, password, face,
preferences, try it); bundled model used offline; the tray restarts a
stopped service; the stored password stops being replayed after two
rejections; face-aware exposure and fixed focus; learning after password
sign-ins and through the day; walk-past re-locking; Win+L that sticks;
routine-aware sign-in screen; caller check enforced; setup fail-safes.
