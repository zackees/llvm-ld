"""Build and package llvm-ld-coff for one host platform.

llvm-ld is LLD's **COFF** linker (the lld-link/MSVC and MinGW drivers) as a shared library with a
C ABI. Every build of it links **Windows PE/COFF** executables and DLLs. The triple a release
archive is named after is the *host* the library runs on, never the target it links: the Linux
and macOS builds are Windows cross-linkers. Release assets are therefore named
`llvm-ld-coff-<version>-<host-triple>` so that is never ambiguous.

`.github/workflows/release.yml` runs this once per host in its matrix. It configures with the same
flags as ci.yml, builds the library and runner, runs the ABI smoke test where the host can execute
it, and packages the result together with the header and the license and provenance notices.

With `--pgo` (#46) the host is built with clang PGO + ThinLTO instead, in four steps: an
instrumented build, training links over `--train-corpus` (tests/perf/gen_corpus.py output, linked
through the runner so the shipped library is what gets profiled), `llvm-profdata merge`, and an
optimized build with `-fprofile-use -flto=thin`. The flags go in through CFLAGS/CXXFLAGS/LDFLAGS,
which CMake appends to the platform defaults (a `-DCMAKE_CXX_FLAGS` would drop MSVC's /EHsc).

`--reference` (the previous release's archive for the same host) turns on the gate: the new runner
and the reference runner link the same corpora, and any byte difference in the EXE or PDB fails the
build. PGO, ThinLTO and the compiler switch (gcc->clang, MSVC->clang-cl) must change nothing in the
output. The gate also times a few paired links of reference vs new, for the release notes.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTICES = ("LICENSE", "LICENSE-LLVM.txt", "LICENSE-MIMALLOC.txt", "LICENSE-LIBXML2.txt", "PROVENANCE.md")
PRODUCT = "llvm-ld-coff"


@dataclass(frozen=True)
class Host:
    """How to build for, and name the archive of, one host platform."""

    triple: str
    os: str  # windows | linux | macos
    osx_arch: str | None = None  # CMAKE_OSX_ARCHITECTURES, when it differs from the runner's
    can_execute: bool = True  # whether this runner can run what it built

    @property
    def library(self) -> str:
        return {"windows": "llvm_ld.dll", "linux": "libllvm_ld.so", "macos": "libllvm_ld.dylib"}[self.os]

    @property
    def runner(self) -> str:
        return "llvm-ld-runner.exe" if self.os == "windows" else "llvm-ld-runner"

    @property
    def archive_suffix(self) -> str:
        return ".zip" if self.os == "windows" else ".tar.gz"


HOSTS = {
    host.triple: host
    for host in (
        Host("x86_64-pc-windows-msvc", "windows"),
        Host("aarch64-pc-windows-msvc", "windows"),
        Host("x86_64-unknown-linux-gnu", "linux"),
        Host("aarch64-unknown-linux-gnu", "linux"),
        Host("x86_64-unknown-linux-musl", "linux"),
        Host("aarch64-unknown-linux-musl", "linux"),
        Host("aarch64-apple-darwin", "macos", osx_arch="arm64"),
        # Built on an arm64 macOS runner; Rosetta 2 runs the x86_64 smoke test.
        Host("x86_64-apple-darwin", "macos", osx_arch="x86_64"),
    )
}


def archive_stem(version: str, host: Host) -> str:
    return f"{PRODUCT}-{version}-{host.triple}"


# Keep the shared library loadable on hosts whose libstdc++ is older than the builder's.
LINUX_STATIC_RUNTIME = "-static-libstdc++ -static-libgcc"


def launcher_args(launcher: str) -> list[str]:
    """Compiler launcher (zccache in CI). Explicitly empty for "none", so a launcher recorded in a
    reused CMakeCache.txt is cleared rather than kept."""
    value = "" if launcher == "none" else launcher
    args = [f"-DCMAKE_C_COMPILER_LAUNCHER={value}", f"-DCMAKE_CXX_COMPILER_LAUNCHER={value}"]
    if launcher != "none":
        # As LLVM does for sccache: PCH compiles are not cacheable (the root CMakeLists.txt applies
        # this for zccache too; the standalone tablegen configure needs it passed).
        args.append("-DCMAKE_DISABLE_PRECOMPILE_HEADERS=ON")
    return args


def configure_command(host: Host, build_dir: Path, toolchain: list[str] | None = None,
                      launcher: str = "none") -> list[str]:
    """The CMake configure line. With `toolchain` (PGO), the linker flags come from LDFLAGS instead."""
    command = [
        "cmake", "-S", str(REPO_ROOT), "-B", str(build_dir), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_APPEND_VC_REV=OFF",
        "-DLLVM_LD_ENABLE_PPROF=OFF",
        "-DLLVM_LD_ENABLE_DHAT=OFF",
        *launcher_args(launcher),
    ]
    if host.os == "linux" and toolchain is None:
        command.append(f"-DCMAKE_SHARED_LINKER_FLAGS={LINUX_STATIC_RUNTIME}")
        command.append(f"-DCMAKE_EXE_LINKER_FLAGS={LINUX_STATIC_RUNTIME}")
    if host.osx_arch:
        command.append(f"-DCMAKE_OSX_ARCHITECTURES={host.osx_arch}")
    return command + (toolchain or [])


# ------------------------------------------------------------------ PGO (#46)

PDB_FLAGS = ["/debug:full", "/pdbaltpath:%_PDB%", "/pdb:link.pdb"]
# (mode, profile, threads, variants) linked by the instrumented runner. The corpus job in
# release.yml generates exactly these with tests/perf/gen_corpus.py.
TRAINING = [
    ("debug", "small", (1, 4), ("nopdb", "pdb")),
    ("debug", "medium", (1, 4), ("nopdb", "pdb")),
    ("release", "small", (1, 4), ("nopdb", "pdb")),
    ("release", "medium", (1, 4), ("nopdb", "pdb")),
    ("thinlto", "small", (4,), ("nopdb", "pdb")),
]
# (mode, profile, variant) whose output must be byte-identical between reference and new runner.
GATE = [
    ("release", "medium", "pdb"),
    ("release", "medium", "nopdb"),
    ("debug", "small", "pdb"),
    ("thinlto", "small", "pdb"),
]
TIMING = ("release", "medium", "pdb")
TIMING_PAIRS = 9


def pgo_toolchain(host: Host) -> list[str]:
    """CMake cache entries that select the PGO toolchain for this host."""
    if host.os == "windows":
        return ["-DCMAKE_C_COMPILER=clang-cl", "-DCMAKE_CXX_COMPILER=clang-cl",
                "-DCMAKE_LINKER=lld-link", "-DCMAKE_AR=llvm-lib"]
    if host.os == "linux":
        return ["-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++",
                "-DCMAKE_AR=" + (shutil.which("llvm-ar") or "llvm-ar"),
                "-DCMAKE_RANLIB=" + (shutil.which("llvm-ranlib") or "llvm-ranlib")]
    return ["-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++"]  # Apple clang


def windows_profile_runtime(host: Host, into: Path) -> str:
    """clang's profile runtime library, which lld-link needs explicitly (CMake links with lld-link).

    It is copied into the build directory first: the runner's LLVM lives under
    "C:\\Program Files", and a path with a space does not survive LDFLAGS.
    """
    resource = subprocess.run(["clang-cl", "-print-resource-dir"], capture_output=True, text=True,
                              check=True).stdout.strip()
    arch = "aarch64" if host.triple.startswith("aarch64") else "x86_64"
    candidates = sorted(glob.glob(os.path.join(resource, "lib", "**", "clang_rt.profile*.lib"), recursive=True))
    matching = [c for c in candidates if arch in c or f"{arch}-pc-windows-msvc" in c]
    if not matching:
        raise SystemExit(f"release_build: no clang_rt.profile library for {arch} under {resource}: {candidates}")
    into.mkdir(parents=True, exist_ok=True)
    copy = into / "clang_rt.profile.lib"
    shutil.copy2(matching[0], copy)
    return str(copy.resolve())


def pgo_env(host: Host, stage: str, profile: Path) -> dict[str, str]:
    """CFLAGS/CXXFLAGS/LDFLAGS for the instrumented (`generate`) or optimized (`use`) build."""
    if stage == "generate":
        cflags = f"-fprofile-generate={profile}"
        if host.os == "windows":
            ldflags = windows_profile_runtime(host, profile.parent)
        elif host.os == "linux":
            # lld for the instrumented link too: with the default GNU ld the instrumented
            # library's final link stalled the aarch64 runner until the 6-hour timeout.
            ldflags = f"{cflags} -fuse-ld=lld"
        else:
            ldflags = cflags
    else:
        cflags = (f"-fprofile-use={profile} -flto=thin -Wno-profile-instr-unprofiled "
                  "-Wno-profile-instr-out-of-date -Wno-backend-plugin")
        if host.os == "windows":
            ldflags = ""  # lld-link runs ThinLTO on bitcode inputs by itself
        elif host.os == "linux":
            ldflags = "-flto=thin -fuse-ld=lld"
        else:
            ldflags = "-flto=thin"
    if host.os == "linux":
        ldflags = f"{LINUX_STATIC_RUNTIME} {ldflags}".strip()
    return {"CFLAGS": cflags, "CXXFLAGS": cflags, "LDFLAGS": ldflags}


def profdata_tool(host: Host) -> list[str]:
    return ["xcrun", "llvm-profdata"] if host.os == "macos" else ["llvm-profdata"]


def library_env(host: Host, directory: Path) -> dict[str, str]:
    """Environment that makes `directory`'s runner load `directory`'s library."""
    env = dict(os.environ)
    if host.os == "linux":
        env["LD_LIBRARY_PATH"] = str(directory)
    elif host.os == "macos":
        env["DYLD_LIBRARY_PATH"] = str(directory)
    return env  # Windows: the DLL sits next to the runner


def link_args(threads: int, variant: str) -> list[str]:
    return ["winlink", "lld-link", "@link.rsp", "/out:link.exe", f"/threads:{threads}",
            *(PDB_FLAGS if variant == "pdb" else [])]


def child_cpu_seconds() -> float | None:
    """CPU time of finished children so far (None where the OS does not report it)."""
    try:
        import resource
    except ImportError:  # Windows
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def link(host: Host, directory: Path, corpus: Path, threads: int, variant: str, env: dict | None = None) -> float:
    return link_timed(host, directory, corpus, threads, variant, env)[0]


def link_timed(host: Host, directory: Path, corpus: Path, threads: int, variant: str,
               env: dict | None = None) -> tuple[float, float | None]:
    """(wall seconds, CPU seconds or None) of one link through `directory`'s runner."""
    runner = directory / host.runner
    cpu_before = child_cpu_seconds()
    start = time.perf_counter()
    subprocess.run([str(runner), *link_args(threads, variant)], cwd=corpus, check=True,
                   env=env or library_env(host, directory), stdout=subprocess.DEVNULL)
    wall = time.perf_counter() - start
    cpu_after = child_cpu_seconds()
    return wall, (None if cpu_before is None else cpu_after - cpu_before)


