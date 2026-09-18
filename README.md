# llvm-ld

A narrow, C-compatible library around the pinned LLVM LLD WinLink and MinGW PE/COFF drivers, using
the native allocator shipped by checksum-verified `mimalloc-pprof` 0.9.5.

This is bootstrap work tracked by issue #1. The ABI is versioned and size-tagged, accepts
length-delimited UTF-8 arguments, forwards raw diagnostic bytes to callbacks, serializes calls,
and permanently rejects re-entry after LLD reports unsafe state. See `PROVENANCE.md` for locked
inputs, licenses, allocator ownership, and the upstream update procedure.

## Link speed

The COFF driver's PDB emission was parallelized and its input scan rewritten; the linker produces
byte-identical EXE and PDB output and links substantially faster. The panels below are republished
from `main` by the `link-benchmark` workflow, at most once a day and only when the commit has moved.

[![Link speedup by thread count, one line per corpus; higher is better](https://raw.githubusercontent.com/zackees/llvm-ld/benchmark-stats/link-speedup-threads.svg)](https://zackees.github.io/llvm-ld/#threads)

[![Link speedup over published runs at the highest measured thread count](https://raw.githubusercontent.com/zackees/llvm-ld/benchmark-stats/link-speedup-history.svg)](https://zackees.github.io/llvm-ld/#history)

Each point is a paired A/B measured in a single run on a single machine: the current linker against
a baseline built from the payload as it stood before the link-speed patches, linking the same
corpora interleaved. Hosted runners are shared and noisy, so absolute wall time compared across
runs is deliberately not published — a paired ratio is what survives that noise. Every cell is
gated on byte-identical output: `tests/perf/bench.py` refuses to report a timing unless the
candidate's EXE and PDB match the baseline's exactly and both are self-deterministic.

**Scope and caveats**

- Thread cap: the workflow measures 1, 2 and 4 threads, plus the runner's core count when that is
  larger than 4. The hosted `ubuntu-24.04` runner has 4 cores, so the history panel's "highest
  measured thread count" is 4 there. The +46% headline was measured at 16 threads on a local workstation, and the
  published panels do not show that regime.
- Allocator: on Linux the benchmarked `llvm-ld-direct` allocates through glibc malloc, because
  `MI_MALLOC_OVERRIDE` is defined only under `if(WIN32)` in `CMakeLists.txt`. The shipped Windows
  DLL allocates through mimalloc. These are parallelisation patches, and contention behaviour
  differs between glibc arenas and mimalloc.
- Platform transfer: the numbers are measured on Linux and the shipping target is Windows. That
  the relative speedup carries over to Windows is asserted, not measured. One known divergence
  sits in an optimised phase: `createFutureForFile` is `std::launch::deferred` on Linux and
  `std::launch::async` under `_WIN64`. A Windows measurement of the same paired ratio is still to
  be done (see issue #23).

The underlying data is on the [`benchmark-stats` branch](https://github.com/zackees/llvm-ld/tree/benchmark-stats)
(`latest.json`, `history.jsonl`) and on the [dashboard](https://zackees.github.io/llvm-ld/).

## Building

The supported configuration is CMake with the Ninja generator on MSVC. The Visual Studio
generator is not supported: `llvm/utils/LLVMVisualizers` is absent from the vendored closure, but
the VS generator defaults `LLVM_ADD_NATIVE_VISUALIZERS_TO_SOLUTION` to `ON`, so configure fails. The
mimalloc-pprof allocator payload is fetched and checksum-verified at first configure, so a
network-free fresh clone cannot configure.

CI caches the pinned LLVM object compiles with sccache on every platform, since the payload is
SHA-pinned and identical across PRs; warm runs hit above 99%. For the same effect locally, pass
`-DCMAKE_CXX_COMPILER_LAUNCHER=sccache -DCMAKE_C_COMPILER_LAUNCHER=sccache` to `cmake`. Leave
precompiled headers alone: sccache keys on preprocessed source, so `/Yu` compiles cache normally
and disabling PCH only makes cold builds slower. A first run on a new branch is cold regardless,
because GitHub Actions scopes its cache per branch.

`/manifestinput:` merging (`/manifest:embed` and side-by-side) runs entirely in-process against a
vendored, pinned libxml2 static library — no external Windows SDK `mt.exe` is required.
