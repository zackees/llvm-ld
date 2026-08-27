#include "llvm_ld.h"
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
  if (argc != 2) return 64;
  int dhat=!strcmp(argv[1],"mimalloc-dhat"), pprof=!strcmp(argv[1],"mimalloc-pprof");
  if (dhat && llvm_ld_profiler_start(LLVM_LD_PROFILER_DHAT) != LLVM_LD_OK) return 6;
  if (pprof && llvm_ld_profiler_start(LLVM_LD_PROFILER_SAMPLED) != LLVM_LD_OK) return 7;
  llvm_ld_allocator_info info = {0}; info.struct_size=sizeof(info);
  if (llvm_ld_allocator_get_info(&info) != LLVM_LD_OK) return 1;
  int expected=!strcmp(argv[1],"mimalloc") || dhat || pprof;
  if (!!info.sampled_profiler_active != pprof || !!info.dhat_active != dhat) return 2;
  if (pprof && !info.sampled_profiler_compiled) return 5;
  if (!!info.uses_mimalloc != expected || (!!info.state_cookie != expected)) return 3;
#if defined(_WIN32)
  if (!!info.crt_redirected != expected) return 4;
#endif
  printf("{\"mimalloc\":%u,\"pprof_compiled\":%u,\"pprof_active\":%u,\"dhat_active\":%u,\"crt_redirected\":%u}\n",
    info.uses_mimalloc,info.sampled_profiler_compiled,info.sampled_profiler_active,info.dhat_active,info.crt_redirected);
  if (dhat && llvm_ld_profiler_stop(LLVM_LD_PROFILER_DHAT) != LLVM_LD_OK) return 8;
  if (pprof && llvm_ld_profiler_stop(LLVM_LD_PROFILER_SAMPLED) != LLVM_LD_OK) return 9;
  return 0;
}
