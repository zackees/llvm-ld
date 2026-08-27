#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, pathlib, re, sys

def allocator_contract(cmake: str) -> None:
    if cmake.count('target_sources(llvm_ld PRIVATE "${LLVM_LD_MIMALLOC_ROOT}/mimalloc-pprof-amalgamated.c")') != 1:
        raise ValueError("exactly one mimalloc translation unit is required")
    if "MI_MALLOC_OVERRIDE=1" not in cmake:
        raise ValueError("Windows CRT allocation redirection is required")

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--root", type=pathlib.Path, required=True); p.add_argument("--positive-controls", action="store_true"); a=p.parse_args()
    root=a.root.resolve(); locked=(root/"cmake"/"LockedInputs.cmake").read_text()
    required={"LLVM_LD_LLVM_COMMIT":"ea7d852a70e8bdfaf601d6626a760f9771b2c4b4",
              "LLVM_LD_MIMALLOC_VERSION":"0.9.5",
              "LLVM_LD_MIMALLOC_SHA256":"9143eb24178e618a9671d22074e2e64badd61454a1ae5c042325daf8238f5407"}
    for key,value in required.items():
        if not re.search(rf'set\({key} "{re.escape(value)}"\)', locked): raise SystemExit(f"bad lock: {key}")
    for path in ["LICENSE","LICENSE-LLVM.txt","LICENSE-MIMALLOC.txt","PROVENANCE.md","include/llvm_ld.h"]:
        if not (root/path).is_file(): raise SystemExit(f"missing required attribution: {path}")
    llvm_manifest=json.loads((root/"provenance"/"llvm-source-closure.json").read_text())
    llvm_root=root/"llvm-project"; actual_llvm={}
    for path in llvm_root.rglob("*"):
        if path.is_file(): actual_llvm[str(path.relative_to(llvm_root)).replace("\\","/")]=hashlib.sha256(path.read_bytes()).hexdigest()
    expected_llvm=llvm_manifest.get("files", {})
    if actual_llvm!=expected_llvm:
        actual_paths=set(actual_llvm); expected_paths=set(expected_llvm)
        extras=sorted(actual_paths-expected_paths)
        missing=sorted(expected_paths-actual_paths)
        changed=sorted(path for path in actual_paths & expected_paths
                       if actual_llvm[path]!=expected_llvm[path])
        details=[]
        for label,paths in (("extra",extras),("missing",missing),("hash-mismatch",changed)):
            if paths: details.append(f"{label} ({len(paths)}): " + ", ".join(paths[:20]))
        raise SystemExit("self-contained LLVM payload differs from locked source closure; " + "; ".join(details))
    inv=root/"provenance"/"mimalloc-pprof-files.json"
    if inv.exists():
        for rel,want in json.loads(inv.read_text()).items():
            path=root/"upstream"/"mimalloc-pprof-0.9.5"/rel
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=want: raise SystemExit(f"drift: {rel}")
    cmake=(root/"CMakeLists.txt").read_text()
    allocator_contract(cmake)
    if "LLVM_LD_ENABLE_PPROF \"Compile" not in cmake or "LLVM_LD_ENABLE_DHAT \"Compile" not in cmake: raise SystemExit("profiling switches missing")
    if 'LLVM_LD_USE_MIMALLOC "Route native allocation through mimalloc" ON' not in cmake: raise SystemExit("mimalloc must default on")
    if a.positive_controls:
        controls=[cmake.replace("MI_MALLOC_OVERRIDE=1", "MI_OVERRIDE_MISSING=1"),
                  cmake.replace('target_sources(llvm_ld PRIVATE "${LLVM_LD_MIMALLOC_ROOT}/mimalloc-pprof-amalgamated.c")',
                                'target_sources(llvm_ld PRIVATE "${LLVM_LD_MIMALLOC_ROOT}/mimalloc-pprof-amalgamated.c" "${LLVM_LD_MIMALLOC_ROOT}/mimalloc-pprof-amalgamated.c")')]
        for mutated in controls:
            try: allocator_contract(mutated)
            except ValueError: continue
            raise SystemExit("allocator negative mutation escaped the verifier")
    return 0
if __name__ == "__main__": sys.exit(main())
