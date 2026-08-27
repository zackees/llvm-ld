# llvm-ld

A narrow, C-compatible library around the pinned LLVM LLD WinLink and MinGW PE/COFF drivers, using
the native allocator shipped by checksum-verified `mimalloc-pprof` 0.9.5.

This is bootstrap work tracked by issue #1. The ABI is versioned and size-tagged, accepts
length-delimited UTF-8 arguments, forwards raw diagnostic bytes to callbacks, serializes calls,
and permanently rejects re-entry after LLD reports unsafe state. See `PROVENANCE.md` for locked
inputs, licenses, allocator ownership, and the upstream update procedure.

## Building

The supported configuration is CMake with the Ninja generator on MSVC. The Visual Studio
generator is not supported: `llvm/utils/LLVMVisualizers` is pruned from the payload, but the VS
generator defaults `LLVM_ADD_NATIVE_VISUALIZERS_TO_SOLUTION` to `ON`, so configure fails. The
mimalloc-pprof allocator payload is fetched and checksum-verified at first configure, so a
network-free fresh clone cannot configure.
