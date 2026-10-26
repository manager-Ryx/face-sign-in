#pragma once
#include <windows.h>
#include <string>

// Minimal blocking named-pipe client matching the Python FaceService protocol.
// Sends a single JSON request and reads a single JSON reply. Timeout is best-effort.
namespace FaceUnlock {

// Returns true on success; on success, fills `response` with the raw reply JSON.
bool PipeCall(const std::wstring& pipeName,
              const std::string& requestJson,
              std::string& response,
              DWORD timeoutMs = 30000,
              HANDLE cancelEvent = nullptr);

// Convenience: assemble {"cmd":"unlock"} and parse out username/password/domain.
// Any parse error -> returns false.
bool RequestUnlock(std::wstring& username,
                   std::wstring& password,
                   std::wstring& domain,
                   std::string& errorOut,
                   std::string& attemptId,
                   HANDLE cancelEvent = nullptr);

// A staged adaptive sample is committed only when Windows reports that the
// serialized credential was accepted. A missing acknowledgement expires
// safely without changing the profile.
bool ReportUnlockResult(const std::string& attemptId,
                        bool accepted,
                        DWORD timeoutMs = 1000);

}  // namespace FaceUnlock
