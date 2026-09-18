// Library-segment static initializer: #pragma init_seg(lib) places it in .CRT$XCL, which the CRT
// runs before the default user segment (.CRT$XCU) used by init_b.cpp.
#include "common.h"

#pragma init_seg(lib)

volatile int g_lib_seed;

namespace {
struct LibSeed {
  LibSeed() { g_lib_seed = shared_inline(411); }
};
LibSeed lib_seed;
} // namespace
