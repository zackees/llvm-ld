#!/usr/bin/env python3
"""Convert a reviewed CI closure artifact into the committed LLVM import allowlist."""
from __future__ import annotations
import argparse, json, pathlib


def canonical_case(root: pathlib.Path, relative: str) -> str:
    """The on-disk spelling of `relative` under `root`.

    macOS filesystems are case-insensitive, so a macOS build's depfiles record a header under
    whatever case its #include used (e.g. `llvm/ASMParser/...` for upstream `llvm/AsmParser/...`).
    That path does not exist on a case-sensitive checkout, and the materializer rightly refuses it.
    """
    current = root
    parts = []
    for part in pathlib.PurePosixPath(relative).parts:
        exact = current / part
        if exact.exists():
            match = part
        else:
            folded = [child.name for child in current.iterdir() if child.name.lower() == part.lower()] if current.is_dir() else []
            if len(folded) != 1:
                raise SystemExit(f"closure path not found in any case under {root}: {relative}")
            match = folded[0]
        parts.append(match)
        current = current / match
    return "/".join(parts)


def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument("artifact",type=pathlib.Path); p.add_argument("output",type=pathlib.Path)
    p.add_argument("--canonicalize-against",type=pathlib.Path,default=None,help="upstream tree to resolve path case against (required for closures from case-insensitive hosts such as macOS)")
    a=p.parse_args()
    raw=json.loads(a.artifact.read_text(encoding="utf-8")); marker="/_deps/llvm_project-src/"
    files={}
    for key,sha in raw["translation_units"].items():
        if key.startswith("llvm/"): files[key.removeprefix("llvm/")]=sha
        elif marker in key: files[key.split(marker,1)[1]]=sha
    if a.canonicalize_against is not None:
        canonical={}
        for relative,sha in files.items():
            real=canonical_case(a.canonicalize_against,relative)
            if real in canonical and canonical[real]!=sha: raise SystemExit(f"case-folded duplicates disagree: {relative} vs {real}")
            canonical[real]=sha
        files=canonical
    if len(files)<100: raise SystemExit(f"implausibly small LLVM closure: {len(files)}")
    a.output.write_text(json.dumps({"llvm_commit":raw["llvm_commit"],"derivation":"CMake file-api + compile_commands.json + post-build Ninja depfile graph","files":dict(sorted(files.items()))},indent=2)+"\n",encoding="utf-8")
if __name__=="__main__": main()
