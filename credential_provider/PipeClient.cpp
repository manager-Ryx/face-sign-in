#include "PipeClient.h"
#include <wtsapi32.h>
#include <algorithm>
#include <vector>

#pragma comment(lib, "wtsapi32.lib")

namespace FaceUnlock {

// A cold camera/model path can exceed twelve seconds after the workstation has
// been locked for a long time.  This is only a maximum wait; successful warm
// scans still return immediately.  Keeping the pipe alive prevents the Python
// service from finishing a valid scan after LogonUI has already closed it.
static constexpr DWORD kUnlockRequestTimeoutMs = 30000;

static void SecureErase(std::string& value) {
    if (!value.empty()) {
        SecureZeroMemory(value.data(), value.size());
        value.clear();
    }
}

struct SensitiveJson {
    std::string response;
    std::string username;
    std::string password;
    std::string domain;

    ~SensitiveJson() {
        SecureErase(response);
        SecureErase(username);
        SecureErase(password);
        SecureErase(domain);
    }
};

static std::wstring Utf8ToWide(const std::string& value) {
    if (value.empty()) return L"";
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                    value.data(), static_cast<int>(value.size()),
                                    nullptr, 0);
    if (count <= 0) return L"";
    std::wstring result(count, L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                             value.data(), static_cast<int>(value.size()),
                             result.data(), count)) {
        SecureZeroMemory(result.data(), result.size() * sizeof(wchar_t));
        return L"";
    }
    return result;
}

// Extract a top-level JSON string. Supports the escapes emitted by Python's
// json.dumps for ordinary Windows account names and passwords.
static bool ExtractString(const std::string& json,
                          const std::string& key,
                          std::string& out) {
    const std::string needle = "\"" + key + "\"";
    size_t cursor = json.find(needle);
    if (cursor == std::string::npos) return false;
    cursor = json.find(':', cursor + needle.size());
    if (cursor == std::string::npos) return false;
    cursor = json.find('"', cursor + 1);
    if (cursor == std::string::npos) return false;
    ++cursor;

    out.clear();
    while (cursor < json.size()) {
        const char ch = json[cursor++];
        if (ch == '"') return true;
        if (ch != '\\') {
            out.push_back(ch);
            continue;
        }
        if (cursor >= json.size()) return false;
        const char escaped = json[cursor++];
        switch (escaped) {
            case '"': out.push_back('"'); break;
            case '\\': out.push_back('\\'); break;
            case '/': out.push_back('/'); break;
            case 'b': out.push_back('\b'); break;
            case 'f': out.push_back('\f'); break;
            case 'n': out.push_back('\n'); break;
            case 'r': out.push_back('\r'); break;
            case 't': out.push_back('\t'); break;
            default:
                // Non-ASCII text is emitted directly by this service. Reject
                // unsupported escape forms instead of misreading credentials.
                return false;
        }
    }
    return false;
}

static bool ExtractBool(const std::string& json,
                        const std::string& key,
                        bool& out) {
    const std::string needle = "\"" + key + "\"";
    size_t cursor = json.find(needle);
    if (cursor == std::string::npos) return false;
    cursor = json.find(':', cursor + needle.size());
    if (cursor == std::string::npos) return false;
    ++cursor;
    while (cursor < json.size() &&
           (json[cursor] == ' ' || json[cursor] == '\t')) ++cursor;
    if (json.compare(cursor, 4, "true") == 0) {
        out = true;
        return true;
    }
    if (json.compare(cursor, 5, "false") == 0) {
        out = false;
        return true;
    }
    return false;
}

static bool WaitOrCancel(HANDLE cancelEvent, DWORD timeoutMs) {
    if (!cancelEvent) {
        Sleep(timeoutMs);
        return true;
    }
    return WaitForSingleObject(cancelEvent, timeoutMs) == WAIT_TIMEOUT;
}

// The user a token belongs to, as a copy of its SID.
static bool TokenUserSid(HANDLE token, std::vector<BYTE>& out) {
    DWORD size = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &size);
    if (size == 0) return false;
    std::vector<BYTE> buffer(size);
    if (!GetTokenInformation(token, TokenUser, buffer.data(), size, &size)) return false;
    PSID sid = reinterpret_cast<TOKEN_USER*>(buffer.data())->User.Sid;
    if (!IsValidSid(sid)) return false;
    const BYTE* bytes = static_cast<const BYTE*>(sid);
    out.assign(bytes, bytes + GetLengthSid(sid));
    return true;
}

