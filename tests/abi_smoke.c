#include "llvm_ld.h"
#include <stddef.h>
int main(void) {
  if (llvm_ld_abi_version() != LLVM_LD_ABI_VERSION) return 1;
  llvm_ld_allocator_info allocator = {0}; allocator.struct_size = sizeof(allocator);
  if (llvm_ld_allocator_get_info(&allocator) != LLVM_LD_OK || !allocator.uses_mimalloc || !allocator.state_cookie) return 3;
  if (allocator.sampled_profiler_active || allocator.dhat_active) return 4;
#if defined(_WIN32)
  if (!allocator.crt_redirected) return 5;
#endif
  llvm_ld_request request = {0};
  llvm_ld_result result = {0};
  request.struct_size = sizeof(request);
  request.abi_version = LLVM_LD_ABI_VERSION;
  result.struct_size = sizeof(result);
  return llvm_ld_invoke(&request, &result) == LLVM_LD_E_INVALID ? 0 : 2;
}
