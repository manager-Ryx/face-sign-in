#pragma once
#include <windows.h>
#include <credentialprovider.h>
#include <atomic>
#include <memory>
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
    // force: you selected the tile or pressed Enter (the first request says so).
    bool StartUnlockWorker(bool force);
    void UnlockWorker(unsigned generation, bool selected);
    // Stop the current worker; waits briefly, then lets it finish on its own.
    void RetireWorker();
    bool IsCurrent(unsigned generation) const { return m_generation.load() == generation; }
    void NotifyCredentialsChanged();

    LONG m_cRef;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO m_cpus;
    FaceCredential* m_pCred;
    ICredentialProviderEvents* m_pEvents;
    UINT_PTR m_adviseContext;
    HANDLE m_stopEvent;
    std::thread m_worker;
    std::shared_ptr<void> m_workerDone;   // set by the current worker as it exits
    std::mutex m_eventsMutex;
    std::atomic<bool> m_workerActive;
    std::atomic<ULONGLONG> m_lastRearmTick;
    std::atomic<unsigned> m_generation;   // a worker from an older generation is stale
};

}  // namespace FaceUnlock
