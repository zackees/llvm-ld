#include "lld/Common/Driver.h"
#include "llvm/Support/raw_ostream.h"
#include <cstring>
#include <vector>

LLD_HAS_DRIVER(coff)
LLD_HAS_DRIVER(mingw)

int main(int argc, char **argv) {
  if (argc < 3 || (std::strcmp(argv[1], "winlink") && std::strcmp(argv[1], "mingw"))) {
    llvm::errs() << "usage: llvm-ld-direct <winlink|mingw> <linker arguments...>\n";
    return 64;
  }
  std::vector<const char *> args(argv + 2, argv + argc);
  lld::DriverDef driver = !std::strcmp(argv[1], "winlink")
                              ? lld::DriverDef{lld::WinLink, &lld::coff::link}
                              : lld::DriverDef{lld::MinGW, &lld::mingw::link};
  try { return lld::lldMain(args, llvm::outs(), llvm::errs(), {driver}).retCode; }
  catch (...) { return 70; }
}
