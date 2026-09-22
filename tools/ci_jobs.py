#!/usr/bin/env python3
"""The CI jobs, as code (#55, #56). Every workflow job that builds or measures calls into this.

The GitHub Actions side is one template, `.github/actions/ci-job`:

    python3 tools/ci_jobs.py prepare <job>   toolchain setup, then the job's cache identities
    (restore the job's exact-input artifact cache, if it declares one)
    zackees/zccache                          compile cache for the job's cache group
    python3 tools/ci_jobs.py run <job>       the job itself, under a zccache stats session
    (save the artifact cache; zccache cleanup saves the compile cache; prune; upload timing)

so a workflow only checks out, calls the template, and moves artifacts. A job is a `Job` in `JOBS`:
its compile-cache group (jobs that compile identical commands share one), an optional exact-input
artifact cache (a hash of every input that can change the job's output; on a hit the job is
skipped), a toolchain setup, and the body. `run` writes `timing-<job>.json` (seconds, warm, zccache
hits/misses) for tools/bench_ci.py's floors.

Subcommands
  prepare JOB      run the job's setup, print/emit start, cache-group, artifact-key/-path, save
  run JOB          run the job (skipped when --cached true), write its timing record
  post JOB         the job's post phase (after the cache saves), added to its timing record
  floor JOB        fail when a warm run exceeded the job's time floor (cold runs: report only)
  prune JOB        delete the superseded compile-cache / build-tree entries this run replaced
  list             print the job table

Stdlib only.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bench_ci  # noqa: E402
import ci_build  # noqa: E402

# The zccache release the template installs (`zackees/zccache@<tag>` with `zccache-version`).
ZCCACHE_VERSION = "1.14.8"
# zccache's client treats a compile that has not answered within 180 s (queue wait included) as a
# wedged daemon and fails it with exit code 113. The largest PGO-instrumented LLVM TUs
# (SelectionDAGBuilder.cpp, PassBuilder.cpp) take longer on a 4-core runner, which failed
# link-benchmark's pgo-instr twice (runs 35460448617, 35461848322). One hour still catches a real
# wedge well before any job timeout. Set for every command a job runs (main()).
ZCCACHE_ENV = {"ZCCACHE_WEDGE_RECV_TIMEOUT_SECS": "3600"}
BOLT_DIR = "/usr/lib/llvm-18/bin"


def log(message: str) -> None:
    print(message, flush=True)


def sh(command: list[str] | str, **kwargs) -> subprocess.CompletedProcess:
    shown = command if isinstance(command, str) else shlex.join(command)
    log(f"+ {shown}")
    return subprocess.run(command, check=True, cwd=kwargs.pop("cwd", ROOT), **kwargs)


def out(command: list[str] | str) -> str:
    return subprocess.run(command, shell=isinstance(command, str), capture_output=True, text=True,
                          cwd=ROOT).stdout.strip()


def gh_output(**values: str) -> None:
    for key, value in values.items():
        log(f"{key}={value}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def summary(line: str) -> None:
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def temp() -> Path:
    return Path(os.environ.get("RUNNER_TEMP") or ROOT / "build-ci")


def saves_caches(env: dict[str, str] | None = None) -> bool:
    """Caches are written by main, and by an explicit dispatch on any ref. GitHub scopes a cache to
    the ref that saved it and every other ref (PRs, tags, branches) can read main's, so a PR saving
    would only spend the 10 GB budget; a dispatch is deliberate (e.g. proving a branch warm)."""
    env = os.environ if env is None else env
    event = env.get("GITHUB_EVENT_NAME")
    if event == "workflow_dispatch":
        return True
    return env.get("GITHUB_REF") == "refs/heads/main" and event in ("push", "schedule", "workflow_call")


# ------------------------------------------------------------------ jobs


@dataclasses.dataclass
class Job:
    name: str
    body: Callable[[argparse.Namespace], None]
    # zccache compile-cache group; jobs that compile identical commands share one. None: no compile.
    cache_group: str | None = None
    # Exact-input artifact cache: key function and the directory it holds (relative to RUNNER_TEMP,
    # or to the workspace when it starts with "./").
    artifact_key: Callable[[argparse.Namespace], str] | None = None
    artifact_dir: str = ""
    # True when the job only reads the artifact cache (a later job of the same key saves it).
    artifact_read_only: bool = False
    # True when later jobs of the same run restore the artifact, so it is saved on every ref.
    artifact_needed_downstream: bool = False
    # False when the artifact is a toolchain the job provisions (xwin, clang-cl), not its output.
    artifact_skips_job: bool = True
    setup: Callable[[argparse.Namespace], None] | None = None
    # Build-tree cache: (key, restore prefix, path), restored by prefix, saved by main after `run`.
    tree: Callable[[], tuple[str, str, str]] | None = None
    # Work that must follow the cache saves (e.g. anything that dirties the build tree).
    post: Callable[[argparse.Namespace], None] | None = None
    # Warm-run time floor for this job (prepare -> end of post); None: no floor here.
    warm_max_minutes: float | None = None


def apt_install(*packages: str) -> None:
    sh(["sudo", "apt-get", "update", "-qq"])
    sh(["sudo", "apt-get", "install", "-y", "-qq", *packages], stdout=subprocess.DEVNULL)


def setup_pgo_toolchain(args: argparse.Namespace) -> None:
    apt_install("lld", "llvm", "libclang-rt-dev", "bolt-18")
    if not Path(BOLT_DIR, "llvm-bolt").exists():
        raise SystemExit(f"::error::llvm-bolt missing from {BOLT_DIR}")


def pgo_artifact_key(args: argparse.Namespace) -> str:
    identities = [out("clang --version").splitlines()[0], out(f"{BOLT_DIR}/llvm-bolt --version").splitlines()[0]]
    return bench_ci.pgo_key(ROOT, identities)


# --- link-benchmark ------------------------------------------------------


def bench_matrix(args: argparse.Namespace) -> list[list[str]]:
    command = [sys.executable, "tests/perf/gen_corpus.py", "--print-matrix", "--max-threads", str(os.cpu_count()),
               "--runs", str(args.runs), "--lto-runs", str(args.lto_runs)]
    if args.smoke:
        command.append("--smoke")
    rows = [line.split() for line in out(command).splitlines() if line.strip()]
    if not rows:
        raise SystemExit("::error::gen_corpus.py --print-matrix printed no cells")
    return rows


def corpus_key(args: argparse.Namespace) -> str:
    """The corpora are deterministic given the generator, clang and the (fixed, CI) checkout path."""
    gen = bench_ci.hash_paths(ROOT, ["tests/perf/gen_corpus.py"])[:16]
    clang = "".join(ch for ch in out("clang --version").splitlines()[0] if ch.isalnum() or ch == ".")
    return f"corpus-{'smoke' if args.smoke else 'full'}-{gen}-{clang}"


def job_bench_corpus(args: argparse.Namespace) -> None:
    for mode, profile in sorted({(row[0], row[1]) for row in bench_matrix(args)}):
        # gen_corpus.py skips every object that is already current, so a restored cache is a no-op.
        sh([sys.executable, "tests/perf/gen_corpus.py", "--out", "build-perf/corpus", "--mode", mode,
            "--profile", profile, "--jobs", str(os.cpu_count())])
    summary(f"corpus size: {out('du -sh build-perf/corpus | cut -f1')}")


def link_speed_files(baseline_ref: str | None = None) -> list[str]:
    """The link-speed payload files, from provenance/payload-prune.json; with baseline_ref, only
    those that exist there (a file added later is built from HEAD, with a warning)."""
    data = json.loads((ROOT / "provenance" / "payload-prune.json").read_text())
    files = ["llvm-project/" + e["path"] for e in data["patched"] if "link-speed" in e["reason"]]
    if not files:
        raise SystemExit("::error::provenance/payload-prune.json lists no link-speed payload files")
    if baseline_ref is None:
        return files
    kept = []
    for path in files:
        if subprocess.run(["git", "cat-file", "-e", f"{baseline_ref}:{path}"], cwd=ROOT,
                          capture_output=True).returncode == 0:
            kept.append(path)
        else:
            log(f"::warning::{path} does not exist at {baseline_ref}; the baseline uses HEAD's version")
    if not kept:
        raise SystemExit(f"::error::no link-speed payload file exists at {baseline_ref}")
    return kept


def wait_for_ci(sha: str, minutes: int = 45) -> None:
    """ci.yml's build-linux builds the baseline for every main push; racing it used to fall back to
    a cold in-job build silently, so wait for it (bounded) instead."""
    for attempt in range(minutes * 2):
        status = out(["gh", "run", "list", "--workflow", "ci.yml", "--commit", sha, "--event", "push",
                      "--limit", "1", "--json", "status", "--jq", '.[0].status // "none"']) or "none"
        if status not in ("in_progress", "queued", "pending", "waiting"):
            log(f"ci.yml for {sha}: {status}")
            return
        log(f"ci.yml for {sha} is {status}; waiting ({attempt + 1}/{minutes * 2})")
        time.sleep(30)
    log(f"::warning::ci.yml for {sha} did not finish in {minutes} minutes; building in-job")


def fetch_prebuilt_baseline(sha: str, baseline_ref: str, dest: Path) -> str | None:
    """ci.yml uploads llvm-ld-bench-bins-<sha> (candidate, baseline, bench-bins.json). Returns the
    source run id when a matching artifact was installed as dest."""
    name = f"llvm-ld-bench-bins-{sha}"
    run_ids = out(["gh", "run", "list", "--workflow", "ci.yml", "--commit", sha, "--event", "push",
                   "--limit", "20", "--json", "databaseId", "--jq", ".[].databaseId"]).split()
    want = {"source_sha": sha, "baseline_ref": baseline_ref, "files": link_speed_files(),
            "compiler": out("clang --version").splitlines()[0]}
    for run_id in run_ids:
        download = temp() / "prebuilt"
        shutil.rmtree(download, ignore_errors=True)
        if subprocess.run(["gh", "run", "download", run_id, "--name", name, "--dir", str(download)],
                          cwd=ROOT).returncode != 0:
            continue
        try:
            recorded = json.loads((download / "bench-bins.json").read_text())
        except (OSError, ValueError) as exc:
            log(f"::warning::run {run_id}: unreadable bench-bins.json ({exc})")
            continue
        mismatched = [k for k, v in want.items() if recorded.get(k) != v]
        if mismatched or not (download / "baseline").is_file():
            log(f"::warning::run {run_id}: bench-bins.json mismatch on {mismatched or ['baseline']}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(download / "baseline", dest)
        dest.chmod(0o755)
        return run_id
    return None


def job_bench_plain(args: argparse.Namespace) -> None:
    """The stock-lld baseline: prebuilt by ci.yml when available, else built here (plain -O3)."""
    sha, baseline_ref = os.environ["GITHUB_SHA"], os.environ["BASELINE_REF"]
    dest = temp() / "bin" / "baseline"
    if os.environ.get("GITHUB_REF") == "refs/heads/main":
        wait_for_ci(sha)
    source_run = fetch_prebuilt_baseline(sha, baseline_ref, dest)
    if source_run:
        args.detail = f"baseline prebuilt by ci.yml run {source_run}"
        args.compiled = False
        summary(f"baseline: prebuilt by ci.yml run {source_run}")
        return
    build = ROOT / "build"
    sh(ci_build.configure_command(ROOT, build, "zccache", "clang", "clang++", None))
    sh(ci_build.build_command(build, ["llvm-ld-direct"]))
    files = link_speed_files(baseline_ref)
    sh(["git", "checkout", baseline_ref, "--", *files])
    try:
        sh(ci_build.build_command(build, ["llvm-ld-direct"]))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(build / "llvm-ld-direct", dest)
    finally:
        sh(["git", "checkout", "HEAD", "--", *files])
    args.detail = "baseline built in-job under zccache"
    summary("baseline: no usable ci.yml artifact for this commit; built in-job")


def job_bench_pgo_instr(args: argparse.Namespace) -> None:
    """PGO stage 1: instrumented build + training -> llvm-ld.profdata (+ the training corpus)."""
    instr, profile = temp() / "pgo-instr", temp() / "pgo-profile"
    for stage in ("instrument", "train", "merge"):
        command = [sys.executable, "tools/pgo_build.py", stage, "--instr-dir", str(instr)]
        sh(command + (["--launcher", "zccache"] if stage == "instrument" else []))
    profile.mkdir(parents=True, exist_ok=True)
    shutil.copy(instr / "llvm-ld.profdata", profile)
    sh(["tar", "-C", str(instr), "--exclude=*.cpp", "-czf", str(profile / "train-corpus.tar.gz"), "train-corpus"])


def job_bench_pgo_opt(args: argparse.Namespace) -> None:
    """PGO stage 2: -fprofile-use + ThinLTO build, then BOLT -> pgo-final/llvm-ld-direct."""
    instr, build, final = temp() / "pgo-instr", temp() / "pgo-out", temp() / "pgo-final"
    sh(["tar", "-C", str(instr), "-xzf", str(instr / "train-corpus.tar.gz")])
    sh([sys.executable, "tools/pgo_build.py", "optimize", "--bolt", "--launcher", "zccache",
        "--instr-dir", str(instr), "--out-dir", str(build)])
    sh([sys.executable, "tools/pgo_build.py", "bolt", "--bolt-dir", BOLT_DIR, "--instr-dir", str(instr),
        "--out-dir", str(build)])
    final.mkdir(parents=True, exist_ok=True)
    shutil.copy(build / "llvm-ld-direct", final)
    shutil.copy(instr / "llvm-ld.profdata", final)


def job_bench_cells(args: argparse.Namespace) -> None:
    """One build mode's paired A/B cells (candidate and baseline interleaved on this runner)."""
    bins, cells = temp() / "bin", temp() / "cells"
    for name in ("candidate", "baseline"):
        (bins / name).chmod(0o755)
    if (bins / "candidate").read_bytes() == (bins / "baseline").read_bytes():
        raise SystemExit("::error::baseline and candidate are the same binary")
    cells.mkdir(parents=True, exist_ok=True)
    summary("| cell | wall-clock cost |\n|---|---|")
    for mode, profile, threads, runs, variant in (r for r in bench_matrix(args) if r[0] == args.mode):
        start = time.time()
        sh([sys.executable, "tests/perf/bench.py", "--candidate", str(bins / "candidate"),
            "--baseline", str(bins / "baseline"), "--corpus", f"build-perf/corpus/{mode}/{profile}",
            "--variant", variant, "--runs", runs, "--threads", threads,
            "--json", str(cells / f"{mode}-{profile}-t{threads}-{variant}.json")], stdin=subprocess.DEVNULL)
        summary(f"| {mode}/{profile}/t{threads}/{variant} | {time.time() - start:.0f} s |")
    meta = {
        "image": out("grep '^PRETTY_NAME=' /etc/os-release | cut -d= -f2- | tr -d '\"'"),
        "cpu": out("lscpu | grep 'Model name:' | cut -d: -f2- | xargs"),
        "cores": out("nproc"), "kernel": out("uname -r"), "arch": out("uname -m"),
        "compiler": out("clang --version | head -1"),
    }
    (cells / f"meta-{args.mode}.json").write_text(json.dumps(meta))


