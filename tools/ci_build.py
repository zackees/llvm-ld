#!/usr/bin/env python3
"""Drive ci.yml build-linux's configure+build, cached across runs by zccache.

CI's build-linux job and local developers both configure the same CMake
project, build the same Ninja targets, and want the same thing out of a
compiler-cache-warm build tree: skip recompiling object files whose inputs
have not changed. sccache (via `CMAKE_*_COMPILER_LAUNCHER`) already caches
individual compiler invocations, but a compiler cache alone still leaves
Ninja re-*stat*-ing and re-deciding every target on a fully restored build
tree, because a restored tree's mtimes do not match what Ninja last saw. The
build-tree cache closes that gap with zccache's content-verified mtime
replay (zackees/llvm-ld#21): `zccache snapshot` walks the workspace once and
records path/size/mtime/BLAKE3 for every file into a manifest; a later
`zccache replay` restores the recorded mtime onto a file only when that
file's current size and BLAKE3 still match, so a byte-identical restore of
the build tree reads to Ninja as "nothing changed" and Ninja's own
dependency graph decides what, if anything, still needs rebuilding.

That leaves one gap replay alone does not close: it only ever *applies* a
recorded mtime to a file it has verified is unchanged; anything else (a
missing file, a size mismatch, a content mismatch) is left exactly as it
was found. If a *changed* file already happens to carry an old mtime -
because it came from a tar/rsync-preserved tree, or because a local checkout
or patch step backdated it - replay has no reason to touch it, and Ninja
would then silently treat a stale object as up to date. This script closes
that hole itself: before calling `zccache replay` it stamps every
manifest-listed regular file to the *current* time (`stamp_fresh`), and only
then runs replay. After that pair of steps, every file's mtime is either the
exact value replay restored (content-verified unchanged - safe to skip) or
"just now" (anything replay did not touch - safe to rebuild), independent of
whatever mtimes happened to be on disk beforehand. That ordering, not
zccache alone, is what makes trusting a restored build tree correct rather
than merely convenient.

The corollary is that a low applied ratio from `zccache replay` is never a
correctness problem, only a cache-warmth one: every file that replay did not
verify keeps the fresh mtime `stamp_fresh` gave it, so Ninja rebuilds it from
scratch exactly as it would with an empty cache. `cmd_replay` therefore only
warns below `--warn-below`; it never fails the build over a cache miss.

Subcommands
  key       print (and optionally emit to $GITHUB_OUTPUT) the build-tree
            cache key a CI cache-restore/cache-save step should key on
  replay    stamp every manifest-listed file fresh, then zccache-replay the
            verified-unchanged mtimes back to make a restored tree look warm
  build     configure (with the resolved launcher) and build DEFAULT_TARGETS
            (or --target), reporting how many Ninja edges ran
  snapshot  write the mtime manifest the next run's `replay` will consume
  all       replay, then build, then snapshot - one command for local use
            (CI runs the three steps separately so the cache restore/save
            and the main-only snapshot can sit between them)

Local usage: install zccache in a venv (`pip install zccache==1.14.0`), then
from the repository root run `python tools/ci_build.py all`. `--launcher`
defaults to `auto`, which picks zccache when it is on PATH and falls back to
an uncached build (`none`) otherwise, so the command works with or without
zccache installed - it just is not build-tree-cached without it.

Stdlib only, by design: CI installs zccache itself as an explicit, reviewed
step (mozilla-actions/sccache-action's analogue for zccache), and this
driver script must not grow a hidden pip-install dependency of its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

ZCCACHE_VERSION = "1.14.0"

# Identical to ci.yml build-linux's `cmake --build build --target ...` today.
DEFAULT_TARGETS = [
    "llvm_ld",
    "abi_smoke",
    "abi_contract",
    "abi_state_test",
    "allocator-probe",
    "llvm-ld-runner",
    "llvm-ld-direct",
]

DEFAULT_BUILD_DIR = "build"
# The default manifest lives inside the build directory, so caching `build/`
# (e.g. via actions/cache) also caches the manifest for free. `snapshot`
# excludes build_dir, so the manifest never lists (and therefore never
# fingerprints) itself.
MANIFEST_NAME = "zccache-mtimes.json"
DEFAULT_TRACE_FILE = "cmake-trace.jsonl"
DEFAULT_WARN_BELOW = 0.9

# Cache-key namespace. Bump the version suffix on any change to what the key
# covers, so an old cache entry cannot be misread under a new meaning.
KEY_PREFIX = "llvm-ld-buildtree-v1"

# Files/directories (relative to the workspace) whose content determines
# whether a build-tree cache entry is still valid for this checkout. A
# missing entry is skipped rather than erroring, so the key stays computable
# from a shallow or partially populated checkout.
KEY_INPUT_PATHS = [
    "CMakeLists.txt",
    "exports.map",
    "cmake",
    "src",
    "include",
    "tools",
    "tests",
    "provenance/llvm-source-closure.json",
    "provenance/payload-prune.json",
]
# Narrower slice of KEY_INPUT_PATHS folded into the restore-prefix (rather
# than only the primary key), so a cache-restore step's prefix match still
# narrows to entries built against the same pinned LLVM/payload closure.
PAYLOAD_KEY_PATHS = [
    "provenance/llvm-source-closure.json",
    "provenance/payload-prune.json",
]

INSTALL_HINT = (
    "install it with `pip install zccache==1.14.0` (in a venv), "
    "`pipx install zccache==1.14.0` or `uv tool install zccache==1.14.0`"
)

_EDGE_LINE = re.compile(r"^\[\d+/\d+\] ")


# --------------------------------------------------------------- arguments


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    """Resolve --workspace/--build-dir/--manifest to absolute paths.

    --workspace resolves against the current directory, like any other CLI
    path. --build-dir and --manifest, when given relative, resolve against
    the *workspace* rather than the current directory, so a caller can `cd`
    anywhere and still name workspace-relative paths consistently.
    """
    workspace = Path(args.workspace).resolve()
    build_dir = Path(args.build_dir)
    if not build_dir.is_absolute():
        build_dir = workspace / build_dir
    build_dir = build_dir.resolve()
    if args.manifest is None:
        manifest = build_dir / MANIFEST_NAME
    else:
        manifest = Path(args.manifest)
        if not manifest.is_absolute():
            manifest = workspace / manifest
        manifest = manifest.resolve()
    return workspace, build_dir, manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    summary = (__doc__ or "").strip().splitlines()[0] if __doc__ else ""
    parser = argparse.ArgumentParser(prog="ci_build.py", description=summary)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--workspace", default=".", help="workspace root (default: .)")
        sub.add_argument(
            "--build-dir",
            default=DEFAULT_BUILD_DIR,
            help=f"build directory, relative to --workspace (default: {DEFAULT_BUILD_DIR})",
        )
        sub.add_argument(
            "--manifest",
            default=None,
            help=f"mtime manifest path, relative to --workspace (default: <build-dir>/{MANIFEST_NAME})",
        )

    def add_replay_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--warn-below",
            type=float,
            default=DEFAULT_WARN_BELOW,
            help="warn (never fail) if the applied ratio is below this (default: %(default)s)",
        )
        sub.add_argument(
            "--import-upstream",
            action="store_true",
            help="import upstream mimalloc/libxml2 first if either is missing on disk",
        )

    def add_build_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--launcher",
            choices=["auto", "zccache", "sccache", "none"],
            default="auto",
            help="compiler launcher (default: auto, meaning zccache if on PATH else none)",
        )
        sub.add_argument(
            "--target",
            action="append",
            dest="targets",
            default=None,
            help="cmake --build target (repeatable; default: the ci.yml build-linux target set)",
        )
        sub.add_argument(
            "--cmake-arg",
            action="append",
            dest="cmake_args",
            default=None,
            help="extra cmake configure argument (repeatable)",
        )
        sub.add_argument("--c-compiler", default="clang")
        sub.add_argument("--cxx-compiler", default="clang++")
        sub.add_argument(
            "--trace-file",
            default=DEFAULT_TRACE_FILE,
            help=(
                "cmake --trace-redirect target, relative to --workspace "
                f"(default: {DEFAULT_TRACE_FILE})"
            ),
        )
        sub.add_argument(
            "--no-trace",
            action="store_true",
            help="skip --trace-expand (audit_build.py's --cmake-trace input)",
        )
        sub.add_argument(
            "--expect-no-work",
            action="store_true",
            help="exit 1 if ninja would have any edges to run (warm-cache CI assertion)",
        )

    key = subparsers.add_parser("key", help="print the build-tree cache key")
    add_common(key)
    key.add_argument("--launcher", choices=["zccache", "sccache", "none"], default="sccache")
    key.add_argument("--c-compiler", default="clang")
    key.add_argument("--cxx-compiler", default="clang++")
    key.add_argument(
        "--github-output",
        action="store_true",
        help="also append primary/restore-prefix to $GITHUB_OUTPUT",
    )

    replay = subparsers.add_parser(
        "replay", help="restore verified-unchanged mtimes from the manifest"
    )
    add_common(replay)
    add_replay_args(replay)

    build = subparsers.add_parser("build", help="configure and build")
    add_common(build)
    add_build_args(build)

    snapshot = subparsers.add_parser("snapshot", help="write the mtime manifest for next time")
    add_common(snapshot)

    all_cmd = subparsers.add_parser("all", help="replay, then build, then snapshot")
    add_common(all_cmd)
    add_replay_args(all_cmd)
    add_build_args(all_cmd)

    return parser.parse_args(argv)


# ---------------------------------------------------------------- zccache


def find_zccache() -> list[str] | None:
    """Locate a zccache invocation, as an argv prefix.

    `$ZCCACHE`, when set, is shell-split rather than treated as a bare path,
    so a caller can point it at e.g. `uvx --from zccache==1.14.0 zccache`
    without needing a real binary on PATH.
    """
    env = os.environ.get("ZCCACHE")
    if env:
        return shlex.split(env)
    which = shutil.which("zccache")
    if which:
        return [which]
    return None


def require_zccache(purpose: str) -> list[str]:
    found = find_zccache()
    if found is not None:
        return found
    raise SystemExit(
        f"error: zccache {ZCCACHE_VERSION} is required for {purpose} but was not found "
        f"on PATH; {INSTALL_HINT}"
    )


def replay_command(zccache: list[str], workspace: Path, manifest: Path) -> list[str]:
    return [*zccache, "replay", "--workspace", str(workspace), "--manifest", str(manifest), "--json"]


def snapshot_command(zccache: list[str], workspace: Path, build_dir: Path, manifest: Path) -> list[str]:
    return [
        *zccache,
        "snapshot",
        "--workspace",
        str(workspace),
        "--out",
        str(manifest),
        "--exclude",
        str(build_dir),
    ]


# -------------------------------------------------------------- cmake/ninja
#
# LLVM_APPEND_VC_REV=OFF: with it ON, LLVM stamps the git revision into the
# generated VCSRevision.h, so every commit changes LLVMSupport and therefore
# llvm_ld.dll - which defeats byte-identity comparisons across commits and
# poisons the compiler caches (including this script's own build-tree cache:
# a revision-stamped object would never replay-verify against a different
# commit's manifest anyway, but it would also needlessly cold-build sccache).
#
# clang, not the platform's default compiler: link-benchmark.yml builds and
# reports clang (its runner metadata records clang --version and its corpora
# are clang output), and it reuses this job's llvm-ld-direct, so the
# benchmarked binary must come from the same compiler and flags.
# link-benchmark.yml's fallback build (when it cannot reuse this job's
# artifact) must therefore keep the same -DCMAKE_BUILD_TYPE=Release,
# -DLLVM_APPEND_VC_REV=OFF and clang/clang++ flags used here - a divergent
# fallback would silently benchmark a different binary than the one CI
# published. Compile caches also key on the compiler binary, so one compiler
# choice lets link-benchmark's fallback build hit this job's warm cache
# instead of the two workflows competing for cache budget. An explicit
# compiler also cannot drift silently when the runner image changes its
# default.
def configure_command(
    workspace: Path,
    build_dir: Path,
    launcher: str,
    c_compiler: str,
    cxx_compiler: str,
    trace_file: Path | None,
    extra_args: list[str] | None = None,
) -> list[str]:
    # Explicitly empty (not omitted) for "none", so a launcher recorded in a
    # restored CMakeCache.txt from a previous, cached configure is cleared
    # rather than silently left in place.
    launcher_value = "" if launcher == "none" else launcher
    command = [
        "cmake",
        "-S",
        str(workspace),
        "-B",
        str(build_dir),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_APPEND_VC_REV=OFF",
        f"-DCMAKE_C_COMPILER={c_compiler}",
        f"-DCMAKE_CXX_COMPILER={cxx_compiler}",
        f"-DCMAKE_C_COMPILER_LAUNCHER={launcher_value}",
        f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher_value}",
    ]
    if trace_file is not None:
        command += ["--trace-expand", "--trace-format=json-v1", f"--trace-redirect={trace_file}"]
    if extra_args:
        command += extra_args
    return command


def build_command(build_dir: Path, targets: list[str]) -> list[str]:
    return ["cmake", "--build", str(build_dir), "--target", *targets]


def dry_run_command(build_dir: Path, targets: list[str]) -> list[str]:
    return ["ninja", "-C", str(build_dir), "-n", "-d", "explain", *targets]


def count_ninja_edges(output: str) -> int:
    """Number of Ninja progress lines (`[N/M] ...`) in dry-run output.

    "ninja: no work to do." produces none, so this is 0 on a fully warm tree.
    """
    return sum(1 for line in output.splitlines() if _EDGE_LINE.match(line))


def ensure_codemodel_query(build_dir: Path) -> None:
    """Pre-create the CMake file-API query cmake's next configure replies to.

    Mirrors ci.yml's `mkdir -p build/.cmake/api/v1/query && touch
    .../codemodel-v2` step. tools/audit_build.py reads the codemodel-v2
    reply that a configure writes back only when the query file already
    existed beforehand, so this has to run before `configure_command`. An
    existing query file (e.g. carried over in a restored build tree) is left
    untouched rather than re-truncated.
    """
    query_dir = build_dir / ".cmake" / "api" / "v1" / "query"
    query_dir.mkdir(parents=True, exist_ok=True)
    codemodel = query_dir / "codemodel-v2"
    if not codemodel.exists():
        codemodel.touch()


# --------------------------------------------------------------- reporting


def parse_replay_report(stdout: str) -> dict:
    """Pick the trailing JSON report object out of `zccache replay --json` output.

    Scans from the end so a stray informational line before the report
    (there should not be one, but replay's own stdout is not this script's
    to control) cannot shadow it.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "applied" in candidate:
            report = dict(candidate)
            for key in ("total", "applied", "missing", "size_mismatch", "modified"):
                report.setdefault(key, 0)
            if "applied_ratio" not in report:
                total = report["total"]
                report["applied_ratio"] = (report["applied"] / total) if total else 0.0
            return report
    raise ValueError('no replay report (a JSON object with an "applied" field) found in output')


def format_warning(message: str, github_actions: bool) -> str:
    return f"::warning::{message}" if github_actions else f"warning: {message}"


# ------------------------------------------------------------- mtime replay


def is_safe_relative(path: str) -> bool:
    """Mirror zccache's own `is_safe_relative_path`: non-empty, relative, no
    backslashes, and no `""`/`"."`/`".."` path component."""
    if not path or path.startswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def stamp_fresh(workspace: Path, manifest: Path) -> int:
    """Stamp every manifest-listed regular file's mtime to right now.

    This is the other half of the correctness argument in the module
    docstring: `zccache replay` only ever moves a file's mtime *backward* to
    the recorded value, and only for a file it has verified is unchanged by
    content. Running this first means a file replay does not verify -
    because it is missing, changed, or unreadable - is left with a mtime
    from *this* run rather than possibly a stale one inherited from however
    the tree got here (a tar/rsync-preserved checkout, a backdated local
    edit, ...), so Ninja is guaranteed to reconsider it rather than silently
    treat a changed file as already built.
    """
    data = json.loads(manifest.read_text(encoding="utf-8"))
    stamped = 0
    failed = 0
    for entry in data["entries"]:
        path = entry["path"]
        if not is_safe_relative(path):
            continue
        target = workspace / path
        try:
            info = os.lstat(target)
        except OSError:
            continue  # missing: nothing to stamp, replay will report it missing
        if not stat.S_ISREG(info.st_mode):
            continue  # symlinks (and anything else non-regular) are skipped
        try:
            os.utime(target, None)
        except OSError:
            # A file we could not stamp may keep whatever mtime it already
            # had, which costs build time (an unnecessary rebuild, or - only
            # if that old mtime happens to already be fresh - an unverified
            # reuse Ninja's own dependency graph still has to agree to), not
            # correctness: nothing downstream trusts an mtime this function
            # did not itself just set or that `replay` did not itself just
            # content-verify.
            failed += 1
            continue
        stamped += 1
    if failed:
        print(
            f"ci_build: warning: failed to stamp {failed} manifest file(s) fresh; "
            "they may keep a stale mtime",
            flush=True,
        )
    return stamped


# ------------------------------------------------------------------ hashing


def hash_inputs(workspace: Path, paths: list[str]) -> str:
    """sha256 over every regular file reachable from `paths`, keyed by path.

    Each entry in `paths` is either a file (hashed directly) or a directory
    (every regular file under it, recursively, is hashed); a missing entry
    is skipped. `__pycache__` directories and `.pyc` files are skipped so
    that running this script does not change its own cache key.
    """
    files: list[Path] = []
    for raw in paths:
        target = workspace / raw
        if not target.exists():
            continue
        if target.is_dir():
            for candidate in target.rglob("*"):
                if not candidate.is_file():
                    continue
                if "__pycache__" in candidate.parts:
                    continue
                if candidate.suffix == ".pyc":
                    continue
                files.append(candidate)
        elif target.is_file():
            files.append(target)

    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(workspace).as_posix()):
        relative = path.relative_to(workspace).as_posix()
        content_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{relative}\0{content_sha}\n".encode("utf-8"))
    return digest.hexdigest()


def compiler_identity(c_compiler: str, cxx_compiler: str) -> str:
    """Concatenated `--version` output of every tool the build shape depends on."""
    outputs = []
    for command in (
        [c_compiler, "--version"],
        [cxx_compiler, "--version"],
        ["cmake", "--version"],
        ["ninja", "--version"],
    ):
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        outputs.append(result.stdout)
    return "\n---\n".join(outputs)


def compute_cache_keys(workspace: Path, launcher: str, identity: str) -> tuple[str, str]:
    """Return `(primary_key, restore_prefix)` for the build-tree cache.

    `restore_prefix` is stable across ordinary source edits (it only folds
    in platform, launcher, toolchain identity and the pinned-payload
    closure), so a cache-restore step keyed on `primary_key` with
    `restore_prefix` as its fallback still lands on the most recent tree
    built with the same toolchain and payload even when the exact source
    tree has since changed.
    """
    identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    payload_digest = hash_inputs(workspace, PAYLOAD_KEY_PATHS)[:16]
    restore_prefix = f"{KEY_PREFIX}-{sys.platform}-{launcher}-{identity_digest}-{payload_digest}-"
    primary = restore_prefix + hash_inputs(workspace, KEY_INPUT_PATHS)[:24]
    return primary, restore_prefix


def resolve_launcher(requested: str) -> str:
    if requested == "auto":
        return "zccache" if shutil.which("zccache") else "none"
    if requested in ("zccache", "sccache"):
        if shutil.which(requested):
            return requested
        hint = f"; {INSTALL_HINT}" if requested == "zccache" else ""
        raise SystemExit(
            f"error: --launcher {requested} requested but {requested} is not on PATH{hint}"
        )
    return "none"


# ------------------------------------------------------------------ commands


def cmd_key(args: argparse.Namespace) -> int:
    workspace, _build_dir, _manifest = resolve_paths(args)
    identity = compiler_identity(args.c_compiler, args.cxx_compiler)
    primary, restore_prefix = compute_cache_keys(workspace, args.launcher, identity)
    print(f"primary={primary}", flush=True)
    print(f"restore-prefix={restore_prefix}", flush=True)
    if args.github_output:
        output_path = os.environ.get("GITHUB_OUTPUT")
        if output_path:
            with open(output_path, "a", encoding="utf-8") as handle:
                handle.write(f"primary={primary}\n")
                handle.write(f"restore-prefix={restore_prefix}\n")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    workspace, _build_dir, manifest = resolve_paths(args)
    github_actions = os.environ.get("GITHUB_ACTIONS") == "true"

    if args.import_upstream:
        has_mimalloc = any(p.is_dir() for p in workspace.glob("upstream/mimalloc-pprof-*"))
        has_libxml2 = any(p.is_dir() for p in workspace.glob("upstream/libxml2-*"))
        if not has_mimalloc or not has_libxml2:
            # import.py always re-downloads and rewrites its outputs, so it
            # is only run when at least one is missing; its outputs are
            # checksum-verified, so a re-import produces identical content
            # and `replay` still restores their mtimes on a warm run,
            # keeping the vendored libxml2/mimalloc TUs from rebuilding.
            print("ci_build: importing upstream mimalloc/libxml2 (missing on disk)", flush=True)
            subprocess.run(
                [sys.executable, str(workspace / "tools" / "import.py"), "--mimalloc-only"],
                cwd=workspace,
                check=True,
            )

    if not manifest.exists():
        print(
            f"ci_build: no mtime manifest at {manifest} (build-tree cache miss); "
            "skipping replay, expect a full build",
            flush=True,
        )
        return 0

    zccache = require_zccache("replay")

    try:
        stamped = stamp_fresh(workspace, manifest)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(
            format_warning(
                f"unreadable mtime manifest {manifest} ({error}); skipping replay "
                "(costs build time, not correctness)",
                github_actions,
            ),
            flush=True,
        )
        return 0
    print(f"ci_build: stamped {stamped} manifest files fresh", flush=True)

    result = subprocess.run(
        replay_command(zccache, workspace, manifest), capture_output=True, text=True
    )
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode != 0:
        print(
            format_warning(
                f"zccache replay exited {result.returncode}; every file keeps its fresh "
                "mtime, so this costs build time, not correctness",
                github_actions,
            ),
            flush=True,
        )
        return 0

    try:
        report = parse_replay_report(result.stdout)
    except ValueError as error:
        print(
            format_warning(f"could not parse zccache replay output ({error})", github_actions),
            flush=True,
        )
        return 0

    ratio = report["applied_ratio"]
    print(
        f"ci_build: replay applied={report['applied']} missing={report['missing']} "
        f"size_mismatch={report['size_mismatch']} modified={report['modified']} "
        f"total={report['total']} ratio={ratio:.4f}",
        flush=True,
    )
    if ratio < args.warn_below:
        print(
            format_warning(
                f"zccache replay applied ratio {ratio:.4f} is below --warn-below "
                f"{args.warn_below:.4f}; this only costs build time because correctness "
                "comes from the content-verified replay, not from the ratio",
                github_actions,
            ),
            flush=True,
        )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(
                f"- zccache replay: applied={report['applied']} missing={report['missing']} "
                f"size_mismatch={report['size_mismatch']} modified={report['modified']} "
                f"total={report['total']} ratio={ratio:.4f}\n"
            )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    workspace, build_dir, _manifest = resolve_paths(args)
    launcher = resolve_launcher(args.launcher)
    targets = args.targets or DEFAULT_TARGETS

    trace_file: Path | None
    if args.no_trace:
        trace_file = None
    else:
        trace_file = Path(args.trace_file)
        if not trace_file.is_absolute():
            trace_file = workspace / trace_file

    ensure_codemodel_query(build_dir)
    print(f"ci_build: launcher={launcher}", flush=True)

    start = time.monotonic()
    subprocess.run(
        configure_command(
            workspace,
            build_dir,
            launcher,
            args.c_compiler,
            args.cxx_compiler,
            trace_file,
            args.cmake_args,
        ),
        cwd=workspace,
        check=True,
    )
    print(f"ci_build: configure finished in {time.monotonic() - start:.1f}s", flush=True)

    dry_run = subprocess.run(
        dry_run_command(build_dir, targets),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    edges = count_ninja_edges(dry_run.stdout)
    print(f"ci_build: ninja edges to run: {edges}", flush=True)
    explain_lines = [
        line for line in dry_run.stderr.splitlines() if line.startswith("ninja explain:")
    ]
    for line in explain_lines[:20]:
        print(f"ci_build: {line}", flush=True)

    if args.expect_no_work and edges > 0:
        print(
            f"ci_build: error: --expect-no-work but ninja has {edges} edge(s) to run",
            flush=True,
        )
        return 1

    build_start = time.monotonic()
    subprocess.run(build_command(build_dir, targets), cwd=workspace, check=True)
    build_elapsed = time.monotonic() - build_start
    print(f"ci_build: build finished in {build_elapsed:.1f}s ({edges} edges)", flush=True)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(f"- ci_build build: {edges} ninja edge(s), {build_elapsed:.1f}s\n")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    workspace, build_dir, manifest = resolve_paths(args)
    zccache = require_zccache("snapshot")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        snapshot_command(zccache, workspace, build_dir, manifest), cwd=workspace, check=True
    )
    print(f"ci_build: wrote mtime manifest {manifest}", flush=True)
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    result = cmd_replay(args)
    if result != 0:
        return result
    result = cmd_build(args)
    if result != 0:
        return result
    return cmd_snapshot(args)


_HANDLERS = {
    "key": cmd_key,
    "replay": cmd_replay,
    "build": cmd_build,
    "snapshot": cmd_snapshot,
    "all": cmd_all,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    handler = _HANDLERS[args.command]
    try:
        return handler(args)
    except subprocess.CalledProcessError as error:
        command = " ".join(shlex.quote(str(part)) for part in error.cmd)
        print(f"ci_build: command failed: {command}", flush=True)
        return error.returncode if error.returncode else 1


if __name__ == "__main__":
    sys.exit(main())
