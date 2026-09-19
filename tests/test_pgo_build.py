#!/usr/bin/env python3
"""Tests for tools/pgo_build.py's command construction (#43). No build is run.

Stdlib only. Run: python -m unittest discover -s tests -p 'test_pgo_build.py'.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
spec = importlib.util.spec_from_file_location("pgo_build", ROOT / "tools" / "pgo_build.py")
pgo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pgo)


class PgoBuildFlagsTest(unittest.TestCase):
    def test_instrument_flags_cover_compiles_and_every_link(self) -> None:
        flags = pgo.instrument_flags(pathlib.Path("/p/profiles"))
        for var in ("CMAKE_C_FLAGS", "CMAKE_CXX_FLAGS", "CMAKE_EXE_LINKER_FLAGS",
                    "CMAKE_SHARED_LINKER_FLAGS", "CMAKE_MODULE_LINKER_FLAGS"):
            self.assertIn(f"-D{var}=-fprofile-generate=/p/profiles", flags)

    def test_optimize_flags_use_profile_and_thinlto_with_lld_and_llvm_ar(self) -> None:
        flags = pgo.optimize_flags(pathlib.Path("/p/llvm-ld.profdata"))
        cxx = next(f for f in flags if f.startswith("-DCMAKE_CXX_FLAGS="))
        self.assertIn("-fprofile-use=/p/llvm-ld.profdata", cxx)
        self.assertIn("-flto=thin", cxx)
        exe = next(f for f in flags if f.startswith("-DCMAKE_EXE_LINKER_FLAGS="))
        self.assertIn("-flto=thin", exe)
        self.assertIn("-fuse-ld=lld", exe)
        self.assertTrue(any(f.startswith("-DCMAKE_AR=") and "llvm-ar" in f for f in flags))
        self.assertTrue(any(f.startswith("-DCMAKE_RANLIB=") and "llvm-ranlib" in f for f in flags))

    def test_lld_links_carry_the_runtime_rpath(self) -> None:
        flags = pgo.optimize_flags(pathlib.Path("/p/x.profdata"), "/nix/store/gcc-lib/lib")
        exe = next(f for f in flags if f.startswith("-DCMAKE_EXE_LINKER_FLAGS="))
        self.assertIn("-Wl,-rpath,/nix/store/gcc-lib/lib", exe)
        plain = pgo.optimize_flags(pathlib.Path("/p/x.profdata"))
        self.assertFalse(any("-rpath" in f for f in plain))

    def test_bolt_keeps_relocations_and_folds_safely(self) -> None:
        exe = next(f for f in pgo.optimize_flags(pathlib.Path("/p/x.profdata"), None, bolt=True)
                   if f.startswith("-DCMAKE_EXE_LINKER_FLAGS="))
        self.assertIn("-Wl,--emit-relocs", exe)
        plain = next(f for f in pgo.optimize_flags(pathlib.Path("/p/x.profdata"))
                     if f.startswith("-DCMAKE_EXE_LINKER_FLAGS="))
        self.assertNotIn("--emit-relocs", plain)
        self.assertIn("-icf=safe", pgo.BOLT_OPTIMIZE_FLAGS)
        self.assertFalse(any(f in ("-icf=1", "-icf=all") for f in pgo.BOLT_OPTIMIZE_FLAGS))

    def test_builds_share_ci_configure_flags(self) -> None:
        base = pgo.ci_build.configure_command(pathlib.Path("/w"), pathlib.Path("/b"), "none",
                                              "clang", "clang++", None, extra_args=[])
        for flag in ("-DCMAKE_BUILD_TYPE=Release", "-DLLVM_APPEND_VC_REV=OFF"):
            self.assertIn(flag, base)

    def test_llvm_major_parses_both_tools(self) -> None:
        self.assertEqual(pgo.llvm_major("LLVM (http://llvm.org/):\n  LLVM version 21.1.8\n"), 21)
        self.assertEqual(pgo.llvm_major("clang version 21.1.8\nTarget: x86_64"), 21)
        self.assertIsNone(pgo.llvm_major("gcc (GCC) 15.2.0"))


class TrainingWorkloadTest(unittest.TestCase):
    def test_training_covers_every_mode_and_both_variants(self) -> None:
        links = pgo.training_links(pathlib.Path("/c"))
        corpora = {str(corpus) for corpus, _ in links}
        for mode in ("debug", "release", "thinlto"):
            self.assertTrue(any(f"/c/{mode}/" in c for c in corpora), mode)
        with_pdb = [extra for _, extra in links if "/debug:full" in extra]
        without = [extra for _, extra in links if "/debug:full" not in extra]
        self.assertEqual(len(with_pdb), len(without))
        # The large corpora are held out for measurement.
        self.assertFalse(any(c.endswith("/large") for c in corpora))


if __name__ == "__main__":
    unittest.main()
