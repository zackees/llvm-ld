# Provenance and update policy

This repository wraps the COFF and MinGW drivers from LLVM LLD. The locked upstream is LLVM
`llvmorg-23.1.0`, commit `ea7d852a70e8bdfaf601d6626a760f9771b2c4b4`. The repository contains a
self-contained source payload generated from that exact commit and SHA-locked by
`provenance/llvm-source-closure.json`. Builds enable only LLD and the X86 target and link only
`lldCOFF`, `lldMinGW`, `lldCommon`, and LLVM libraries selected by their declared CMake graph.
That build graph is the mechanical dependency closure; additions must appear in generated compile
commands/depfiles and must not read source outside the pinned tree.

The payload committed under `llvm-project/` is not a verbatim mirror of that commit; it is a
pruned subset plus one restored file, and `provenance/llvm-source-closure.json` remains the sole
authority over its exact contents, regenerated from the pruned tree by the same mechanical
evidence pipeline described above rather than hand-edited. Removed are all subdirectories of
`llvm/tools` (the directory's own CMakeLists.txt is kept, since it is the hook that pulls in lld
via `add_llvm_external_project`), all subdirectories of `llvm/utils` except `TableGen`,
`lld/{ELF,MachO,wasm,tools,docs}`, and these `llvm/lib` directories: ABI, FuzzMutate, FileCheck,
InterfaceStub, CAS, DWARFLinker, MCA, ObjCopy, ObjectYAML, HTTP, Debuginfod, DWARFCFIChecker, DWP,
ExecutionEngine, LineEditor, XRay, Telemetry, and DebugInfo/LogicalView. Those targets were
configured but never built by this repository's build graph, and several were latently
unbuildable upstream (lld's ELF/MachO/wasm drivers were missing their `Options.td` and lib-local
headers; the `lld` executable was missing `llvm/Support/LLVMDriver.h`); removing them makes the
build graph honest and shrinks the audit surface by roughly 905 files / 12.7 MB. One file is
restored on top of the prune: `llvm/cmake/modules/CrossCompile.cmake`, taken from upstream at the
same pinned commit, because any `CMAKE_CROSSCOMPILING` configure unconditionally hits
`include(CrossCompile)` at `llvm/CMakeLists.txt:1351`, and this repository intends to support
future Linux-to-Windows cross-compilation. Not stripped, because it is load-bearing: `libc/`
(`llvm/lib/Support/APFloat.cpp` includes `libc/shared/math.h` in LLVM 23), the
AArch64/ARM/PowerPC/RISCV `.td` files TableGen consumes to generate TargetParser headers,
`llvm/utils/TableGen` itself (builds `llvm-tblgen`), and the full linked closure named above (MC,
Object, DebugInfo/{PDB,CodeView,MSF,DWARF}, LTO, Passes, Transforms, Target/X86, Demangle,
WindowsDriver, WindowsManifest, Option, Support).

Beyond deletion and restoration, exactly two upstream CMake files are patched, and they are this
repository's only upstream source deltas — every other retained file stays byte-identical to
upstream. `llvm-project/lld/CMakeLists.txt` has the `add_subdirectory` calls for `tools/lld`,
`docs`, `ELF`, `MachO`, and `wasm` removed. `llvm-project/llvm/lib/CMakeLists.txt` (and,
consequently, `llvm-project/llvm/lib/DebugInfo/CMakeLists.txt`) has the `add_subdirectory` calls
for the unused libraries listed above removed.

The allocator payload is the crates.io `mimalloc-pprof` 0.9.5 archive whose SHA-256 is
`9143eb24178e618a9671d22074e2e64badd61454a1ae5c042325daf8238f5407`. `tools/import.py` verifies
the archive before extraction and inventories every extracted file, including
`.cargo_vcs_info.json`. The native mimalloc tree is compiled once into `llvm_ld`; consumers must
not link another mimalloc state. Windows CRT allocation overrides therefore resolve to this sole
state. The default build compiles sampled pprof hooks out (`MI_PPROF=0`) and leaves exact DHAT
inactive. DHAT support is always present in the 0.9.5 amalgamation but performs no collection until
the versioned `llvm_ld_profiler_start` C API explicitly starts it; diagnostic jobs opt in through
`LLVM_LD_ENABLE_PPROF` or `LLVM_LD_ENABLE_DHAT`. Benchmark jobs reject either option and clear all
profiler activation environment variables.

On MSVC the complete LLVM/LLD closure and wrapper use the static CRT (`/MT`, `/MTd` for Debug).
This is required because mimalloc's supported Windows override is intentionally disabled when
`_DLL` selects the process-wide dynamic CRT. The C ABI transfers only caller-owned argument bytes,
callbacks, and scalar structs, so no allocation crosses the CRT boundary. `allocator-probe` proves
that `malloc` inside the library resolves into the one embedded mimalloc heap; its system-baseline
positive control proves the check fails without that redirection.

To update LLVM, change the tag and full commit together, re-apply the same prune list and the two
CMakeLists.txt patches described above to the new tree, regenerate build dependency evidence, run
the complete deterministic EXE/PDB matrix against a stock LLD built from the same commit, and
review the upstream Apache-2.0-with-LLVM-exception license delta. Never follow a floating branch.

`tools/materialize_llvm_payload.py` is the reproducer: it materializes the committed payload from
a reviewed full upstream tree by applying that same prune list and those same patches, so it is
also the reference for exactly what must be redone on every bump. Normal configuration performs no
LLVM network acquisition. Windows acceptance rejects any closure drift and publishes the fresh
inventory; Linux publishes its platform-specific inventory and both reject undeclared reads.
