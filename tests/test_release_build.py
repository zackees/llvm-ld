"""Packaging logic of tools/release_build.py: naming, file selection, archive layout."""

import sys
import tarfile
from unittest import mock
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



class PgoTest(unittest.TestCase):
    """#46: PGO flags per host and the byte-identity gate against the previous release."""

    def test_pgo_flags_go_through_the_environment_not_cache_flags(self):
        host = release_build.HOSTS["x86_64-unknown-linux-gnu"]
        cmd = release_build.configure_command(host, Path("b"), release_build.pgo_toolchain(host))
        # A -DCMAKE_*_FLAGS here would replace platform defaults (MSVC's /EHsc); the PGO build
        # passes flags via CFLAGS/CXXFLAGS/LDFLAGS instead.
        self.assertFalse(any(c.startswith(("-DCMAKE_C_FLAGS", "-DCMAKE_CXX_FLAGS", "-DCMAKE_SHARED_LINKER_FLAGS"))
                             for c in cmd))
        self.assertIn("-DCMAKE_CXX_COMPILER=clang++", cmd)
        env = release_build.pgo_env(host, "use", Path("/p/x.profdata"))
        self.assertIn("-fprofile-use=/p/x.profdata", env["CXXFLAGS"])
        self.assertIn("-flto=thin", env["CXXFLAGS"])
        self.assertIn("-fuse-ld=lld", env["LDFLAGS"])
        # The static C++ runtime survives the switch to LDFLAGS.
        self.assertIn("-static-libstdc++", env["LDFLAGS"])

    def test_generate_stage_instruments_compiles_and_links(self):
        host = release_build.HOSTS["aarch64-apple-darwin"]
        env = release_build.pgo_env(host, "generate", Path("/p/profiles"))
        self.assertEqual(env["CFLAGS"], "-fprofile-generate=/p/profiles")
        self.assertEqual(env["LDFLAGS"], "-fprofile-generate=/p/profiles")

    def test_linux_links_the_instrumented_build_with_lld_too(self):
        env = release_build.pgo_env(release_build.HOSTS["aarch64-unknown-linux-gnu"], "generate", Path("/p"))
        self.assertIn("-fuse-ld=lld", env["LDFLAGS"])
        self.assertIn("-fprofile-generate=/p", env["LDFLAGS"])

    def test_windows_uses_clang_cl_and_lld_link(self):
        host = release_build.HOSTS["x86_64-pc-windows-msvc"]
        toolchain = release_build.pgo_toolchain(host)
        self.assertIn("-DCMAKE_CXX_COMPILER=clang-cl", toolchain)
        self.assertIn("-DCMAKE_LINKER=lld-link", toolchain)
        self.assertIn("-DCMAKE_AR=llvm-lib", toolchain)
        self.assertEqual(release_build.pgo_env(host, "use", Path("p"))["LDFLAGS"], "")

    def test_a_changed_profile_discards_the_optimized_build_tree(self):
        """-fprofile-use is invisible to ninja: mixing two profiles breaks the ThinLTO link."""
        with tempfile.TemporaryDirectory() as tmp:
            build, profdata = Path(tmp) / "build-release", Path(tmp) / "llvm-ld.profdata"
            build.mkdir()
            (build / "stale.o").write_text("old")
            profdata.write_bytes(b"profile-one")
            release_build.drop_stale_profile_objects(build, profdata)
            self.assertFalse((build / "stale.o").exists(), "a tree with no stamp is discarded")
            (build / "fresh.o").write_text("new")
            release_build.drop_stale_profile_objects(build, profdata)
            self.assertTrue((build / "fresh.o").exists(), "the same profile keeps the tree")
            profdata.write_bytes(b"profile-two")
            release_build.drop_stale_profile_objects(build, profdata)
            self.assertFalse((build / "fresh.o").exists(), "a new profile discards the tree")

    def test_pgo_builds_use_an_uninstrumented_native_tablegen(self):
        """#58: instrumented tablegen stalled the aarch64 leg; both PGO builds get a plain one."""
        host = release_build.HOSTS["x86_64-apple-darwin"]
        commands, envs = [], []

        def fake_run(command, **kwargs):
            commands.append(command)
            envs.append(kwargs.get("env"))
            if "merge" in command:  # llvm-profdata merge -o <profdata> <raws...>
                Path(command[command.index("-o") + 1]).write_bytes(b"merged-profile")
                return
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(release_build, "run", side_effect=fake_run), \
                mock.patch.object(release_build, "link", side_effect=lambda h, d, c, t, v, env: \
                    Path(env["LLVM_PROFILE_FILE"].replace("%p-%m", "1")).touch()):
            release_build.pgo_build(host, Path(tmp) / "build-release", Path(tmp), launcher="zccache")
        configures = [c for c in commands if c[:2] == ["cmake", "-S"]]
        tblgen, instr, opt = configures
        self.assertTrue(tblgen[2].endswith("llvm-project/llvm"))
        self.assertIsNone(envs[commands.index(tblgen)], "tablegen must not see the PGO CFLAGS")
        self.assertFalse(any(a.startswith("-DCMAKE_OSX_ARCHITECTURES") for a in tblgen), "tablegen runs natively")
        for build in (instr, opt):
            self.assertTrue(any(a.startswith("-DLLVM_NATIVE_TOOL_DIR=") and a.endswith("build-release-tblgen/bin")
                                for a in build))
            self.assertIn("-DCMAKE_CXX_COMPILER_LAUNCHER=zccache", build)

    def test_no_launcher_clears_a_cached_one(self):
        cmd = release_build.configure_command(release_build.HOSTS["x86_64-unknown-linux-gnu"], Path("b"))
        self.assertIn("-DCMAKE_CXX_COMPILER_LAUNCHER=", cmd)

    def test_a_hung_link_fails_instead_of_burning_the_job_budget(self):
        host = release_build.HOSTS["x86_64-unknown-linux-gnu"]

        def hang(command, **kwargs):
            raise release_build.subprocess.TimeoutExpired(command, kwargs["timeout"])
        with mock.patch.object(release_build.subprocess, "run", side_effect=hang):
            with self.assertRaises(SystemExit) as caught:
                release_build.link_timed(host, Path("b"), Path("/corpus/release/medium"), 4, "pdb")
        self.assertIn("hung", str(caught.exception))
        self.assertIn("20 min", str(caught.exception))

    def test_profdata_tool_is_xcrun_on_macos(self):
        self.assertEqual(release_build.profdata_tool(release_build.HOSTS["x86_64-apple-darwin"]),
                         ["xcrun", "llvm-profdata"])

    def test_training_and_gate_use_the_corpus_job_set(self):
        trained = {(m, p) for m, p, _, _ in release_build.TRAINING}
        gated = {(m, p) for m, p, _ in release_build.GATE}
        self.assertTrue(gated <= trained)
        self.assertIn(("thinlto", "small"), trained)

    def _fake_runner(self, directory: Path, payload: str) -> None:
        directory.mkdir(parents=True)
        runner = directory / "llvm-ld-runner"
        runner.write_text("#!/bin/sh\n"
                          f"printf '{payload}' > link.exe\n"
                          "for a in \"$@\"; do case \"$a\" in /pdb:*) printf pdb > link.pdb;; esac; done\n")
        runner.chmod(0o755)

    def _corpora(self, root: Path) -> Path:
        for mode, profile, _ in release_build.GATE:
            (root / mode / profile).mkdir(parents=True, exist_ok=True)
        return root

    @unittest.skipIf(sys.platform == "win32", "fake runners are shell scripts")
    def test_gate_passes_on_identical_output_and_fails_on_any_difference(self):
        host = release_build.HOSTS["x86_64-unknown-linux-gnu"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpora = self._corpora(root / "corpus")
            self._fake_runner(root / "ref", "same")
            self._fake_runner(root / "new", "same")
            summary = release_build.gate(host, root / "new", root / "ref", corpora)
            self.assertIn("new vs previous release", summary)
            self.assertIn("% CPU", summary)
            self._fake_runner(root / "bad", "different")
            with self.assertRaises(SystemExit) as caught:
                release_build.gate(host, root / "bad", root / "ref", corpora)
            self.assertIn("GATE FAILED", str(caught.exception))

    def test_reference_archive_is_unpacked_to_its_runner_directory(self):
        host = release_build.HOSTS["x86_64-unknown-linux-gnu"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = release_build.package(host, "v0", ReleaseBuildTest._fake_build(self, root, host), root / "dist")
            directory = release_build.extract_reference(archive, root / "ref")
            self.assertEqual(directory.name, "llvm-ld-coff-v0-x86_64-unknown-linux-gnu")
            self.assertTrue((directory / "llvm-ld-runner").stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