// Who the service must be running as: the user signed in to this session.
// The sign-in screen runs as SYSTEM and may ask Windows directly. Run as an
// ordinary user (a test program), that isn't allowed, and the user is us.
static bool ExpectedServerSid(std::vector<BYTE>& out) {
    DWORD session = 0;
    if (!ProcessIdToSessionId(GetCurrentProcessId(), &session)) return false;
    HANDLE token = nullptr;
    if (!WTSQueryUserToken(session, &token)) {
        if (GetLastError() != ERROR_PRIVILEGE_NOT_HELD) return false;  // e.g. nobody signed in
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
    }
    const bool ok = TokenUserSid(token, out);
    CloseHandle(token);
    return ok;
}

// Refuse a pipe created by anyone but the signed-in user: another account
// could otherwise create \\.\pipe\FaceUnlock first and answer with a
// password of its choosing (failed sign-ins that can lock the account).
static bool VerifyPipeServer(HANDLE pipe) {
    ULONG serverPid = 0;
    if (!GetNamedPipeServerProcessId(pipe, &serverPid) || serverPid == 0) return false;
    std::vector<BYTE> expected;
    std::vector<BYTE> actual;
    if (!ExpectedServerSid(expected)) return false;
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, serverPid);
    if (!process) return false;
    HANDLE token = nullptr;
    const BOOL opened = OpenProcessToken(process, TOKEN_QUERY, &token);
    CloseHandle(process);
    if (!opened) return false;
    const bool read = TokenUserSid(token, actual);
    CloseHandle(token);
    return read && EqualSid(expected.data(), actual.data());
}

static bool OverlappedWait(HANDLE pipe,
                           OVERLAPPED& ov,
                           DWORD timeoutMs,
                           DWORD& transferred,
                           HANDLE cancelEvent) {
    HANDLE handles[2] = {ov.hEvent, cancelEvent};
    DWORD waitResult = WaitForMultipleObjects(cancelEvent ? 2 : 1,
                                              handles, FALSE, timeoutMs);
    if (waitResult != WAIT_OBJECT_0) {
        CancelIoEx(pipe, &ov);
        GetOverlappedResult(pipe, &ov, &transferred, TRUE);
        return false;
    }
    return GetOverlappedResult(pipe, &ov, &transferred, FALSE) != 0;
}

bool PipeCall(const std::wstring& pipeName,
              const std::string& requestJson,
              std::string& response,
              DWORD timeoutMs,
              HANDLE cancelEvent) {
    const DWORD startTick = GetTickCount();
    auto remaining = [&]() -> DWORD {
        const DWORD elapsed = GetTickCount() - startTick;
        return elapsed >= timeoutMs ? 0 : timeoutMs - elapsed;
    };

    HANDLE pipe = INVALID_HANDLE_VALUE;
    while (remaining() > 0) {
        if (cancelEvent && WaitForSingleObject(cancelEvent, 0) != WAIT_TIMEOUT) {
            return false;
        }
        // Identification level: the service may check who is calling (the
        // sign-in screen, as SYSTEM) but can never act as SYSTEM.
        pipe = CreateFileW(pipeName.c_str(), GENERIC_READ | GENERIC_WRITE,
                           0, nullptr, OPEN_EXISTING,
                           FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT |
                               SECURITY_IDENTIFICATION,
                           nullptr);
        if (pipe != INVALID_HANDLE_VALUE) break;

        const DWORD error = GetLastError();
        if (error == ERROR_PIPE_BUSY) {
            const DWORD waitMs = (std::min)(remaining(), static_cast<DWORD>(250));
            if (waitMs == 0) break;  // 0 would mean "the server's default wait"
            WaitNamedPipeW(pipeName.c_str(), waitMs);
        } else if (error == ERROR_FILE_NOT_FOUND) {
            if (!WaitOrCancel(cancelEvent,
                              (std::min)(remaining(), static_cast<DWORD>(200)))) {
                return false;
            }
        } else {
            return false;
        }
    }
    if (pipe == INVALID_HANDLE_VALUE) return false;
    if (!VerifyPipeServer(pipe)) {
        CloseHandle(pipe);
        return false;
    }

    DWORD mode = PIPE_READMODE_MESSAGE;
    if (!SetNamedPipeHandleState(pipe, &mode, nullptr, nullptr)) {
        CloseHandle(pipe);
        return false;
    }

    HANDLE event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!event) {
        CloseHandle(pipe);
        return false;
    }

    bool success = false;
    OVERLAPPED overlapped{};
    overlapped.hEvent = event;
    DWORD transferred = 0;
    std::vector<char> buffer(65536);

    do {
        BOOL wrote = WriteFile(pipe, requestJson.data(),
                               static_cast<DWORD>(requestJson.size()),
                               nullptr, &overlapped);
        if (!wrote && GetLastError() != ERROR_IO_PENDING) break;
        if (!OverlappedWait(pipe, overlapped, remaining(), transferred,
                            cancelEvent)) break;
        if (transferred != requestJson.size()) break;

        ResetEvent(event);
        ZeroMemory(&overlapped, sizeof(overlapped));
        overlapped.hEvent = event;
        BOOL read = ReadFile(pipe, buffer.data(), static_cast<DWORD>(buffer.size()),
                             nullptr, &overlapped);
        const DWORD readError = read ? ERROR_SUCCESS : GetLastError();
        if (!read && readError != ERROR_IO_PENDING && readError != ERROR_MORE_DATA) {
            break;
        }
        if (!OverlappedWait(pipe, overlapped, remaining(), transferred,
                            cancelEvent)) break;
        if (transferred == 0) break;

        response.assign(buffer.data(), transferred);
        success = true;
    } while (false);

    SecureZeroMemory(buffer.data(), buffer.size());
    CloseHandle(event);
    CloseHandle(pipe);
    return success;
}

