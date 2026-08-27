#include "llvm_ld.h"
#include "lld/Common/Driver.h"
#include "llvm/Support/raw_ostream.h"
#include <atomic>
#include <cstring>
#include <string>
#include <vector>
#include <cstdlib>
#if defined(LLVM_LD_USE_MIMALLOC)
#include "mimalloc-pprof-amalgamated.h"
#endif

LLD_HAS_DRIVER(coff)
LLD_HAS_DRIVER(mingw)

namespace {
std::atomic_flag invoking = ATOMIC_FLAG_INIT;
std::atomic<bool> poisoned{false};
class CallbackStream final : public llvm::raw_ostream {
public:
  CallbackStream(llvm_ld_write_fn fn, void *ctx) : fn_(fn), ctx_(ctx) { SetUnbuffered(); }
private:
  void write_impl(const char *ptr, size_t size) override {
    if (fn_ && size) fn_(ctx_, reinterpret_cast<const uint8_t *>(ptr), size);
  }
  uint64_t current_pos() const override { return 0; }
  llvm_ld_write_fn fn_; void *ctx_;
};
struct Unlock { ~Unlock() { invoking.clear(std::memory_order_release); } };
bool valid_utf8(const uint8_t *s, size_t n) {
  for (size_t i=0;i<n;) {
    uint8_t c=s[i++]; if (c==0) return false; if (c<0x80) continue;
    unsigned extra=(c>=0xC2&&c<=0xDF)?1:(c>=0xE0&&c<=0xEF)?2:(c>=0xF0&&c<=0xF4)?3:99;
    if (extra==99 || i+extra>n) return false;
    uint8_t first=s[i];
    if ((c==0xE0&&first<0xA0)||(c==0xED&&first>=0xA0)||(c==0xF0&&first<0x90)||(c==0xF4&&first>=0x90)) return false;
    for(unsigned j=0;j<extra;++j) if((s[i++]&0xC0)!=0x80) return false;
  }
  return true;
}
}

extern "C" uint32_t llvm_ld_abi_version(void) { return LLVM_LD_ABI_VERSION; }
extern "C" int32_t llvm_ld_allocator_get_info(llvm_ld_allocator_info *info) {
  if (!info || info->struct_size < sizeof(*info)) return LLVM_LD_E_INVALID;
  std::memset(reinterpret_cast<uint8_t *>(info) + sizeof(info->struct_size), 0,
              sizeof(*info) - sizeof(info->struct_size));
#if defined(LLVM_LD_USE_MIMALLOC)
  info->uses_mimalloc=1; info->mimalloc_version=static_cast<uint32_t>(mi_version());
#if MI_PPROF
  info->sampled_profiler_compiled=1;
#endif
  info->sampled_profiler_active=mi_prof_is_enabled()?1:0; info->dhat_active=mi_dhat_is_enabled()?1:0;
  info->state_cookie=reinterpret_cast<uintptr_t>(&mi_version);
  void *probe=std::malloc(17); info->crt_redirected=(probe && mi_is_in_heap_region(probe))?1:0; std::free(probe);
#endif
  return LLVM_LD_OK;
}
extern "C" int32_t llvm_ld_profiler_start(uint32_t profiler) {
#if defined(LLVM_LD_USE_MIMALLOC)
  if (invoking.test_and_set(std::memory_order_acquire)) return LLVM_LD_E_BUSY;
  Unlock unlock;
  if (profiler == LLVM_LD_PROFILER_DHAT) return mi_dhat_start() ? LLVM_LD_OK : LLVM_LD_E_EXCEPTION;
#if MI_PPROF
  if (profiler == LLVM_LD_PROFILER_SAMPLED) return mi_prof_start(0) ? LLVM_LD_OK : LLVM_LD_E_EXCEPTION;
#else
  if (profiler == LLVM_LD_PROFILER_SAMPLED) return LLVM_LD_E_UNSUPPORTED;
#endif
  return LLVM_LD_E_INVALID;
#else
  (void)profiler;
  return LLVM_LD_E_UNSUPPORTED;
#endif
}
extern "C" int32_t llvm_ld_profiler_stop(uint32_t profiler) {
#if defined(LLVM_LD_USE_MIMALLOC)
  if (invoking.test_and_set(std::memory_order_acquire)) return LLVM_LD_E_BUSY;
  Unlock unlock;
  if (profiler == LLVM_LD_PROFILER_DHAT) { mi_dhat_stop(); return LLVM_LD_OK; }
#if MI_PPROF
  if (profiler == LLVM_LD_PROFILER_SAMPLED) { mi_prof_stop(); return LLVM_LD_OK; }
#else
  if (profiler == LLVM_LD_PROFILER_SAMPLED) return LLVM_LD_E_UNSUPPORTED;
#endif
  return LLVM_LD_E_INVALID;
#else
  (void)profiler;
  return LLVM_LD_E_UNSUPPORTED;
#endif
}
extern "C" int32_t llvm_ld_invoke(const llvm_ld_request *req, llvm_ld_result *out) {
  if (!req || !out || req->struct_size < sizeof(*req) || out->struct_size < sizeof(*out) ||
      req->abi_version != LLVM_LD_ABI_VERSION || (req->argc && (!req->argv || !req->argv_lengths))) return LLVM_LD_E_INVALID;
  if (poisoned.load(std::memory_order_acquire)) return LLVM_LD_E_POISONED;
  if (invoking.test_and_set(std::memory_order_acquire)) return LLVM_LD_E_BUSY;
  Unlock unlock;
  try {
    std::vector<std::string> owned; owned.reserve(req->argc);
    std::vector<const char *> args; args.reserve(req->argc);
    for (uint32_t i = 0; i < req->argc; ++i) {
      if ((!req->argv[i] && req->argv_lengths[i]) || (req->argv[i] && !valid_utf8(req->argv[i],req->argv_lengths[i]))) return LLVM_LD_E_INVALID;
      if (req->argv_lengths[i]==0) owned.emplace_back();
      else owned.emplace_back(reinterpret_cast<const char *>(req->argv[i]), req->argv_lengths[i]);
    }
    for (const auto &s : owned) args.push_back(s.c_str());
    CallbackStream stdout_os(req->stdout_write, req->callback_context);
    CallbackStream stderr_os(req->stderr_write, req->callback_context);
    lld::DriverDef driver;
    if (req->driver == LLVM_LD_DRIVER_WINLINK) driver = {lld::WinLink, &lld::coff::link};
    else if (req->driver == LLVM_LD_DRIVER_MINGW) driver = {lld::MinGW, &lld::mingw::link};
    else return LLVM_LD_E_INVALID;
    lld::Result result = lld::lldMain(args, stdout_os, stderr_os, {driver});
    out->return_code = result.retCode;
    out->can_run_again = result.canRunAgain ? 1 : 0;
    if (!result.canRunAgain) poisoned.store(true, std::memory_order_release);
    return LLVM_LD_OK;
  } catch (...) {
    poisoned.store(true, std::memory_order_release);
    return LLVM_LD_E_EXCEPTION;
  }
}