# --- ci.yml, correctness.yml, benchmark.yml (#57) -------------------------

PY = sys.executable
EXPECTED_EXPORTS = ["llvm_ld_abi_version", "llvm_ld_allocator_get_info", "llvm_ld_invoke",
                    "llvm_ld_profiler_start", "llvm_ld_profiler_stop"]
WINDOWS = os.name == "nt"


def check_exports(actual: list[str]) -> None:
    actual = sorted(set(actual))
    if actual != sorted(EXPECTED_EXPORTS):
        raise SystemExit(f"::error::C ABI export allowlist differs: {actual} != {sorted(EXPECTED_EXPORTS)}")
    log(f"exports ok: {', '.join(actual)}")


def audit_closure(build: str, trace: str, output: str, extra: list[str] | None = None) -> None:
    """The mechanically derived build closure must equal provenance/llvm-source-closure.json."""
    inputs, deps = ROOT / f"{build}-inputs.txt", ROOT / f"{build}-deps.txt"
    inputs.write_text(out(["ninja", "-C", build, "-t", "inputs", "llvm_ld"]) + "\n")
    deps.write_text(out(["ninja", "-C", build, "-t", "deps"]) + "\n")
    sh([PY, "tools/audit_build.py", "--source", ".", "--build", build, "--ninja-inputs", str(inputs),
        "--ninja-deps", str(deps), "--cmake-trace", trace, "--expected-llvm-closure",
        "provenance/llvm-source-closure.json", *(extra or []), "--output", output])


