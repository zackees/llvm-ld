#!/usr/bin/env python3
"""Tests for tools/bench_ci.py (#50): exact-input key and the floor evaluation.

Stdlib only. Run: python -m unittest discover -s tests -p 'test_bench_ci.py'.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bench_ci  # noqa: E402

FLOORS = json.loads((ROOT / "tools" / "bench_floors.json").read_text())


def cell(mode, corpus, variant, speedup, threads=4, noisy=False):
    spread = 0.5 if noisy else 0.01
    side = lambda ms: {"wall_ms": ms, "wall_ms_q1": ms * (1 - spread), "wall_ms_q3": ms * (1 + spread)}
    return {"mode": mode, "corpus": corpus, "variant": variant, "threads": threads,
            "speedup_percent": speedup, "speedup_percent_q1": speedup - 1, "speedup_percent_q3": speedup + 1,
            "baseline": side(100.0), "candidate": side(100.0 * (1 - speedup / 100))}


def healthy_cells():
    cells = []
    for mode in ("debug", "release"):
        for corpus in ("small", "medium", "large"):
            cells.append(cell(mode, corpus, "pdb", 35.0))
            cells.append(cell(mode, corpus, "nopdb", 12.0))
    cells += [cell("thinlto", "small", "pdb", 40.0), cell("thinlto", "small", "nopdb", 40.0)]
    return cells


def timings(warm=True, minutes=3.0):
    return [{"job": j, "seconds": minutes * 60, "warm": warm} for j in ("plain", "pgo-instr", "pgo-opt")] + \
           [{"job": "cells:debug", "seconds": minutes * 60, "warm": True}]


class FloorsTest(unittest.TestCase):
    def test_healthy_warm_run_passes(self):
        violations, report = bench_ci.evaluate(FLOORS, timings(), {"cells": healthy_cells()}, 12 * 60)
        self.assertEqual(violations, [], report)
        self.assertTrue(any("WARM" in line for line in report))

    def test_ci_time_floors_are_enforced_only_when_warm(self):
        slow = timings(minutes=60)
        warm_violations, _ = bench_ci.evaluate(FLOORS, slow, {"cells": healthy_cells()}, 130 * 60)
        self.assertTrue(any("warm pgo-opt" in v for v in warm_violations))
        self.assertTrue(any("warm run took" in v for v in warm_violations))
        cold = [dict(t, warm=False) if t["job"] == "pgo-opt" else t for t in slow]
        cold_violations, report = bench_ci.evaluate(FLOORS, cold, {"cells": healthy_cells()}, 130 * 60)
        self.assertEqual(cold_violations, [])
        self.assertTrue(any("not enforced" in line for line in report))

    def test_missing_build_timing_means_not_warm(self):
        partial = [t for t in timings(minutes=60) if t["job"] != "pgo-instr"]
        violations, report = bench_ci.evaluate(FLOORS, partial, {"cells": healthy_cells()}, 130 * 60)
        self.assertEqual(violations, [])
        self.assertTrue(any("missing timings: pgo-instr" in line for line in report))

    def test_link_speed_regression_fails_on_every_run(self):
        cells = healthy_cells()
        for c in cells:
            if c["mode"] == "release" and c["corpus"] == "large" and c["variant"] == "pdb":
                c["speedup_percent"] = 8.0
        violations, _ = bench_ci.evaluate(FLOORS, timings(warm=False), {"cells": cells}, None)
        self.assertEqual(violations, ["release/large/pdb/t4 speedup +8.0% < 20%"])

    def test_noisy_cells_are_skipped_but_counted(self):
        cells = healthy_cells()
        cells[0] = cell("debug", "small", "pdb", -50.0, noisy=True)
        violations, report = bench_ci.evaluate(FLOORS, timings(), {"cells": cells}, 10 * 60)
        self.assertEqual(violations, [])
        self.assertTrue(any("noisy, skipped" in line for line in report))
        too_noisy = [cell(c["mode"], c["corpus"], c["variant"], c["speedup_percent"], noisy=True) for c in cells]
        violations, _ = bench_ci.evaluate(FLOORS, timings(), {"cells": too_noisy}, 10 * 60)
        self.assertTrue(any("noisy cells" in v for v in violations))

    def test_only_each_modes_peak_thread_count_is_checked(self):
        cells = healthy_cells() + [cell("release", "large", "pdb", 1.0, threads=1)]
        violations, _ = bench_ci.evaluate(FLOORS, timings(), {"cells": cells}, 10 * 60)
        self.assertEqual(violations, [])


class KeyTest(unittest.TestCase):
    def test_key_changes_with_inputs_and_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for rel in bench_ci.PGO_KEY_PATHS:
                path = root / rel
                if "." in path.name:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(rel)
                else:
                    path.mkdir(parents=True, exist_ok=True)
                    (path / "a.txt").write_text(rel)
            base = bench_ci.pgo_key(root, ["clang 18"])
            self.assertEqual(base, bench_ci.pgo_key(root, ["clang 18"]))
            self.assertNotEqual(base, bench_ci.pgo_key(root, ["clang 19"]))
            (root / "src" / "a.txt").write_text("changed")
            self.assertNotEqual(base, bench_ci.pgo_key(root, ["clang 18"]))

    def test_key_ignores_files_outside_the_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "x.cpp").write_text("x")
            before = bench_ci.pgo_key(root, [])
            (root / "README.md").write_text("docs change")
            (root / "tools").mkdir()
            (root / "tools" / "bench_report.py").write_text("chart change")
            self.assertEqual(before, bench_ci.pgo_key(root, []))


if __name__ == "__main__":
    unittest.main()
