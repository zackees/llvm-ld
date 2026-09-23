#!/usr/bin/env python3
"""Tests for tools/ci_build.py, the script-driven build wrapper that drives
CMake/Ninja through zccache's build-tree cache (stamp-fresh, replay, snapshot)
instead of ci.yml hand-rolling the same steps.

The end-to-end class is the key regression: it proves that after a
stamp-fresh + replay cycle restores recorded mtimes on unmodified sources,
a genuinely edited source (even one whose new mtime is *older* than
everything under build/) is still detected and rebuilt, rather than being
mistaken for a cache hit and leaving a stale binary in place.

stdlib only, no pytest. Run: python -m unittest discover -s tests -p
'test_ci_build.py'. Needs no network access. The end-to-end class is skipped
unless zccache, cmake, ninja, and a C compiler are all on PATH.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI_BUILD_PATH = ROOT / "tools" / "ci_build.py"

spec = importlib.util.spec_from_file_location("ci_build", CI_BUILD_PATH)
ci_build = importlib.util.module_from_spec(spec)
sys.modules["ci_build"] = ci_build
spec.loader.exec_module(ci_build)


def _isolated_env(empty_path_dir: str) -> dict[str, str]:
    """A full replacement environment: PATH only sees `empty_path_dir`, ZCCACHE unset."""
    env = dict(os.environ)
    env.pop("ZCCACHE", None)
    env["PATH"] = empty_path_dir
    return env


class ConstantsTest(unittest.TestCase):
    def test_zccache_version_and_install_hint(self) -> None:
        self.assertEqual(ci_build.ZCCACHE_VERSION, "1.14.11")
        self.assertIn("pip install zccache==1.14.11", ci_build.INSTALL_HINT)

    def test_default_targets(self) -> None:
        self.assertEqual(
            ci_build.DEFAULT_TARGETS,
            [
                "llvm_ld",
                "abi_smoke",
                "abi_contract",
                "abi_state_test",
                "allocator-probe",
                "llvm-ld-runner",
                "llvm-ld-direct",
            ],
        )

    def test_misc_defaults(self) -> None:
        self.assertEqual(ci_build.DEFAULT_BUILD_DIR, "build")
        self.assertEqual(ci_build.MANIFEST_NAME, "zccache-mtimes.json")
        self.assertEqual(ci_build.DEFAULT_TRACE_FILE, "cmake-trace.jsonl")
        self.assertEqual(ci_build.DEFAULT_WARN_BELOW, 0.9)
        self.assertEqual(ci_build.KEY_PREFIX, "llvm-ld-buildtree-v1")


class ParseArgsAndResolvePathsTest(unittest.TestCase):
    def test_key_defaults(self) -> None:
        args = ci_build.parse_args(["key"])
        self.assertEqual(args.command, "key")
        self.assertEqual(args.workspace, ".")
        self.assertEqual(args.build_dir, ci_build.DEFAULT_BUILD_DIR)
        self.assertIsNone(args.manifest)
        self.assertEqual(args.launcher, "sccache")
        self.assertEqual(args.c_compiler, "clang")
        self.assertEqual(args.cxx_compiler, "clang++")
        self.assertFalse(args.github_output)

    def test_replay_defaults(self) -> None:
        args = ci_build.parse_args(["replay"])
        self.assertEqual(args.command, "replay")
        self.assertEqual(args.workspace, ".")
        self.assertEqual(args.build_dir, ci_build.DEFAULT_BUILD_DIR)
        self.assertIsNone(args.manifest)
        self.assertEqual(args.warn_below, ci_build.DEFAULT_WARN_BELOW)
        self.assertFalse(args.import_upstream)

    def test_build_defaults(self) -> None:
        args = ci_build.parse_args(["build"])
        self.assertEqual(args.command, "build")
        self.assertEqual(args.workspace, ".")
        self.assertEqual(args.build_dir, ci_build.DEFAULT_BUILD_DIR)
        self.assertIsNone(args.manifest)
        self.assertEqual(args.launcher, "auto")
        self.assertIsNone(args.targets)
        self.assertIsNone(args.cmake_args)
        self.assertEqual(args.c_compiler, "clang")
        self.assertEqual(args.cxx_compiler, "clang++")
        self.assertEqual(args.trace_file, ci_build.DEFAULT_TRACE_FILE)
        self.assertFalse(args.no_trace)
        self.assertFalse(args.expect_no_work)

    def test_snapshot_defaults(self) -> None:
        args = ci_build.parse_args(["snapshot"])
        self.assertEqual(args.command, "snapshot")
        self.assertEqual(args.workspace, ".")
        self.assertEqual(args.build_dir, ci_build.DEFAULT_BUILD_DIR)
        self.assertIsNone(args.manifest)

    def test_all_defaults(self) -> None:
        args = ci_build.parse_args(["all"])
        self.assertEqual(args.command, "all")
        self.assertEqual(args.workspace, ".")
        self.assertEqual(args.build_dir, ci_build.DEFAULT_BUILD_DIR)

    def test_build_target_repeated_collects_both(self) -> None:
        args = ci_build.parse_args(["build", "--target", "a", "--target", "b"])
        self.assertEqual(args.targets, ["a", "b"])

    def test_build_cmake_arg_accepted(self) -> None:
        args = ci_build.parse_args(["build", "--cmake-arg=-DFOO=1"])
        self.assertEqual(args.cmake_args, ["-DFOO=1"])

    def test_resolve_paths_relative_build_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp).resolve()
            args = ci_build.parse_args(
                ["build", "--workspace", str(tmp), "--build-dir", "out"]
            )
            workspace, build_dir, manifest = ci_build.resolve_paths(args)
            self.assertEqual(pathlib.Path(workspace), tmp)
            self.assertEqual(pathlib.Path(build_dir), tmp / "out")
            self.assertEqual(pathlib.Path(manifest), tmp / "out" / ci_build.MANIFEST_NAME)

    def test_resolve_paths_absolute_manifest_kept(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp).resolve()
            other_manifest = tmp / "elsewhere" / "manifest.json"
            args = ci_build.parse_args(
                [
                    "build",
                    "--workspace",
                    str(tmp),
                    "--build-dir",
                    "out",
                    "--manifest",
                    str(other_manifest),
                ]
            )
            _workspace, _build_dir, manifest = ci_build.resolve_paths(args)
            self.assertEqual(pathlib.Path(manifest), other_manifest)


class ConfigureCommandTest(unittest.TestCase):
    def test_launcher_and_trace_flags_in_ci_yml_order(self) -> None:
        cmd = ci_build.configure_command(
            "/ws", "/ws/build", "sccache", "clang", "clang++", "cmake-trace.jsonl"
        )
        self.assertEqual(
            cmd,
            [
                "cmake",
                "-S",
                "/ws",
                "-B",
                "/ws/build",
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DLLVM_APPEND_VC_REV=OFF",
                "-DCMAKE_C_COMPILER=clang",
                "-DCMAKE_CXX_COMPILER=clang++",
                "-DCMAKE_C_COMPILER_LAUNCHER=sccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=sccache",
                "--trace-expand",
                "--trace-format=json-v1",
                "--trace-redirect=cmake-trace.jsonl",
            ],
        )

    def test_launcher_none_yields_empty_launcher_value(self) -> None:
        cmd = ci_build.configure_command("/ws", "/ws/build", "none", "clang", "clang++", None)
        self.assertIn("-DCMAKE_C_COMPILER_LAUNCHER=", cmd)
        self.assertIn("-DCMAKE_CXX_COMPILER_LAUNCHER=", cmd)

    def test_trace_file_none_omits_trace_flags(self) -> None:
        cmd = ci_build.configure_command("/ws", "/ws/build", "sccache", "clang", "clang++", None)
        self.assertNotIn("--trace-expand", cmd)
        self.assertNotIn("--trace-format=json-v1", cmd)
        self.assertFalse(any(str(part).startswith("--trace-redirect=") for part in cmd))

    def test_extra_args_appended_last(self) -> None:
        cmd = ci_build.configure_command(
            "/ws",
            "/ws/build",
            "none",
            "clang",
            "clang++",
            None,
            extra_args=["-DFOO=1", "-DBAR=2"],
        )
        self.assertEqual(cmd[-2:], ["-DFOO=1", "-DBAR=2"])


class CommandBuildersTest(unittest.TestCase):
    def test_build_command(self) -> None:
        self.assertEqual(
            ci_build.build_command("/ws/build", ["a", "b"]),
            ["cmake", "--build", "/ws/build", "--target", "a", "b"],
        )

    def test_dry_run_command(self) -> None:
        self.assertEqual(
            ci_build.dry_run_command("/ws/build", ["a", "b"]),
            ["ninja", "-C", "/ws/build", "-n", "-d", "explain", "a", "b"],
        )

    def test_replay_command(self) -> None:
        self.assertEqual(
            ci_build.replay_command(["zccache"], "/ws", "/ws/build/zccache-mtimes.json"),
            [
                "zccache",
                "replay",
                "--workspace",
                "/ws",
                "--manifest",
                "/ws/build/zccache-mtimes.json",
                "--json",
            ],
        )

    def test_snapshot_command(self) -> None:
        self.assertEqual(
            ci_build.snapshot_command(
                ["zccache"], "/ws", "/ws/build", "/ws/build/zccache-mtimes.json"
            ),
            [
                "zccache",
                "snapshot",
                "--workspace",
                "/ws",
                "--out",
                "/ws/build/zccache-mtimes.json",
                "--exclude",
                "/ws/build",
            ],
        )


class CountNinjaEdgesTest(unittest.TestCase):
    def test_counts_edge_lines_and_ignores_explain_noise(self) -> None:
        text = (
            "ninja explain: output cmake-trace.jsonl older than input\n"
            "[1/3] Building CXX object foo.cpp.o\n"
            "ninja explain: some other reason\n"
            "[2/3] Building CXX object bar.cpp.o\n"
            "[3/3] Linking CXX executable tiny\n"
        )
        self.assertEqual(ci_build.count_ninja_edges(text), 3)

    def test_no_work_to_do_is_zero(self) -> None:
        self.assertEqual(ci_build.count_ninja_edges("ninja: no work to do.\n"), 0)


class ParseReplayReportTest(unittest.TestCase):
    def test_parses_last_json_line_after_non_json_preamble(self) -> None:
        stdout = (
            "zccache: connecting to workspace\n"
            '{"total":10,"applied":9,"missing":1,"size_mismatch":0,'
            '"modified":0,"applied_ratio":0.9}\n'
        )
        report = ci_build.parse_replay_report(stdout)
        self.assertEqual(report["applied"], 9)
        self.assertEqual(report["total"], 10)
        self.assertEqual(report["missing"], 1)

    def test_raises_valueerror_on_garbage(self) -> None:
        with self.assertRaises(ValueError):
            ci_build.parse_replay_report("not json\nstill not json\n")


class FormatWarningTest(unittest.TestCase):
    def test_github_actions_mode(self) -> None:
        self.assertEqual(ci_build.format_warning("low ratio", True), "::warning::low ratio")

    def test_plain_mode(self) -> None:
        self.assertEqual(ci_build.format_warning("low ratio", False), "warning: low ratio")


class IsSafeRelativeTest(unittest.TestCase):
    def test_safe_path(self) -> None:
        self.assertTrue(ci_build.is_safe_relative("a/b.c"))

    def test_unsafe_paths(self) -> None:
        for unsafe in ("", "/abs", "../x", "a/../b", "a//b", "a\\b", "./a"):
            with self.subTest(unsafe=unsafe):
                self.assertFalse(ci_build.is_safe_relative(unsafe))


class StampFreshTest(unittest.TestCase):
    def test_stamps_only_safe_existing_regular_files_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            ws = tmp / "ws"
            (ws / "sub").mkdir(parents=True)

            a_c = ws / "a.c"
            b_h = ws / "sub" / "b.h"
            a_c.write_text("int a;\n", encoding="utf-8")
            b_h.write_text("int b;\n", encoding="utf-8")

            old_ts = 978307200.0  # 2001-01-01T00:00:00Z
            os.utime(a_c, (old_ts, old_ts))
            os.utime(b_h, (old_ts, old_ts))

            escape_c = tmp / "escape.c"
            escape_c.write_text("int e;\n", encoding="utf-8")
            os.utime(escape_c, (old_ts, old_ts))

            link_c = ws / "link.c"
            try:
                os.symlink(a_c, link_c)
            except (OSError, NotImplementedError, AttributeError):
                pass  # platform without symlink support: link.c is simply absent

            manifest = {
                "version": 1,
                "entries": [
                    {"path": "a.c", "size": a_c.stat().st_size, "mtime_ns": 0, "blake3": ""},
                    {
                        "path": "sub/b.h",
                        "size": b_h.stat().st_size,
                        "mtime_ns": 0,
                        "blake3": "",
                    },
                    {"path": "link.c", "size": 0, "mtime_ns": 0, "blake3": ""},
                    {"path": "missing.c", "size": 0, "mtime_ns": 0, "blake3": ""},
                    {"path": "../escape.c", "size": 0, "mtime_ns": 0, "blake3": ""},
                ],
            }
            manifest_path = ws / "zccache-mtimes.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            stamped = ci_build.stamp_fresh(ws, manifest_path)
            self.assertEqual(stamped, 2)

            now = time.time()
            self.assertLess(abs(a_c.stat().st_mtime - now), 60)
            self.assertLess(abs(b_h.stat().st_mtime - now), 60)
            self.assertAlmostEqual(escape_c.stat().st_mtime, old_ts, delta=2)


class HashInputsAndCacheKeysTest(unittest.TestCase):
    @staticmethod
    def _make_workspace(tmp: pathlib.Path) -> pathlib.Path:
        ws = tmp / "ws"
        (ws / "src").mkdir(parents=True)
        (ws / "tools" / "__pycache__").mkdir(parents=True)
        (ws / "provenance").mkdir(parents=True)
        (ws / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8"
        )
        (ws / "src" / "x.cpp").write_text("int x() { return 1; }\n", encoding="utf-8")
        (ws / "tools" / "y.py").write_text("def y():\n    return 1\n", encoding="utf-8")
        (ws / "provenance" / "llvm-source-closure.json").write_text("{}\n", encoding="utf-8")
        (ws / "provenance" / "payload-prune.json").write_text("{}\n", encoding="utf-8")
        (ws / "tools" / "__pycache__" / "y.cpython-312.pyc").write_bytes(b"\x00\x01\x02")
        return ws

    def test_hash_inputs_deterministic_and_reacts_to_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ws = self._make_workspace(pathlib.Path(raw_tmp))
            paths = ["CMakeLists.txt", "src/x.cpp"]
            first = ci_build.hash_inputs(ws, paths)
            second = ci_build.hash_inputs(ws, paths)
            self.assertEqual(first, second)
            self.assertIsInstance(first, str)

            (ws / "src" / "x.cpp").write_text("int x() { return 2; }\n", encoding="utf-8")
            changed = ci_build.hash_inputs(ws, paths)
            self.assertNotEqual(first, changed)

    def test_compute_cache_keys_deterministic_and_prefix_relationship(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ws = self._make_workspace(pathlib.Path(raw_tmp))
            primary1, prefix1 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            primary2, prefix2 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            self.assertEqual(primary1, primary2)
            self.assertEqual(prefix1, prefix2)
            self.assertTrue(primary1.startswith(prefix1))
            self.assertTrue(prefix1.startswith(ci_build.KEY_PREFIX + "-"))
            self.assertTrue(prefix1.endswith("-"))

    def test_editing_source_file_changes_primary_not_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ws = self._make_workspace(pathlib.Path(raw_tmp))
            primary1, prefix1 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            (ws / "src" / "x.cpp").write_text("int x() { return 99; }\n", encoding="utf-8")
            primary2, prefix2 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            self.assertNotEqual(primary1, primary2)
            self.assertEqual(prefix1, prefix2)

    def test_editing_payload_prune_changes_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ws = self._make_workspace(pathlib.Path(raw_tmp))
            _primary1, prefix1 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            (ws / "provenance" / "payload-prune.json").write_text(
                '{"changed": true}\n', encoding="utf-8"
            )
            _primary2, prefix2 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            self.assertNotEqual(prefix1, prefix2)

    def test_changing_identity_changes_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ws = self._make_workspace(pathlib.Path(raw_tmp))
            _primary1, prefix1 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            _primary2, prefix2 = ci_build.compute_cache_keys(ws, "sccache", "runner-2")
            self.assertNotEqual(prefix1, prefix2)

    def test_changing_launcher_changes_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ws = self._make_workspace(pathlib.Path(raw_tmp))
            _primary1, prefix1 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            _primary2, prefix2 = ci_build.compute_cache_keys(ws, "zccache", "runner-1")
            self.assertNotEqual(prefix1, prefix2)

    def test_editing_pycache_artifact_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ws = self._make_workspace(pathlib.Path(raw_tmp))
            primary1, prefix1 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            (ws / "tools" / "__pycache__" / "y.cpython-312.pyc").write_bytes(
                b"\x00\x01\x02\x03\x04\x05"
            )
            primary2, prefix2 = ci_build.compute_cache_keys(ws, "sccache", "runner-1")
            self.assertEqual(primary1, primary2)
            self.assertEqual(prefix1, prefix2)


class MainReplayCacheMissTest(unittest.TestCase):
    def test_replay_without_manifest_is_a_cache_miss_and_needs_no_zccache(self) -> None:
        with tempfile.TemporaryDirectory() as raw_ws, tempfile.TemporaryDirectory() as empty_path_dir:
            ws = pathlib.Path(raw_ws)
            with mock.patch.dict(os.environ, _isolated_env(empty_path_dir), clear=True):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = ci_build.main(["replay", "--workspace", str(ws)])
                self.assertEqual(rc, 0)
                self.assertIn("build-tree cache miss", buf.getvalue())


class ZccacheDiscoveryTest(unittest.TestCase):
    def test_require_zccache_raises_with_install_hint(self) -> None:
        with tempfile.TemporaryDirectory() as empty_path_dir:
            with mock.patch.dict(os.environ, _isolated_env(empty_path_dir), clear=True):
                with self.assertRaises(SystemExit) as ctx:
                    ci_build.require_zccache("build")
                message = str(ctx.exception)
                self.assertIn("zccache", message)
                self.assertIn("pip install zccache==1.14.11", message)

    def test_find_zccache_honours_env_override(self) -> None:
        with mock.patch.dict(
            os.environ, {"ZCCACHE": "uvx --from zccache==1.14.11 zccache"}
        ):
            result = ci_build.find_zccache()
            self.assertEqual(result, ["uvx", "--from", "zccache==1.14.11", "zccache"])


class ResolveLauncherTest(unittest.TestCase):
    def test_none_passes_through(self) -> None:
        self.assertEqual(ci_build.resolve_launcher("none"), "none")

    def test_sccache_missing_raises(self) -> None:
        with tempfile.TemporaryDirectory() as empty_path_dir:
            with mock.patch.dict(os.environ, _isolated_env(empty_path_dir), clear=True):
                with self.assertRaises(SystemExit):
                    ci_build.resolve_launcher("sccache")


def _first_available(*names: str) -> str | None:
    for name in names:
        if shutil.which(name):
            return name
    return None


_ZCCACHE_AVAILABLE = shutil.which("zccache") is not None
_CMAKE_AVAILABLE = shutil.which("cmake") is not None
_NINJA_AVAILABLE = shutil.which("ninja") is not None
_C_COMPILER = _first_available("clang", "cc")
_CXX_COMPILER = "clang++" if _C_COMPILER == "clang" else "c++"

_E2E_READY = bool(_ZCCACHE_AVAILABLE and _CMAKE_AVAILABLE and _NINJA_AVAILABLE and _C_COMPILER)
_E2E_SKIP_REASON = "requires zccache, cmake, ninja, and a C compiler on PATH"


@unittest.skipUnless(_E2E_READY, _E2E_SKIP_REASON)
class CiBuildEndToEndTest(unittest.TestCase):
    """The key regression: stamp-fresh + replay must not paper over real edits.

    Without a hash check behind stamp-fresh-then-replay, Ninja would trust a
    restored mtime, see the (older) source as not newer than its build
    products, and keep the stale object -- leaving the rebuilt binary still
    printing the old value even though the source changed. Step (f) below
    pins that a genuine edit is always caught, regardless of how old the new
    mtime is made to look.
    """

    def test_stale_source_after_snapshot_is_detected_and_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ws = pathlib.Path(raw_tmp)
            (ws / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.20)\n"
                "project(tiny C)\n"
                "add_executable(tiny main.c util.c)\n",
                encoding="utf-8",
            )
            (ws / "util.h").write_text("int util(void);\n", encoding="utf-8")
            (ws / "util.c").write_text(
                '#include "util.h"\nint util(void) { return 1; }\n', encoding="utf-8"
            )
            (ws / "main.c").write_text(
                '#include <stdio.h>\n#include "util.h"\n'
                'int main(void) { printf("%d\\n", util()); return 0; }\n',
                encoding="utf-8",
            )

            build_args = [
                "--launcher",
                "none",
                "--target",
                "tiny",
                "--no-trace",
                "--c-compiler",
                _C_COMPILER,
                "--cxx-compiler",
                _CXX_COMPILER,
            ]
            build_dir = ws / "build"
            tiny = build_dir / "tiny"

            # a) initial build: some work happens, and the binary runs.
            result = self._run("build", ws, *build_args)
            self.assertGreater(
                self._edges_from(result.stdout),
                0,
                f"expected edges > 0:\n{result.stdout}\n{result.stderr}",
            )
            self.assertEqual(self._run_tiny(tiny), "1")

            # b) snapshot: manifest records the sources, not the build tree.
            self._run("snapshot", ws)
            manifest = build_dir / "zccache-mtimes.json"
            self.assertTrue(manifest.is_file())
            manifest_text = manifest.read_text(encoding="utf-8")
            for expected in ("main.c", "util.c", "util.h", "CMakeLists.txt"):
                self.assertIn(expected, manifest_text)
            manifest_data = json.loads(manifest_text)
            for entry in manifest_data["entries"]:
                self.assertFalse(entry["path"].startswith("build/"), entry["path"])

            # c) simulate a fresh checkout: sources all look newer than the build tree.
            newest_build_mtime = max(
                p.stat().st_mtime for p in build_dir.rglob("*") if p.is_file()
            )
            fresh_ts = newest_build_mtime + 5
            for src in ("main.c", "util.c", "util.h", "CMakeLists.txt"):
                os.utime(ws / src, (fresh_ts, fresh_ts))

            # d) replay restores the recorded mtimes on the untouched sources.
            replay_result = self._run("replay", ws)
            report = self._parse_replay_line(replay_result.stdout)
            self.assertGreaterEqual(report["applied"], 4, replay_result.stdout)
            self.assertEqual(report["modified"], 0, replay_result.stdout)
            self.assertEqual(report["size_mismatch"], 0, replay_result.stdout)

            # e) rebuilding now sees no work at all.
            result = self._run("build", ws, *build_args, "--expect-no-work")
            self.assertIn("ci_build: ninja edges to run: 0", result.stdout)

            # f) KEY REGRESSION: a real edit, stamped *older* than the whole build
            # tree, must still be rebuilt. A naive stamp-fresh-then-replay (mtime
            # only, no content check) would leave Ninja looking at what appears to
            # be an old, unchanged source and keep the stale object -- the binary
            # would keep printing "1" even though util.c now returns 2.
            (ws / "util.c").write_text(
                '#include "util.h"\nint util(void) { return 2; }\n', encoding="utf-8"
            )
            oldest_build_mtime = min(
                p.stat().st_mtime for p in build_dir.rglob("*") if p.is_file()
            )
            old_ts = oldest_build_mtime - 3600
            os.utime(ws / "util.c", (old_ts, old_ts))

            replay_result = self._run("replay", ws)
            report = self._parse_replay_line(replay_result.stdout)
            self.assertGreaterEqual(
                report["modified"] + report["size_mismatch"], 1, replay_result.stdout
            )

            result = self._run("build", ws, *build_args)
            self.assertGreaterEqual(
                self._edges_from(result.stdout),
                1,
                f"expected edges >= 1:\n{result.stdout}\n{result.stderr}",
            )
            self.assertEqual(self._run_tiny(tiny), "2")

    @staticmethod
    def _run(sub: str, ws: pathlib.Path, *extra: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [sys.executable, str(CI_BUILD_PATH), sub, "--workspace", str(ws), *extra],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise AssertionError(
                f"ci_build.py {sub} failed (exit {exc.returncode}):\n"
                f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
            ) from exc

    @staticmethod
    def _run_tiny(tiny: pathlib.Path) -> str:
        result = subprocess.run([str(tiny)], capture_output=True, text=True)
        return result.stdout.strip()

    @staticmethod
    def _edges_from(stdout: str) -> int:
        marker = "ci_build: ninja edges to run: "
        for line in stdout.splitlines():
            if line.startswith(marker):
                return int(line[len(marker):].strip())
        raise AssertionError(f"no edges line found in:\n{stdout}")

    @staticmethod
    def _parse_replay_line(stdout: str) -> dict[str, float]:
        prefix = "ci_build: replay "
        for line in stdout.splitlines():
            if line.startswith(prefix):
                fields = dict(item.split("=", 1) for item in line[len(prefix):].split())
                return {
                    "applied": int(fields["applied"]),
                    "missing": int(fields["missing"]),
                    "size_mismatch": int(fields["size_mismatch"]),
                    "modified": int(fields["modified"]),
                    "total": int(fields["total"]),
                    "ratio": float(fields["ratio"]),
                }
        raise AssertionError(f"no replay summary line found in:\n{stdout}")


if __name__ == "__main__":
    unittest.main()