bool RequestUnlock(std::wstring& username,
                   std::wstring& password,
                   std::wstring& domain,
                   std::string& errorOut,
                   std::string& attemptId,
                   bool selected,
                   HANDLE cancelEvent) {
    const std::wstring pipe = L"\\\\.\\pipe\\FaceUnlock";
    SensitiveJson json;
    const std::string request = selected ? "{\"cmd\":\"unlock\",\"selected\":true}"
                                         : "{\"cmd\":\"unlock\",\"selected\":false}";
    if (!PipeCall(pipe, request, json.response,
                  kUnlockRequestTimeoutMs,
                  cancelEvent)) {
        errorOut = (cancelEvent &&
                    WaitForSingleObject(cancelEvent, 0) != WAIT_TIMEOUT)
                       ? "cancelled"
                       : "pipe-unavailable";
        return false;
    }

    bool ok = false;
    if (!ExtractBool(json.response, "ok", ok) || !ok) {
        std::string reason;
        ExtractString(json.response, "reason", reason);
        errorOut = reason.empty() ? "no-match" : reason;
        SecureErase(reason);
        return false;
    }

    if (!ExtractString(json.response, "username", json.username) ||
        !ExtractString(json.response, "password", json.password)) {
        errorOut = "malformed-response";
        return false;
    }
    ExtractString(json.response, "domain", json.domain);
    ExtractString(json.response, "attempt_id", attemptId);

    username = Utf8ToWide(json.username);
    password = Utf8ToWide(json.password);
    domain = Utf8ToWide(json.domain.empty() ? "." : json.domain);
    if (username.empty() || password.empty() || domain.empty()) {
        errorOut = "malformed-response";
        return false;
    }
    return true;
}

bool ReportUnlockResult(const std::string& attemptId,
                        bool accepted,
                        ULONG status,
                        ULONG substatus,
                        DWORD timeoutMs) {
    if (attemptId.empty()) return true;
    // attemptId is generated by secrets.token_urlsafe, so it cannot contain
    // JSON metacharacters and may be embedded without a general serializer.
    const std::string request =
        "{\"cmd\":\"unlock_result\",\"attempt_id\":\"" + attemptId +
        "\",\"accepted\":" + (accepted ? "true" : "false") +
        ",\"status\":" + std::to_string(status) +
        ",\"substatus\":" + std::to_string(substatus) + "}";
    std::string response;
    const bool called = PipeCall(L"\\\\.\\pipe\\FaceUnlock", request,
                                 response, timeoutMs, nullptr);
    bool ok = false;
    const bool parsed = called && ExtractBool(response, "ok", ok) && ok;
    SecureErase(response);
    return parsed;
}

}  // namespace FaceUnlock