def build(host: Host, build_dir: Path, toolchain: list[str] | None, env: dict[str, str] | None,
          launcher: str = "none") -> None:
    full_env = dict(os.environ, **env) if env else None
    run(configure_command(host, build_dir, toolchain, launcher), env=full_env)
    run(["cmake", "--build", str(build_dir), "--target", *BUILD_TARGETS], env=full_env)


TABLEGEN_TARGETS = ["llvm-tblgen", "llvm-min-tblgen"]


def native_tablegen(host: Host, tblgen_dir: Path, launcher: str) -> Path:
    """An uninstrumented, native-architecture tablegen for the PGO builds (#58).

    Built instrumented, every tablegen run of the instrumented build merged its profile into one
    shared file; on the aarch64 Linux runner that stalled at `Building Options.inc` for 5.5 hours
    (run 35443823220). Tablegen's output does not depend on how tablegen was compiled, so this
    changes no byte of the library; it also skips building tablegen twice with PGO flags, and on
    x86_64 macOS (built on arm64) it runs natively instead of under Rosetta. Same standalone
    configure as ci.yml's cross stage 1: the root cache settings are not inherited, so every
    LLVM_INCLUDE_* guard the pruned payload needs is repeated.
    """
    compilers = [arg for arg in pgo_toolchain(host) if arg.startswith(("-DCMAKE_C_COMPILER=", "-DCMAKE_CXX_COMPILER="))]
    run(["cmake", "-S", str(REPO_ROOT / "llvm-project" / "llvm"), "-B", str(tblgen_dir), "-G", "Ninja",
         "-DCMAKE_BUILD_TYPE=Release", "-DLLVM_APPEND_VC_REV=OFF", "-DLLVM_TARGETS_TO_BUILD=X86",
         "-DLLVM_ENABLE_PROJECTS=", "-DLLVM_INCLUDE_TESTS=OFF", "-DLLVM_INCLUDE_EXAMPLES=OFF",
         "-DLLVM_INCLUDE_BENCHMARKS=OFF", "-DLLVM_BUILD_TOOLS=OFF", "-DLLVM_ENABLE_ZLIB=OFF",
         "-DLLVM_ENABLE_ZSTD=OFF", "-DLLVM_ENABLE_LIBXML2=OFF", "-DLLVM_ENABLE_TERMINFO=OFF",
         *compilers, *launcher_args(launcher)])
    run(["cmake", "--build", str(tblgen_dir), "--target", *TABLEGEN_TARGETS])
    return (tblgen_dir / "bin").resolve()


