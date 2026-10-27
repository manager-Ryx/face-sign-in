#pragma once
#include <windows.h>
#include <string>

// Minimal blocking named-pipe client matching the Python FaceService protocol.
// Sends a single JSON request and reads a single JSON reply. Timeout is best-effort.
namespace FaceUnlock {

// Returns true on success; on success, fills `response` with the raw reply JSON.
// The pipe is used only when its server runs as the user signed in to this
// session, so a look-alike pipe created by anyone else is never answered.
bool PipeCall(const std::wstring& pipeName,
              const std::string& requestJson,
              std::string& response,
              DWORD timeoutMs = 30000,
              HANDLE cancelEvent = nullptr);

// Convenience: assemble {"cmd":"unlock"} and parse out username/password/domain.
// `selected` says the request comes from you selecting the tile or pressing
// Enter, not from the tile's own retries. Any parse error -> returns false.
bool RequestUnlock(std::wstring& username,
                   std::wstring& password,
                   std::wstring& domain,
                   std::string& errorOut,
                   std::string& attemptId,
                   bool selected,
                   HANDLE cancelEvent = nullptr);

// A staged adaptive sample is committed only when Windows reports that the
// serialized credential was accepted. A missing acknowledgement expires
// safely without changing the profile. `status`/`substatus` are Windows'
// result codes, so the service can tell a wrong password from, say, an
// unreachable domain controller (kNotSubmitted: never reached Windows).
constexpr ULONG kNotSubmitted = 0xFFFFFFFF;
bool ReportUnlockResult(const std::string& attemptId,
                        bool accepted,
                        ULONG status,
                        ULONG substatus,
                        DWORD timeoutMs = 1000);

}  // namespace FaceUnlock
