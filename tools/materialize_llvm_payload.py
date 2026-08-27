#!/usr/bin/env python3
"""Materialize the reviewed LLVM closure as a self-contained source payload."""
from __future__ import annotations
import argparse, hashlib, json, pathlib, shutil

def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=pathlib.Path,required=True)
    parser.add_argument("--manifest",type=pathlib.Path,required=True)
    parser.add_argument("--output",type=pathlib.Path,required=True)
    args=parser.parse_args()
    source=args.source.resolve(); output=args.output.resolve()
    manifest=json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("llvm_commit")!="ea7d852a70e8bdfaf601d6626a760f9771b2c4b4":
        raise SystemExit("refusing payload from unexpected LLVM commit")
    files=manifest.get("files",{})
    if len(files)<100: raise SystemExit("implausibly small LLVM payload")
    if output.exists(): shutil.rmtree(output)
    for relative,want in sorted(files.items()):
        rel=pathlib.PurePosixPath(relative)
        if rel.is_absolute() or ".." in rel.parts: raise SystemExit(f"unsafe manifest path: {relative}")
        src=source.joinpath(*rel.parts); dst=output.joinpath(*rel.parts)
        if not src.is_file(): raise SystemExit(f"missing locked LLVM input: {relative}")
        actual=hashlib.sha256(src.read_bytes()).hexdigest()
        if actual!=want: raise SystemExit(f"LLVM input drift: {relative}")
        dst.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(src,dst)
    print(f"materialized {len(files)} SHA-verified LLVM files into {output}")

if __name__=="__main__": main()
