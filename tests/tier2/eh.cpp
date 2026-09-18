// C++ exceptions with nested try/catch, a rethrow, and non-trivial destructors unwound across
// several frames: exercises .pdata/.xdata and the EH tables.
#include "common.h"

namespace {
int g_dtor_sum;

struct Guard {
  int id;
  explicit Guard(int i) : id(i) {}
  ~Guard() { g_dtor_sum += id; }
};

struct Error {
  int code;
};

__declspec(noinline) void thrower(int depth) {
  Guard guard(depth);
  if (depth == 0)
    throw Error{7};
  thrower(depth - 1);
}
} // namespace

int eh_check() {
  g_dtor_sum = 0;
  int caught = 0;
  try {
    try {
      thrower(3);
    } catch (Error &e) {
      Guard inner(100);
      caught = e.code;
      throw;
    }
  } catch (const Error &e) {
    caught += e.code * 10;
  }
  // Guards 0..3 unwind during the throw, then `inner` (100) during the rethrow.
  return caught == 77 && g_dtor_sum == 106;
}