def launcher_or_none(compiler: str | None = None) -> str:
    """Select zccache when installed, failing closed if its compiler probe is broken."""
    if not shutil.which("zccache"):
        return "none"
    if compiler and not zccache_compiles(compiler):
        raise SystemExit(f"zccache cannot compile through {compiler}")
    return "zccache"


def zccache_compiles(compiler: str) -> bool:
    """True when `zccache <compiler> ...` compiles a trivial TU the way CMake would invoke it."""
    msvc = compiler.endswith("cl") or compiler.endswith("cl.exe")
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp, "probe.cpp")
        source.write_text("int probe() { return 0; }\n")
        if msvc:
            command = ["zccache", compiler, "/nologo", "/c", "/showIncludes",
                       f"/Fo{Path(tmp, 'probe.obj')}", str(source)]
        else:
            command = ["zccache", compiler, "-c", "-o", str(Path(tmp, "probe.o")), str(source)]
        probe = subprocess.run(command, capture_output=True, text=True, cwd=tmp)
    if probe.returncode == 0:
        return True
    log(f"::error::zccache cannot compile through {compiler}: {probe.stderr.strip()[:200]}")
    return False


def cmake_build(build: str, targets: list[str]) -> None:
    sh(["cmake", "--build", build, "--target", *targets])


def msvc_build(build: str, cmake_args: list[str], targets: list[str], trace: str | None = None) -> None:
    """Configure and build with MSVC cl under zccache."""
    ci_build.ensure_codemodel_query(ROOT / build)

    def attempt(launcher: str) -> None:
        value = "" if launcher == "none" else launcher
        command = ["cmake", "-S", ".", "-B", build, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
                   "-DLLVM_APPEND_VC_REV=OFF", f"-DCMAKE_C_COMPILER_LAUNCHER={value}",
                   f"-DCMAKE_CXX_COMPILER_LAUNCHER={value}", *cmake_args]
        if trace:
            command += ["--trace-expand", "--trace-format=json-v1", f"--trace-redirect={trace}"]
        sh(command)
        cmake_build(build, targets)

    attempt(launcher_or_none(msvc_compiler()))


def msvc_compiler() -> str:
    """The compiler CMake picks from the MSVC developer environment."""
    return "clang-cl" if os.environ.get("CC", "").startswith("clang-cl") else "cl"


def buildtree_cache() -> tuple[str, str, str]:
    identity = ci_build.compiler_identity("clang", "clang++")
    primary, restore = ci_build.compute_cache_keys(ROOT, "zccache", identity)
    return primary, restore, "build"


def job_ci_linux(args: argparse.Namespace) -> None:
    sh([PY, "-m", "unittest", "discover", "-s", "tests", "-p", "test_ci_build.py"])
    sh([PY, "tools/ci_build.py", "replay", "--import-upstream"])
    sh([PY, "tools/ci_build.py", "build", "--launcher", launcher_or_none()])
    audit_closure("build", "cmake-trace.jsonl", "source-closure.json")
    exports = out(["nm", "-D", "--defined-only", "--extern-only", "build/libllvm_ld.so"])
    check_exports([line.split()[-1] for line in exports.splitlines() if line.strip()])
    sh(["ctest", "--test-dir", "build", "--output-on-failure"])
    # The build-tree cache is saved right after this phase, while build/ still matches HEAD; the
    # baseline linker (post) dirties it with BASELINE_REF objects, so it must come after the save.
    if saves_caches():
        sh([PY, "tools/ci_build.py", "snapshot"])


