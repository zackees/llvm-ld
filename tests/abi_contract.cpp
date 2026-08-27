#include "llvm_ld.h"
#include <cstdint>
#include <string>

static std::string diagnostics;
static void capture(void *, const uint8_t *bytes, size_t length) {
  diagnostics.append(reinterpret_cast<const char *>(bytes), length);
}
static int invoke_bad(uint32_t driver) {
  const uint8_t *argv[] = {reinterpret_cast<const uint8_t *>("lld-link"), reinterpret_cast<const uint8_t *>("/definitely-invalid")};
  size_t lengths[] = {8, 19};
  llvm_ld_request req{}; llvm_ld_result out{};
  req.struct_size=sizeof(req); req.abi_version=LLVM_LD_ABI_VERSION; req.driver=driver;
  req.argc=2; req.argv=argv; req.argv_lengths=lengths; req.stderr_write=capture;
  out.struct_size=sizeof(out);
  int status=llvm_ld_invoke(&req, &out);
  return status == LLVM_LD_OK ? out.return_code : status;
}
int main() {
  llvm_ld_result out{}; out.struct_size=sizeof(out);
  if (llvm_ld_invoke(nullptr, &out) != LLVM_LD_E_INVALID) return 1;
  if (invoke_bad(99) != LLVM_LD_E_INVALID) return 2;
  const uint8_t invalid_utf8[]={0xC0,0xAF}; const uint8_t *bad_argv[]={invalid_utf8}; size_t bad_lengths[]={2};
  llvm_ld_request malformed{}; malformed.struct_size=sizeof(malformed); malformed.abi_version=LLVM_LD_ABI_VERSION;
  malformed.driver=LLVM_LD_DRIVER_WINLINK; malformed.argc=1; malformed.argv=bad_argv; malformed.argv_lengths=bad_lengths;
  if (llvm_ld_invoke(&malformed,&out)!=LLVM_LD_E_INVALID) return 5;
  const uint8_t embedded_nul[]={'x',0,'y'}; bad_argv[0]=embedded_nul; bad_lengths[0]=3;
  if (llvm_ld_invoke(&malformed,&out)!=LLVM_LD_E_INVALID) return 6;
  if (invoke_bad(LLVM_LD_DRIVER_WINLINK) == 0 || diagnostics.empty()) return 3;
  diagnostics.clear();
  if (invoke_bad(LLVM_LD_DRIVER_WINLINK) == 0 || diagnostics.empty()) return 4;
  return 0;
}
