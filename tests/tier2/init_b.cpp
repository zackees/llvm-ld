// User-segment static initializer that depends on init_a.cpp's library-segment initializer having
// already run. If the linker merged .CRT$XC* in the wrong order it would observe zero.
#include "common.h"

int g_user_saw_seed = -1;

namespace {
struct UserInit {
  UserInit() { g_user_saw_seed = g_lib_seed; }
};
UserInit user_init;
} // namespace
