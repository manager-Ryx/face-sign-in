# Roadmap

What is still needed before a public release, then ideas ranked by value
for effort. "Done" items are listed at the end for reference.

## Before a public release (must)

1. **A recognition model you may sell.** DeepFace's ArcFace weights (trained
   on MS1M/Glint-style data) are generally licensed for non-commercial use.
   Ship a commercially licensed model, ideally as ONNX (see *Systems*), with a
   one-time migration that rebuilds every profile from the enrollment photos.
2. **Identity and signing.** The tile still uses the original project's
   CLSID in `credential_provider/guid.h`. Generate a new one (keep
   `presence_monitor/system_setup.py` in step – a test checks this; existing
   installs need the tile re-registered), and sign the DLL and both
   executables (OV/EV certificate). Windows SmartScreen and LogonUI both
   trust signed binaries far more.
3. **A clean-machine test.** Install a release on a fresh Windows 11 PC and
   walk through first-run setup: tile installed, tasks registered, model
   downloaded, face sign-in and auto-lock working after a restart. For
   offline installs, build with `BUNDLE_ARCFACE=1` (licence permitting).
4. **Release channel.** Own update server, signed releases, fail-closed
   checksum and Authenticode checks (the updater reads this project's GitHub
   releases and is off by default).
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
- **Notifications** with real buttons ("Update password", "Add to lock screen")
  through Windows toasts. Selecting a notification already opens its fix.

## Systems

- **ONNX Runtime** instead of TensorFlow: service memory from ~570 MB to an
  estimated ~150 MB, start-up in about a second, installer a fraction of the
  size. Keep the parity tests.
- **A real Windows service** for matching, with the secret out of user mode
  (or certificate-based logon instead of password replay) – the commercial
  architecture.
- **End-to-end tests** on a VM that lock and unlock through LogonUI.
- **Crash reports** kept locally, opt-in to send.

## UI and onboarding polish

- A lock-screen preview on the Overview: "this is what you'll see".
- Screen-reader and keyboard-only pass over every screen with a screen reader
  running; high-contrast test. (Accessible names, tooltips and shortcuts are in.)
- The animated guide `hello-only.webp` (a placeholder shows the steps until then).

## Done in this release

UX pass (0.3.0-beta.3): a live "Is this you?" camera check in setup; a
head-turn animation, shutter sound (with mute) and per-photo flash during
face capture; a lighting meter in both; Copy diagnostics; a first face
sign-in notice and a weekly summary; notifications that open their fix;
busy buttons, in-place results, toasts with Undo, saved ticks,
reset-to-default and search for settings, a step indicator, gentle fades
(off when Windows' animation effects are off), and a remembered window size.

First-run setup (consent, automatic readiness with fixes, password, face,
preferences, try it); a bundled model is used offline; the tray restarts a
stopped service; the stored password stops being replayed after two
rejections; face-aware exposure and fixed focus; learning after password
sign-ins and through the day; walk-past re-locking; Win+L that sticks;
routine-aware sign-in screen; caller check enforced; setup fail-safes.
Before publishing (0.3): the fixes from an independent bug audit, including
a tile that only trusts the signed-in user's pipe, expires a recognised
face after 15 seconds and tells the service when you selected it.