def job_ci_linux_bench_bins(args: argparse.Namespace) -> None:
    """Main pushes: the link-benchmark baseline linker, built by swapping the link-speed payload
    files to BASELINE_REF and rebuilding incrementally (see CLAUDE.md, Where the binaries come
    from). The candidate is copied first and nothing rebuilds after the files are restored."""
    if not (os.environ.get("GITHUB_REF") == "refs/heads/main" and os.environ.get("GITHUB_EVENT_NAME") == "push"):
        log("not a push to main; no link-benchmark binaries")
        return
    baseline_ref = os.environ["BASELINE_REF"]
    dest = temp() / "bench-bins"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "build" / "llvm-ld-direct", dest / "candidate")
    files = link_speed_files()
    sh(["git", "checkout", baseline_ref, "--", *files])
    try:
        cmake_build("build", ["llvm-ld-direct"])
        shutil.copy(ROOT / "build" / "llvm-ld-direct", dest / "baseline")
    finally:
        sh(["git", "checkout", "HEAD", "--", *files])
    if (dest / "candidate").read_bytes() == (dest / "baseline").read_bytes():
        raise SystemExit("::error::baseline and candidate are the same binary; the payload has no link-speed delta")
    (dest / "bench-bins.json").write_text(json.dumps({
        "source_sha": os.environ["GITHUB_SHA"], "baseline_ref": baseline_ref, "files": files,
        "compiler": out("clang --version").splitlines()[0], "run_id": os.environ.get("GITHUB_RUN_ID", ""),
    }, sort_keys=True))


def setup_cross_clang(args: argparse.Namespace) -> None:
    """The pinned apt.llvm.org clang-cl/lld-link/llvm-lib/llvm-rc/llvm-nm (versions in ci.yml's
    job env; see PROVENANCE.md). The signing key's SHA-256 is checked before it is trusted."""
    env = os.environ
    series, version = env["LLVM_LD_CLANG_APT_PACKAGE_SERIES"], env["LLVM_LD_CLANG_APT_VERSION"]
    key = temp() / "llvm-snapshot.gpg.key"
    sh(["curl", "-fsSL", "-o", str(key), "https://apt.llvm.org/llvm-snapshot.gpg.key"])
    digest = hashlib.sha256(key.read_bytes()).hexdigest()
    if digest != "8b2a587ffd672c4687e7581dad4b2f6c1bb2ad6b480cd9771ba2ff48e0b8c75d":
        raise SystemExit(f"::error::apt.llvm.org signing key changed: {digest}")
    dearmored = temp() / "llvm-snapshot.gpg"
    with open(key, "rb") as src, open(dearmored, "wb") as dst:
        subprocess.run(["gpg", "--dearmor"], stdin=src, stdout=dst, check=True)
    sh(["sudo", "install", "-m", "644", str(dearmored), "/usr/share/keyrings/llvm-snapshot.gpg"])
    line = (f"deb [signed-by=/usr/share/keyrings/llvm-snapshot.gpg] https://apt.llvm.org/noble/ "
            f"{env['LLVM_LD_CLANG_APT_SUITE']} main\n")
    subprocess.run(["sudo", "tee", f"/etc/apt/sources.list.d/llvm-{series}.list"], input=line.encode(),
                   check=True, stdout=subprocess.DEVNULL)
    # clang-cl-N ships in clang-N (pulled in by clang-tools-N); llvm-lib/-rc/-nm in llvm-N;
    # lld-link-N only in lld-N, without which /usr/local/bin/lld-link dangles.
    apt_install(*(f"{pkg}-{series}={version}" for pkg in ("clang-tools", "llvm", "lld")))
    for tool in ("clang-cl", "lld-link", "llvm-lib", "llvm-rc", "llvm-nm"):
        sh(["sudo", "ln", "-sf", f"/usr/bin/{tool}-{series}", f"/usr/local/bin/{tool}"])
    if env["LLVM_LD_CLANG_VERSION"] not in out(["clang-cl", "--version"]):
        raise SystemExit(f"::error::clang-cl is not {env['LLVM_LD_CLANG_VERSION']}")


def xwin_root() -> Path:
    return temp() / "xwin-splat"


def xwin_key(args: argparse.Namespace) -> str:
    env = os.environ
    # -dbglibs is part of the identity: a splat without --include-debug-libs lacks msvcrtd & co.
    return (f"xwin-{env['LLVM_LD_XWIN_VERSION']}-manifest{env['LLVM_LD_XWIN_MANIFEST_SHA256']}"
            f"-sdk{env['LLVM_LD_XWIN_SDK_VERSION']}-crt{env['LLVM_LD_XWIN_CRT_VERSION']}-dbglibs")


def provision_xwin() -> None:
    if (xwin_root() / "crt").is_dir():
        log(f"xwin splat restored at {xwin_root()}")
        return
    env = os.environ
    sh(["cargo", "install", "xwin", "--version", env["LLVM_LD_XWIN_VERSION"], "--locked"])
    manifest = ROOT / "provenance" / "vs-channel-manifest.json"
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != env["LLVM_LD_XWIN_MANIFEST_SHA256"]:
        raise SystemExit("::error::provenance/vs-channel-manifest.json does not match its pinned SHA-256")
    # --manifest pins the SDK/CRT catalog (PROVENANCE.md); --include-debug-libs keeps the *d.lib
    # CRTs CMake's Debug try-compiles need.
    sh(["xwin", "--accept-license", "--manifest", str(manifest), "--sdk-version", env["LLVM_LD_XWIN_SDK_VERSION"],
        "--crt-version", env["LLVM_LD_XWIN_CRT_VERSION"], "splat", "--include-debug-libs",
        "--output", str(xwin_root())])


