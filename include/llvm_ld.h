#ifndef LLVM_LD_H
#define LLVM_LD_H
#include <stddef.h>
#include <stdint.h>
#if defined(_WIN32)
# if defined(LLVM_LD_BUILDING_LIBRARY)
#  define LLVM_LD_API __declspec(dllexport)
# else
#  define LLVM_LD_API __declspec(dllimport)
# endif
#else
# define LLVM_LD_API __attribute__((visibility("default")))
#endif
#ifdef __cplusplus
extern "C" {
#endif
#define LLVM_LD_ABI_VERSION 1u
typedef enum llvm_ld_driver { LLVM_LD_DRIVER_WINLINK = 1, LLVM_LD_DRIVER_MINGW = 2 } llvm_ld_driver;
typedef enum llvm_ld_profiler { LLVM_LD_PROFILER_SAMPLED = 1, LLVM_LD_PROFILER_DHAT = 2 } llvm_ld_profiler;
typedef void (*llvm_ld_write_fn)(void *context, const uint8_t *bytes, size_t length);
typedef struct llvm_ld_request {
  uint32_t struct_size;
  uint32_t abi_version;
  uint32_t driver;
  uint32_t argc;
  const uint8_t *const *argv;
  const size_t *argv_lengths;
  llvm_ld_write_fn stdout_write;
  llvm_ld_write_fn stderr_write;
  void *callback_context;
} llvm_ld_request;
typedef struct llvm_ld_result {
  uint32_t struct_size;
  int32_t return_code;
  uint8_t can_run_again;
  uint8_t reserved[3];
} llvm_ld_result;
typedef struct llvm_ld_allocator_info {
  uint32_t struct_size;
  uint32_t mimalloc_version;
  uint8_t uses_mimalloc;
  uint8_t sampled_profiler_compiled;
  uint8_t sampled_profiler_active;
  uint8_t dhat_active;
  uint8_t crt_redirected;
  uint8_t reserved[7];
  uintptr_t state_cookie;
} llvm_ld_allocator_info;
enum { LLVM_LD_OK = 0, LLVM_LD_E_INVALID = -1, LLVM_LD_E_BUSY = -2, LLVM_LD_E_POISONED = -3, LLVM_LD_E_EXCEPTION = -4, LLVM_LD_E_UNSUPPORTED = -5 };
LLVM_LD_API uint32_t llvm_ld_abi_version(void);
LLVM_LD_API int32_t llvm_ld_allocator_get_info(llvm_ld_allocator_info *info);
LLVM_LD_API int32_t llvm_ld_profiler_start(uint32_t profiler);
LLVM_LD_API int32_t llvm_ld_profiler_stop(uint32_t profiler);
LLVM_LD_API int32_t llvm_ld_invoke(const llvm_ld_request *request, llvm_ld_result *result);
#ifdef __cplusplus
}
#endif
#endif
