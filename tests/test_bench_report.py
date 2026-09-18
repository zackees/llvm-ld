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


def matrix(max_threads: int = 4) -> list[tuple[str, str, int, int, str]]:
    out = subprocess.run(
        [sys.executable, str(GEN_CORPUS), "--print-matrix", "--max-threads", str(max_threads)],
        capture_output=True, text=True, check=True,
    ).stdout
    return [(m, p, int(t), int(r), v) for m, p, t, r, v in (line.split() for line in out.splitlines())]


def make_cell(mode: str, corpus: str, threads: int, runs: int, variant: str,
              speedup: float = 23.0, base_ms: float = 100.0) -> dict:
    """Mirrors the cell JSON tests/perf/bench.py writes."""
    has_pdb = GEN.VARIANTS[variant]["pdb"]
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
        "variant": variant,
        "runs": runs,
        "threads": threads,
        "extra": [f"/threads:{threads}"],
        "gate": {"candidate": dict(gate_side), "baseline": dict(gate_side)},
        "candidate": {"wall_ms": base_ms * ratio, "wall_ms_q1": base_ms * 0.99 * ratio,
                      "wall_ms_q3": base_ms * 1.02 * ratio, "cpu_ms": 150.0, "rss_mb": 50.0},
        "baseline": {"wall_ms": base_ms, "wall_ms_q1": base_ms * 0.98, "wall_ms_q3": base_ms * 1.03,
                     "cpu_ms": 160.0, "rss_mb": 49.0},
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
        name = f"{cell['mode']}-{pathlib.Path(cell['corpus']).name}-t{cell['threads']}-{cell['variant']}.json"
        (cells_dir / name).write_text(json.dumps(cell), encoding="utf-8")


def full_cells(max_threads: int = 4) -> list[dict]:
    """The PDB adds 300 ms (well clear of noise) except for LTO, where it adds 1%
    (inside the IQR), so both the stacked and the within-noise paths render."""
    cells = []
    for mode, corpus, threads, runs, variant in matrix(max_threads):
        lto = GEN.MODES[mode]["lto"]
        if variant == "nopdb":
            cells.append(make_cell(mode, corpus, threads, runs, variant, speedup=10.0,
                                   base_ms=10000.0 if lto else 100.0))
        else:
            cells.append(make_cell(mode, corpus, threads, runs, variant, speedup=35.0,
                                   base_ms=10100.0 if lto else 400.0))
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
    def test_renders_the_chart_and_passes_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            result = render(tmp, full_cells())
            self.assertEqual(result.returncode, 0, f"render failed: {result.stdout}\n{result.stderr}")
            checked = validate(tmp)
            self.assertEqual(checked.returncode, 0, f"validate-site failed: {checked.stdout}\n{checked.stderr}")

            site = tmp / "site"
            names = {path.name for path in site.iterdir()}
            self.assertEqual(names, REPORT.SITE_FILES)
            # One chart, under the name the README already hotlinks.
            self.assertEqual(REPORT.SVG_FILES, {"link-speed-overview-dark.svg"})

            chart = (site / "link-speed-overview-dark.svg").read_text(encoding="utf-8")
            for spec in GEN.MODES.values():
                self.assertIn(f">{spec['label']}<", chart)
            for expected in (
                "<title>", "<pattern", 'stroke-dasharray="3 2"', "stock lld-link", "llvm-ld",
                "+ PDB (extra time for /debug:full)", "#1f6feb", "#a5d6ff",
                # 400 -> 260 ms with the PDB, 100 -> 90 ms without: PDB 300 -> 170 ms.
                "PDB 300 ms → 170 ms (-43%) · link -10%",
                "PDB cost within noise: codegen dominates",
                "not measured: ThinLTO codegen is too slow",
            ):
                self.assertIn(expected, chart)
            # The legend comes before the grid.
            self.assertLess(chart.index("+ PDB (extra time"), chart.index(REPORT.CORPUS_LABELS["small"]))

            index_html = (site / "index.html").read_text(encoding="utf-8")
            for expected in (
                "link-speed-overview-dark.svg", "background:#0d1117", "glibc", "MI_MALLOC_OVERRIDE", "16 threads",
                "asserted, not measured", "Thread counts measured this run: 1, 2, 4", "/debug:full", "none",
            ):
                self.assertIn(expected, index_html)

            latest = json.loads((site / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["schema_version"], "llvm-ld-link-latest-v3")
            self.assertEqual([m["id"] for m in latest["modes"]], list(GEN.MODES))
            self.assertEqual([v["id"] for v in latest["variants"]], list(GEN.VARIANTS))
            nopdb = [cell for cell in latest["cells"] if cell["variant"] == "nopdb"]
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

    def test_chart_tracks_the_actual_peak_thread_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            result = render(tmp, full_cells(max_threads=16), cores="16")
            self.assertEqual(result.returncode, 0, f"render failed: {result.stdout}\n{result.stderr}")
            index_html = (tmp / "site" / "index.html").read_text(encoding="utf-8")
            self.assertIn("16 threads here", index_html)
            chart = (tmp / "site" / "link-speed-overview-dark.svg").read_text(encoding="utf-8")
            self.assertIn("16 threads (each build type", chart)


    def test_link_change_inside_noise_is_not_quoted(self) -> None:
        cells = full_cells()
        for cell in cells:
            if cell["mode"] == "release" and cell["variant"] == "nopdb" and cell["corpus"].endswith("/medium"):
                cell["speedup_percent"], cell["speedup_percent_q1"], cell["speedup_percent_q3"] = -158.0, -170.0, 65.0
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            self.assertEqual(render(tmp, cells).returncode, 0)
            chart = (tmp / "site" / "link-speed-overview-dark.svg").read_text(encoding="utf-8")
            self.assertIn("link within noise", chart)
            self.assertNotIn("+158%", chart)


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

    def test_unknown_variant_fails(self) -> None:
        cells = full_cells()
        cells[0]["variant"] = "halfpdb"
        self.assert_render_fails(cells, "unknown or missing link variant")

    def test_unpaired_variant_fails(self) -> None:
        cells = [cell for cell in full_cells()
                 if not (cell["mode"] == "release" and cell["variant"] == "nopdb" and cell["threads"] == 2)]
        self.assert_render_fails(cells, "has no 'nopdb' cell")

    def test_too_few_runs_fails(self) -> None:
        cells = full_cells()
        cells[0]["runs"] = 3
        self.assert_render_fails(cells, "needs >= 4")

    def test_pdb_gate_must_match_variant(self) -> None:
        cells = full_cells()
        nopdb = next(cell for cell in cells if cell["variant"] == "nopdb")
        nopdb["gate"]["candidate"]["pdb"] = "c" * 64
        self.assert_render_fails(cells, "PDB gate does not match variant")

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
        self.assertTrue(lto and all(threads == 4 and runs == 5 for _, _, threads, runs, _ in lto))
        self.assertEqual(len(cells), len(set(cells)))
        # Every measured point is linked in both variants.
        points = {}
        for mode, corpus, threads, _, variant in cells:
            points.setdefault((mode, corpus, threads), set()).add(variant)
        self.assertTrue(all(variants == set(GEN.VARIANTS) for variants in points.values()))

    def test_smoke_matrix_is_small_only(self) -> None:
        out = subprocess.run(
            [sys.executable, str(GEN_CORPUS), "--print-matrix", "--max-threads", "4", "--smoke"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertTrue(out and all(line.split()[1] == "small" for line in out.splitlines()))


if __name__ == "__main__":
    unittest.main()
