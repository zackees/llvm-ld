#!/usr/bin/env python3
"""Generate a deterministic COFF object corpus for linker benchmarking.

Freestanding C++ (no system headers), compiled with clang targeting x86_64-pc-windows-msvc with
CodeView debug info. The shape is chosen to exercise what a real link exercises: many translation
units sharing a large header (type-record merging, ghash), inline/template COMDATs duplicated
across TUs (ICF, symbol resolution), plenty of relocations, string literals, and static data.

Output layout: <out>/<profile>/{NNNN.obj, link.rsp, manifest.json}.  The .rsp is the argument
list used by bench.py; every path in it is relative to <out>/<profile>.

Usage: gen_corpus.py --out build-perf/corpus --profile small|medium|large [--clang clang]
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
    if idx == 0:
        out.append('extern "C" void ext_sink(u64 v) { volatile u64 sink = v; (void)sink; }')
        out.append('extern "C" int entry(void) { return (int)fn_0_0(3); }')
    return "\n".join(out) + "\n"

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--profile", choices=PROFILES, default="medium")
    ap.add_argument("--clang", default=os.environ.get("CLANG", "clang"))
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    a = ap.parse_args()
    p = PROFILES[a.profile]
    root = a.out / a.profile
    src = root / "src"; src.mkdir(parents=True, exist_ok=True)
    (src / "shared.h").write_text(header(p["types"], p["inlines"]))
    flags = ["--target=x86_64-pc-windows-msvc", "-c", "-O2", "-g", "-gcodeview", "-fno-exceptions",
             "-fno-rtti", "-ffreestanding", "-nostdinc", "-nostdinc++", "-w", "-Wno-error",
             "-fms-compatibility", "-mno-incremental-linker-compatible"]
    def build(i: int) -> str:
        cpp = src / f"{i:04d}.cpp"; obj = root / f"{i:04d}.obj"
        text = tu(i, p["funcs"], p["types"], p["inlines"], p["tus"])
        if not cpp.exists() or cpp.read_text() != text: cpp.write_text(text)
        if not obj.exists() or obj.stat().st_mtime < cpp.stat().st_mtime:
            subprocess.run([a.clang, *flags, str(cpp), "-o", str(obj)], check=True,
                           stderr=subprocess.DEVNULL)
        return f"{i:04d}.obj"
    with ThreadPoolExecutor(a.jobs) as ex: objs = list(ex.map(build, range(p["tus"])))
    rsp = ["/entry:entry", "/subsystem:console", "/nodefaultlib", "/brepro", "/debug:full",
           "/pdbaltpath:%_PDB%", "/opt:ref", "/opt:icf", *objs]
    (root / "link.rsp").write_text("\n".join(rsp) + "\n")
    digest = hashlib.sha256()
    for o in objs: digest.update((root / o).read_bytes())
    total = sum((root / o).stat().st_size for o in objs)
    manifest = dict(profile=a.profile, **p, objects=len(objs), object_bytes=total, objects_sha256=digest.hexdigest())
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))
    return 0

if __name__ == "__main__": sys.exit(main())
