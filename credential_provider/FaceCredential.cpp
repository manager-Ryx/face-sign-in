#include "FaceCredential.h"
#include "FaceCredentialProvider.h"
#include "helpers.h"
#include "PipeClient.h"
#include "dll.h"
#include "guid.h"

namespace FaceUnlock {

static HRESULT AllocStr(PCWSTR src, PWSTR* dst) {
    if (!src || !dst) return E_POINTER;
    size_t cch = wcslen(src) + 1;
    *dst = static_cast<PWSTR>(CoTaskMemAlloc(cch * sizeof(WCHAR)));
    if (!*dst) return E_OUTOFMEMORY;
    wcscpy_s(*dst, cch, src);
    return S_OK;
}

static void SecureErase(std::wstring& value) {
    if (!value.empty()) {
        SecureZeroMemory(value.data(), value.size() * sizeof(wchar_t));
        value.clear();
    }
}

static void SecureErase(std::string& value) {
    if (!value.empty()) {
        SecureZeroMemory(value.data(), value.size());
        value.clear();
    }
}

struct SensitiveCredential {
    std::wstring username;
    std::wstring password;
    std::wstring domain;

    ~SensitiveCredential() {
        SecureErase(username);
        SecureErase(password);
        SecureErase(domain);
    }
};

FaceCredential::FaceCredential(FaceCredentialProvider* owner)
    : m_cRef(1), m_cpus(CPUS_INVALID), m_pEvents(nullptr),
      m_label(L"Face Unlock"), m_status(L"Looking for you"), m_ready(false),
      m_readyUntil(0), m_owner(owner) {
    DllAddRef();
}

FaceCredential::~FaceCredential() {
    UnAdvise();
    ClearPendingCredential();
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        SecureErase(m_inflightAttemptId);
    }
    DllRelease();
}

HRESULT FaceCredential::Initialize(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus) {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_cpus = cpus;
    return S_OK;
}

bool FaceCredential::CachePendingCredential(const std::wstring& username,
                                            const std::wstring& password,
                                            const std::wstring& domain,
                                            const std::string& attemptId) {
    if (username.empty() || password.empty()) return false;
    std::lock_guard<std::mutex> lock(m_mutex);
    SecureErase(m_cachedUsername);
    SecureErase(m_cachedPassword);
    SecureErase(m_cachedDomain);
    m_cachedUsername = username;
    m_cachedPassword = password;
    m_cachedDomain = domain.empty() ? L"." : domain;
    SecureErase(m_cachedAttemptId);
    m_cachedAttemptId = attemptId;
    m_ready = true;
    m_readyUntil = GetTickCount64() + kPendingLifetimeMs;
    m_status = L"Face recognised";
    return true;
}

bool FaceCredential::HasPendingCredential() {
    std::lock_guard<std::mutex> lock(m_mutex);
    ExpireLocked();
    return m_ready;
}

bool FaceCredential::ExpirePendingCredential() {
    std::lock_guard<std::mutex> lock(m_mutex);
    return ExpireLocked();
}

bool FaceCredential::ExpireLocked() {
    if (!m_ready || GetTickCount64() < m_readyUntil) return false;
    ClearLocked();
    m_status = L"Looking for you";
    return true;
}

bool FaceCredential::ConsumePendingCredential(std::wstring& username,
                                              std::wstring& password,
                                              std::wstring& domain,
                                              std::string& attemptId) {
    std::lock_guard<std::mutex> lock(m_mutex);
    ExpireLocked();
    if (!m_ready) return false;
    username = m_cachedUsername;
    password = m_cachedPassword;
    domain = m_cachedDomain;
    attemptId = m_cachedAttemptId;
    SecureErase(m_cachedUsername);
    SecureErase(m_cachedPassword);
    SecureErase(m_cachedDomain);
    SecureErase(m_cachedAttemptId);
    m_ready = false;
    m_status = L"Looking for you";
    return true;
}

void FaceCredential::ClearPendingCredential() {
    std::lock_guard<std::mutex> lock(m_mutex);
    ClearLocked();
}

void FaceCredential::ClearLocked() {
    SecureErase(m_cachedUsername);
    SecureErase(m_cachedPassword);
    SecureErase(m_cachedDomain);
    SecureErase(m_cachedAttemptId);
    m_ready = false;
}

void FaceCredential::ClearOwner() {
    std::lock_guard<std::mutex> lock(m_mutex);
    m_owner = nullptr;
}

void FaceCredential::Rearm() {
    FaceCredentialProvider* owner = nullptr;
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        owner = m_owner;
    }
    if (owner) owner->RearmUnlockWorker();
}

IFACEMETHODIMP FaceCredential::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == IID_ICredentialProviderCredential) {
        *ppv = static_cast<ICredentialProviderCredential*>(this);
        AddRef();
        return S_OK;
    }
    return E_NOINTERFACE;
}

IFACEMETHODIMP_(ULONG) FaceCredential::AddRef() {
    return InterlockedIncrement(&m_cRef);
}

IFACEMETHODIMP_(ULONG) FaceCredential::Release() {
    LONG c = InterlockedDecrement(&m_cRef);
    if (c == 0) delete this;
    return c;
}

IFACEMETHODIMP FaceCredential::Advise(ICredentialProviderCredentialEvents* events) {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_pEvents) m_pEvents->Release();
    m_pEvents = events;
    if (m_pEvents) m_pEvents->AddRef();
    return S_OK;
}

IFACEMETHODIMP FaceCredential::UnAdvise() {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_pEvents) {
        m_pEvents->Release();
        m_pEvents = nullptr;
    }
    return S_OK;
}

