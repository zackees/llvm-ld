// Shared header for the Tier 2 COFF corpus (issue #28). Everything here is emitted as a COMDAT in
// every TU that includes it, so the linker must deduplicate it to a single definition.
#pragma once

inline int shared_inline(int x) { return x * 3 + 1; }

inline int &shared_counter() {
  static int counter;
  return counter;
}

template <typename T> struct Accum {
  static T total;
  static T add(T v) {
    total += v;
    return total;
  }
};
template <typename T> T Accum<T>::total = 0;

__declspec(selectany) int g_selectany = 42;

typedef int (*int_fn)(int);

// comdat_a.cpp / comdat_b.cpp
int comdat_a_touch();
int comdat_b_touch();
int *comdat_a_selectany();
int *comdat_b_selectany();
int *comdat_a_accum_total();
int *comdat_b_accum_total();
int_fn comdat_a_inline_addr();
int_fn comdat_b_inline_addr();
int fold_a(int x);
int fold_b(int x);
int nofold_a(int x);
int nofold_b(int x);

// init_a.cpp / init_b.cpp
// volatile: keeps LTO from evaluating init_b.cpp's constructor at compile time with a stale value.
extern volatile int g_lib_seed;
extern int g_user_saw_seed;

// eh.cpp / tls.cpp / scale.cpp (generated)
int eh_check();
int tls_check();
int scale_check();

// C and assembly TUs
extern "C" int weak_check(void);
extern "C" int strong_check(void);
extern "C" int c_sum(const int *values, int count);
extern "C" long long asm_triple(long long x);
