#!/usr/bin/env python3
"""Build llvm-ld-direct with PGO + ThinLTO, without changing a single output byte (#43).

The linker binary itself is compiled better; what it does is unchanged. Every
measurement of the result goes through tests/perf/bench.py's byte-identity gate
against the plain build of the same source.

Stages (each a subcommand; `all` runs them in order):

  instrument  configure + build <instr-dir> with -fprofile-generate on every
              compile AND link. The flags are set at the top level on purpose:
              LLVM_BUILD_INSTRUMENTED only reaches the llvm/ subdirectory scope
              and would miss the final llvm-ld-direct link (no profile runtime).
  train       run the instrumented llvm-ld-direct over the training workload
              (TRAINING below) from tests/perf/gen_corpus.py; not timed.
  merge       llvm-profdata merge the raw profiles into <instr-dir>/llvm-ld.profdata
              (llvm-profdata must match the compiler's LLVM major version).
  plain       configure + build <plain-dir> with exactly the same flags minus
              PGO/LTO: the control that PGO+ThinLTO is measured against.
  optimize    configure + build <out-dir> with -fprofile-use=<profdata> and
              -flto=thin on every compile, and -flto=thin -fuse-ld=lld on links
              (llvm-ar/llvm-ranlib, because the static libraries hold bitcode).
              With --bolt it also keeps relocations (--emit-relocs) for BOLT.
  bolt        post-link layout optimization of <out-dir>/llvm-ld-direct with
              llvm-bolt (Linux): instrument, train on the same workload, merge,
              then reorder blocks/functions and split hot/cold code (no ICF).
              Measured +6.1% wall / -5.5% CPU on top of PGO for a
              ThinLTO link, byte-identical output (codegen round 2). The pre-BOLT
              binary is kept as llvm-ld-direct.pre-bolt.

The configure flags are tools/ci_build.py's (Release, clang, LLVM_APPEND_VC_REV=OFF)
plus the PGO/LTO flags, so the two builds differ only in how they are compiled.

Training workload: the medium corpora (and ThinLTO small) exercise every hot path
the #43 hotspot profile found. Measure on held-out corpora (large, ThinLTO medium)
so the result is not an artifact of training on the benchmark.

Usage:
  python tools/pgo_build.py all [--instr-dir build-pgo-instr] [--out-dir build-pgo] [--nice 10]
  python tools/pgo_build.py all --bolt [--bolt-dir <dir with llvm-bolt>]   # + BOLT (Linux)
  python tools/pgo_build.py plain [--plain-dir build-plain]
  python tests/perf/bench.py --candidate build-pgo/llvm-ld-direct \
      --baseline build-plain/llvm-ld-direct --corpus <corpus> --variant pdb --threads 4
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_build  # noqa: E402  (same configure flags as CI)

TARGET = "llvm-ld-direct"
PROFDATA_NAME = "llvm-ld.profdata"
PROFILE_SUBDIR = "profiles"

# (mode, profile, threads, variants): what the instrumented linker links during training.
TRAINING = [
    ("debug", "small", (1, 4), ("nopdb", "pdb")),
    ("debug", "medium", (1, 4), ("nopdb", "pdb")),
    ("release", "small", (1, 4), ("nopdb", "pdb")),
    ("release", "medium", (1, 4), ("nopdb", "pdb")),
    ("thinlto", "small", (4,), ("nopdb", "pdb")),
]
PDB_FLAGS = ["/debug:full", "/pdbaltpath:%_PDB%", "/pdb:train.pdb"]


def instrument_flags(profile_dir: Path) -> list[str]:
    flag = f"-fprofile-generate={profile_dir}"
    return [
        f"-DCMAKE_C_FLAGS={flag}",
        f"-DCMAKE_CXX_FLAGS={flag}",
        f"-DCMAKE_EXE_LINKER_FLAGS={flag}",
        f"-DCMAKE_SHARED_LINKER_FLAGS={flag}",
        f"-DCMAKE_MODULE_LINKER_FLAGS={flag}",
    ]


def runtime_rpath(cxx: str) -> str | None:
    """The RUNPATH the normal toolchain gives a C++ program, or None if it adds none.

    Linking with -fuse-ld=lld bypasses toolchain wrappers: NixOS's cc-wrapper adds
    the libstdc++ RUNPATH only through its own wrapped ld, so the freshly built
    llvm-tblgen and llvm-ld-direct could not find their C++ runtime. Probing a tiny
    program linked the normal way and repeating its RUNPATH fixes that; on a
    conventional distro the probe has no RUNPATH and nothing is added.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        source, binary = Path(tmp) / "probe.cpp", Path(tmp) / "probe"
        source.write_text("#include <string>\nint main() { return std::string(\"x\").size() - 1; }\n")
        try:
            subprocess.run([cxx, str(source), "-o", str(binary)], check=True, capture_output=True)
            dynamic = subprocess.run(["readelf", "-d", str(binary)], check=True, capture_output=True,
                                     text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return None
    match = re.search(r"\((?:RUNPATH|RPATH)\)\s+Library r(?:un)?path: \[([^\]]+)\]", dynamic)
    return match.group(1) if match else None


def optimize_flags(profdata: Path, rpath: str | None = None, bolt: bool = False) -> list[str]:
    compile_flags = " ".join([
        f"-fprofile-use={profdata}",
        # Functions the training never reached are fine; stale/mismatched ones
        # would mean the profile is from a different source and are reported.
        "-Wno-profile-instr-unprofiled",
        "-flto=thin",
    ])
    link_flags = "-flto=thin -fuse-ld=lld" + (f" -Wl,-rpath,{rpath}" if rpath else "")
    if bolt:
        link_flags += " -Wl,--emit-relocs"  # BOLT needs the relocations to move code
    return [
        f"-DCMAKE_C_FLAGS={compile_flags}",
        f"-DCMAKE_CXX_FLAGS={compile_flags}",
        f"-DCMAKE_EXE_LINKER_FLAGS={link_flags}",
        f"-DCMAKE_SHARED_LINKER_FLAGS={link_flags}",
        f"-DCMAKE_MODULE_LINKER_FLAGS={link_flags}",
        "-DCMAKE_AR=" + (shutil.which("llvm-ar") or "llvm-ar"),
        "-DCMAKE_RANLIB=" + (shutil.which("llvm-ranlib") or "llvm-ranlib"),
    ]


def llvm_major(version_output: str) -> int | None:
    match = re.search(r"(?:LLVM|clang) version (\d+)", version_output)
    return int(match.group(1)) if match else None


def check_profdata_matches_compiler(cxx: str) -> str:
    profdata = shutil.which("llvm-profdata")
    if not profdata:
        raise SystemExit("llvm-profdata not found on PATH")
    tool = llvm_major(subprocess.run([profdata, "--version"], capture_output=True, text=True).stdout)
    compiler = llvm_major(subprocess.run([cxx, "--version"], capture_output=True, text=True).stdout)
    if tool is None or compiler is None or tool != compiler:
        raise SystemExit(f"llvm-profdata (LLVM {tool}) must match {cxx} (LLVM {compiler})")
    return profdata


def training_links(corpus_root: Path) -> list[tuple[Path, list[str]]]:
    """(corpus dir, extra link args) for every training link."""
    links = []
    for mode, profile, threads, variants in TRAINING:
        for thread_count in threads:
            for variant in variants:
                extra = [f"/threads:{thread_count}"] + (PDB_FLAGS if variant == "pdb" else [])
                links.append((corpus_root / mode / profile, extra))
    return links


def run(command: list[str], nice: int, **kwargs) -> None:
    if nice:
        command = ["nice", "-n", str(nice), *command]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def build(workspace: Path, build_dir: Path, extra: list[str], args: argparse.Namespace) -> None:
    configure = ci_build.configure_command(
        workspace, build_dir, args.launcher, args.cc, args.cxx, None, extra_args=extra
    )
    run(configure, args.nice, cwd=workspace)
    run(ci_build.build_command(build_dir, [TARGET]), args.nice, cwd=workspace)


def cmd_instrument(args: argparse.Namespace) -> int:
    profile_dir = args.instr_dir.resolve() / PROFILE_SUBDIR
    build(args.workspace, args.instr_dir, instrument_flags(profile_dir), args)
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    instr = args.instr_dir.resolve()
    profile_dir = instr / PROFILE_SUBDIR
    linker = instr / TARGET
    if not linker.exists():
        raise SystemExit(f"{linker} missing: run `instrument` first")
    # Profiles written while building (the instrumented llvm-tblgen runs during
    # the build) are not the linker's workload; drop them.
    shutil.rmtree(profile_dir, ignore_errors=True)
    profile_dir.mkdir(parents=True)
    corpus_root = instr / "train-corpus"
    for mode, profile, _, _ in TRAINING:
        run([sys.executable, "tests/perf/gen_corpus.py", "--out", str(corpus_root), "--mode", mode,
             "--profile", profile, "--jobs", str(os.cpu_count() or 4)], args.nice, cwd=args.workspace,
            stdout=subprocess.DEVNULL)
    env = dict(os.environ, LLVM_PROFILE_FILE=str(profile_dir / "llvm-ld-%p-%m.profraw"))
    for corpus, extra in training_links(corpus_root):
        run([str(linker), "winlink", "lld-link", "@link.rsp", "/out:train.exe", *extra],
            args.nice, cwd=corpus, env=env, stdout=subprocess.DEVNULL)
    count = len(list(profile_dir.glob("*.profraw")))
    if count == 0:
        raise SystemExit("training produced no .profraw files")
    print(f"training: {len(training_links(corpus_root))} links, {count} raw profile(s)")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    instr = args.instr_dir.resolve()
    profdata = check_profdata_matches_compiler(args.cxx)
    raws = sorted((instr / PROFILE_SUBDIR).glob("*.profraw"))
    if not raws:
        raise SystemExit("no .profraw files: run `train` first")
    run([profdata, "merge", "-o", str(instr / PROFDATA_NAME), *map(str, raws)], 0)
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    profdata = args.instr_dir.resolve() / PROFDATA_NAME
    if not profdata.exists():
        raise SystemExit(f"{profdata} missing: run `merge` first")
    build(args.workspace, args.out_dir, optimize_flags(profdata, runtime_rpath(args.cxx), args.bolt), args)
    print(f"built {args.out_dir / TARGET}")
    return 0


BOLT_OPTIMIZE_FLAGS = [
    "-reorder-blocks=ext-tsp", "-reorder-functions=cdsort", "-split-functions",
    "-split-all-cold", "-split-eh", "-use-gnu-stack",
    # No -icf: folding identical functions can break code that compares function
    # pointers (the output gate would not catch that), BOLT 18 has no "safe" mode
    # (its -icf is a boolean), and the layout, not the ~280 KB folding, is the gain.
]


def bolt_tool(name: str, bolt_dir: Path | None) -> str:
    path = str(bolt_dir / name) if bolt_dir else shutil.which(name)
    if not path:
        raise SystemExit(f"{name} not found (pass --bolt-dir)")
    return path


def cmd_bolt(args: argparse.Namespace) -> int:
    out = args.out_dir.resolve()
    binary = out / TARGET
    pre = out / (TARGET + ".pre-bolt")
    if not binary.exists():
        raise SystemExit(f"{binary} missing: run `optimize --bolt` first")
    shutil.copy2(binary, pre)
    llvm_bolt = bolt_tool("llvm-bolt", args.bolt_dir)
    work = out / "bolt"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    instrumented = work / "instrumented"
    run([llvm_bolt, str(pre), "-instrument", "-o", str(instrumented),
         f"--instrumentation-file={work / 'prof.fdata'}", "--instrumentation-file-append-pid"], 0,
        stdout=subprocess.DEVNULL)
    corpus_root = args.instr_dir.resolve() / "train-corpus"
    for corpus, extra in training_links(corpus_root):
        run([str(instrumented), "winlink", "lld-link", "@link.rsp", "/out:train.exe", *extra],
            args.nice, cwd=corpus, stdout=subprocess.DEVNULL)
    parts = sorted(work.glob("prof.fdata.*"))
    if not parts:
        raise SystemExit("BOLT training produced no profile")
    merged = work / "merged.fdata"
    with open(merged, "wb") as out_file:
        subprocess.run([bolt_tool("merge-fdata", args.bolt_dir), *map(str, parts)], check=True,
                       stdout=out_file, stderr=subprocess.DEVNULL)
    run([llvm_bolt, str(pre), "-o", str(binary), f"-data={merged}", *BOLT_OPTIMIZE_FLAGS], 0,
        stdout=subprocess.DEVNULL)
    shutil.rmtree(work)
    print(f"BOLT-optimized {binary} (pre-BOLT kept as {pre.name})")
    return 0


def cmd_plain(args: argparse.Namespace) -> int:
    build(args.workspace, args.plain_dir, [], args)
    print(f"built {args.plain_dir / TARGET}")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    steps = [cmd_instrument, cmd_train, cmd_merge, cmd_optimize] + ([cmd_bolt] if args.bolt else [])
    for step in steps:
        step(args)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["instrument", "train", "merge", "optimize", "bolt", "plain", "all"])
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--instr-dir", type=Path, default=Path("build-pgo-instr"))
    parser.add_argument("--out-dir", type=Path, default=Path("build-pgo"))
    parser.add_argument("--plain-dir", type=Path, default=Path("build-plain"))
    parser.add_argument("--bolt", action="store_true",
                        help="optimize keeps relocations; all also runs the bolt stage (Linux)")
    parser.add_argument("--bolt-dir", type=Path, help="directory holding llvm-bolt and merge-fdata")
    parser.add_argument("--cc", default="clang")
    parser.add_argument("--cxx", default="clang++")
    parser.add_argument("--nice", type=int, default=0, help="run builds and training under nice -n N")
    parser.add_argument("--launcher", default="none",
                        help="compiler launcher (e.g. sccache in CI); 'none' by default")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    handler = {"instrument": cmd_instrument, "train": cmd_train, "merge": cmd_merge,
               "optimize": cmd_optimize, "bolt": cmd_bolt, "plain": cmd_plain, "all": cmd_all}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
