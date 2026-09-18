"""Packaging logic of tools/release_build.py: naming, file selection, archive layout."""

import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import release_build  # noqa: E402


class ReleaseBuildTest(unittest.TestCase):
    def test_every_published_host_is_named_as_the_coff_linker(self):
        # The triple names the host; the product name says what it links.
        self.assertEqual(len(release_build.HOSTS), 8)
        for triple, host in release_build.HOSTS.items():
            stem = release_build.archive_stem("v1.2.3", host)
            self.assertEqual(stem, f"llvm-ld-coff-v1.2.3-{triple}")

    def test_library_file_names_follow_the_host_os(self):
        hosts = release_build.HOSTS
        self.assertEqual(hosts["x86_64-pc-windows-msvc"].library, "llvm_ld.dll")
        self.assertEqual(hosts["aarch64-unknown-linux-musl"].library, "libllvm_ld.so")
        self.assertEqual(hosts["x86_64-apple-darwin"].library, "libllvm_ld.dylib")

    def test_linux_builds_link_the_cxx_runtime_statically(self):
        cmd = release_build.configure_command(release_build.HOSTS["x86_64-unknown-linux-gnu"], Path("b"))
        self.assertIn("-DCMAKE_SHARED_LINKER_FLAGS=-static-libstdc++ -static-libgcc", cmd)

    def test_macos_x64_is_cross_built_for_x86_64(self):
        cmd = release_build.configure_command(release_build.HOSTS["x86_64-apple-darwin"], Path("b"))
        self.assertIn("-DCMAKE_OSX_ARCHITECTURES=x86_64", cmd)

    def _fake_build(self, root: Path, host) -> Path:
        build = root / "build"
        build.mkdir()
        (build / host.library).write_bytes(b"lib")
        (build / host.runner).write_bytes(b"runner")
        if host.os == "windows":
            (build / "llvm_ld.lib").write_bytes(b"implib")
        return build

    def test_windows_archive_is_a_zip_with_the_import_library(self):
        host = release_build.HOSTS["aarch64-pc-windows-msvc"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = release_build.package(host, "v9", self._fake_build(root, host), root / "dist")
            self.assertEqual(archive.name, "llvm-ld-coff-v9-aarch64-pc-windows-msvc.zip")
            names = {Path(n).name for n in zipfile.ZipFile(archive).namelist()}
            self.assertTrue({"llvm_ld.dll", "llvm_ld.lib", "llvm_ld.h", "llvm-ld-runner.exe", "LICENSE-LLVM.txt"} <= names)

    def test_unix_archive_is_a_tarball_with_one_top_level_directory(self):
        host = release_build.HOSTS["x86_64-unknown-linux-gnu"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = release_build.package(host, "v9", self._fake_build(root, host), root / "dist")
            with tarfile.open(archive) as bundle:
                tops = {m.name.split("/")[0] for m in bundle.getmembers()}
                names = {Path(m.name).name for m in bundle.getmembers()}
            self.assertEqual(tops, {"llvm-ld-coff-v9-x86_64-unknown-linux-gnu"})
            self.assertIn("libllvm_ld.so", names)
            self.assertNotIn("llvm_ld.lib", names)

    def test_a_missing_build_output_fails_loudly(self):
        host = release_build.HOSTS["x86_64-apple-darwin"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / "build"
            build.mkdir()
            with self.assertRaises(SystemExit):
                release_build.package(host, "v9", build, root / "dist")


if __name__ == "__main__":
    unittest.main()
