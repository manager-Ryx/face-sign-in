#include "FaceCredentialProvider.h"
#include "FaceCredential.h"
#include "PipeClient.h"
#include "dll.h"
#include "helpers.h"
#include <algorithm>
#include <new>

namespace FaceUnlock {

static void SecureErase(std::wstring& value) {
    if (!value.empty()) {
        SecureZeroMemory(value.data(), value.size() * sizeof(wchar_t));
        value.clear();
    }
}

FaceCredentialProvider::FaceCredentialProvider()
    : m_cRef(1), m_cpus(CPUS_INVALID), m_pCred(nullptr), m_pEvents(nullptr),
      m_adviseContext(0),
      m_stopEvent(CreateEventW(nullptr, TRUE, FALSE, nullptr)),
      m_workerActive(false), m_lastRearmTick(0) {
    DllAddRef();
}

FaceCredentialProvider::~FaceCredentialProvider() {
    UnAdvise();
    if (m_worker.joinable()) {
        // The worker holds a COM reference. If releasing that reference is
        // what destroys us, its std::thread object must be detached here.
        m_worker.detach();
    }
    if (m_pCred) {
        m_pCred->Release();
        m_pCred = nullptr;
    }
    if (m_stopEvent) {
        CloseHandle(m_stopEvent);
        m_stopEvent = nullptr;
    }
    DllRelease();
}

IFACEMETHODIMP FaceCredentialProvider::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == IID_ICredentialProvider) {
        *ppv = static_cast<ICredentialProvider*>(this);
        AddRef();
        return S_OK;
    }
    return E_NOINTERFACE;
}

IFACEMETHODIMP_(ULONG) FaceCredentialProvider::AddRef() {
    return InterlockedIncrement(&m_cRef);
}

IFACEMETHODIMP_(ULONG) FaceCredentialProvider::Release() {
    LONG c = InterlockedDecrement(&m_cRef);
    if (c == 0) delete this;
    return c;
}

IFACEMETHODIMP FaceCredentialProvider::SetUsageScenario(
    CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus,
    DWORD) {
    switch (cpus) {
        case CPUS_LOGON:
        case CPUS_UNLOCK_WORKSTATION:
            m_cpus = cpus;
            if (!m_pCred) {
                m_pCred = new (std::nothrow) FaceCredential(this);
                if (!m_pCred) return E_OUTOFMEMORY;
            }
            return m_pCred->Initialize(cpus);
        default:
            return E_NOTIMPL;
    }
}

IFACEMETHODIMP FaceCredentialProvider::SetSerialization(
    const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION*) {
    return E_NOTIMPL;
}

IFACEMETHODIMP FaceCredentialProvider::Advise(ICredentialProviderEvents* events,
                                              UINT_PTR adviseContext) {
    if (!events) return E_POINTER;
    if (!m_stopEvent) return E_OUTOFMEMORY;

    // Advise can be repeated during LogonUI refreshes. Fully retire the old
    // worker before binding to the new callback/context.
    UnAdvise();
    if (m_worker.joinable()) {
        // A synchronous callback attempted to re-advise from the worker
        // itself. Let that one-shot worker unwind before accepting a new one.
        return HRESULT_FROM_WIN32(ERROR_BUSY);
    }
    {
        std::lock_guard<std::mutex> lock(m_eventsMutex);
        m_pEvents = events;
        m_pEvents->AddRef();
        m_adviseContext = adviseContext;
    }

    if (!StartUnlockWorker(false)) {
        std::lock_guard<std::mutex> lock(m_eventsMutex);
        m_pEvents->Release();
        m_pEvents = nullptr;
        m_adviseContext = 0;
        return E_OUTOFMEMORY;
    }
    return S_OK;
}

IFACEMETHODIMP FaceCredentialProvider::UnAdvise() {
    if (m_stopEvent) SetEvent(m_stopEvent);
    if (m_worker.joinable()) {
        if (m_worker.get_id() != std::this_thread::get_id()) {
            m_worker.join();
        } else {
            // Called from the worker itself (a synchronous CredentialsChanged
            // callback). It cannot join itself; detach so a later Advise can
            // start a fresh worker instead of failing with ERROR_BUSY. The
            // worker keeps its own COM reference until it returns.
            m_worker.detach();
        }
    }
    m_workerActive.store(false);

    {
        std::lock_guard<std::mutex> lock(m_eventsMutex);
        if (m_pEvents) {
            m_pEvents->Release();
            m_pEvents = nullptr;
        }
        m_adviseContext = 0;
    }
    if (m_pCred) m_pCred->ClearPendingCredential();
    return S_OK;
}

void FaceCredentialProvider::NotifyCredentialsChanged() {
    ICredentialProviderEvents* events = nullptr;
    UINT_PTR context = 0;
    {
        std::lock_guard<std::mutex> lock(m_eventsMutex);
        events = m_pEvents;
        context = m_adviseContext;
        if (events) events->AddRef();
    }
    if (events) {
        events->CredentialsChanged(context);
        events->Release();
    }
}

