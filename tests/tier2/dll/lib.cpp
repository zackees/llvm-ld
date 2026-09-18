// tier2lib.dll: one C-linkage and one C++-mangled export, plus a DLL-side static initializer.
#define TIER2_BUILD_DLL
#include "lib.h"

namespace {
int g_dll_bias;
struct DllInit {
  DllInit() { g_dll_bias = 1000; }
};
DllInit dll_init;
} // namespace

extern "C" TIER2_API int dll_add(int a, int b) { return a + b + g_dll_bias; }
TIER2_API int dll_scale(int x) { return x * 25; }
