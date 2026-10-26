# Face Unlock Credential Provider (C++)

A minimal Windows Credential Provider that watches for the enrolled user while
LogonUI is active. It asks the Python `FaceService` to verify the camera feed
over `\\.\pipe\FaceUnlock`, then returns a standard
`KERB_INTERACTIVE_UNLOCK_LOGON` serialization after a verified match.

## Build

Prerequisites:

- Visual Studio 2022 with Desktop development with C++
- CMake 3.20+
- A recent Windows SDK

```powershell
cmake -B build -A x64
cmake --build build --config Release
```

Output: `build\Release\FaceCredentialProvider.dll`.

Registration is deliberately separate and requires an administrator:

```powershell
.\register.ps1 -Action register
```

Use `.\register.ps1 -Action unregister` to remove it.

## Event-driven unlock flow

1. While LogonUI advises the provider, a cancellable worker polls FaceService
   one request at a time. Failed attempts wait 100 ms initially, then back off
   to at most 1 second. Each request performs the configured multi-frame vote,
   so this is inference-limited rather than a fixed camera frames-per-second loop.
2. FaceService verifies multiple frames and liveness, then returns the enrolled
   user's credential only on a match.
3. The provider caches that credential for no more than 15 seconds and calls
   `ICredentialProviderEvents::CredentialsChanged`.
4. `GetCredentialCount` requests auto-logon only while the one-shot credential
   is ready. `SetSelected` remains non-triggering for auto-logon, but it
   explicitly re-arms a stale worker when the tile is selected.
5. `GetSerialization` consumes the cached credential, clears temporary
   plaintext, and returns it to LogonUI. If no credential is ready, it also
   requests the same re-arm so an Enter/submit action refreshes the camera.
6. If Windows calls `ReportResult`, the provider acknowledges success or
   failure to FaceService. Only a successful acknowledgement can commit staged
   adaptive embeddings; failed or unreported attempts expire unchanged.

Selecting the Face Unlock tile or pressing Enter when no credential is ready is
an explicit re-arm signal. The provider cancels and joins the stale worker,
releases the associated camera request, and starts one fresh worker. A
one-second debounce prevents repeated key events from creating worker storms.

During failed votes, FaceService retains only safe near-anchor embeddings in a
bounded in-memory pre-auth buffer. If a later face attempt is accepted by
Windows, those candidates are included in the normal adaptive transaction.
Raw frames are never retained and failed/unreported attempts never train the
profile.

Each Advise cycle can produce only one automatic submission. This prevents a
redraw or rejected password from causing an authentication loop. Standard PIN
and password providers remain visible.

## Security notes

- The DLL runs in `LogonUI.exe` as LocalSystem. FaceService runs in the existing
  user session so it can decrypt the user's DPAPI-protected credential.
- The named-pipe DACL allows only that Windows user and LocalSystem.
- Plaintext exists briefly in both processes after a successful face match. It
  is zeroed when consumed, cancelled, expired, or unadvised.
- This remains a convenience feature, not Windows Hello. Keep a normal PIN or
  password recovery path available.
- Generate a unique credential-provider GUID before distributing a build.
- Adaptive enrollment never treats a failed attempt as training data. Windows
  may omit `ReportResult` on some successful paths; those unlocks simply do not
  adapt. This conservative false-negative is intentional to prevent poisoning.

Actual LogonUI behavior cannot be proven by unit tests. Build first, then use an
explicit, recoverable installation test with the unregister script available.
