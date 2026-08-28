#!/usr/bin/env python3
"""Audit CMake/Ninja's mechanically derived source closure and emit a SHA inventory."""
from __future__ import annotations
import argparse, hashlib, json, pathlib, shlex, sys

SOURCE_SUFFIXES={".c",".cc",".cpp",".cxx",".h",".hh",".hpp",".inc",".td",".def"}
def inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try: path.relative_to(root); return True
    except ValueError: return False
def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--source",type=pathlib.Path,required=True); p.add_argument("--build",type=pathlib.Path,required=True); p.add_argument("--ninja-inputs",type=pathlib.Path,required=True); p.add_argument("--ninja-deps",type=pathlib.Path); p.add_argument("--output",type=pathlib.Path,required=True); p.add_argument("--cmake-trace",type=pathlib.Path); p.add_argument("--expected-llvm-closure",type=pathlib.Path); a=p.parse_args()
    source=a.source.resolve(); build=a.build.resolve()
    commands=json.loads((build/"compile_commands.json").read_text(encoding="utf-8"))
    if not commands: raise SystemExit("empty compile database")
    fetched=list((build/"_deps").glob("llvm_project-src")); llvm_roots=fetched or [source/"llvm-project"]
    if not llvm_roots[0].joinpath("llvm","CMakeLists.txt").is_file(): raise SystemExit("pinned LLVM source payload missing")
    # Most-specific roots first because the FetchContent and generated trees live below build,
    # which itself can live below the checkout.
    mimalloc_root=(source/"upstream"/"mimalloc-pprof-0.9.5").resolve()
    allowed={
        "llvm":llvm_roots[0].resolve(),
        "generated":build,
        "src":(source/"src").resolve(),
        "include":(source/"include").resolve(),
        "tests":(source/"tests").resolve(),
        "tools":(source/"tools").resolve(),
        "cmake":(source/"cmake").resolve(),
        "mimalloc":mimalloc_root,
    }
    # exports.map is the ELF version script the shared-library link reads directly; it is a
    # first-party input like CMakeLists.txt, so it is declared and hashed into the inventory
    # rather than exempted.
    allowed_files={(source/"CMakeLists.txt").resolve(),(source/"exports.map").resolve()}
    # LLVM's find_first_existing_vc_file (AddLLVM.cmake) makes this checkout's .git/logs/HEAD a
    # dependency of the generated VCSRevision.h so the revision stamp refreshes on commit. That is
    # VCS metadata, not build source: no byte of it is compiled or included, so exempting it does
    # not weaken the guarantee that every compiled/included byte comes from the pinned LLVM payload
    # or a declared input. Scoped to this repository's own .git, and only to files that are not
    # source-shaped - a header stashed under .git and #included would still arrive through the
    # dependency channel and must still be rejected. Deliberately not added to the inventory.
    vcs_dir=(source/".git").resolve()
    mimalloc_manifest=json.loads((source/"provenance"/"mimalloc-pprof-files.json").read_text(encoding="utf-8"))
    def record(candidate: pathlib.Path, context: str) -> None:
        if inside(candidate,vcs_dir) and candidate.suffix.lower() not in SOURCE_SUFFIXES: return
        if candidate in allowed_files:
            inventory[f"project/{candidate.name}"]=hashlib.sha256(candidate.read_bytes()).hexdigest()
            return
        matching=[(name,root) for name,root in allowed.items() if inside(candidate,root)]
        if not matching: raise SystemExit(f"undeclared {context}: {candidate}")
        if not candidate.is_file(): return
        name,root=matching[0]; relative=str(candidate.relative_to(root)).replace(chr(92),'/')
        digest=hashlib.sha256(candidate.read_bytes()).hexdigest()
        if name=="mimalloc" and mimalloc_manifest.get(relative)!=digest:
            raise SystemExit(f"undeclared or modified mimalloc {context}: {relative}")
        inventory[f"{name}/{relative}"]=digest
    inventory={}
    for entry in commands:
        candidate=pathlib.Path(entry["file"])
        if not candidate.is_absolute(): candidate=pathlib.Path(entry["directory"])/candidate
        candidate=candidate.resolve()
        record(candidate,"compile input")
    for raw in a.ninja_inputs.read_text(encoding="utf-8",errors="replace").splitlines():
        raw=raw.strip()
        if not raw: continue
        candidate=pathlib.Path(raw)
        if not candidate.is_absolute(): candidate=build/candidate
        try: candidate=candidate.resolve()
        except OSError: continue
        if candidate.exists() and candidate.is_file() and (inside(candidate,source) or inside(candidate,build)):
            record(candidate,"build source read")
    if a.ninja_deps:
        for raw in a.ninja_deps.read_text(encoding="utf-8",errors="replace").splitlines():
            if not raw[:1].isspace(): continue
            candidate=pathlib.Path(raw.strip())
            if not candidate.is_absolute(): candidate=build/candidate
            candidate=candidate.resolve()
            if candidate.is_file() and (inside(candidate,source) or inside(candidate,build)):
                record(candidate,"dependency read")
    if a.cmake_trace:
        for line in a.cmake_trace.read_text(encoding="utf-8",errors="replace").splitlines():
            try: entry=json.loads(line)
            except json.JSONDecodeError: continue
            trace_file=pathlib.Path(entry.get("file","")).resolve()
            candidate=trace_file
            if candidate.is_file() and (inside(candidate,source) or inside(candidate,build)):
                record(candidate,"CMake trace read")
            for value in entry.get("args",[]):
                candidate=pathlib.Path(value)
                if not candidate.is_absolute(): candidate=trace_file.parent/candidate
                try: candidate=candidate.resolve()
                except OSError: continue
                if candidate.is_file() and (inside(candidate,source) or inside(candidate,build)):
                    record(candidate,"CMake argument read")
    reply=build/".cmake"/"api"/"v1"/"reply"
    if not list(reply.glob("codemodel-v2-*.json")): raise SystemExit("CMake file-api codemodel missing")
    if a.expected_llvm_closure:
        expected=json.loads(a.expected_llvm_closure.read_text(encoding="utf-8"))
        actual={key.removeprefix("llvm/"):value for key,value in inventory.items() if key.startswith("llvm/")}
        locked=expected.get("files",{}); added=sorted(set(actual)-set(locked)); changed=sorted(key for key,value in actual.items() if locked.get(key)!=value)
        if expected.get("llvm_commit")!="ea7d852a70e8bdfaf601d6626a760f9771b2c4b4" or added or changed:
            raise SystemExit(f"LLVM source closure drift: added={added[:10]}, changed={changed[:10]}")
    a.output.write_text(json.dumps({"llvm_commit":"ea7d852a70e8bdfaf601d6626a760f9771b2c4b4","translation_units":inventory},indent=2)+"\n",encoding="utf-8")
    return 0
if __name__=="__main__": sys.exit(main())
