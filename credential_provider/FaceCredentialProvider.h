#pragma once
#include <windows.h>
#include <credentialprovider.h>
#include <atomic>
#include <mutex>
#include <thread>

namespace FaceUnlock {

class FaceCredential;

class FaceCredentialProvider : public ICredentialProvider {
public:
    FaceCredentialProvider();
    ~FaceCredentialProvider();

    IFACEMETHODIMP QueryInterface(REFIID, void**) override;
    IFACEMETHODIMP_(ULONG) AddRef() override;
    IFACEMETHODIMP_(ULONG) Release() override;

    IFACEMETHODIMP SetUsageScenario(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus, DWORD dwFlags) override;
    IFACEMETHODIMP SetSerialization(const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION*) override;
    IFACEMETHODIMP Advise(ICredentialProviderEvents*, UINT_PTR) override;
    IFACEMETHODIMP UnAdvise() override;
    IFACEMETHODIMP GetFieldDescriptorCount(DWORD* pdwCount) override;
    IFACEMETHODIMP GetFieldDescriptorAt(DWORD dwIndex,
                                        CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** ppcpfd) override;
    IFACEMETHODIMP GetCredentialCount(DWORD* pdwCount, DWORD* pdwDefault,
                                      BOOL* pbAutoLogonWithDefault) override;
    IFACEMETHODIMP GetCredentialAt(DWORD dwIndex, ICredentialProviderCredential** ppcpc) override;

    // Re-arm the one-shot lock-screen worker after the user selects the tile
    // or presses Enter while no credential is ready.
    void RearmUnlockWorker();

private:
    bool StartUnlockWorker(bool force);
    void UnlockWorker();
    void NotifyCredentialsChanged();

    LONG m_cRef;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO m_cpus;
    FaceCredential* m_pCred;
    ICredentialProviderEvents* m_pEvents;
    UINT_PTR m_adviseContext;
    HANDLE m_stopEvent;
    std::thread m_worker;
    std::mutex m_eventsMutex;
    std::atomic<bool> m_workerActive;
    std::atomic<ULONGLONG> m_lastRearmTick;
};

}  // namespace FaceUnlock