def drop_stale_profile_objects(build_dir: Path, profdata: Path) -> None:
    """Discard an optimized build tree that was compiled from a different profile.

    `-fprofile-use=<file>` is invisible to CMake and ninja, so a rebuilt profile does not
    invalidate a single object. Reusing the tree then mixes objects from two profiles and ThinLTO
    fails with `linking module flags 'ProfileSummary': IDs have conflicting values` (PR #48, the
    aarch64-musl leg, after a retry retrained). Training is not deterministic, so any rerun that
    reaches `merge` produces a new profile: the tree has to go with it.
    """
    stamp = build_dir / ".pgo-profile.sha256"
    digest = hashlib.sha256(profdata.read_bytes()).hexdigest()
    recorded = stamp.read_text().strip() if stamp.is_file() else None
    if build_dir.is_dir() and recorded != digest:
        print(f"release_build: profile changed ({recorded} -> {digest[:16]}); discarding {build_dir}", flush=True)
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    stamp.write_text(digest + "\n")


def pgo_build(host: Host, build_dir: Path, corpus_root: Path, launcher: str = "none") -> None:
    instr_dir = build_dir.parent / (build_dir.name + "-instr")
    tools = native_tablegen(host, build_dir.parent / (build_dir.name + "-tblgen"), launcher)
    toolchain = [*pgo_toolchain(host), f"-DLLVM_NATIVE_TOOL_DIR={tools}"]
    profiles = (instr_dir / "profiles").resolve()
    build(host, instr_dir, toolchain, pgo_env(host, "generate", profiles), launcher)
    # Nothing but the linker's training runs should have written profiles; start clean anyway.
    shutil.rmtree(profiles, ignore_errors=True)
    profiles.mkdir(parents=True)
    env = library_env(host, instr_dir.resolve())
    env["LLVM_PROFILE_FILE"] = str(profiles / "llvm-ld-%p-%m.profraw")
    count = 0
    for mode, profile, threads, variants in TRAINING:
        for thread_count in threads:
            for variant in variants:
                link(host, instr_dir.resolve(), corpus_root / mode / profile, thread_count, variant, env)
                count += 1
    raws = sorted(profiles.glob("*.profraw"))
    if not raws:
        raise SystemExit("release_build: training produced no .profraw files")
    print(f"training: {count} links, {len(raws)} raw profile(s)", flush=True)
    profdata = (instr_dir / "llvm-ld.profdata").resolve()
    run([*profdata_tool(host), "merge", "-o", str(profdata), *map(str, raws)])
    drop_stale_profile_objects(build_dir, profdata)
    build(host, build_dir, toolchain, pgo_env(host, "use", profdata), launcher)


