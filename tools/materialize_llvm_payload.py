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
    args=parser.parse_args()
    source=args.source.resolve(); output=args.output.resolve()
    manifest=json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("llvm_commit")!="ea7d852a70e8bdfaf601d6626a760f9771b2c4b4":
        raise SystemExit("refusing payload from unexpected LLVM commit")
    files=manifest.get("files",{})
    if len(files)<100: raise SystemExit("implausibly small LLVM payload")
    prune_path=args.prune_declaration or args.manifest.parent/"payload-prune.json"
    prune_declaration=json.loads(prune_path.read_text(encoding="utf-8"))
    prune=prune_declaration.get("prune",[]); keep_exceptions=prune_declaration.get("keep_exceptions",[])
    patched=[entry["path"] for entry in prune_declaration.get("patched",[])]
    # Patched files' correct content lives only in the repo's current output tree, which the
    # rmtree below is about to erase, so it must be captured first.
    preserved={}
    for relative in patched:
        path=output.joinpath(*pathlib.PurePosixPath(relative).parts)
        if not path.is_file(): raise SystemExit(f"patched file missing from repo: {relative}")
        preserved[relative]=path.read_bytes()
    if output.exists(): shutil.rmtree(output)
    prune_hits={p:0 for p in prune}
    copied=0
    for relative,want in sorted(files.items()):
        rel=pathlib.PurePosixPath(relative)
        if rel.is_absolute() or ".." in rel.parts: raise SystemExit(f"unsafe manifest path: {relative}")
        dst=output.joinpath(*rel.parts)
        if relative in patched:
            dst.parent.mkdir(parents=True,exist_ok=True); dst.write_bytes(preserved[relative])
            print(f"preserved patched file (not upstream): {relative}")
            continue
        if is_pruned(relative,prune,keep_exceptions):
            for p in prune:
                if relative.startswith(p): prune_hits[p]+=1
            continue
        src=source.joinpath(*rel.parts)
        if not src.is_file(): raise SystemExit(f"missing locked LLVM input: {relative}")
        actual=hashlib.sha256(src.read_bytes()).hexdigest()
        if actual!=want: raise SystemExit(f"LLVM input drift: {relative}")
        dst.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(src,dst); copied+=1
    for prefix,hits in prune_hits.items():
        if hits==0: raise SystemExit(f"prune prefix matched nothing in manifest: {prefix}")
    print(f"materialized {copied} SHA-verified LLVM files into {output}, "
          f"pruned {sum(prune_hits.values())}, preserved {len(patched)} patched files")

if __name__=="__main__": main()
