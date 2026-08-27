#!/usr/bin/env python3
"""Convert a reviewed CI closure artifact into the committed LLVM import allowlist."""
from __future__ import annotations
import argparse, json, pathlib
def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument("artifact",type=pathlib.Path); p.add_argument("output",type=pathlib.Path); a=p.parse_args()
    raw=json.loads(a.artifact.read_text(encoding="utf-8")); marker="/_deps/llvm_project-src/"
    files={}
    for key,sha in raw["translation_units"].items():
        if key.startswith("llvm/"): files[key.removeprefix("llvm/")]=sha
        elif marker in key: files[key.split(marker,1)[1]]=sha
    if len(files)<100: raise SystemExit(f"implausibly small LLVM closure: {len(files)}")
    a.output.write_text(json.dumps({"llvm_commit":raw["llvm_commit"],"derivation":"CMake file-api + compile_commands.json + post-build Ninja depfile graph","files":dict(sorted(files.items()))},indent=2)+"\n",encoding="utf-8")
if __name__=="__main__": main()
