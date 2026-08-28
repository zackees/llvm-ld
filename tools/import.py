#!/usr/bin/env python3
"""Reproducibly acquire and verify the locked upstream inputs."""
from __future__ import annotations
import argparse, hashlib, io, json, pathlib, shutil, tarfile, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
LLVM_COMMIT = "ea7d852a70e8bdfaf601d6626a760f9771b2c4b4"
MI_VERSION = "0.9.5"
MI_SHA256 = "9143eb24178e618a9671d22074e2e64badd61454a1ae5c042325daf8238f5407"
XML2_VERSION = "2.15.3"
XML2_SHA256 = "78262a6e7ac170d6528ebfe2efccdf220191a5af6a6cd61ea4a9a9a5042c7a07"

def download(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                return response.read()
        except Exception as error:
            last_error=error
            if attempt != 3: time.sleep(2**attempt)
    raise RuntimeError(f"download failed after four attempts: {url}") from last_error

def safe_extract(data: bytes, target: pathlib.Path, mode: str = "r:gz") -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    names=[]
    with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as archive:
        for member in archive.getmembers():
            p = pathlib.PurePosixPath(member.name)
            if p.is_absolute() or ".." in p.parts:
                raise RuntimeError(f"unsafe archive member: {member.name}")
            names.append(member.name)
        archive.extractall(target, filter="data")
    return names

def import_mimalloc() -> None:
    url=f"https://crates.io/api/v1/crates/mimalloc-pprof/{MI_VERSION}/download"
    data=download(url)
    actual=hashlib.sha256(data).hexdigest()
    if actual != MI_SHA256: raise RuntimeError(f"mimalloc archive checksum mismatch: {actual}")
    out=ROOT / "upstream" / f"mimalloc-pprof-{MI_VERSION}"
    if out.exists(): shutil.rmtree(out)
    staging=ROOT / "upstream" / ".mimalloc-import"
    if staging.exists(): shutil.rmtree(staging)
    names=safe_extract(data, staging)
    roots={pathlib.PurePosixPath(n).parts[0] for n in names if n}
    if len(roots) != 1: raise RuntimeError("unexpected crate archive shape")
    staging.joinpath(next(iter(roots))).replace(out)
    staging.rmdir()
    inventory={str(p.relative_to(out)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(out.rglob("*")) if p.is_file()}
    (ROOT/"provenance"/"mimalloc-pprof-files.json").write_text(json.dumps(inventory, indent=2)+"\n")

def import_libxml2() -> None:
    url=f"https://download.gnome.org/sources/libxml2/2.15/libxml2-{XML2_VERSION}.tar.xz"
    data=download(url)
    actual=hashlib.sha256(data).hexdigest()
    if actual != XML2_SHA256: raise RuntimeError(f"libxml2 archive checksum mismatch: {actual}")
    out=ROOT / "upstream" / f"libxml2-{XML2_VERSION}"
    if out.exists(): shutil.rmtree(out)
    staging=ROOT / "upstream" / ".libxml2-import"
    if staging.exists(): shutil.rmtree(staging)
    names=safe_extract(data, staging, mode="r:xz")
    roots={pathlib.PurePosixPath(n).parts[0] for n in names if n}
    if len(roots) != 1: raise RuntimeError("unexpected libxml2 archive shape")
    staging.joinpath(next(iter(roots))).replace(out)
    staging.rmdir()
    inventory={str(p.relative_to(out)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(out.rglob("*")) if p.is_file()}
    (ROOT/"provenance"/"libxml2-files.json").write_text(json.dumps(inventory, indent=2)+"\n")

def import_llvm() -> None:
    data=download(f"https://github.com/llvm/llvm-project/archive/{LLVM_COMMIT}.tar.gz")
    parent=ROOT/"upstream"; staging=parent/".llvm-import"
    if staging.exists(): shutil.rmtree(staging)
    names=safe_extract(data, staging)
    root=staging/pathlib.PurePosixPath(names[0]).parts[0]
    out=parent/"llvm-project"
    if out.exists(): shutil.rmtree(out)
    root.replace(out); staging.rmdir()

def main() -> None:
    # --mimalloc-only is a legacy name kept for backward compatibility with existing callers
    # (CI provenance/test-windows-cross jobs, cmake/Import.cmake): it means "skip the LLVM
    # download", not "import mimalloc exclusively". All pinned vendored third-party deps
    # (mimalloc, libxml2) are always imported; only the (large) LLVM checkout is gated by the flag.
    parser=argparse.ArgumentParser(); parser.add_argument("--mimalloc-only", action="store_true")
    args=parser.parse_args(); (ROOT/"upstream").mkdir(exist_ok=True); (ROOT/"provenance").mkdir(exist_ok=True)
    import_mimalloc()
    import_libxml2()
    if not args.mimalloc_only: import_llvm()
if __name__ == "__main__": main()
