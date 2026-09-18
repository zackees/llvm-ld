// Second instantiation site for the COMDATs in common.h; see comdat_a.cpp.
#include "common.h"

int comdat_b_touch() {
  ++shared_counter();
  return Accum<int>::add(7);
}
int *comdat_b_selectany() { return &g_selectany; }
int *comdat_b_accum_total() { return &Accum<int>::total; }
int_fn comdat_b_inline_addr() { return &shared_inline; }

__declspec(noinline) int fold_b(int x) { return x * 7 + 3; }
__declspec(noinline) int nofold_b(int x) { return x * 9 + 4; }
