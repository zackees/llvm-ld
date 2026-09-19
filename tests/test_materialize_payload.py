#!/usr/bin/env python3
"""Reproducer test for tools/materialize_llvm_payload.py.

Reconstructs a faithful upstream stand-in for the pinned LLVM commit from this
repository's own git history (the pre-strip commit still holds the full
6488-file upstream tree), runs the reproducer against it, and asserts the
result is byte-identical to the committed llvm-project/ payload. Also proves
the test can fail, by tampering the stand-in and checking the reproducer
raises the expected drift error.

stdlib only, no pytest. Run directly: python tests/test_materialize_payload.py
"""
from __future__ import annotations
import hashlib, json, os, pathlib, shutil, subprocess, sys, tempfile, time, zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
MATERIALIZE = ROOT / "tools" / "materialize_llvm_payload.py"
MANIFEST = ROOT / "provenance" / "llvm-source-closure.json"
PRUNE = ROOT / "provenance" / "payload-prune.json"
LLVM_PROJECT = ROOT / "llvm-project"
CROSS_COMPILE_REL = pathlib.PurePosixPath("llvm/cmake/modules/CrossCompile.cmake")
# Documented fallback only: used if no commit in history can be located that
# still contains a known-pruned upstream path. Kept in sync manually if the
# branch is ever rebased such that this becomes stale.
FALLBACK_PRE_STRIP_SHA = "5ef95b5fd6856e9120616888fab215ccb3213410"
PROBE_PATH = "llvm-project/lld/ELF/Driver.cpp"


def run(*args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True)


def find_pre_strip_commit() -> str:
    """Locate the newest commit whose tree still contains a known-pruned path."""
    result = run("git", "log", "--all", "--format=%H", "--", PROBE_PATH, cwd=ROOT)
    if result.returncode == 0:
        for sha in result.stdout.split():
            probe = run("git", "cat-file", "-e", f"{sha}:{PROBE_PATH}", cwd=ROOT)
            if probe.returncode == 0:
                return sha
    probe = run("git", "cat-file", "-e", f"{FALLBACK_PRE_STRIP_SHA}:{PROBE_PATH}", cwd=ROOT)
    if probe.returncode == 0:
        return FALLBACK_PRE_STRIP_SHA
    raise SystemExit(
        "could not locate a pre-strip commit containing "
        f"{PROBE_PATH} (searched git log --all and the hardcoded fallback "
        f"{FALLBACK_PRE_STRIP_SHA})"
    )


