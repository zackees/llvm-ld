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
evidence pipeline described above rather than hand-edited: it lists 5,609 files. Removed are all
subdirectories of `llvm/tools` (the directory's own CMakeLists.txt is kept, since it is the hook
that pulls in lld via `add_llvm_external_project`), `lld/{ELF,MachO,wasm,tools,docs}`, and these
`llvm/lib` directories: ABI, FuzzMutate, FileCheck, InterfaceStub, CAS, DWARFLinker, MCA, ObjCopy,
ObjectYAML, HTTP, Debuginfod, DWARFCFIChecker, DWP, ExecutionEngine, LineEditor, XRay, Telemetry,
and DebugInfo/LogicalView; also `llvm/lib/Target/X86/MCA`. That is 880 files. Those targets were
configured but never built by this repository's build graph, and several were latently
unbuildable upstream (lld's ELF/MachO/wasm drivers were missing their `Options.td` and lib-local
headers; the `lld` executable was missing `llvm/Support/LLVMDriver.h`); removing them makes the
build graph honest and shrinks the audit surface accordingly. `llvm/utils` and `llvm/docs` were
initially pruned too but had to be restored: `llvm/CMakeLists.txt` adds `utils` inside its
`LLVM_INCLUDE_UTILS` block and `docs` inside its `LLVM_INCLUDE_DOCS` block, both of which default
to `ON` and neither of which this repository turns off, and configure failed without them, so both
are present in the payload today, byte-identical to upstream. One further file is restored on top of the prune:
`llvm/cmake/modules/CrossCompile.cmake`, taken from upstream at the same pinned commit, because
any `CMAKE_CROSSCOMPILING` configure unconditionally hits `include(CrossCompile)` at
`llvm/CMakeLists.txt:1351`, and this repository intends to support future Linux-to-Windows
cross-compilation. Not stripped, because it is load-bearing: `libc/` (`llvm/lib/Support/APFloat.cpp`
includes `libc/shared/math.h` in LLVM 23), the AArch64/ARM/PowerPC/RISCV `.td` files TableGen
consumes to generate TargetParser headers, `llvm/utils/TableGen` itself (builds `llvm-tblgen`), and
the full linked closure named above (MC, Object, DebugInfo/{PDB,CodeView,MSF,DWARF}, LTO, Passes,
Transforms, Target/X86, Demangle, WindowsDriver, WindowsManifest, Option, Support).

Beyond deletion and restoration, exactly five upstream CMake files are patched, and they are this
repository's only upstream source deltas — every other retained file stays byte-identical to
upstream. `llvm-project/lld/CMakeLists.txt` has the `add_subdirectory` calls for `tools/lld`,
`docs`, `ELF`, `MachO`, and `wasm` removed. `llvm-project/llvm/lib/CMakeLists.txt` has the
`add_subdirectory` calls for the pruned `llvm/lib/*` directories removed.
`llvm-project/llvm/lib/DebugInfo/CMakeLists.txt` has the `add_subdirectory` call for pruned
`DebugInfo/LogicalView` removed. `llvm-project/llvm/lib/Target/X86/CMakeLists.txt` has the
`add_subdirectory` call for pruned `Target/X86/MCA` removed, since that directory declares a
`LINK_COMPONENTS` dependency on the pruned `llvm/lib/MCA` component.
`llvm-project/llvm/lib/Frontend/Offloading/CMakeLists.txt` has its `LINK_COMPONENTS` entry for the
pruned `ObjectYAML` component dropped; `LLVMFrontendOffloading` is configured but is not in the
lldCOFF/lldMinGW/lldCommon link closure, so this could not be caught by that link graph. The
latter two patches were needed because kept directories referenced pruned components and configure
failed without them.

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

To update LLVM, change the tag and full commit together, re-apply the same prune list and the five
CMakeLists.txt patches described above to the new tree, regenerate build dependency evidence, run
the complete deterministic EXE/PDB matrix against a stock LLD built from the same commit, and
review the upstream Apache-2.0-with-LLVM-exception license delta. Never follow a floating branch.

`tools/materialize_llvm_payload.py` is the reproducer: it materializes the committed payload from
a reviewed full upstream tree by applying that same prune list and those same patches, so it is
also the reference for exactly what must be redone on every bump. It takes the patched files'
content from the committed payload (or an explicit `--patched-source`), never from `--output`, so
it can materialize into a scratch directory and be diffed against `llvm-project/` as an
independent reproduction check; it stages into `<output>.materialize-tmp` and swaps only after
every SHA check passes, so an aborted run cannot leave a half-written payload. Normal
configuration performs no
LLVM network acquisition. Windows acceptance rejects any closure drift and publishes the fresh
inventory; Linux publishes its platform-specific inventory and both reject undeclared reads.

## Vendored libxml2 (in-process Windows manifest merging)

