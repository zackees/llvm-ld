#include "llvm_ld.h"
#include "lld/Common/Driver.h"
#include "llvm/Support/raw_ostream.h"
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>

LLD_HAS_DRIVER(coff)
LLD_HAS_DRIVER(mingw)
namespace lld::coff { bool link(llvm::ArrayRef<const char *>, llvm::raw_ostream &, llvm::raw_ostream &, bool, bool) { return true; } }
namespace lld::mingw { bool link(llvm::ArrayRef<const char *>, llvm::raw_ostream &, llvm::raw_ostream &, bool, bool) { return true; } }

namespace {
std::mutex mutex;
std::condition_variable cv;
bool entered=false, release_call=false;
std::atomic<int> mode{0};
std::string stdout_bytes, stderr_bytes;
void capture_out(void *, const uint8_t *p, size_t n) { stdout_bytes.append(reinterpret_cast<const char *>(p), n); }
void capture_err(void *, const uint8_t *p, size_t n) { stderr_bytes.append(reinterpret_cast<const char *>(p), n); }
llvm_ld_request request() {
  static const uint8_t arg[]={'l','l','d','-','l','i','n','k'}; static const uint8_t *argv[]={arg}; static const size_t lengths[]={sizeof(arg)};
  llvm_ld_request r{}; r.struct_size=sizeof(r); r.abi_version=LLVM_LD_ABI_VERSION; r.driver=LLVM_LD_DRIVER_WINLINK;
  r.argc=1; r.argv=argv; r.argv_lengths=lengths; r.stdout_write=capture_out; r.stderr_write=capture_err; return r;
}
}

namespace lld {
Result lldMain(llvm::ArrayRef<const char *> args, llvm::raw_ostream &out, llvm::raw_ostream &err,
               llvm::ArrayRef<DriverDef> drivers) {
  if (args.size()!=1 || std::string(args[0])!="lld-link" || drivers.size()!=1) return {91,true};
  out << llvm::StringRef("out\0bytes",9); err << llvm::StringRef("err\0bytes",9);
  if (mode.load()==1) {
    std::unique_lock<std::mutex> lock(mutex); entered=true; cv.notify_all(); cv.wait(lock,[]{return release_call;});
  }
  return mode.load()==2 ? Result{17,false} : Result{7,true};
}
}

int main() {
  llvm_ld_request r=request(); llvm_ld_result result{}; result.struct_size=sizeof(result);
  if (llvm_ld_invoke(&r,&result)!=LLVM_LD_OK || result.return_code!=7 || !result.can_run_again) return 1;
  if (stdout_bytes!=std::string("out\0bytes",9) || stderr_bytes!=std::string("err\0bytes",9)) return 2;
  mode=1; std::thread first([&]{ llvm_ld_result x{}; x.struct_size=sizeof(x); if(llvm_ld_invoke(&r,&x)!=LLVM_LD_OK) std::abort(); });
  { std::unique_lock<std::mutex> lock(mutex); cv.wait(lock,[]{return entered;}); }
  llvm_ld_result busy{}; busy.struct_size=sizeof(busy);
  if (llvm_ld_invoke(&r,&busy)!=LLVM_LD_E_BUSY) return 3;
  { std::lock_guard<std::mutex> lock(mutex); release_call=true; } cv.notify_all(); first.join();
  mode=2; llvm_ld_result poison{}; poison.struct_size=sizeof(poison);
  if (llvm_ld_invoke(&r,&poison)!=LLVM_LD_OK || poison.return_code!=17 || poison.can_run_again) return 4;
  if (llvm_ld_invoke(&r,&poison)!=LLVM_LD_E_POISONED) return 5;
  return 0;
}