def job_ci_linux_cross(args: argparse.Namespace) -> None:
    provision_xwin()
    os.environ["LLVM_LD_XWIN_ROOT"] = str(xwin_root())
    launcher = launcher_or_none("clang-cl")
    launch = [f"-DCMAKE_C_COMPILER_LAUNCHER={launcher if launcher != 'none' else ''}",
              f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher if launcher != 'none' else ''}"]
    if launcher != "none":  # the standalone configure does not read the root CMakeLists.txt rule
        launch.append("-DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON")
    # Stage 1: native tablegen. The standalone llvm configure does not inherit the root cache
    # settings, so every LLVM_INCLUDE_* guard the pruned payload needs is repeated.
    sh(["cmake", "-S", "llvm-project/llvm", "-B", "build-tblgen", "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_APPEND_VC_REV=OFF", "-DLLVM_TARGETS_TO_BUILD=X86", "-DLLVM_ENABLE_PROJECTS=",
        "-DLLVM_INCLUDE_TESTS=OFF", "-DLLVM_INCLUDE_EXAMPLES=OFF", "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_BUILD_TOOLS=OFF", "-DLLVM_ENABLE_ZLIB=OFF", "-DLLVM_ENABLE_ZSTD=OFF",
        "-DLLVM_ENABLE_LIBXML2=OFF", "-DLLVM_ENABLE_TERMINFO=OFF", *launch])
    cmake_build("build-tblgen", ["llvm-tblgen", "llvm-min-tblgen"])
    # Stage 2: the clang-cl + xwin cross build.
    ci_build.ensure_codemodel_query(ROOT / "build-cross")

    def cross(launcher: str) -> None:
        value = "" if launcher == "none" else launcher
        sh(["cmake", "-S", ".", "-B", "build-cross", "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
            "-DLLVM_APPEND_VC_REV=OFF", "-DCMAKE_TOOLCHAIN_FILE=cmake/WinMsvcCross.cmake",
            f"-DLLVM_NATIVE_TOOL_DIR={ROOT / 'build-tblgen' / 'bin'}", "-DLLVM_DISABLE_ASSEMBLY_FILES=ON",
            f"-DCMAKE_C_COMPILER_LAUNCHER={value}", f"-DCMAKE_CXX_COMPILER_LAUNCHER={value}",
            "--trace-expand", "--trace-format=json-v1", "--trace-redirect=cmake-trace-cross.jsonl"])
        cmake_build("build-cross", ["llvm_ld", "abi_smoke", "abi_contract", "abi_state_test", "allocator-probe",
                                    "llvm-ld-runner", "llvm-ld-direct"])

    cross(launcher)
    audit_closure("build-cross", "cmake-trace-cross.jsonl", "source-closure-linux-cross.json",
                  ["--native-tool-dir", "build-tblgen/bin"])
    # The import library's symbol table lists exactly the exports (plus __imp_ twins and three
    # linker-generated import descriptors, filtered by shape so a leaked export still fails).
    names = set()
    for line in out(["llvm-nm", "--defined-only", "--extern-only", "build-cross/llvm_ld.lib"]).splitlines():
        fields = line.split()
        if len(fields) >= 2 and len(fields[-2]) == 1 and fields[-2].isalpha():
            name = fields[-1].removeprefix("__imp_")
            if not (name.startswith("__IMPORT_DESCRIPTOR_") or name == "__NULL_IMPORT_DESCRIPTOR"
                    or name.endswith("_NULL_THUNK_DATA")):
                names.add(name)
    check_exports(sorted(names))
    stage = ROOT / "stage"
    stage.mkdir(exist_ok=True)
    for name in ("llvm_ld.dll", "abi_smoke.exe", "abi_contract.exe", "abi_state_test.exe",
                 "allocator-probe.exe", "llvm-ld-runner.exe"):
        shutil.copy(ROOT / "build-cross" / name, stage)


def dumpbin_exports(dll: str) -> list[str]:
    return sorted(set(re.findall(r"\bllvm_ld_[A-Za-z_]+", out(["dumpbin", "/nologo", "/exports", dll]))))


def job_ci_windows(args: argparse.Namespace) -> None:
    # The system-baseline targets are built here too so the shared windows-msvc-release cache that
    # this job saves on main also covers correctness-coff and bench-allocator (warm = 0 misses).
    base = ["-DLLVM_LD_ENABLE_PPROF=OFF", "-DLLVM_LD_ENABLE_DHAT=OFF", "-DLLVM_LD_BUILD_SYSTEM_BASELINE=ON"]
    msvc_build("build", base, ["llvm_ld", "abi_smoke", "abi_contract", "abi_state_test", "allocator-probe",
                               "llvm-ld-runner", "llvm-ld-direct", "llvm-ld-runner-system",
                               "allocator-probe-system"], trace="cmake-trace.jsonl")
    sh(["ctest", "--test-dir", "build", "--output-on-failure"])
    check_exports(dumpbin_exports("build/llvm_ld.dll"))
    audit_closure("build", "cmake-trace.jsonl", "source-closure.json")
    # The two diagnostic allocators must activate (each a reconfigure of the same tree).
    for flags, probe in ((["-DLLVM_LD_ENABLE_PPROF=ON", "-DLLVM_LD_ENABLE_DHAT=OFF"], "mimalloc-pprof"),
                         (["-DLLVM_LD_ENABLE_PPROF=OFF", "-DLLVM_LD_ENABLE_DHAT=ON"], "mimalloc-dhat")):
        msvc_build("build", flags, ["llvm_ld", "allocator-probe"])
        sh([str(ROOT / "build" / "allocator-probe.exe"), probe])


CLANGCL_MEMBERS = ["bin/clang.exe", "bin/clang-cl.exe", "bin/libclang.dll", "bin/libiomp5md.dll", "bin/libomp.dll",
                   "bin/LLVM-C.dll", "bin/LTO.dll", "bin/Remarks.dll", "bin/lld-link.exe", "lib/clang"]


def clangcl_root() -> Path:
    return temp() / "pinned-clangcl"


def clangcl_key(args: argparse.Namespace) -> str:
    return f"clangcl-v2-{os.environ['LLVM_LD_CLANGCL_VERSION']}-{os.environ['LLVM_LD_CLANGCL_ARCHIVE_SHA256']}"


def provision_clangcl() -> None:
    """The pinned clang-cl for the LTO rows and the Tier 2 external lld-link (#6, #30): only the
    members those need are extracted from the checksum-verified release archive."""
    version = os.environ["LLVM_LD_CLANGCL_VERSION"]
    prefix = f"clang+llvm-{version}-x86_64-pc-windows-msvc"
    bindir = clangcl_root() / prefix / "bin"
    if not (bindir / "clang-cl.exe").exists():
        archive = temp() / "clang-llvm.tar.zst"
        url = (f"https://github.com/llvm/llvm-project/releases/download/llvmorg-{version}/"
               f"clang%2Bllvm-{version}-x86_64-pc-windows-msvc.tar.zst")
        sh(["curl", "-fsSL", "-o", str(archive), url])
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != os.environ["LLVM_LD_CLANGCL_ARCHIVE_SHA256"]:
            raise SystemExit(f"::error::pinned clang-cl archive checksum mismatch: {digest}")
        clangcl_root().mkdir(parents=True, exist_ok=True)
        # Windows' own bsdtar reads .tar.zst; Git Bash's GNU tar (first on a bash step's PATH) does not.
        tar = str(Path(os.environ.get("SystemRoot", "C:/Windows"), "System32", "tar.exe")) if WINDOWS else "tar"
        sh([tar, "-xf", str(archive), "-C", str(clangcl_root()), *(f"{prefix}/{m}" for m in CLANGCL_MEMBERS)])
        archive.unlink()
    reported = out([str(bindir / "clang-cl.exe"), "--version"])
    if version not in reported:
        raise SystemExit(f"::error::pinned clang-cl reported {reported!r}, not {version}")
    if os.environ.get("GITHUB_PATH"):
        with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as handle:
            handle.write(f"{bindir}\n")
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as handle:
            handle.write(f"LLVM_LD_CLANGCL_ROOT={clangcl_root()}\n")


