// Interface of tier2lib.dll. The EXE imports it through /delayload, which only supports function
// imports, so no data is exported.
#pragma once

#ifdef TIER2_BUILD_DLL
#define TIER2_API __declspec(dllexport)
#else
#define TIER2_API __declspec(dllimport)
#endif

extern "C" TIER2_API int dll_add(int a, int b);
TIER2_API int dll_scale(int x);
