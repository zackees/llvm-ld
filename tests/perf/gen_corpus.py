#!/usr/bin/env python3
"""Generate a deterministic COFF object corpus for linker benchmarking.

Freestanding C++ (no system headers), compiled with clang targeting x86_64-pc-windows-msvc with
CodeView debug info. The shape is chosen to exercise what a real link exercises: many translation
units sharing a large header (type-record merging, ghash), inline/template COMDATs duplicated
across TUs (ICF, symbol resolution), plenty of relocations, string literals, and static data.

Build modes (MODES) pair how the objects are compiled with how they are linked, so the published
charts can say which kind of build each number is for: Debug + PDB, Release + PDB (the only mode
measured before #32), Release without a PDB (a control: the PDB-emission patches cannot apply), and
ThinLTO + PDB. MODES is the single source of truth for the measurement matrix: the link-benchmark
workflow iterates `--print-matrix` rather than hand-writing loops, and tools/bench_report.py
mirrors the mode ids (a test asserts they match).

Output layout: <out>/<mode>/<profile>/{NNNN.obj, link.rsp, manifest.json}. The .rsp is the argument
list used by bench.py; every path in it is relative to that directory. manifest.json records the
mode, its flags and whether it produces a PDB, which bench.py reads.

Usage: gen_corpus.py --out build-perf/corpus --mode debug|release-pdb|release-nopdb|thinlto
                     --profile small|medium|large [--clang clang]
       gen_corpus.py --print-matrix --max-threads N [--smoke] [--runs R] [--lto-runs L]
"""
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

PROFILES = {
    # tus, functions per TU, shared types in header, inline functions in header
    "small":  dict(tus=64,   funcs=40,  types=60,  inlines=40),
    "medium": dict(tus=512,  funcs=60,  types=200, inlines=120),
    "large":  dict(tus=2048, funcs=80,  types=400, inlines=200),
}

COMMON_CFLAGS = ["--target=x86_64-pc-windows-msvc", "-c", "-fno-exceptions", "-fno-rtti",
                 "-ffreestanding", "-nostdinc", "-nostdinc++", "-w", "-Wno-error",
                 "-fms-compatibility", "-mno-incremental-linker-compatible"]
COMMON_LINK = ["/entry:entry", "/subsystem:console", "/nodefaultlib", "/brepro", "/machine:x64"]

# threads: "all" measures 1, 2, 4 and nproc (deduplicated); "max" measures the highest only.
# runs: None means the workflow's --runs; the LTO mode takes --lto-runs because every link runs
# LLVM codegen and costs far more.
MODES = {
    "debug": dict(
        label="Debug + PDB", cflags=["-O0", "-g", "-gcodeview"],
        link_flags=["/debug:full", "/opt:noref", "/opt:noicf"], pdb=True,
        corpora=["small", "medium", "large"], threads="all", lto=False,
        note="a Debug build: unoptimized objects, largest debug info, no linker GC or ICF"),
    "release-pdb": dict(
        label="Release + PDB", cflags=["-O2", "-g", "-gcodeview"],
        link_flags=["/debug:full", "/opt:ref", "/opt:icf"], pdb=True,
        corpora=["small", "medium", "large"], threads="all", lto=False,
        note="RelWithDebInfo; the mode measured until now"),
    "release-nopdb": dict(
        label="Release, no PDB", cflags=["-O2"],
        link_flags=["/opt:ref", "/opt:icf"], pdb=False,
        corpora=["small", "medium", "large"], threads="all", lto=False,
        note="control: no PDB is produced, so the PDB-emission patches cannot apply; expect ~0%"),
    "thinlto": dict(
        label="ThinLTO + PDB", cflags=["-O2", "-g", "-gcodeview", "-flto=thin"],
        link_flags=["/debug:full", "/opt:ref", "/opt:icf"], pdb=True,
        corpora=["small"], threads="max", lto=True,
        note="the link runs LLVM codegen, which the patches do not touch; expect a small gain. "
             "Only small is measured: one medium ThinLTO link takes ~2 minutes even on 16 cores"),
}


def thread_list(policy: str, max_threads: int) -> list[int]:
    counts = sorted({1, 2, 4, max_threads})
    return counts if policy == "all" else [counts[-1]]


def matrix(max_threads: int, smoke: bool, runs: int, lto_runs: int) -> list[tuple[str, str, int, int]]:
    cells = []
    for mode, spec in MODES.items():
        for profile in (["small"] if smoke else spec["corpora"]):
            for threads in thread_list(spec["threads"], max_threads):
                cells.append((mode, profile, threads, lto_runs if spec["lto"] else runs))
    return cells


def header(types: int, inlines: int) -> str:
    out = ["#pragma once", "typedef unsigned long long u64; typedef unsigned int u32; typedef int i32;"]
    for t in range(types):
        fields = "\n".join(f"  u64 f{k}; i32 g{k}; char name{k}[{8 + (k % 5) * 4}];" for k in range(4 + t % 6))
        out.append(f"struct T{t} {{\n{fields}\n  struct T{max(t-1,0)} *prev; struct T{t} *next;\n}};")
        out.append(f"enum E{t} {{ E{t}_a = {t}, E{t}_b, E{t}_c = {t*7} }};")
    for i in range(inlines):
        out.append(
            f"template <typename S> static inline u64 inl{i}(S *p, u32 n) {{\n"
            f"  u64 acc = {i * 2654435761 % (1 << 31)}u;\n"
            f"  for (u32 k = 0; k < n; ++k) {{ acc = acc * 6364136223846793005ull + p->f{i % 4} + k; p->g{i % 4} ^= (i32)acc; }}\n"
            f"  return acc;\n}}")
        out.append(
            f"template <typename T> inline T tpl{i}(T a, T b) {{ return a * {i + 3} + b - (T){i}; }}")
    out.append("extern \"C\" void ext_sink(u64);")
    return "\n".join(out) + "\n"

