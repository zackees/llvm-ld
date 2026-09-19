# llvm-ld

A narrow, C-compatible library around the pinned LLVM LLD WinLink and MinGW PE/COFF drivers, using
the native allocator shipped by checksum-verified `mimalloc-pprof` 0.9.5.

This is bootstrap work tracked by issue #1. The ABI is versioned and size-tagged, accepts
length-delimited UTF-8 arguments, forwards raw diagnostic bytes to callbacks, serializes calls,
and permanently rejects re-entry after LLD reports unsafe state. See `PROVENANCE.md` for locked
inputs, licenses, allocator ownership, and the upstream update procedure.

## Link speed

The COFF driver's PDB emission was parallelized and its input scan rewritten; the linker produces
byte-identical output and links substantially faster. The chart shows the current `main` only; the
`link-benchmark` workflow republishes it at most once a day, and only when the commit has moved.

<a href="https://zackees.github.io/llvm-ld/#overview"><img alt="Link time of stock lld-link and llvm-ld by build type (Debug, Release, ThinLTO) and corpus size, split into the link itself and the extra time the PDB adds" src="https://raw.githubusercontent.com/zackees/llvm-ld/benchmark-stats/link-speed-overview-dark.svg"></a>

Rows are build types (Debug `clang -O0 -g`, Release `clang -O2 -g`, ThinLTO `clang -O2 -g
-flto=thin`), columns are corpus sizes (64, 512 and 2048 objects). Stock `lld-link` is dark blue,
llvm-ld is light blue. Each corpus is linked twice with the same objects: the **solid** segment is
the link without `/debug`, and the **hatched** segment is the extra time `/debug:full` adds to
write the PDB, which is where the patches work. When that extra time is inside run-to-run noise
(ThinLTO, where codegen dominates), the cell shows a plain bar and says so.

Every number is a paired A/B measured in a single run on a single machine: the current linker
against a baseline built from the payload as it stood before the link-speed patches, linking the
same corpus interleaved. Hosted runners are shared and noisy, so times are only ever compared within
one run. Every cell is gated on byte-identical output: `tests/perf/bench.py` refuses to report a
timing unless the candidate's EXE (and PDB, when one is written) match the baseline's exactly and
both are self-deterministic.

**Scope and caveats**

- Thread cap: non-LTO modes measure 1, 2 and 4 threads, plus the runner's core count when that is
  larger than 4. The hosted `ubuntu-24.04` runner has 4 cores, so the chart shows 4 threads
  there. The +46% headline was measured at 16 threads on a local workstation, and the
  published charts do not show that regime.
- ThinLTO is measured on the small corpus at the highest thread count only, with 5 paired links:
  every ThinLTO link runs LLVM codegen, and one medium-corpus link takes about two minutes even on
  16 cores.
- Allocator: on Linux the benchmarked `llvm-ld-direct` allocates through glibc malloc, because
  `MI_MALLOC_OVERRIDE` is defined only under `if(WIN32)` in `CMakeLists.txt`. The shipped Windows
  DLL allocates through mimalloc. These are parallelisation patches, and contention behaviour
  differs between glibc arenas and mimalloc.
- Platform transfer: the numbers are measured on Linux and the shipping target is Windows. That
  the relative speedup carries over to Windows is asserted, not measured. One known divergence
  sits in an optimised phase: `createFutureForFile` is `std::launch::deferred` on Linux and
  `std::launch::async` under `_WIN64`. A Windows measurement of the same paired ratio is still to
  be done (see issue #23).

The underlying data is [`latest.json`](https://github.com/zackees/llvm-ld/blob/benchmark-stats/latest.json)
on the `benchmark-stats` branch, and every cell is tabulated on the
[dashboard](https://zackees.github.io/llvm-ld/).

### Faster links you can opt into

By default llvm-ld's output is byte-identical to stock `lld-link` at the pinned LLVM version, and
that is gated in CI. Faster links that **change the output** are only ever enabled by you, with an
explicit flag (#40); llvm-ld never turns one on by itself. These upstream lld flags qualify today
(same bytes from stock `lld-link` and llvm-ld for the same flag):

| flag | what it changes | measured effect (ThinLTO, small corpus, 4 threads) |
|---|---|---|
| `/opt:lldlto=0` | skips cross-module (LTO) optimization; each module keeps the optimization it was compiled with | **~8x faster link** (11.3 s -> 1.4 s) |
| `/opt:lldlto=1` | lighter cross-module optimization pipeline | ~8% faster |
| `/opt:lldltocgo=1` | lower codegen optimization level | no measurable gain |
| `/lldltocache:<dir>` | nothing: output is identical; unchanged modules skip codegen on relink | incremental relinks only |

With a ThinLTO build, nearly all link time is LLVM optimization and codegen rather than linking,
so these flags are where large ThinLTO speedups come from; they trade runtime performance of the
linked program for link time.

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
