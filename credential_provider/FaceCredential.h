#pragma once
#include <windows.h>
#include <credentialprovider.h>
#include <mutex>
#include <string>

namespace FaceUnlock {

class FaceCredentialProvider;

class FaceCredential : public ICredentialProviderCredential {
public:
    explicit FaceCredential(FaceCredentialProvider* owner);
    ~FaceCredential();

    HRESULT Initialize(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus);
    // A verified face is good for kPendingLifetimeMs: after that it is erased
    // and can't be used, however LogonUI or the worker were interrupted.
    bool CachePendingCredential(const std::wstring& username,
                                const std::wstring& password,
                                const std::wstring& domain,
                                const std::string& attemptId);
    bool HasPendingCredential();
    // Erases an expired credential; true if there was one (the tile must redraw).
    bool ExpirePendingCredential();
    void ClearPendingCredential();
    // The provider is going away: never call back into it again.
    void ClearOwner();

    // IUnknown
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    IFACEMETHODIMP_(ULONG) AddRef() override;
    IFACEMETHODIMP_(ULONG) Release() override;

    // ICredentialProviderCredential
    IFACEMETHODIMP Advise(ICredentialProviderCredentialEvents* pcpce) override;
    IFACEMETHODIMP UnAdvise() override;
    IFACEMETHODIMP SetSelected(BOOL* pbAutoLogon) override;
    IFACEMETHODIMP SetDeselected() override;
    IFACEMETHODIMP GetFieldState(DWORD dwFieldID,
                                 CREDENTIAL_PROVIDER_FIELD_STATE* pcpfs,
                                 CREDENTIAL_PROVIDER_FIELD_INTERACTIVE_STATE* pcpfis) override;
    IFACEMETHODIMP GetStringValue(DWORD dwFieldID, PWSTR* ppwsz) override;
    IFACEMETHODIMP GetBitmapValue(DWORD dwFieldID, HBITMAP* phbmp) override;
    IFACEMETHODIMP GetCheckboxValue(DWORD, BOOL*, PWSTR*) override { return E_NOTIMPL; }
    IFACEMETHODIMP GetSubmitButtonValue(DWORD dwFieldID, DWORD* pdwAdjacentTo) override;
    IFACEMETHODIMP GetComboBoxValueCount(DWORD, DWORD*, DWORD*) override { return E_NOTIMPL; }
    IFACEMETHODIMP GetComboBoxValueAt(DWORD, DWORD, PWSTR*) override { return E_NOTIMPL; }
    IFACEMETHODIMP SetStringValue(DWORD, PCWSTR) override { return S_OK; }
    IFACEMETHODIMP SetCheckboxValue(DWORD, BOOL) override { return E_NOTIMPL; }
    IFACEMETHODIMP SetComboBoxSelectedValue(DWORD, DWORD) override { return E_NOTIMPL; }
    IFACEMETHODIMP CommandLinkClicked(DWORD) override { return E_NOTIMPL; }
    IFACEMETHODIMP GetSerialization(CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE* pcpgsr,
                                    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs,
                                    PWSTR* ppwszOptionalStatusText,
                                    CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) override;
    IFACEMETHODIMP ReportResult(NTSTATUS ntsStatus, NTSTATUS ntsSubstatus,
                                PWSTR* ppwszOptionalStatusText,
                                CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) override;

private:
    bool ConsumePendingCredential(std::wstring& username,
                                  std::wstring& password,
                                  std::wstring& domain,
                                  std::string& attemptId);
    bool ExpireLocked();     // m_mutex held
    void ClearLocked();      // m_mutex held
    void Rearm();            // tell the provider you asked to be let in

    LONG m_cRef;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO m_cpus;
    ICredentialProviderCredentialEvents* m_pEvents;
    mutable std::mutex m_mutex;
    std::wstring m_label;
    std::wstring m_status;
    std::wstring m_cachedUsername;
    std::wstring m_cachedPassword;
    std::wstring m_cachedDomain;
    std::string m_cachedAttemptId;
    std::string m_inflightAttemptId;
    bool m_ready;
    ULONGLONG m_readyUntil;
    FaceCredentialProvider* m_owner;
};

constexpr ULONGLONG kPendingLifetimeMs = 15000;

}  // namespace FaceUnlock
