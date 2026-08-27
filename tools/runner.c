#include "llvm_ld.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void write_stdout(void *context, const uint8_t *bytes, size_t length) {
  (void)context; (void)fwrite(bytes, 1, length, stdout);
}
static void write_stderr(void *context, const uint8_t *bytes, size_t length) {
  (void)context; (void)fwrite(bytes, 1, length, stderr);
}

int main(int argc, char **argv) {
  if (argc < 3 || (strcmp(argv[1], "winlink") && strcmp(argv[1], "mingw"))) {
    fprintf(stderr, "usage: llvm-ld-runner <winlink|mingw> <linker arguments...>\n");
    return 64;
  }
  uint8_t **bytes = (uint8_t **)calloc((size_t)argc - 2, sizeof(*bytes));
  size_t *lengths = (size_t *)calloc((size_t)argc - 2, sizeof(*lengths));
  if (!bytes || !lengths) return 70;
  for (int i = 2; i < argc; ++i) { bytes[i-2] = (uint8_t *)argv[i]; lengths[i-2] = strlen(argv[i]); }
  llvm_ld_request req = {0}; llvm_ld_result result = {0};
  req.struct_size=sizeof(req); req.abi_version=LLVM_LD_ABI_VERSION;
  req.driver=!strcmp(argv[1], "winlink") ? LLVM_LD_DRIVER_WINLINK : LLVM_LD_DRIVER_MINGW;
  req.argc=(uint32_t)(argc-2); req.argv=(const uint8_t *const *)bytes; req.argv_lengths=lengths;
  req.stdout_write=write_stdout; req.stderr_write=write_stderr;
  result.struct_size=sizeof(result);
  int32_t status=llvm_ld_invoke(&req, &result);
  free(bytes); free(lengths);
  if (status != LLVM_LD_OK) { fprintf(stderr, "llvm_ld_invoke failed: %d\n", status); return 70; }
  return result.return_code;
}