def job_correctness_coff(args: argparse.Namespace) -> None:
    msvc_build("build", ["-DLLVM_LD_BUILD_SYSTEM_BASELINE=ON"],
               ["llvm_ld", "llvm-ld-runner", "llvm-ld-runner-system", "llvm-ld-direct"])
    provision_clangcl()


def job_bench_allocator(args: argparse.Namespace) -> None:
    msvc_build("build", ["-DLLVM_LD_ENABLE_PPROF=OFF", "-DLLVM_LD_ENABLE_DHAT=OFF",
                         "-DLLVM_LD_BUILD_SYSTEM_BASELINE=ON"],
               ["llvm-ld-runner", "llvm-ld-runner-system", "allocator-probe", "allocator-probe-system"])



# --- release.yml (#58) ---------------------------------------------------

RELEASE_CORPORA = [("debug", "small"), ("debug", "medium"), ("release", "small"), ("release", "medium"),
                   ("thinlto", "small")]


def release_corpus_key(args: argparse.Namespace) -> str:
    gen = bench_ci.hash_paths(ROOT, ["tests/perf/gen_corpus.py", "tools/release_build.py"])[:16]
    clang = "".join(ch for ch in out("clang --version").splitlines()[0] if ch.isalnum() or ch == ".")
    return f"release-corpus-{gen}-{clang}"


def job_release_corpus(args: argparse.Namespace) -> None:
    """The PGO training and gate corpora (host-independent COFF), one tarball for every leg."""
    for mode, profile in RELEASE_CORPORA:
        sh([PY, "tests/perf/gen_corpus.py", "--out", "pgo-corpus", "--mode", mode, "--profile", profile,
            "--jobs", str(os.cpu_count())])
    for source in (ROOT / "pgo-corpus").rglob("*.cpp"):
        source.unlink()
    (ROOT / "release-corpus").mkdir(exist_ok=True)
    sh(["tar", "czf", "release-corpus/pgo-corpus.tar.gz", "pgo-corpus"])


def release_kind(triple: str) -> str:
    if "windows" in triple:
        return "windows"
    if "musl" in triple:
        return "musl"
    return "macos" if "apple" in triple else "linux"


def setup_release(args: argparse.Namespace) -> None:
    kind = release_kind(args.triple)
    if kind == "linux":
        apt_install("ninja-build", "clang", "lld", "llvm", "libclang-rt-dev")
    elif kind == "macos":
        sh(["brew", "install", "ninja"])
    elif kind == "windows" and not shutil.which("clang-cl"):
        sh(["choco", "install", "llvm", "-y", "--no-progress"])
    if kind == "windows":
        llvm_bin = r"C:\Program Files\LLVM\bin"
        os.environ["PATH"] = llvm_bin + os.pathsep + os.environ["PATH"]
        if os.environ.get("GITHUB_PATH"):
            with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as handle:
                handle.write(llvm_bin + "\n")


def release_version() -> str:
    if os.environ.get("GITHUB_REF", "").startswith("refs/tags/"):
        return os.environ["GITHUB_REF_NAME"]
    return f"v0.0.0-{os.environ.get('GITHUB_SHA', 'local')[:12]}"


def fetch_reference(triple: str) -> Path:
    """The previous release's archive for this host, for release_build.py's byte-identity gate."""
    previous = out(["gh", "release", "list", "--limit", "1", "--exclude-drafts", "--exclude-pre-releases",
                    "--json", "tagName", "--jq", ".[0].tagName"])
    if not previous:
        raise SystemExit("::error::no previous release to gate against")
    reference = ROOT / "reference"
    reference.mkdir(exist_ok=True)
    sh(["gh", "release", "download", previous, "-D", str(reference), "-p", f"llvm-ld-coff-{previous}-{triple}.*",
        "--clobber"])
    archive = next(reference.glob("llvm-ld-coff-*"))
    summary(f"### PGO gate against {previous}")
    return archive


