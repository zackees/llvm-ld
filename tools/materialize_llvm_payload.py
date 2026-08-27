#!/usr/bin/env python3
"""Materialize the reviewed LLVM closure as a self-contained source payload."""
from __future__ import annotations
import argparse, hashlib, json, pathlib, shutil

def is_pruned(relative: str, prune: list[str], keep_exceptions: list[str]) -> bool:
    if any(relative==k or relative.startswith(k) for k in keep_exceptions): return False
    return any(relative.startswith(p) for p in prune)

def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=pathlib.Path,required=True)
    parser.add_argument("--manifest",type=pathlib.Path,required=True)
    parser.add_argument("--output",type=pathlib.Path,required=True)
    parser.add_argument("--prune-declaration",type=pathlib.Path,default=None)
    parser.add_argument("--patched-source",type=pathlib.Path,default=None,
                        help="tree holding this repository's patched copies of the retained upstream "
                             "CMake files (default: the committed llvm-project payload beside the manifest)")
    args=parser.parse_args()
    source=args.source.resolve(); output=args.output.resolve()
    if source==output or source.is_relative_to(output) or output.is_relative_to(source):
        raise SystemExit(f"--output must be a separate tree from --source: {output} vs {source}")
    manifest=json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("llvm_commit")!="ea7d852a70e8bdfaf601d6626a760f9771b2c4b4":
        raise SystemExit("refusing payload from unexpected LLVM commit")
    files=manifest.get("files",{})
    if len(files)<100: raise SystemExit("implausibly small LLVM payload")
    prune_path=args.prune_declaration or args.manifest.parent/"payload-prune.json"
    prune_declaration=json.loads(prune_path.read_text(encoding="utf-8"))
    prune=prune_declaration.get("prune",[]); keep_exceptions=prune_declaration.get("keep_exceptions",[])
    patched=[entry["path"] for entry in prune_declaration.get("patched",[])]
    # The patched files' correct content exists nowhere upstream, so it is read from the committed
    # payload (or an explicit --patched-source) rather than from --output, which may not exist yet
    # and which this run replaces.
    patched_source=(args.patched_source or args.manifest.parent.parent/"llvm-project").resolve()
    preserved={}
    for relative in patched:
        path=patched_source.joinpath(*pathlib.PurePosixPath(relative).parts)
        if not path.is_file(): raise SystemExit(f"patched file missing from {patched_source}: {relative}")
        preserved[relative]=path.read_bytes()
    # Stage beside the output and swap only on success, so a mid-run failure cannot destroy an
    # existing payload (the common case is --output pointing at the committed llvm-project tree).
    staging=output.parent/(output.name+".materialize-tmp")
    if staging.exists(): shutil.rmtree(staging)
    prune_hits={p:0 for p in prune}
    copied=0
    seen=set()
    for src in sorted(p for p in source.rglob("*") if p.is_file()):
        rel=src.relative_to(source)
        if ".." in rel.parts: raise SystemExit(f"unsafe upstream path: {rel}")
        relative="/".join(rel.parts)
        if is_pruned(relative,prune,keep_exceptions):
            for p in prune:
                if relative.startswith(p): prune_hits[p]+=1
            continue
        if relative not in files: continue
        if relative in seen: raise SystemExit(f"duplicate payload path (case collision?): {relative}")
        seen.add(relative)
        dst=staging.joinpath(*rel.parts)
        dst.parent.mkdir(parents=True,exist_ok=True)
        if relative in patched:
            dst.write_bytes(preserved[relative])
            print(f"preserved patched file (not upstream): {relative}")
            continue
        want=files[relative]
        actual=hashlib.sha256(src.read_bytes()).hexdigest()
        if actual!=want: raise SystemExit(f"LLVM input drift: {relative}")
        shutil.copyfile(src,dst); copied+=1
    missing=sorted(set(files)-seen)
    if missing: raise SystemExit(f"missing locked LLVM input(s): {missing[:20]}")
    for prefix,hits in prune_hits.items():
        if hits==0: raise SystemExit(f"prune prefix matched nothing upstream, declaration is stale: {prefix}")
    if output.exists(): shutil.rmtree(output)
    staging.replace(output)
    print(f"materialized {copied} SHA-verified LLVM files into {output}, "
          f"pruned {sum(prune_hits.values())}, preserved {len(patched)} patched files")

if __name__=="__main__": main()
