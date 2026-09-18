// Template/inline/selectany COMDATs instantiated here and in comdat_b.cpp, plus one member of an
// ICF-foldable pair (fold_a/fold_b have identical bodies) and one of an unfoldable pair.
#include "common.h"

int comdat_a_touch() {
  ++shared_counter();
  return Accum<int>::add(5);
}
int *comdat_a_selectany() { return &g_selectany; }
int *comdat_a_accum_total() { return &Accum<int>::total; }
int_fn comdat_a_inline_addr() { return &shared_inline; }

__declspec(noinline) int fold_a(int x) { return x * 7 + 3; }
__declspec(noinline) int nofold_a(int x) { return x * 7 + 4; }