IFACEMETHODIMP FaceCredential::SetSelected(BOOL* pbAutoLogon) {
    if (!pbAutoLogon) return E_POINTER;
    // Selection is deliberately non-triggering. Only a successful background
    // verification can make GetCredentialCount request one auto-logon.
    *pbAutoLogon = FALSE;

    ICredentialProviderCredentialEvents* events = nullptr;
    std::wstring status;
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        status = m_ready ? L"Face recognised" : L"Looking for you";
        m_status = status;
        events = m_pEvents;
        if (events) events->AddRef();
    }
    if (events) {
        events->SetFieldString(this, FIELD_STATUS, status.c_str());
        events->Release();
    }
    Rearm();
    return S_OK;
}

IFACEMETHODIMP FaceCredential::SetDeselected() { return S_OK; }

IFACEMETHODIMP FaceCredential::GetFieldState(
    DWORD dwFieldID,
    CREDENTIAL_PROVIDER_FIELD_STATE* pcpfs,
    CREDENTIAL_PROVIDER_FIELD_INTERACTIVE_STATE* pcpfis) {
    if (!pcpfs || !pcpfis) return E_POINTER;
    if (dwFieldID >= FIELD_COUNT) return E_INVALIDARG;
    *pcpfs = s_FieldStatePairs[dwFieldID].cpfs;
    *pcpfis = s_FieldStatePairs[dwFieldID].cpfis;
    return S_OK;
}

IFACEMETHODIMP FaceCredential::GetStringValue(DWORD dwFieldID, PWSTR* ppwsz) {
    if (!ppwsz) return E_POINTER;
    std::wstring value;
    switch (dwFieldID) {
        case FIELD_LABEL:
            value = m_label;
            break;
        case FIELD_STATUS: {
            std::lock_guard<std::mutex> lock(m_mutex);
            value = m_status;
            break;
        }
        default:
            return E_INVALIDARG;
    }
    return AllocStr(value.c_str(), ppwsz);
}

IFACEMETHODIMP FaceCredential::GetBitmapValue(DWORD dwFieldID, HBITMAP* phbmp) {
    if (!phbmp) return E_POINTER;
    if (dwFieldID != FIELD_TILE_IMAGE) return E_INVALIDARG;
    *phbmp = nullptr;
    return E_NOTIMPL;
}

IFACEMETHODIMP FaceCredential::GetSubmitButtonValue(DWORD dwFieldID,
                                                    DWORD* pdwAdjacentTo) {
    if (!pdwAdjacentTo) return E_POINTER;
    if (dwFieldID != FIELD_SUBMIT) return E_INVALIDARG;
    *pdwAdjacentTo = FIELD_STATUS;
    return S_OK;
}

IFACEMETHODIMP FaceCredential::GetSerialization(
    CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE* pcpgsr,
    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs,
    PWSTR* ppwszOptionalStatusText,
    CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) {
    if (!pcpgsr || !pcpcs || !pcpsiOptionalStatusIcon) return E_POINTER;

    *pcpgsr = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
    *pcpsiOptionalStatusIcon = CPSI_NONE;
    ZeroMemory(pcpcs, sizeof(*pcpcs));
    if (ppwszOptionalStatusText) *ppwszOptionalStatusText = nullptr;

    SensitiveCredential credential;
    std::string attemptId;
    if (!ConsumePendingCredential(credential.username,
                                  credential.password,
                                  credential.domain,
                                  attemptId)) {
        Rearm();
        if (ppwszOptionalStatusText) {
            AllocStr(L"Waiting for a verified face - use another sign-in option if needed",
                     ppwszOptionalStatusText);
        }
        return S_FALSE;
    }

    CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus;
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        cpus = m_cpus;
    }

    HRESULT hr = E_FAIL;
    try {
        hr = KerbPackInteractiveUnlock(credential.domain,
                                       credential.username,
                                       credential.password,
                                       cpus,
                                       pcpcs);
    } catch (...) {
        hr = E_FAIL;
    }
    if (FAILED(hr)) {
        ReportUnlockResult(attemptId, false, kNotSubmitted, 0);
        SecureErase(attemptId);
        {
            std::lock_guard<std::mutex> lock(m_mutex);
            m_status = L"Face sign-in failed. Use another sign-in option.";
        }
        *pcpsiOptionalStatusIcon = CPSI_ERROR;
        return S_FALSE;
    }

    pcpcs->clsidCredentialProvider = CLSID_FaceCredentialProvider;
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        SecureErase(m_inflightAttemptId);
        m_inflightAttemptId = attemptId;
    }
    SecureErase(attemptId);
    *pcpgsr = CPGSR_RETURN_CREDENTIAL_FINISHED;
    return S_OK;
}

IFACEMETHODIMP FaceCredential::ReportResult(
    NTSTATUS ntsStatus,
    NTSTATUS ntsSubstatus,
    PWSTR* ppwszOptionalStatusText,
    CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) {
    std::string attemptId;
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        attemptId = m_inflightAttemptId;
        SecureErase(m_inflightAttemptId);
    }
    if (!attemptId.empty()) {
        // STATUS_SUCCESS is zero. Anything else explicitly discards the
        // staged sample. If Windows omits ReportResult, the service expires it.
        // The codes let the service tell a wrong password from other failures.
        ReportUnlockResult(attemptId, ntsStatus == 0, static_cast<ULONG>(ntsStatus),
                           static_cast<ULONG>(ntsSubstatus), 750);
        SecureErase(attemptId);
    }
    if (ppwszOptionalStatusText) *ppwszOptionalStatusText = nullptr;
    if (pcpsiOptionalStatusIcon) *pcpsiOptionalStatusIcon = CPSI_NONE;
    return S_OK;
}

}  // namespace FaceUnlock
