#include <windows.h>
#include <unknwn.h>
#include "guid.h"
#include "dll.h"

EXTERN_C IMAGE_DOS_HEADER __ImageBase;

namespace FaceUnlock {
HRESULT CreateClassFactory(REFIID riid, void** ppv);
}

// Live COM objects and server locks. While this is non-zero the DLL must
// stay loaded: the provider's worker thread runs code from this module.
static LONG g_DllRefs = 0;

void DllAddRef() { InterlockedIncrement(&g_DllRefs); }
void DllRelease() { InterlockedDecrement(&g_DllRefs); }

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
    return TRUE;
}

STDAPI DllCanUnloadNow(void) {
    return (g_DllRefs == 0) ? S_OK : S_FALSE;
}

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, LPVOID* ppv) {
    if (rclsid != CLSID_FaceCredentialProvider) return CLASS_E_CLASSNOTAVAILABLE;
    return FaceUnlock::CreateClassFactory(riid, ppv);
}

// Registration (regsvr32). Credential providers must be registered for the
// whole machine, so keys go under HKLM explicitly rather than HKCR, which
// falls back to the per-user hive for non-elevated callers.
static HRESULT WriteReg(PCWSTR sub, PCWSTR name, PCWSTR value) {
    HKEY key;
    LONG r = RegCreateKeyExW(HKEY_LOCAL_MACHINE, sub, 0, nullptr, 0, KEY_WRITE, nullptr,
                             &key, nullptr);
    if (r != ERROR_SUCCESS) return HRESULT_FROM_WIN32(r);
    r = RegSetValueExW(key, name, 0, REG_SZ, reinterpret_cast<const BYTE*>(value),
                       static_cast<DWORD>((wcslen(value) + 1) * sizeof(WCHAR)));
    RegCloseKey(key);
    return HRESULT_FROM_WIN32(r);
}

// The CLSID string is derived from guid.h so the two can never disagree.
static bool ClsidString(WCHAR (&text)[40]) {
    return StringFromGUID2(CLSID_FaceCredentialProvider, text, 40) > 0;
}

STDAPI DllRegisterServer(void) {
    WCHAR path[MAX_PATH];
    if (!GetModuleFileNameW(reinterpret_cast<HMODULE>(&__ImageBase), path, MAX_PATH)) {
        return HRESULT_FROM_WIN32(GetLastError());
    }
    WCHAR clsid[40];
    if (!ClsidString(clsid)) return E_FAIL;
    WCHAR sub[MAX_PATH];
    HRESULT hr;

    wsprintfW(sub, L"SOFTWARE\\Classes\\CLSID\\%s", clsid);
    if (FAILED(hr = WriteReg(sub, nullptr, L"Face Unlock Credential Provider"))) return hr;
    wsprintfW(sub, L"SOFTWARE\\Classes\\CLSID\\%s\\InprocServer32", clsid);
    if (FAILED(hr = WriteReg(sub, nullptr, path))) return hr;
    if (FAILED(hr = WriteReg(sub, L"ThreadingModel", L"Apartment"))) return hr;
    wsprintfW(sub,
        L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Authentication\\Credential Providers\\%s",
        clsid);
    return WriteReg(sub, nullptr, L"Face Unlock Credential Provider");
}

STDAPI DllUnregisterServer(void) {
    WCHAR clsid[40];
    if (!ClsidString(clsid)) return E_FAIL;
    WCHAR sub[MAX_PATH];
    wsprintfW(sub,
        L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Authentication\\Credential Providers\\%s",
        clsid);
    RegDeleteTreeW(HKEY_LOCAL_MACHINE, sub);
    wsprintfW(sub, L"SOFTWARE\\Classes\\CLSID\\%s", clsid);
    RegDeleteTreeW(HKEY_LOCAL_MACHINE, sub);
    return S_OK;
}