def job_release_build(args: argparse.Namespace) -> None:
    """One release host: PGO + ThinLTO build, byte-identity gate against the previous release, and
    packaging (tools/release_build.py), with zccache as the compiler launcher."""
    triple, kind = args.triple, release_kind(args.triple)
    with tarfile.open(ROOT / "pgo-corpus.tar.gz") as corpus:  # portable: Windows legs too
        corpus.extractall(ROOT, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    archive = fetch_reference(triple).relative_to(ROOT)
    common = ["tools/release_build.py", "--triple", triple, "--version", release_version(), "--pgo",
              "--train-corpus", "pgo-corpus", "--reference", str(archive)]
    if kind == "musl":
        args.unobserved = True
        release_build_in_alpine(common)
        return

    sh([PY, *common, "--launcher", launcher_or_none("clang-cl" if kind == "windows" else "clang++")])


def release_build_in_alpine(common: list[str]) -> None:
    """musl hosts build inside Alpine on the same-architecture runner, so the library links musl
    and its tests run natively. zccache's Linux release binary is static musl, so the host's copy
    runs in the container; the host daemon is stopped first so the container's daemon owns the
    restored cache directory, which the template saves afterwards. Its compiles are not visible
    to this job's zccache session, so the job is never reported warm."""
    zccache = shutil.which("zccache")
    mounts, env, launcher, cache_root = [], [], "none", ""
    if zccache:
        cache_root = out([zccache, "cache-root"]) or str(Path.home() / ".zccache")
        Path(cache_root).mkdir(parents=True, exist_ok=True)
        subprocess.run([zccache, "stop"], capture_output=True)
        mounts = ["-v", f"{Path(zccache).parent}:/opt/zccache:ro", "-v", f"{cache_root}:/zccache-cache"]
        # The container's daemon logs to the checkout, so a failure shows zccache's side of it.
        env = ["-e", "ZCCACHE_CACHE_DIR=/zccache-cache", "-e", "ZCCACHE_LOG_FILE=/src/zccache-daemon.log",
               *(f"-e{key}={value}" for key, value in ZCCACHE_ENV.items()),
               "-e", "PATH=/opt/zccache:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
        launcher = "zccache"
    script = ("apk add --no-cache build-base cmake ninja python3 linux-headers git clang lld llvm compiler-rt && "
              "clang --version && python3 " + " ".join(shlex.quote(a) for a in common) + f" --launcher {launcher}")
    daemon_log = ROOT / "zccache-daemon.log"
    try:
        sh(["docker", "run", "--rm", "-v", f"{ROOT}:/src", "-w", "/src", *mounts, *env,
            "-e", "GITHUB_STEP_SUMMARY=/src/step-summary.md", "alpine:3.20", "sh", "-euc", script])
    except subprocess.CalledProcessError:
        if daemon_log.exists():
            log("::group::zccache daemon log (last 200 lines)")
            log("\n".join(daemon_log.read_text(errors="replace").splitlines()[-200:]))
            log("::endgroup::")
        raise
    finally:
        uid_gid = f"{os.getuid()}:{os.getgid()}"
        sh(["sudo", "chown", "-R", uid_gid, str(ROOT)])
        if zccache:
            sh(["sudo", "chown", "-R", uid_gid, cache_root])
        note = ROOT / "step-summary.md"
        if note.exists():
            summary(note.read_text())
            note.unlink()
        for rotated in ROOT.glob("zccache-daemon.log*"):
            rotated.unlink()


JOBS: dict[str, Job] = {job.name: job for job in [
    Job("bench-corpus", job_bench_corpus, artifact_key=corpus_key, artifact_dir="./build-perf/corpus",
        artifact_needed_downstream=True),
    Job("bench-plain", job_bench_plain, cache_group="linux-clang-release"),
    Job("bench-pgo-instr", job_bench_pgo_instr, cache_group="linux-clang-pgo-instr",
        artifact_key=pgo_artifact_key, artifact_dir="pgo-final", artifact_read_only=True,
        setup=setup_pgo_toolchain),
    Job("bench-pgo-opt", job_bench_pgo_opt, cache_group="linux-clang-pgo-opt",
        artifact_key=pgo_artifact_key, artifact_dir="pgo-final", setup=setup_pgo_toolchain),
    Job("bench-cells", job_bench_cells),
    # Floors are warm-run limits on prepare -> end of post (#57); cold runs only report them.
    Job("ci-linux", job_ci_linux, cache_group="linux-clang-release", tree=buildtree_cache,
        post=job_ci_linux_bench_bins, warm_max_minutes=5),
    Job("ci-linux-cross", job_ci_linux_cross, cache_group="linux-clangcl-cross", setup=setup_cross_clang,
        artifact_key=xwin_key, artifact_dir="xwin-splat", artifact_skips_job=False, warm_max_minutes=8),
    Job("ci-windows", job_ci_windows, cache_group="windows-msvc-release", warm_max_minutes=10.5),
    Job("correctness-coff", job_correctness_coff, cache_group="windows-msvc-release",
        artifact_key=clangcl_key, artifact_dir="pinned-clangcl", artifact_skips_job=False, warm_max_minutes=5),
    Job("bench-allocator", job_bench_allocator, cache_group="windows-msvc-release", warm_max_minutes=5),
    Job("release-corpus", job_release_corpus, artifact_key=release_corpus_key, artifact_dir="./release-corpus"),
    # Release legs compile PGO-instrumented and -fprofile-use objects, so each triple has its own
    # group; no floor, since a release is rarely warm (the scheduled main run refreshes the caches).
    Job("release-build", job_release_build, cache_group="release-{triple}", setup=setup_release),
]}


# ------------------------------------------------------------------ zccache session


def zccache_cmd() -> list[str]:
    return shlex.split(os.environ.get("ZCCACHE") or "zccache")


def session_start() -> str | None:
    try:
        text = subprocess.run([*zccache_cmd(), "session-start", "--stats"], capture_output=True, text=True,
                              check=True).stdout
        return json.loads(text.strip().splitlines()[-1])["session_id"]
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, IndexError) as exc:
        log(f"::warning::zccache session-start failed ({exc}); the job is reported cold")
        return None


def session_end(session: str) -> dict:
    text = subprocess.run([*zccache_cmd(), "session-end", "--json", session], capture_output=True,
                          text=True).stdout
    try:
        return json.loads(text)
    except ValueError:
        return {"status": "error", "raw": text[-400:]}


# A job is warm when nearly every compile came from the cache. Not "zero misses": a GitHub cache
# entry is immutable, so whatever a key was saved with is what later runs get, and a job that
# compiles anything new (bench-plain rebuilds six payload files at BASELINE_REF) keeps missing on
# those few objects for as long as that key lives. 1755 hits / 36 misses is a warm run by any
# useful definition; 0 misses would make the CI-time floors unenforceable forever.
WARM_HIT_RATIO = 0.9


def is_warm(stats: dict | None) -> bool:
    if not stats or stats.get("status") != "ok":
        return False
    hits, misses = stats.get("hits") or 0, stats.get("misses") or 0
    if hits + misses == 0:  # nothing compiled at all (a build-tree or artifact cache hit)
        return True
    return hits / (hits + misses) >= WARM_HIT_RATIO


# ------------------------------------------------------------------ commands


def timing_path(job: str, label: str | None) -> Path:
    safe = (label or job).replace(":", "-").replace("/", "-")
    return temp() / "timing" / f"timing-{safe}.json"


def cache_group(job: Job, args: argparse.Namespace) -> str:
    """The job's zccache group; "{triple}" etc. are filled from the job's arguments."""
    return (job.cache_group or "").format(**vars(args))


def artifact_path(job: Job) -> Path:
    if job.artifact_dir.startswith("./"):
        return ROOT / job.artifact_dir[2:]
    return temp() / job.artifact_dir


def artifact_cache_path(job: Job) -> str:
    """The path given to actions/cache. actions/cache folds the path *as spelled* into the entry's
    version, so a workspace artifact must be spelled exactly as the jobs that restore it spell it
    (relative, e.g. build-perf/corpus); an absolute spelling made every restore of the corpus miss
    (run 35463179074)."""
    return job.artifact_dir[2:] if job.artifact_dir.startswith("./") else str(artifact_path(job))


def cmd_prepare(args: argparse.Namespace) -> int:
    job = JOBS[args.job]
    start = time.time()
    if job.setup:
        job.setup(args)
    tree_key, tree_restore, tree_path = job.tree() if job.tree else ("", "", "")
    gh_output(
        start=f"{start:.0f}",
        **{"cache-group": cache_group(job, args),
           "artifact-key": job.artifact_key(args) if job.artifact_key else "",
           "artifact-path": artifact_cache_path(job) if job.artifact_key else "",
           "artifact-save": "true" if job.artifact_key and not job.artifact_read_only
           and (saves_caches() or job.artifact_needed_downstream) else "false",
           "artifact-skips": "true" if job.artifact_skips_job else "false",
           "tree-key": tree_key, "tree-restore": tree_restore, "tree-path": tree_path,
           "tree-save": "true" if job.tree and saves_caches() else "false",
           "has-post": "true" if job.post else "false",
           "save": "true" if saves_caches() else "false",
           "zccache-version": ZCCACHE_VERSION,
           "timing-name": timing_path(args.job, args.label).stem},
    )
    return 0


def run_body(job: Job, body: Callable[[argparse.Namespace], None], args: argparse.Namespace) -> tuple[int, dict | None]:
    """Run one phase of a job under a zccache stats session; never raises."""
    session = session_start() if job.cache_group else None
    if session:
        os.environ["ZCCACHE_SESSION_ID"] = session
    status, stats = 0, None
    try:
        body(args)
    except subprocess.CalledProcessError as exc:
        log(f"::error::{job.name}: command failed with exit code {exc.returncode}")
        status = 1
    except (Exception, SystemExit) as exc:  # the timing record is still written
        log(f"::error::{job.name}: {exc}")
        status = 1
    finally:
        if session:
            stats = session_end(session)
            log(f"zccache: {stats.get('compilations')} compilations, {stats.get('hits')} hits, "
                f"{stats.get('misses')} misses, {stats.get('non_cacheable')} non-cacheable")
    return status, stats


def cmd_run(args: argparse.Namespace) -> int:
    job = JOBS[args.job]
    start = args.start or time.time()
    args.detail, args.compiled = "", job.cache_group is not None
    stats, status = None, 0
    if args.cached == "true":
        args.detail, args.compiled = "exact-input cache hit, skipped", False
        log(f"{job.name}: exact-input artifact cache hit; nothing to do")
    else:
        status, stats = run_body(job, job.body, args)
    # A job whose compiles ran where this session cannot see them is never warm.
    warm = status == 0 and not getattr(args, "unobserved", False) and (
        args.cached == "true" or not args.compiled or is_warm(stats))
    record = {"job": args.label or job.name, "seconds": round(time.time() - start, 1), "warm": warm,
              "detail": args.detail or ("built" if args.compiled else "")}
    if stats and args.compiled:
        record["zccache"] = {k: stats.get(k) for k in ("compilations", "hits", "misses", "non_cacheable")}
        summary(f"zccache {record['job']}: {stats.get('hits')} hits, {stats.get('misses')} misses (warm={warm})")
    path = timing_path(job.name, args.label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1) + "\n")
    log(json.dumps(record))
    gh_output(warm="true" if warm else "false")
    return status


