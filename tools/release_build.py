"""Build and package llvm-ld-coff for one host platform.

llvm-ld is LLD's **COFF** linker (the lld-link/MSVC and MinGW drivers) as a shared library with a
C ABI. Every build of it links **Windows PE/COFF** executables and DLLs. The triple a release
archive is named after is the *host* the library runs on, never the target it links: the Linux
and macOS builds are Windows cross-linkers. Release assets are therefore named
`llvm-ld-coff-<version>-<host-triple>` so that is never ambiguous.

`.github/workflows/release.yml` runs this once per host in its matrix. It configures with the same
flags as ci.yml, builds the library and runner, runs the ABI smoke test where the host can execute
it, and packages the result together with the header and the license and provenance notices.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
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


def configure_command(host: Host, build_dir: Path) -> list[str]:
    command = [
        "cmake", "-S", str(REPO_ROOT), "-B", str(build_dir), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_APPEND_VC_REV=OFF",
        "-DLLVM_LD_ENABLE_PPROF=OFF",
        "-DLLVM_LD_ENABLE_DHAT=OFF",
    ]
    if host.os == "linux":
        # Keep the shared library loadable on hosts whose libstdc++ is older than the builder's.
        command.append("-DCMAKE_SHARED_LINKER_FLAGS=-static-libstdc++ -static-libgcc")
        command.append("-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc")
    if host.osx_arch:
        command.append(f"-DCMAKE_OSX_ARCHITECTURES={host.osx_arch}")
    return command


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
    args = parser.parse_args(argv)
    host = HOSTS[args.triple]

    run(configure_command(host, args.build_dir))
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
    args.out_dir.mkdir(parents=True, exist_ok=True)
    archive = package(host, args.version, args.build_dir, args.out_dir)
    print(f"packaged {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
