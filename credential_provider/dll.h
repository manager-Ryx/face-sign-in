#pragma once

// Module reference count: every live COM object and server lock holds one,
// so DllCanUnloadNow never lets COM unload code that is still running.
void DllAddRef();
void DllRelease();
