# llvm-ld

A narrow, C-compatible library around the pinned LLVM LLD WinLink and MinGW PE/COFF drivers, using
the native allocator shipped by checksum-verified `mimalloc-pprof` 0.9.5.

This is bootstrap work tracked by issue #1. The ABI is versioned and size-tagged, accepts
length-delimited UTF-8 arguments, forwards raw diagnostic bytes to callbacks, serializes calls,
and permanently rejects re-entry after LLD reports unsafe state. See `PROVENANCE.md` for locked
inputs, licenses, allocator ownership, and the upstream update procedure.

## Building

The supported configuration is CMake with the Ninja generator on MSVC. The Visual Studio
generator is not supported: `llvm/utils/LLVMVisualizers` is absent from the vendored closure, but
the VS generator defaults `LLVM_ADD_NATIVE_VISUALIZERS_TO_SOLUTION` to `ON`, so configure fails. The
mimalloc-pprof allocator payload is fetched and checksum-verified at first configure, so a
network-free fresh clone cannot configure.

CI caches the pinned LLVM object compiles with sccache on every platform, since the
payload is SHA-pinned and identical across PRs. For the same effect locally, pass
`-DCMAKE_CXX_COMPILER_LAUNCHER=sccache -DCMAKE_C_COMPILER_LAUNCHER=sccache -DLLVM_ENABLE_PCH=OFF`
to `cmake` (PCH must stay off — sccache does not cache MSVC PCH compiles).

`/manifestinput:` merging (`/manifest:embed` and side-by-side) runs entirely in-process against a
vendored, pinned libxml2 static library — no external Windows SDK `mt.exe` is required.