def extract_reference(archive: Path, into: Path) -> Path:
    """Unpack the previous release's archive; returns the directory holding its runner."""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(into)
    else:
        with tarfile.open(archive) as bundle:
            bundle.extractall(into, filter="data")
    tops = [path for path in into.iterdir() if path.is_dir()]
    if len(tops) != 1:
        raise SystemExit(f"release_build: expected one top-level directory in {archive}")
    for path in tops[0].iterdir():  # tarfile's data filter drops the executable bit on some hosts
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o111)
    return tops[0]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gate(host: Host, new_dir: Path, reference_dir: Path, corpus_root: Path) -> str:
    """Byte-identity of new vs reference runner on GATE, then paired timing on TIMING."""
    for mode, profile, variant in GATE:
        corpus = corpus_root / mode / profile
        outputs = {}
        for name, directory in (("reference", reference_dir), ("new", new_dir)):
            for leftover in ("link.exe", "link.pdb"):
                (corpus / leftover).unlink(missing_ok=True)
            link(host, directory, corpus, 4, variant)
            outputs[name] = [digest(corpus / "link.exe")] + ([digest(corpus / "link.pdb")] if variant == "pdb" else [])
        if outputs["reference"] != outputs["new"]:
            raise SystemExit(f"release_build: GATE FAILED: {mode}/{profile}/{variant} output differs from the reference")
        print(f"gate ok: {mode}/{profile}/{variant} byte-identical to the reference", flush=True)
    mode, profile, variant = TIMING
    corpus = corpus_root / mode / profile
    link(host, reference_dir, corpus, 4, variant)  # warm the page cache for both sides
    link(host, new_dir, corpus, 4, variant)
    wall_ratios, cpu_ratios = [], []
    for pair in range(TIMING_PAIRS):
        order = [reference_dir, new_dir] if pair % 2 == 0 else [new_dir, reference_dir]
        times = {d: link_timed(host, d, corpus, 4, variant) for d in order}
        wall_ratios.append(times[new_dir][0] / times[reference_dir][0])
        if times[new_dir][1] and times[reference_dir][1]:
            cpu_ratios.append(times[new_dir][1] / times[reference_dir][1])
    cpu_text = (f", {100 * (statistics.median(cpu_ratios) - 1):+.1f}% CPU" if cpu_ratios else "")
    summary = (f"{host.triple}: {mode}/{profile} + PDB at 4 threads, new vs previous release: "
               f"{100 * (1 - statistics.median(wall_ratios)):+.1f}% faster wall{cpu_text} "
               f"(paired median of {TIMING_PAIRS}; hosted runners are noisy)")
    print(summary, flush=True)
    return summary