libxml2 2.15.3 is vendored as a pinned static dependency so `LLVM_ENABLE_LIBXML2=ON` and
`llvm::windows_manifest::isAvailable()` returns true, which makes lld's COFF driver merge
`/manifestinput:` fragments in-process through `WindowsManifestMerger` instead of falling back to
`createManifestXmlWithExternalMt()`, which shells out to `mt.exe` from the Windows SDK and fails
outright when that tool is not on PATH. This removes a runtime dependency on an external SDK tool
for a feature this repository otherwise implements entirely from vendored/pinned sources. The
source archive is `https://download.gnome.org/sources/libxml2/2.15/libxml2-2.15.3.tar.xz`, SHA-256
`78262a6e7ac170d6528ebfe2efccdf220191a5af6a6cd61ea4a9a9a5042c7a07`, verified against GNOME's
published `.sha256sum`. Only a minimal feature set is compiled: output, threads, and push parsing
are on; html, catalog, xpath, xinclude, reader, writer, regexp, schemas, relaxng, c14n, http,
iconv, icu, zlib, and modules are all off, since none of them are needed to parse and serialize the
small manifest fragments this merge path handles. It is built as a static library linked with the
static CRT, matching the rest of this repository's MSVC configuration, so no external DLL is
introduced and no allocation crosses a CRT boundary that mimalloc does not own. Zero upstream LLVM
files are patched to wire this in; libxml2 is an added, pinned dependency, not a source delta to
the payload described above. Because `WindowsManifestMerger` serializes by walking libxml2's
document-order linked lists, merged output is deterministic at a pinned libxml2 version but is not
guaranteed byte-stable across libxml2 versions, so anything that checks the exact bytes of a merged
manifest against `tests/manifest_input.manifest` (or a fixture like it) is coupled to this pinned
version and must be reviewed whenever libxml2 is bumped.

## Cross-compilation provenance (Linux -> x86_64-pc-windows-msvc)

`build-linux-cross` produces `llvm_ld.dll` on an ubuntu-24.04 runner using clang-cl + lld-link +
llvm-lib targeting `x86_64-pc-windows-msvc`, with the MSVC CRT and Windows SDK supplied by an
`xwin` splat (`cmake/WinMsvcCross.cmake`; splat root from `LLVM_LD_XWIN_ROOT`). A reproducible
build requires recording, alongside the LLVM tag/commit above, every toolchain input that affects
the resulting bytes: the pinned clang/lld version used as `clang-cl`/`lld-link`/`llvm-lib`, the
pinned `xwin` version, the exact Visual Studio channel manifest consumed by `xwin --manifest` and
its SHA-256, and the `--sdk-version`/`--crt-version` passed to `xwin splat`. These are recorded as
CI job environment variables in `.github/workflows/ci.yml` (`build-linux-cross`); any change to one
of them is a toolchain bump and must be reviewed the same way an LLVM tag bump is.

The currently pinned values, all in `build-linux-cross`'s job `env`:

- **clang/lld** (`clang-cl`/`lld-link`/`llvm-lib`/`llvm-rc`/`llvm-nm`): apt.llvm.org
  `llvm-toolchain-noble-22` at the exact snapshot version
  `1:22.1.8~++20260714014902+ca7933e47d3a-1~exp1~20260714135019.80`
  (`LLVM_LD_CLANG_APT_VERSION`), installed via `apt-get install clang-tools-22=<that version>
  llvm-22=<that version>` so a removed snapshot build fails the install outright rather than
  silently resolving to a different build. The apt.llvm.org signing key is fetched and
  checked against SHA-256 `8b2a587ffd672c4687e7581dad4b2f6c1bb2ad6b480cd9771ba2ff48e0b8c75d`
  before being trusted -- the same key/recipe `zackees/reld`'s `ci.yml` validates
  (`CLANG_PACKAGE_VERSION`), reused here rather than re-deriving a fresh, unproven pin. Must
  stay >= clang 19: the pinned MSVC CRT's `yvals_core.h` hard-errors below that
  (`STL1000: Unexpected compiler version, expected Clang 19.0.0 or newer`).
- **xwin**: `0.6.5`, installed via `cargo install xwin --version 0.6.5 --locked`.
- **Visual Studio channel manifest**: a snapshot of `https://aka.ms/vs/17/release/channel`
  fetched 2026-09-17, committed at `provenance/vs-channel-manifest.json`, SHA-256
  `fca418ba94ffbcfb7a2b25f10f16f39dd09660568d21eef4bd3f274cb0b27b8c` (checked in CI before
  `xwin --manifest provenance/vs-channel-manifest.json splat` runs). At fetch time this
  resolved to Visual Studio `17.14.41` (September 2026), build `17.14.37710.0`. Pinning the
  top-level manifest pins the whole resolution tree: the per-product sub-manifests (Build
  Tools, SDK, CRT payload catalogs) it references are fixed, versioned URLs embedded in this
  file, not further "latest" indirections.
