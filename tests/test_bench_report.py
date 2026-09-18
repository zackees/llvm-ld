#!/usr/bin/env python3
"""Tests for tools/bench_report.py: per-build-mode charts, sealing and disclosure.

Renders a sealed site from synthetic measurement cells shaped like the JSON
tests/perf/bench.py writes (one per cell of `gen_corpus.py --print-matrix`),
then checks the exact published file set, that every chart names its mode's
flags and note, that the dashboard discloses the validity gaps, and that the
render passes validate-site with no rule loosened. Negative tests cover the
inputs render must refuse.

Stdlib only, no pytest. Run: python -m unittest discover -s tests -p
'test_bench_*.py'. Needs no network access and no built linker.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCH_REPORT = ROOT / "tools" / "bench_report.py"
GEN_CORPUS = ROOT / "tests" / "perf" / "gen_corpus.py"

BASELINE_REF = "f9cdd65cbfb5f64654ce428524a8126652bed8a7"
SOURCE_SHA = "a" * 40
ACTIONS_RUN_URL = "https://github.com/zackees/llvm-ld/actions/runs/1"


def load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = load(GEN_CORPUS, "gen_corpus_under_test")
REPORT = load(BENCH_REPORT, "bench_report_under_test")


def matrix(max_threads: int = 4) -> list[tuple[str, str, int, int]]:
    out = subprocess.run(
        [sys.executable, str(GEN_CORPUS), "--print-matrix", "--max-threads", str(max_threads)],
        capture_output=True, text=True, check=True,
    ).stdout
    return [(m, p, int(t), int(r)) for m, p, t, r in (line.split() for line in out.splitlines())]


def make_cell(mode: str, corpus: str, threads: int, runs: int, speedup: float = 23.0) -> dict:
    """Mirrors the cell JSON tests/perf/bench.py writes."""
    has_pdb = GEN.MODES[mode]["pdb"]
    gate_side = {
        "exe": "a" * 64,
        "pdb": "b" * 64 if has_pdb else None,
        "exe_bytes": 1000,
        "pdb_bytes": 2000 if has_pdb else None,
    }
    ratio = 1 - speedup / 100
    return {
        "corpus": f"/x/build-perf/corpus/{mode}/{corpus}",
        "mode": mode,
        "runs": runs,
        "threads": threads,
        "extra": [f"/threads:{threads}"],
        "gate": {"candidate": dict(gate_side), "baseline": dict(gate_side)},
        "candidate": {"wall_ms": 100.0 * ratio, "wall_ms_q1": 99.0 * ratio, "wall_ms_q3": 102.0 * ratio,
                      "cpu_ms": 150.0, "rss_mb": 50.0},
        "baseline": {"wall_ms": 100.0, "wall_ms_q1": 98.0, "wall_ms_q3": 103.0, "cpu_ms": 160.0, "rss_mb": 49.0},
        "paired_wall_ratio_median": ratio,
        "paired_wall_ratio_q1": ratio - 0.01,
        "paired_wall_ratio_q3": ratio + 0.01,
        "speedup_percent": speedup,
        "speedup_percent_q1": speedup - 1,
        "speedup_percent_q3": speedup + 1,
        "rss_delta_percent": 2.0,
        "cpu_delta_percent": -6.0,
    }


def write_cells(cells_dir: pathlib.Path, cells: list[dict]) -> None:
    cells_dir.mkdir(parents=True, exist_ok=True)
    for cell in cells:
        name = f"{cell['mode']}-{pathlib.Path(cell['corpus']).name}-t{cell['threads']}.json"
        (cells_dir / name).write_text(json.dumps(cell), encoding="utf-8")


def full_cells(max_threads: int = 4) -> list[dict]:
    cells = []
    for mode, corpus, threads, runs in matrix(max_threads):
        # The control mode gets a negative cell so the hanging-bar path is rendered.
        speedup = -1.5 if not GEN.MODES[mode]["pdb"] and corpus == "small" else 23.0
        cells.append(make_cell(mode, corpus, threads, runs, speedup))
    return cells


def render(tmp: pathlib.Path, cells: list[dict], cores: str = "4") -> subprocess.CompletedProcess[str]:
    write_cells(tmp / "cells", cells)
    return subprocess.run(
        [
            sys.executable, str(BENCH_REPORT), "render",
            "--cells-dir", str(tmp / "cells"),
            "--output-dir", str(tmp / "site"),
            "--detached-digest-out", str(tmp / "manifest.sha256"),
            "--baseline-ref", BASELINE_REF,
            "--run-id", "1", "--run-attempt", "1",
            "--source-sha", SOURCE_SHA,
            "--actions-run-url", ACTIONS_RUN_URL,
            "--runner-cores", cores, "--runner-cpu", "TestCPU",
        ],
        capture_output=True, text=True,
    )


def validate(tmp: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BENCH_REPORT), "validate-site", "--site-dir", str(tmp / "site"),
         "--detached-digest", str(tmp / "manifest.sha256")],
        capture_output=True, text=True,
    )


class BenchReportRenderTest(unittest.TestCase):
    def test_renders_every_mode_and_passes_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            result = render(tmp, full_cells())
            self.assertEqual(result.returncode, 0, f"render failed: {result.stdout}\n{result.stderr}")
            checked = validate(tmp)
            self.assertEqual(checked.returncode, 0, f"validate-site failed: {checked.stdout}\n{checked.stderr}")

            site = tmp / "site"
            names = {path.name for path in site.iterdir()}
            self.assertEqual(names, REPORT.SITE_FILES)
            self.assertEqual(len(REPORT.SVG_FILES), 1 + len(GEN.MODES))
            self.assertFalse(any("light" in name for name in names))
            self.assertFalse(any("history" in name for name in names))

            for mode, spec in GEN.MODES.items():
                for theme in ("dark",):
                    svg = (site / f"link-speed-{mode}-{theme}.svg").read_text(encoding="utf-8")
                    self.assertIn(" ".join(spec["link_flags"]), svg)
                    self.assertIn(REPORT.escaped(spec["note"]), svg)
                    self.assertIn("<title>", svg)
            overview = (site / "link-speed-overview-dark.svg").read_text(encoding="utf-8")
            self.assertIn("medium, large not measured", overview)
            self.assertIn("-1.5%", overview)
            thinlto = (site / "link-speed-thinlto-dark.svg").read_text(encoding="utf-8")
            self.assertIn("not measured: ThinLTO codegen", thinlto)

            index_html = (site / "index.html").read_text(encoding="utf-8")
            for expected in (
                "link-speed-overview-dark.svg", "background:#0d1117", "glibc", "MI_MALLOC_OVERRIDE", "16 threads",
                "asserted, not measured", "Thread counts measured this run: 1, 2, 4",
                'id="release-nopdb"', "none",
            ):
                self.assertIn(expected, index_html)
            self.assertNotIn("history", index_html)

            latest = json.loads((site / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["schema_version"], "llvm-ld-link-latest-v2")
            self.assertEqual([m["id"] for m in latest["modes"]], list(GEN.MODES))
            self.assertTrue(all("mode" in cell for cell in latest["cells"]))
            nopdb = [cell for cell in latest["cells"] if cell["mode"] == "release-nopdb"]
            self.assertTrue(nopdb and all(cell["gate"]["pdb_sha256"] is None for cell in nopdb))

    def test_render_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for raw in (a, b):
                self.assertEqual(render(pathlib.Path(raw), full_cells()).returncode, 0)
            for name in sorted(REPORT.SVG_FILES | {"index.html"}):
                self.assertEqual(
                    (pathlib.Path(a) / "site" / name).read_bytes(),
                    (pathlib.Path(b) / "site" / name).read_bytes(),
                    name,
                )

    def test_overview_tracks_the_actual_peak_thread_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            result = render(tmp, full_cells(max_threads=16), cores="16")
            self.assertEqual(result.returncode, 0, f"render failed: {result.stdout}\n{result.stderr}")
            index_html = (tmp / "site" / "index.html").read_text(encoding="utf-8")
            self.assertIn("overview compares modes at 16 threads", index_html)
            overview = (tmp / "site" / "link-speed-overview-dark.svg").read_text(encoding="utf-8")
            self.assertIn("at 16 threads", overview)


class BenchReportRejectsTest(unittest.TestCase):
    def assert_render_fails(self, cells: list[dict], message: str) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            result = render(pathlib.Path(raw_tmp), cells)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)

    def test_missing_mode_fails(self) -> None:
        cells = [cell for cell in full_cells() if cell["mode"] != "debug"]
        self.assert_render_fails(cells, "no cells for build mode")

    def test_unknown_mode_fails(self) -> None:
        cells = full_cells()
        cells[0]["mode"] = "turbo"
        self.assert_render_fails(cells, "unknown or missing build mode")

    def test_too_few_runs_fails(self) -> None:
        cells = full_cells()
        cells[0]["runs"] = 3
        self.assert_render_fails(cells, "needs >= 4")

    def test_pdb_gate_must_match_mode(self) -> None:
        cells = full_cells()
        nopdb = next(cell for cell in cells if cell["mode"] == "release-nopdb")
        nopdb["gate"]["candidate"]["pdb"] = "c" * 64
        self.assert_render_fails(cells, "PDB gate does not match mode")

    def test_remote_srcset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            self.assertEqual(render(tmp, full_cells()).returncode, 0)
            index = tmp / "site" / "index.html"
            index.write_text(
                index.read_text(encoding="utf-8").replace(
                    "</body>", '<picture><source srcset="https://example.com/x.svg"></picture></body>'
                ),
                encoding="utf-8",
            )
            with self.assertRaises(REPORT.ReportError):
                REPORT.validate_html_links(index, tmp / "site")


class CorpusMatrixTest(unittest.TestCase):
    def test_matrix_covers_every_mode_and_respects_limits(self) -> None:
        cells = matrix(4)
        self.assertEqual({mode for mode, *_ in cells}, set(GEN.MODES))
        lto = [cell for cell in cells if GEN.MODES[cell[0]]["lto"]]
        self.assertTrue(lto and all(threads == 4 and runs == 5 for _, _, threads, runs in lto))
        self.assertEqual(len(cells), len(set(cells)))

    def test_smoke_matrix_is_small_only(self) -> None:
        out = subprocess.run(
            [sys.executable, str(GEN_CORPUS), "--print-matrix", "--max-threads", "4", "--smoke"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertTrue(out and all(line.split()[1] == "small" for line in out.splitlines()))


if __name__ == "__main__":
    unittest.main()
