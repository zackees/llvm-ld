#!/usr/bin/env python3
"""Tests for tools/bench_report.py's disclosure of benchmark validity gaps.

Renders a sealed site from synthetic measurement cells shaped like the JSON
tests/perf/bench.py writes, then checks that the rendered index.html and SVG
panels disclose the thread cap, the Linux/glibc allocator and the unmeasured
Linux-to-Windows transfer -- and that the render still passes validate-site
with no rule loosened.

Stdlib only, no pytest. Run: python -m unittest discover -s tests -p
'test_bench_report.py'. Needs no network access and no built linker.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCH_REPORT = ROOT / "tools" / "bench_report.py"

BASELINE_REF = "f9cdd65cbfb5f64654ce428524a8126652bed8a7"
SOURCE_SHA = "a" * 40
ACTIONS_RUN_URL = "https://github.com/zackees/llvm-ld/actions/runs/1"
HISTORY_PANEL = "link-speedup-history.svg"


def make_cell(corpus: str, threads: int) -> dict:
    """Mirrors the cell JSON tests/perf/bench.py writes (see bench.py:98-118)."""
    gate_side = {
        "exe": "a" * 64,
        "pdb": "b" * 64,
        "exe_bytes": 1000,
        "pdb_bytes": 2000,
    }
    return {
        "corpus": f"/x/build-perf/corpus/{corpus}",
        "runs": 3,
        "threads": threads,
        "extra": [],
        "gate": {"candidate": dict(gate_side), "baseline": dict(gate_side)},
        "candidate": {"wall_ms": 100.0, "cpu_ms": 150.0, "rss_mb": 50.0},
        "baseline": {"wall_ms": 130.0, "cpu_ms": 160.0, "rss_mb": 49.0},
        "paired_wall_ratio_median": 0.77,
        "speedup_percent": 23.0,
        "rss_delta_percent": 2.0,
        "cpu_delta_percent": -6.0,
    }


def write_cells(cells_dir: pathlib.Path, threads_by_corpus: dict[str, list[int]]) -> None:
    for corpus, threads_values in threads_by_corpus.items():
        for threads in threads_values:
            path = cells_dir / f"{corpus}-t{threads}.json"
            path.write_text(json.dumps(make_cell(corpus, threads)), encoding="utf-8")


def render(
    tmp: pathlib.Path, threads_by_corpus: dict[str, list[int]]
) -> subprocess.CompletedProcess[str]:
    cells_dir = tmp / "cells"
    cells_dir.mkdir()
    write_cells(cells_dir, threads_by_corpus)
    history_in = tmp / "history.jsonl"
    history_in.write_text("", encoding="utf-8")
    site_dir = tmp / "site"
    digest_out = tmp / "manifest.sha256"
    return subprocess.run(
        [
            sys.executable,
            str(BENCH_REPORT),
            "render",
            "--cells-dir",
            str(cells_dir),
            "--history-in",
            str(history_in),
            "--output-dir",
            str(site_dir),
            "--detached-digest-out",
            str(digest_out),
            "--baseline-ref",
            BASELINE_REF,
            "--run-id",
            "1",
            "--run-attempt",
            "1",
            "--source-sha",
            SOURCE_SHA,
            "--actions-run-url",
            ACTIONS_RUN_URL,
            "--runner-cores",
            "4",
            "--runner-cpu",
            "TestCPU",
            "--initialize-history",
        ],
        capture_output=True,
        text=True,
    )


class BenchReportDisclosureTest(unittest.TestCase):
    def test_renders_and_discloses_validity_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            result = render(tmp, {"small": [1, 2, 4], "medium": [1, 2, 4]})
            self.assertEqual(
                result.returncode, 0, f"render failed: {result.stdout}\n{result.stderr}"
            )

            site_dir = tmp / "site"
            digest_out = tmp / "manifest.sha256"
            validated = subprocess.run(
                [
                    sys.executable,
                    str(BENCH_REPORT),
                    "validate-site",
                    "--site-dir",
                    str(site_dir),
                    "--detached-digest",
                    str(digest_out),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                validated.returncode,
                0,
                f"validate-site failed: {validated.stdout}\n{validated.stderr}",
            )

            index_html = (site_dir / "index.html").read_text(encoding="utf-8")
            for expected in (
                "glibc",
                "MI_MALLOC_OVERRIDE",
                "16 threads",
                "asserted, not measured",
                "Thread counts measured this run: 1, 2, 4",
            ):
                self.assertIn(expected, index_html)

            history_svg = (site_dir / HISTORY_PANEL).read_text(encoding="utf-8")
            self.assertIn("at 4 threads", history_svg)

    def test_history_panel_tracks_the_actual_peak_thread_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            result = render(tmp, {"small": [1, 2, 4, 16], "medium": [1, 2, 4, 16]})
            self.assertEqual(
                result.returncode, 0, f"render failed: {result.stdout}\n{result.stderr}"
            )

            site_dir = tmp / "site"
            index_html = (site_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("history panel tracks 16 threads", index_html)


if __name__ == "__main__":
    unittest.main()