def cmd_post(args: argparse.Namespace) -> int:
    """The job's post phase; its time is added to the job's timing record."""
    job = JOBS[args.job]
    start = time.time()
    status, _ = run_body(job, job.post, args)
    path = timing_path(job.name, args.label)
    record = json.loads(path.read_text())
    record["seconds"] = round(record["seconds"] + time.time() - start, 1)
    record["warm"] = record["warm"] and status == 0
    path.write_text(json.dumps(record, indent=1) + "\n")
    return status


def check_floor(job: Job, record: dict) -> str | None:
    """A violation message when a warm run exceeded the job's floor; cold runs are only reported."""
    if job.warm_max_minutes is None:
        return None
    minutes = record["seconds"] / 60
    if record["warm"] and minutes > job.warm_max_minutes:
        return f"warm {record['job']} took {minutes:.1f} min > floor {job.warm_max_minutes} min"
    return None


def cmd_floor(args: argparse.Namespace) -> int:
    job = JOBS[args.job]
    path = timing_path(job.name, args.label)
    if job.warm_max_minutes is None or not path.exists():
        return 0
    record = json.loads(path.read_text())
    violation = check_floor(job, record)
    state = "warm" if record["warm"] else "cold (floor reported, not enforced)"
    line = (f"{record['job']}: {record['seconds'] / 60:.1f} min, {state}; "
            f"warm floor {job.warm_max_minutes} min")
    if record.get("zccache"):
        line += f"; zccache {record['zccache'].get('hits')} hits / {record['zccache'].get('misses')} misses"
    log(line)
    summary(f"- floor: {line}")
    if violation:
        log(f"::error::{violation}")
        return 1
    return 0


def prune_prefix(prefix: str, keep: str) -> None:
    ref = os.environ.get("GITHUB_REF", "refs/heads/main")
    listed = out(["gh", "cache", "list", "--key", prefix, "--ref", ref, "--limit", "100", "--json", "id,key"])
    for entry in json.loads(listed or "[]"):
        if entry["key"].startswith(prefix) and entry["key"] != keep:
            sh(["gh", "cache", "delete", str(entry["id"])])


def cmd_prune(args: argparse.Namespace) -> int:
    """zackees/zccache keys its compile cache by commit (zccache-<os>-<arch>-<group>-<sha>) and
    restores by prefix, so every saving run adds an entry; build trees likewise. Keep only the
    entries this run saved, so one job cannot grow into the 10 GB budget."""
    job = JOBS[args.job]
    if not saves_caches():
        log("this run saves no caches; nothing to prune")
        return 0
    if job.cache_group:
        prefix = f"zccache-{os.environ.get('RUNNER_OS', '')}-{os.environ.get('RUNNER_ARCH', '')}-{cache_group(job, args)}-"
        prune_prefix(prefix, prefix + os.environ["GITHUB_SHA"])
    if job.tree and args.tree_key:
        prune_prefix(ci_build.KEY_PREFIX + "-", args.tree_key)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for job in JOBS.values():
        print(f"{job.name:18} cache={job.cache_group or '-':24} artifact={job.artifact_dir or '-'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("prepare", cmd_prepare), ("run", cmd_run), ("post", cmd_post), ("floor", cmd_floor)):
        p = sub.add_parser(name)
        p.add_argument("job", choices=sorted(JOBS))
        p.add_argument("--label", help="timing name when one job runs several times (e.g. cells:debug)")
        p.add_argument("--start", type=float, help="epoch seconds the job started (from prepare)")
        p.add_argument("--cached", choices=["true", "false"], default="false")
        p.add_argument("--mode", help="bench-cells: the build mode to measure")
        p.add_argument("--smoke", action="store_true", help="bench-*: the small smoke matrix")
        p.add_argument("--runs", type=int, default=9)
        p.add_argument("--lto-runs", type=int, default=5)
        p.add_argument("--triple", help="release-build: the host triple")
        p.set_defaults(func=func)
    prune = sub.add_parser("prune")
    prune.add_argument("job", choices=sorted(JOBS))
    prune.add_argument("--tree-key", default="")
    prune.add_argument("--triple", help="release-build: the host triple")
    prune.set_defaults(func=cmd_prune)
    sub.add_parser("list").set_defaults(func=cmd_list)
    args = parser.parse_args(argv)
    for key, value in ZCCACHE_ENV.items():
        os.environ.setdefault(key, value)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