bool FaceCredentialProvider::StartUnlockWorker(bool force) {
    if (!m_stopEvent) return false;
    if (m_worker.get_id() == std::this_thread::get_id()) return false;

    if (force) {
        const ULONGLONG now = GetTickCount64();
        const ULONGLONG previous = m_lastRearmTick.load();
        if (previous != 0 && now - previous < 1000) {
            return true;
        }
        m_lastRearmTick.store(now);
    } else if (m_workerActive.load()) {
        return true;
    }

    if (m_worker.joinable()) {
        // PipeCall observes this event and abandons the current request.
        SetEvent(m_stopEvent);
        m_worker.join();
    }
    ResetEvent(m_stopEvent);

    AddRef();  // Keep the provider alive until the worker exits.
    m_workerActive.store(true);
    try {
        m_worker = std::thread([this]() {
            UnlockWorker();
            Release();
        });
    } catch (...) {
        m_workerActive.store(false);
        Release();
        return false;
    }
    return true;
}

void FaceCredentialProvider::RearmUnlockWorker() {
    bool advised = false;
    {
        std::lock_guard<std::mutex> lock(m_eventsMutex);
        advised = m_pEvents != nullptr;
    }
    if (advised) {
        // A tile selection or submit/Enter click is an explicit wake signal.
        // Restarting closes the stale pipe/camera attempt before opening a
        // fresh one, while the one-second debounce prevents worker storms.
        StartUnlockWorker(true);
    }
}

void FaceCredentialProvider::UnlockWorker() {
    struct WorkerGuard {
        std::atomic<bool>& active;
        ~WorkerGuard() { active.store(false); }
    } guard{m_workerActive};

    DWORD backoffMs = 100;
    while (WaitForSingleObject(m_stopEvent, 0) == WAIT_TIMEOUT) {
        std::wstring username;
        std::wstring password;
        std::wstring domain;
        std::string error;
        std::string attemptId;
        bool matched = false;
        try {
            matched = RequestUnlock(username, password, domain, error,
                                    attemptId, m_stopEvent);
        } catch (...) {
            matched = false;
        }

        const bool cancelled = WaitForSingleObject(m_stopEvent, 0) != WAIT_TIMEOUT;
        if (!cancelled && matched && m_pCred &&
            m_pCred->CachePendingCredential(username, password, domain,
                                            attemptId)) {
            SecureErase(username);
            SecureErase(password);
            SecureErase(domain);
            NotifyCredentialsChanged();
            // One verified match produces at most one submission per Advise.
            // This prevents redraws or a rejected password from accumulating
            // automatic authentication attempts.
            if (WaitForSingleObject(m_stopEvent, 15000) == WAIT_TIMEOUT &&
                m_pCred->HasPendingCredential()) {
                // If LogonUI never consumes the match, expire plaintext and
                // withdraw auto-logon readiness after a short window.
                m_pCred->ClearPendingCredential();
                NotifyCredentialsChanged();
            }
            return;
        }

        SecureErase(username);
        SecureErase(password);
        SecureErase(domain);
        if (cancelled) return;

        if (WaitForSingleObject(m_stopEvent, backoffMs) != WAIT_TIMEOUT) return;
        backoffMs = (std::min)(backoffMs * 2, static_cast<DWORD>(1000));
    }
}

IFACEMETHODIMP FaceCredentialProvider::GetFieldDescriptorCount(DWORD* pdwCount) {
    if (!pdwCount) return E_POINTER;
    *pdwCount = FIELD_COUNT;
    return S_OK;
}

IFACEMETHODIMP FaceCredentialProvider::GetFieldDescriptorAt(
    DWORD dwIndex,
    CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** ppcpfd) {
    if (!ppcpfd) return E_POINTER;
    if (dwIndex >= FIELD_COUNT) return E_INVALIDARG;
    CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR src = s_FieldDescriptors[dwIndex];
    auto* out = static_cast<CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR*>(
        CoTaskMemAlloc(sizeof(src)));
    if (!out) return E_OUTOFMEMORY;
    *out = src;
    size_t cch = wcslen(src.pszLabel) + 1;
    out->pszLabel = static_cast<PWSTR>(CoTaskMemAlloc(cch * sizeof(WCHAR)));
    if (!out->pszLabel) {
        CoTaskMemFree(out);
        return E_OUTOFMEMORY;
    }
    wcscpy_s(out->pszLabel, cch, src.pszLabel);
    *ppcpfd = out;
    return S_OK;
}

IFACEMETHODIMP FaceCredentialProvider::GetCredentialCount(
    DWORD* pdwCount,
    DWORD* pdwDefault,
    BOOL* pbAutoLogonWithDefault) {
    if (!pdwCount || !pdwDefault || !pbAutoLogonWithDefault) return E_POINTER;
    *pdwCount = m_pCred ? 1 : 0;
    const bool ready = m_pCred && m_pCred->HasPendingCredential();
    *pdwDefault = ready ? 0 : CREDENTIAL_PROVIDER_NO_DEFAULT;
    *pbAutoLogonWithDefault = ready ? TRUE : FALSE;
    return S_OK;
}

IFACEMETHODIMP FaceCredentialProvider::GetCredentialAt(
    DWORD dwIndex,
    ICredentialProviderCredential** ppcpc) {
    if (!ppcpc) return E_POINTER;
    if (dwIndex != 0 || !m_pCred) return E_INVALIDARG;
    return m_pCred->QueryInterface(IID_ICredentialProviderCredential,
                                   reinterpret_cast<void**>(ppcpc));
}

}  // namespace FaceUnlock
