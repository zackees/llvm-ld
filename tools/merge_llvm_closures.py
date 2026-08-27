#!/usr/bin/env python3
"""Merge reviewed platform closures and explicit configure resources."""
from __future__ import annotations
import argparse, hashlib, json, pathlib

def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument("--source",type=pathlib.Path,required=True); p.add_argument("--output",type=pathlib.Path,required=True); p.add_argument("--extra",action="append",default=[]); p.add_argument("manifests",nargs="+"); a=p.parse_args()
    files={}
    for manifest_path in a.manifests:
        manifest=json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))
        if manifest.get("llvm_commit")!="ea7d852a70e8bdfaf601d6626a760f9771b2c4b4": raise SystemExit("closure commit mismatch")
        for relative,digest in manifest["files"].items():
            if relative in files and files[relative]!=digest: raise SystemExit(f"cross-platform hash mismatch: {relative}")
            files[relative]=digest
    for relative in a.extra:
        path=a.source/pathlib.PurePosixPath(relative)
        if not path.is_file(): raise SystemExit(f"missing configure resource: {relative}")
        files[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
    a.output.write_text(json.dumps({"llvm_commit":"ea7d852a70e8bdfaf601d6626a760f9771b2c4b4","derivation":"union of CMake trace, compile_commands, Ninja target inputs, and compiler deps on Windows/Linux","files":dict(sorted(files.items()))},indent=2)+"\n",encoding="utf-8")

if __name__=="__main__": main()
