#!/usr/bin/env python3
"""Stand up a closure-discovery LLVM tree for a new host.

The committed payload is exactly the files the existing host builds read
(`provenance/llvm-source-closure.json`). A new host (aarch64 Linux, macOS) reads a few more
upstream files, host-specific headers and CMake modules, that were never in that set, so its
build cannot start from the committed payload.

This builds the tree a discovery run starts from instead: the full upstream checkout at the pinned
commit, minus the reviewed directory prune in `provenance/payload-prune.json`, with the declared
patched files taken from the committed payload. It is the pre-closure tree the committed payload
was reduced from. A discovery build over it, audited by `tools/audit_build.py`, yields the host's
closure; `tools/merge_llvm_closures.py` unions it with the existing ones, and
`tools/materialize_llvm_payload.py` reproduces the payload from the merged manifest. Every added
file stays byte-identical to upstream.

The tree replaces `llvm-project/` only in a CI workspace; it is never committed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from materialize_llvm_payload import is_pruned  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
SHIMS = REPO / "cmake" / "find-shims"


def shadowed_modules() -> set[str]:
    """Upstream CMake modules that must stay out of the discovery tree.

    llvm-ld shims some find modules (cmake/find-shims/) so LLVM uses its vendored copies, and the
    shim only wins because the upstream module of the same name is absent from the payload. LLVM
    prepends llvm/cmake/modules to CMAKE_MODULE_PATH, so an upstream copy in the discovery tree
    would shadow the shim and configure a different build than production: FindLibXml2.cmake makes
    configure look for a *system* libxml2 instead of the vendored one. Discovery must build what
    production builds, so these are excluded.
    """
    return {f"llvm/cmake/modules/{path.name}" for path in SHIMS.glob("*.cmake")}


def prepare(upstream: pathlib.Path, output: pathlib.Path, prune_path: pathlib.Path, patched_source: pathlib.Path) -> int:
    declaration = json.loads(prune_path.read_text(encoding="utf-8"))
    prune = declaration.get("prune", [])
    keep = declaration.get("keep_exceptions", [])
    patched = [entry["path"] for entry in declaration.get("patched", [])]

    preserved = {}
    for relative in patched:
        path = patched_source.joinpath(*pathlib.PurePosixPath(relative).parts)
        if not path.is_file():
            raise SystemExit(f"patched file missing from {patched_source}: {relative}")
        preserved[relative] = path.read_bytes()

    staging = output.with_name(output.name + ".discovery-tmp")
    if staging.exists():
        shutil.rmtree(staging)
    excluded = shadowed_modules()
    copied = 0
    for top in ("llvm", "lld", "libc", "cmake", "third-party"):
        base = upstream / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(upstream).as_posix()
            if is_pruned(relative, prune, keep) or relative in excluded:
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    for relative, data in preserved.items():
        target = staging.joinpath(*pathlib.PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    if output.exists():
        shutil.rmtree(output)
    staging.rename(output)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=pathlib.Path, required=True, help="full llvm-project checkout at the pinned commit")
    parser.add_argument("--output", type=pathlib.Path, required=True, help="where to write the discovery tree")
    parser.add_argument("--prune-declaration", type=pathlib.Path, default=REPO / "provenance" / "payload-prune.json")
    parser.add_argument(
        "--patched-source",
        type=pathlib.Path,
        required=True,
        help="a copy of the committed llvm-project payload, the only place the patched files' content exists",
    )
    args = parser.parse_args()
    copied = prepare(args.upstream.resolve(), args.output.resolve(), args.prune_declaration, args.patched_source.resolve())
    print(f"discovery tree: {copied} upstream files after the directory prune, plus the declared patches")


if __name__ == "__main__":
    main()