def build_standin(commit: str, dest: pathlib.Path) -> pathlib.Path:
    """Reconstruct the upstream llvm-project/ tree at `commit` into `dest`, plus CrossCompile.cmake."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / "standin.zip"
    result = run("git", "archive", "--format=zip", commit, "llvm-project", "-o", str(archive), cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"git archive failed for {commit}: {result.stderr}")
    extract_dir = dest / "extracted"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(extract_dir)
    archive.unlink()
    standin_llvm_project = extract_dir / "llvm-project"
    if not standin_llvm_project.is_dir():
        raise SystemExit(f"git archive of {commit} did not produce llvm-project/")
    cross_compile_src = LLVM_PROJECT.joinpath(*CROSS_COMPILE_REL.parts)
    if not cross_compile_src.is_file():
        raise SystemExit(f"working tree is missing {CROSS_COMPILE_REL}, cannot complete stand-in")
    cross_compile_dst = standin_llvm_project.joinpath(*CROSS_COMPILE_REL.parts)
    cross_compile_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cross_compile_src, cross_compile_dst)
    backfill_later_additions(standin_llvm_project)
    return standin_llvm_project


def backfill_later_additions(standin_llvm_project: pathlib.Path) -> None:
    """Add locked files that joined the payload after the pre-strip commit.

    The pre-strip commit predates host-coverage extensions (aarch64 Linux and macOS; see
    PROVENANCE.md, "Host coverage and closure discovery"), so it lacks the upstream files those
    added. They come from the committed payload instead, the same way CrossCompile.cmake does:
    each is unpatched upstream, locked by hash in the closure, and was checked against the
    upstream release tarball when it was added. A declared patched file is never backfilled —
    it must still come from upstream history, or this test would stop proving that the prune
    list and patches reproduce the payload.
    """
    locked = json.loads(MANIFEST.read_text(encoding="utf-8"))["files"]
    patched = {entry["path"] for entry in json.loads(PRUNE.read_text(encoding="utf-8"))["patched"]}
    added = 0
    for relative in locked:
        target = standin_llvm_project.joinpath(*pathlib.PurePosixPath(relative).parts)
        if target.exists():
            continue
        if relative in patched:
            raise SystemExit(f"patched file {relative} is missing from the pre-strip commit; it cannot be backfilled")
        source = LLVM_PROJECT.joinpath(*pathlib.PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        added += 1
    print(f"backfilled {added} later-added locked file(s) from the committed payload")


def hash_tree(root: pathlib.Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file():
            relative = str(path.relative_to(root)).replace("\\", "/")
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def materialize(source: pathlib.Path, output: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return run(
        sys.executable, str(MATERIALIZE),
        "--source", str(source),
        "--manifest", str(MANIFEST),
        "--output", str(output),
        "--patched-source", str(LLVM_PROJECT),
    )


def diff_summary(actual: dict[str, str], expected: dict[str, str]) -> str:
    actual_paths, expected_paths = set(actual), set(expected)
    extras = sorted(actual_paths - expected_paths)
    missing = sorted(expected_paths - actual_paths)
    changed = sorted(p for p in actual_paths & expected_paths if actual[p] != expected[p])
    parts = []
    for label, paths in (("extra", extras), ("missing", missing), ("hash-mismatch", changed)):
        if paths:
            parts.append(f"{label} ({len(paths)}): " + ", ".join(paths[:20]))
    return "; ".join(parts) if parts else "(no diff found, unexpected)"


def positive_control(standin_llvm_project: pathlib.Path, work: pathlib.Path) -> None:
    """The reproducer's output must be byte-identical to the committed llvm-project/."""
    output = work / "materialized"
    result = materialize(standin_llvm_project, output)
    if result.returncode != 0:
        raise SystemExit(
            "materialize_llvm_payload.py failed against the reconstructed stand-in:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    actual = hash_tree(output)
    expected = hash_tree(LLVM_PROJECT)
    if actual != expected:
        raise SystemExit(
            "materialized payload differs from committed llvm-project/: " + diff_summary(actual, expected)
        )
    print(f"positive control OK: {len(actual)} files byte-identical to committed llvm-project/")


def negative_control(standin_llvm_project: pathlib.Path, work: pathlib.Path) -> None:
    """Tampering one locked upstream byte must make the reproducer raise a drift error."""
    tampered_root = work / "tampered"
    # Hardlink instead of copying bytes: cheap on the same volume, and safe because
    # materialize_llvm_payload.py never writes into --source.
    try:
        shutil.copytree(standin_llvm_project, tampered_root, copy_function=os.link)
    except OSError:
        shutil.copytree(standin_llvm_project, tampered_root)
    # Pick a file that is definitely retained (present in the manifest, not pruned/patched),
    # so the tamper is actually observed by the hash check rather than being silently skipped.
    target = tampered_root / "llvm" / "cmake" / "modules" / "CrossCompile.cmake"
    if not target.is_file():
        raise SystemExit("negative control setup failed: expected tamper target missing")
    original = target.read_bytes()
    target.write_bytes(original + b"\n// tampered by test_materialize_payload.py\n")
    output = work / "materialized-tampered"
    result = materialize(tampered_root, output)
    if result.returncode == 0:
        raise SystemExit(
            "NEGATIVE CONTROL FAILED: materialize_llvm_payload.py accepted a tampered upstream "
            "input without error -- the reproducer's hash check is not effective"
        )
    combined = result.stdout + result.stderr
    if "drift" not in combined:
        raise SystemExit(
            "negative control fired but with an unexpected message (expected a drift error): "
            f"{combined}"
        )
    print("negative control OK: tampered input was correctly rejected with:", combined.strip().splitlines()[-1])


def main() -> None:
    if not MATERIALIZE.is_file():
        raise SystemExit(f"missing {MATERIALIZE}")
    if not LLVM_PROJECT.is_dir():
        raise SystemExit(f"missing {LLVM_PROJECT}")
    commit = find_pre_strip_commit()
    print(f"using pre-strip commit as upstream stand-in source: {commit}")
    work = pathlib.Path(tempfile.mkdtemp(prefix="materialize_payload_test_"))
    try:
        t0 = time.perf_counter()
        standin_llvm_project = build_standin(commit, work / "standin")
        t1 = time.perf_counter()
        file_count = sum(1 for p in standin_llvm_project.rglob("*") if p.is_file())
        print(f"reconstructed stand-in with {file_count} files at {standin_llvm_project} ({t1 - t0:.1f}s)")
        positive_control(standin_llvm_project, work)
        t2 = time.perf_counter()
        print(f"positive control took {t2 - t1:.1f}s")
        negative_control(standin_llvm_project, work)
        t3 = time.perf_counter()
        print(f"negative control took {t3 - t2:.1f}s")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("PASS: materialize_llvm_payload.py reproduces the committed llvm-project/ payload")


if __name__ == "__main__":
    main()
