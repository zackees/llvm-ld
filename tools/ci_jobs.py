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
  prune            delete superseded compile-cache entries of one group (main only)
  list             print the job table

Stdlib only.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bench_ci  # noqa: E402
import ci_build  # noqa: E402

# The zccache release the template installs (`zackees/zccache@<tag>` with `zccache-version`).
ZCCACHE_VERSION = "1.14.3"
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
    setup: Callable[[], None] | None = None


def apt_install(*packages: str) -> None:
    sh(["sudo", "apt-get", "update", "-qq"])
    sh(["sudo", "apt-get", "install", "-y", "-qq", *packages], stdout=subprocess.DEVNULL)


def setup_pgo_toolchain() -> None:
    apt_install("lld", "llvm", "libclang-rt-dev", "bolt-18")
    if not Path(BOLT_DIR, "llvm-bolt").exists():
        raise SystemExit(f"::error::llvm-bolt missing from {BOLT_DIR}")


def pgo_artifact_key(args: argparse.Namespace) -> str:
    identities = [out("clang --version").splitlines()[0], out(f"{BOLT_DIR}/llvm-bolt --version").splitlines()[0]]
    return bench_ci.pgo_key(ROOT, identities)


# --- link-benchmark ------------------------------------------------------


def bench_matrix(args: argparse.Namespace) -> list[list[str]]:
    command = ["python3", "tests/perf/gen_corpus.py", "--print-matrix", "--max-threads", str(os.cpu_count()),
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
        sh(["python3", "tests/perf/gen_corpus.py", "--out", "build-perf/corpus", "--mode", mode,
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
        command = ["python3", "tools/pgo_build.py", stage, "--instr-dir", str(instr)]
        sh(command + (["--launcher", "zccache"] if stage == "instrument" else []))
    profile.mkdir(parents=True, exist_ok=True)
    shutil.copy(instr / "llvm-ld.profdata", profile)
    sh(["tar", "-C", str(instr), "--exclude=*.cpp", "-czf", str(profile / "train-corpus.tar.gz"), "train-corpus"])


def job_bench_pgo_opt(args: argparse.Namespace) -> None:
    """PGO stage 2: -fprofile-use + ThinLTO build, then BOLT -> pgo-final/llvm-ld-direct."""
    instr, build, final = temp() / "pgo-instr", temp() / "pgo-out", temp() / "pgo-final"
    sh(["tar", "-C", str(instr), "-xzf", str(instr / "train-corpus.tar.gz")])
    sh(["python3", "tools/pgo_build.py", "optimize", "--bolt", "--launcher", "zccache",
        "--instr-dir", str(instr), "--out-dir", str(build)])
    sh(["python3", "tools/pgo_build.py", "bolt", "--bolt-dir", BOLT_DIR, "--instr-dir", str(instr),
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
        sh(["python3", "tests/perf/bench.py", "--candidate", str(bins / "candidate"),
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


def call_with_bypass_retry(job: Job, body: Callable[[argparse.Namespace], None], args: argparse.Namespace) -> None:
    """Run a compiling job's body; if it fails, run it once more with zccache bypassed
    (ZCCACHE_DISABLE=1: every compile goes straight to the compiler). Ninja resumes where it
    stopped, so only the remaining compiles run uncached. zccache has failed compiles with no
    compiler diagnostic (exit 113 on a slow compile; a silent failure of the instrumented
    PassBuilder.cpp inside Alpine), and a cache fault must cost time, not a red build. A real
    compile error fails the second attempt too. The fallback is a warning and is recorded."""
    try:
        body(args)
    except (subprocess.CalledProcessError, SystemExit):
        if job.cache_group is None or os.environ.get("ZCCACHE_DISABLE") == "1":
            raise
        log(f"::warning::{job.name} failed under zccache; retrying once with zccache bypassed (ZCCACHE_DISABLE=1)")
        summary(f"- zccache: {job.name} failed under zccache and was retried with the cache bypassed")
        os.environ["ZCCACHE_DISABLE"] = "1"
        args.bypassed = True
        body(args)


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
        job.setup()
    gh_output(
        start=f"{start:.0f}",
        **{"cache-group": job.cache_group or "",
           "artifact-key": job.artifact_key(args) if job.artifact_key else "",
           "artifact-path": artifact_cache_path(job) if job.artifact_key else "",
           "artifact-save": "true" if job.artifact_key and not job.artifact_read_only
           and (saves_caches() or job.artifact_needed_downstream) else "false",
           "save": "true" if saves_caches() else "false",
           "zccache-version": ZCCACHE_VERSION,
           "timing-name": timing_path(args.job, args.label).stem},
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    job = JOBS[args.job]
    start = args.start or time.time()
    args.detail, args.compiled = "", job.cache_group is not None
    stats, status = None, 0
    if args.cached == "true":
        args.detail, args.compiled = "exact-input cache hit, skipped", False
        log(f"{job.name}: exact-input artifact cache hit; nothing to do")
    else:
        session = session_start() if job.cache_group else None
        env_session = {"ZCCACHE_SESSION_ID": session} if session else {}
        os.environ.update(env_session)
        try:
            call_with_bypass_retry(job, job.body, args)
        except subprocess.CalledProcessError as exc:
            log(f"::error::{job.name}: command failed with exit code {exc.returncode}")
            status = 1
        except (Exception, SystemExit) as exc:  # the timing record is still written below
            log(f"::error::{job.name}: {exc}")
            status = 1
        finally:
            if session:
                stats = session_end(session)
                log(f"zccache: {stats.get('compilations')} compilations, {stats.get('hits')} hits, "
                    f"{stats.get('misses')} misses, {stats.get('non_cacheable')} non-cacheable")
    warm = status == 0 and not getattr(args, "bypassed", False) and (
        args.cached == "true" or not args.compiled or is_warm(stats))
    record = {"job": args.label or job.name, "seconds": round(time.time() - start, 1), "warm": warm,
              "detail": args.detail or ("built" if args.compiled else "")}
    if getattr(args, "bypassed", False):
        record["detail"] += " (retried with zccache bypassed)"
    if stats and args.compiled:
        record["zccache"] = {k: stats.get(k) for k in ("compilations", "hits", "misses", "non_cacheable")}
        summary(f"zccache {record['job']}: {stats.get('hits')} hits, {stats.get('misses')} misses (warm={warm})")
    path = timing_path(job.name, args.label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1) + "\n")
    log(json.dumps(record))
    gh_output(warm="true" if warm else "false")
    return status


def cmd_prune(args: argparse.Namespace) -> int:
    """zackees/zccache keys its compile cache by commit (zccache-<os>-<arch>-<group>-<sha>) and
    restores by prefix, so every main commit adds an entry. Keep only the one just saved."""
    if not saves_caches():
        log("not a main-branch run; nothing to prune")
        return 0
    prefix = f"zccache-{args.os}-{args.arch}-{args.group}-"
    keep = prefix + os.environ["GITHUB_SHA"]
    listed = out(["gh", "cache", "list", "--key", prefix, "--ref", "refs/heads/main", "--limit", "100",
                  "--json", "id,key"])
    for entry in json.loads(listed or "[]"):
        if entry["key"].startswith(prefix) and entry["key"] != keep:
            sh(["gh", "cache", "delete", str(entry["id"])])
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for job in JOBS.values():
        print(f"{job.name:18} cache={job.cache_group or '-':24} artifact={job.artifact_dir or '-'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("prepare", cmd_prepare), ("run", cmd_run)):
        p = sub.add_parser(name)
        p.add_argument("job", choices=sorted(JOBS))
        p.add_argument("--label", help="timing name when one job runs several times (e.g. cells:debug)")
        p.add_argument("--start", type=float, help="epoch seconds the job started (from prepare)")
        p.add_argument("--cached", choices=["true", "false"], default="false")
        p.add_argument("--mode", help="bench-cells: the build mode to measure")
        p.add_argument("--smoke", action="store_true", help="bench-*: the small smoke matrix")
        p.add_argument("--runs", type=int, default=9)
        p.add_argument("--lto-runs", type=int, default=5)
        p.set_defaults(func=func)
    prune = sub.add_parser("prune")
    prune.add_argument("--group", required=True)
    prune.add_argument("--os", default=os.environ.get("RUNNER_OS", ""))
    prune.add_argument("--arch", default=os.environ.get("RUNNER_ARCH", ""))
    prune.set_defaults(func=cmd_prune)
    sub.add_parser("list").set_defaults(func=cmd_list)
    args = parser.parse_args(argv)
    for key, value in ZCCACHE_ENV.items():
        os.environ.setdefault(key, value)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