BUILD_TARGETS = ["llvm_ld", "llvm-ld-runner", "abi_smoke"]


def package(host: Host, version: str, build_dir: Path, out_dir: Path) -> Path:
    """Lay out one archive: a single top-level directory holding the library, header, runner, notices."""
    stem = archive_stem(version, host)
    staging = out_dir / stem
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    files = [build_dir / host.library, build_dir / host.runner, REPO_ROOT / "include" / "llvm_ld.h"]
    if host.os == "windows":
        files.append(build_dir / "llvm_ld.lib")  # import library, for build-time consumers
    files += [REPO_ROOT / name for name in NOTICES]
    for path in files:
        if not path.is_file():
            raise SystemExit(f"release_build: expected {path} is missing")
        shutil.copy2(path, staging / path.name)

    archive = out_dir / (stem + host.archive_suffix)
    if host.archive_suffix == ".zip":
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staging.rglob("*")):
                bundle.write(path, path.relative_to(out_dir))
    else:
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(staging, arcname=stem)
    shutil.rmtree(staging)
    return archive


def run(command: list[str], **kwargs) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triple", required=True, choices=sorted(HOSTS))
    parser.add_argument("--version", required=True, help="release version, e.g. v0.1.0")
    parser.add_argument("--build-dir", type=Path, default=REPO_ROOT / "build-release")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "dist")
    parser.add_argument("--pgo", action="store_true", help="build with clang PGO + ThinLTO (#46)")
    parser.add_argument("--train-corpus", type=Path, help="gen_corpus.py output to train on and gate with")
    parser.add_argument("--reference", type=Path, help="previous release archive for this host: byte-identity gate")
    parser.add_argument("--launcher", default="none", help="compiler launcher (zccache in CI), or none")
    args = parser.parse_args(argv)
    host = HOSTS[args.triple]

    if args.pgo:
        if not args.train_corpus:
            parser.error("--pgo needs --train-corpus")
        pgo_build(host, args.build_dir, args.train_corpus.resolve(), args.launcher)
    else:
        run(configure_command(host, args.build_dir, launcher=args.launcher))
        run(["cmake", "--build", str(args.build_dir), "--target", *BUILD_TARGETS])
    if host.can_execute:
        # The smoke test loads the freshly built library and makes a real ABI call.
        smoke = args.build_dir / ("abi_smoke.exe" if host.os == "windows" else "abi_smoke")
        env = dict(os.environ)
        if host.os == "linux":
            env["LD_LIBRARY_PATH"] = str(args.build_dir)
        elif host.os == "macos":
            env["DYLD_LIBRARY_PATH"] = str(args.build_dir)
        run([str(smoke)], env=env, cwd=args.build_dir)
    if args.reference:
        if not args.train_corpus:
            parser.error("--reference needs --train-corpus (the gate corpora)")
        if not host.can_execute:
            raise SystemExit("release_build: the gate needs a host that can run what it built")
        reference = extract_reference(args.reference.resolve(), args.build_dir.parent / "reference-release")
        summary = gate(host, args.build_dir.resolve(), reference, args.train_corpus.resolve())
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
                out.write(f"- {summary}\n")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    archive = package(host, args.version, args.build_dir, args.out_dir)
    print(f"packaged {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
