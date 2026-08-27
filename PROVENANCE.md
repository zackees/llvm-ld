# Provenance and update policy

This repository wraps the COFF and MinGW drivers from LLVM LLD. The locked upstream is LLVM
`llvmorg-23.1.0`, commit `ea7d852a70e8bdfaf601d6626a760f9771b2c4b4`. The repository contains a
self-contained source payload generated from that exact commit and SHA-locked by
`provenance/llvm-source-closure.json`. Builds enable only LLD and the X86 target and link only
`lldCOFF`, `lldMinGW`, `lldCommon`, and LLVM libraries selected by their declared CMake graph.
That build graph is the mechanical dependency closure; additions must appear in generated compile
commands/depfiles and must not read source outside the pinned tree.

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

To update LLVM, change the tag and full commit together, regenerate build dependency evidence,
run the complete deterministic EXE/PDB matrix against a stock LLD built from the same commit, and
review the upstream Apache-2.0-with-LLVM-exception license delta. Never follow a floating branch.

`tools/materialize_llvm_payload.py` reproduces the committed payload from a reviewed full upstream
tree. Normal configuration performs no LLVM network acquisition. Windows acceptance rejects any
closure drift and publishes the fresh inventory; Linux publishes its platform-specific inventory
and both reject undeclared reads.
