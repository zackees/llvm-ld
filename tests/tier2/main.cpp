// Tier 2 corpus entry point (issue #28). Checks each linker-sensitive behaviour at runtime and
// exits with the number of failed checks, so a link that is byte-identical but wrong still fails.
#include <stdio.h>

#include "common.h"
#include "dll/lib.h"

extern "C" __declspec(dllimport) void *__stdcall GetModuleHandleA(const char *name);

static int g_failures;

static void check(const char *name, bool ok) {
  printf("tier2: %-28s %s\n", name, ok ? "ok" : "FAIL");
  if (!ok)
    ++g_failures;
}

int main() {
  check("init_seg ordering", g_lib_seed == 1234 && g_user_saw_seed == 1234);

  int after_a = comdat_a_touch();
  int after_b = comdat_b_touch();
  check("template static COMDAT", after_a == 5 && after_b == 12 &&
                                      comdat_a_accum_total() == comdat_b_accum_total());
  check("selectany data", comdat_a_selectany() == comdat_b_selectany() && g_selectany == 42);
  check("inline function COMDAT", comdat_a_inline_addr() == comdat_b_inline_addr() &&
                                      shared_counter() == 2 && shared_inline(2) == 7);

  int_fn fa = fold_a, fb = fold_b, na = nofold_a, nb = nofold_b;
  check("ICF foldable pair", fa(3) == 24 && fb(3) == 24);
  check("ICF unfoldable pair", na != nb && na(3) == 25 && nb(3) == 31);
  printf("tier2: (info) fold_a %s fold_b\n", fa == fb ? "==" : "!=");

  check("C++ exceptions", eh_check() != 0);
  check("TLS", tls_check() != 0);
  check("weak external fallback", weak_check() != 0);
  check("weak external override", strong_check() != 0);

  const int values[] = {1, 2, 3, 4};
  check("C translation unit", c_sum(values, 4) == 10);
  check("ml64 translation unit", asm_triple(14) == 42);
  check("generated scale TU", scale_check() != 0);

  bool loaded_before = GetModuleHandleA("tier2lib.dll") != nullptr;
  int sum = dll_add(2, 3);
  int scaled = dll_scale(4);
  bool loaded_after = GetModuleHandleA("tier2lib.dll") != nullptr;
  check("delay-loaded DLL imports", sum == 1005 && scaled == 100);
  check("delay-load is lazy", !loaded_before && loaded_after);

  printf("tier2: %d failure(s)\n", g_failures);
  return g_failures;
}
