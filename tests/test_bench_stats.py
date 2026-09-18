#!/usr/bin/env python3
"""Tests for tests/perf/bench.py's paired statistics (summarize).

Stdlib only. Run: python -m unittest discover -s tests -p 'test_bench_*.py'.
"""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bench_under_test", ROOT / "tests" / "perf" / "bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def samples(walls_ms: list[float]) -> list[dict]:
    return [dict(wall=w / 1000, cpu=w / 500, rss=100e6) for w in walls_ms]


class SummarizeTest(unittest.TestCase):
    def test_medians_quartiles_and_paired_identity(self) -> None:
        candidate = samples([70, 80, 75, 90, 60])
        baseline = samples([100, 100, 100, 100, 100])
        result = bench.summarize(candidate, baseline)
        self.assertAlmostEqual(result["candidate"]["wall_ms"], 75.0)
        # statistics.quantiles(..., n=4, method="inclusive") on 60,70,75,80,90.
        self.assertAlmostEqual(result["candidate"]["wall_ms_q1"], 70.0)
        self.assertAlmostEqual(result["candidate"]["wall_ms_q3"], 80.0)
        self.assertAlmostEqual(result["paired_wall_ratio_median"], 0.75)
        self.assertAlmostEqual(result["speedup_percent"], 100 * (1 - result["paired_wall_ratio_median"]))
        # A larger ratio is a smaller speedup, so the speedup's q1 comes from the ratio's q3.
        self.assertAlmostEqual(result["speedup_percent_q1"], 100 * (1 - result["paired_wall_ratio_q3"]))
        self.assertAlmostEqual(result["speedup_percent_q3"], 100 * (1 - result["paired_wall_ratio_q1"]))
        self.assertLessEqual(result["speedup_percent_q1"], result["speedup_percent"])
        self.assertLessEqual(result["speedup_percent"], result["speedup_percent_q3"])
        self.assertAlmostEqual(result["cpu_delta_percent"], -25.0)
        self.assertAlmostEqual(result["rss_delta_percent"], 0.0)

    def test_needs_four_pairs(self) -> None:
        with self.assertRaises(ValueError):
            bench.summarize(samples([1, 2, 3]), samples([1, 2, 3]))


if __name__ == "__main__":
    unittest.main()