- **SDK / CRT**: `10.0.26100` / `14.44.17.14`, matching what the native `build-windows` job
  compiles against (see the header-drift note above), resolved against the pinned manifest.

To update any of these: change the value in `.github/workflows/ci.yml`, and for the VS
manifest, re-fetch `https://aka.ms/vs/17/release/channel`, recompute its SHA-256, overwrite
`provenance/vs-channel-manifest.json`, and update both the SHA-256 and the recorded VS
version/build/fetch-date above in the same reviewed change. Confirm `build-linux-cross` and
`test-windows-cross` stay green, with `allocator-probe mimalloc` still reporting
`crt_redirected: 1`, before merging the bump.

The xwin splat itself -- the extracted MSVC CRT headers/libs and Windows SDK headers/libs -- is
never committed to this repository and never published as a CI artifact. Those files are
Microsoft-licensed and not redistributable; only their use to compile Windows binaries on a
non-Windows build machine is permitted, under the Visual Studio Build Tools EULA's OSS/CI carve-out
that `cmake/WinMsvcCross.cmake` documents. CI re-provisions or cache-restores the splat per run; a
splat cache entry is keyed on the pinned xwin/manifest/SDK/CRT versions above, never committed to
git.

Determinism claims for the cross build are narrower than the native ones above. A Linux-built
clang-cl/lld-link `llvm_ld.dll` is never expected to be byte-identical to a Windows-built
cl/link.exe `llvm_ld.dll` -- different compiler front ends and standard library implementations
producing bit-identical PE output across host platforms is not a claim this project makes. The
claim this project does make is cross determinism: two Linux builds of the same commit under the
same pinned toolchain (build-N and build-M) produce identical output, the same self-determinism
property `tests/windows_correctness.ps1` already checks natively on Windows via
`Assert-Same`/`/brepro`. `build-linux-cross` as currently written performs a single build per run
and does not yet re-run and diff a second build-N/build-M pass for the cross toolchain; that
verification is future work, not something CI enforces today.

`test-windows-cross` (`needs: build-linux-cross`) downloads the staged cross-built `llvm_ld.dll`
and test executables onto a windows-2025 runner and runs them natively via
`tools/run_staged_tests.ps1`, including `allocator-probe.exe mimalloc` -- the proof that the
mimalloc static-CRT (`/MT`) malloc override survived cross-compilation, since ctest's own
`CTestTestfile.cmake` embeds Linux build-tree absolute paths and cannot be transplanted to the
Windows runner as-is.

## Pinned clang-cl for the Tier 2 LTO byte-identity rows (issue #6)

MSVC `cl.exe` cannot emit LLVM bitcode, so the ThinLTO/full-LTO rows in
`tests/windows_correctness.ps1` need a real clang-cl. Left unpinned, that compiler floats with
the `windows-2025` runner image: a newer clang-cl could emit a bitcode version the pinned LLVM
`23.1.0` LTO pipeline this repository builds accepts differently, silently invalidating a
byte-identity gate.

Pinned to the official `llvmorg-23.1.0` release asset
`clang+llvm-23.1.0-x86_64-pc-windows-msvc.tar.zst` (the `.zst` variant of the same release the
LLVM source payload above is pinned to -- smaller than the `.tar.xz` variant, same contents),
SHA-256 `1aebf024b959b3835c3bd936da2fa58cd002c61ccf47fce1714447b900bd9837`, checked in CI
(`correctness.yml`'s `deterministic-coff` job) before extraction. Only `clang.exe`/`clang-cl.exe`,
their DLL dependencies (`libclang.dll`, `libiomp5md.dll`, `libomp.dll`, `LLVM-C.dll`, `LTO.dll`,
`Remarks.dll`), and the compiler resource directory (`lib/clang/23/include`, needed for builtin
headers like `intrin.h`) are extracted -- about 490MB, not the release's full ~4GB tree of every
LLVM tool this repository has no use for.

The release also publishes a Sigstore bundle (`.jsonl`) and a detached signature (`.sig`) per
asset. Both are committed alongside this file's directory
(`provenance/clang-llvm-23.1.0-x86_64-pc-windows-msvc.tar.zst.{sig,jsonl}`) for reference and
future verification tooling, but CI does not verify them today -- only the SHA-256 above is
checked programmatically. Full Sigstore verification needs `cosign`/`slsa-verifier` and an
online call to Sigstore's transparency log; that is a real gap relative to the rigor this
document holds other pins to, tracked as follow-up work on issue #6 rather than silently assumed
solved.

To update: download the new release's `clang+llvm-<ver>-x86_64-pc-windows-msvc.tar.zst`, its
`.sig`/`.jsonl`, recompute the archive's SHA-256, replace both files under `provenance/`, and
update `LLVM_LD_CLANGCL_VERSION`/`LLVM_LD_CLANGCL_ARCHIVE_SHA256` in `correctness.yml` in the same
reviewed change. Confirm `deterministic-coff` stays green.