def tu(idx: int, funcs: int, types: int, inlines: int, tus: int) -> str:
    out = [f'#include "shared.h"', f"static struct T{idx % types} g_state{idx}[{2 + idx % 7}];",
           f'static const char *g_strings{idx}[] = {{ "tu{idx}-alpha", "tu{idx}-beta", "shared-string-{idx % 50}", "gamma" }};']
    for f in range(funcs):
        callee_tu = (idx * 31 + f * 17) % tus
        callee_fn = (f * 13 + idx) % funcs
        # cross-TU call to a function in another TU: extern declaration + call site (relocation).
        out.append(f"extern u64 fn_{callee_tu}_{callee_fn}(u32);")
        out.append(
            f"u64 fn_{idx}_{f}(u32 n) {{\n"
            f"  u64 r = inl{(idx + f) % inlines}(&g_state{idx}[{f % 2}], n & 15);\n"
            f"  r += tpl{(idx * 3 + f) % inlines}<u64>(r, (u64)n);\n"
            f"  r ^= (u64)(unsigned char)g_strings{idx}[{f % 4}][0];\n"
            f"  if (n > {1000 + f}) r += fn_{callee_tu}_{callee_fn}(n - 1);\n"
            f"  ext_sink(r);\n  return r;\n}}")
    # Every function is reachable from entry through its TU's table, indexed by a volatile, so
    # neither /opt:ref nor whole-program LTO can discard the corpus: without this a ThinLTO link
    # folded entry to a constant and emitted a 2 KB EXE, measuring nothing.
    fns = ", ".join(f"fn_{idx}_{f}" for f in range(funcs))
    out.append(f"extern u64 (*const g_table{idx}[{funcs}])(u32);")
    out.append(f"u64 (*const g_table{idx}[{funcs}])(u32) = {{ {fns} }};")
    if idx == 0:
        out.append('extern "C" void ext_sink(u64 v) { volatile u64 sink = v; (void)sink; }')
        out.extend(f"extern u64 (*const g_table{t}[{funcs}])(u32);" for t in range(1, tus))
        tables = ", ".join(f"g_table{t}" for t in range(tus))
        out.append(f"static u64 (*const *const g_tables[{tus}])(u32) = {{ {tables} }};")
        out.append(
            'extern "C" int entry(void) {\n'
            "  volatile u32 pick = 3; u64 acc = 0;\n"
            f"  for (u32 t = 0; t < {tus}; ++t)\n"
            f"    for (u32 f = 0; f < {funcs}; ++f) acc += g_tables[t][(f + pick) % {funcs}](pick);\n"
            "  return (int)acc;\n}")
    return "\n".join(out) + "\n"

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--mode", choices=MODES, default="release-pdb")
    ap.add_argument("--profile", choices=PROFILES, default="medium")
    ap.add_argument("--print-matrix", action="store_true",
                    help="print one 'mode profile threads runs' line per measurement cell and exit")
    ap.add_argument("--max-threads", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--smoke", action="store_true", help="with --print-matrix: small corpus only")
    ap.add_argument("--runs", type=int, default=9)
    ap.add_argument("--lto-runs", type=int, default=5)
    ap.add_argument("--clang", default=os.environ.get("CLANG", "clang"))
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    a = ap.parse_args()
    if a.print_matrix:
        for cell in matrix(a.max_threads, a.smoke, a.runs, a.lto_runs):
            print(*cell)
        return 0
    if a.out is None:
        ap.error("--out is required unless --print-matrix is given")
    p = PROFILES[a.profile]
    spec = MODES[a.mode]
    root = a.out / a.mode / a.profile
    src = root / "src"; src.mkdir(parents=True, exist_ok=True)
    (src / "shared.h").write_text(header(p["types"], p["inlines"]))
    flags = [*COMMON_CFLAGS, *spec["cflags"]]
    def build(i: int) -> str:
        cpp = src / f"{i:04d}.cpp"; obj = root / f"{i:04d}.obj"
        text = tu(i, p["funcs"], p["types"], p["inlines"], p["tus"])
        if not cpp.exists() or cpp.read_text() != text: cpp.write_text(text)
        if not obj.exists() or obj.stat().st_mtime < cpp.stat().st_mtime:
            subprocess.run([a.clang, *flags, str(cpp), "-o", str(obj)], check=True,
                           stderr=subprocess.DEVNULL)
        return f"{i:04d}.obj"
    with ThreadPoolExecutor(a.jobs) as ex: objs = list(ex.map(build, range(p["tus"])))
    rsp = [*COMMON_LINK, *spec["link_flags"], *(["/pdbaltpath:%_PDB%"] if spec["pdb"] else []), *objs]
    (root / "link.rsp").write_text("\n".join(rsp) + "\n")
    digest = hashlib.sha256()
    for o in objs: digest.update((root / o).read_bytes())
    total = sum((root / o).stat().st_size for o in objs)
    manifest = dict(mode=a.mode, profile=a.profile, **p, cflags=spec["cflags"], link_flags=spec["link_flags"],
                    pdb=spec["pdb"], objects=len(objs), object_bytes=total, objects_sha256=digest.hexdigest())
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))
    return 0

if __name__ == "__main__": sys.exit(main())
