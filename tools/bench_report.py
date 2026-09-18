#!/usr/bin/env python3
"""Render, seal and publish the llvm-ld link-speed benchmark site.

The published site is a flat directory of exactly SITE_FILES, pushed to the
`benchmark-stats` data branch and deployed to GitHub Pages. The README hotlinks
the SVG panels straight off that branch, so the panels must stay small, legible
at any scale, and provably inert (no script, no external fetch).

What is measured, and why it is a paired A/B rather than a trend of absolute
times: every run builds the current linker and a baseline linker from the
payload as it stood before the link-speed patches (only six files differ, so
the baseline is an incremental rebuild), then links the same corpora with both
binaries interleaved on the same runner. Hosted runners are shared and noisy;
absolute wall time across runs would encode that noise as signal, and it
already produced two wrong conclusions during the optimization work. A paired
ratio measured in one run on one machine does not.

Every published number is gated on byte-identical output: tests/perf/bench.py
refuses to report a timing unless the candidate's EXE and PDB match the
baseline's exactly and both are self-deterministic. A speedup that changes
output bytes is a bug, not a result.

The rendered dashboard also discloses three validity gaps up front, rather
than leaving a reader to infer them: the thread cap of the hosted runner
relative to the historical headline number, the fact that the measured
allocator is glibc malloc on Linux rather than the mimalloc the shipped
Windows build actually uses, and that the Linux-to-Windows transfer of the
relative speedup is asserted, not measured.

What is measured is chosen per build mode (Debug + PDB, Release + PDB, Release
without a PDB as a control, ThinLTO + PDB), defined once in
tests/perf/gen_corpus.py:MODES and imported here. The site shows only the
current run: an overview chart comparing the modes and one paired-time chart
per mode, all in one dark theme.

Subcommands
  render            build the sealed site from measurement cells
  validate-site     re-check a site directory against the manifest and allowlist
  prepare-branch    stage exactly the site into a linked worktree for commit
  validate-revision prove a committed revision matches the sealed site byte for byte
  audit-pages       prove the deployed Pages payload matches the sealed site

Stdlib only, by design: this runs in CI with no pip install step, and a chart
renderer that cannot pull a dependency cannot be broken by one.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

LATEST_SCHEMA = "llvm-ld-link-latest-v2"
MANIFEST_SCHEMA = "llvm-ld-link-site-manifest-v2"
STATISTICS_VERSION = "paired-interleaved-median-iqr-v1"


def _load_generator() -> Any:
    """tests/perf/gen_corpus.py owns the build modes and corpus profiles.

    Importing it (rather than mirroring its tables here) keeps one source of
    truth for what each mode compiles and links; the workflow's measurement
    loops read the same tables through `gen_corpus.py --print-matrix`.
    """
    path = Path(__file__).resolve().parents[1] / "tests" / "perf" / "gen_corpus.py"
    spec = importlib.util.spec_from_file_location("llvm_ld_gen_corpus", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{path}: cannot load the corpus generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GENERATOR = _load_generator()
# Mode ids in publication order (the generator's dict order).
MODES: dict[str, dict] = _GENERATOR.MODES
MODE_IDS = tuple(MODES)

# Corpus ids in publication order. The generator profiles live in
# tests/perf/gen_corpus.py; these are the ones the workflow measures.
CORPUS_IDS = ("small", "medium", "large")
CORPUS_LABELS = {
    corpus: f"{corpus} · {_GENERATOR.PROFILES[corpus]['tus']} objects" for corpus in CORPUS_IDS
}

# One dark theme, always (the README shows the dark charts in either GitHub
# theme). The "-dark" file-name suffix is kept so published URLs stay stable.
THEMES = ("dark",)


def overview_panel_name(theme: str) -> str:
    return f"link-speed-overview-{theme}.svg"


def mode_panel_name(mode: str, theme: str) -> str:
    return f"link-speed-{mode}-{theme}.svg"


SVG_ROLES = {overview_panel_name(theme): f"speed-overview-panel-{theme}" for theme in THEMES}
SVG_ROLES.update(
    {
        mode_panel_name(mode, theme): f"speed-mode-panel-{mode}-{theme}"
        for mode in MODE_IDS
        for theme in THEMES
    }
)
SVG_FILES = frozenset(SVG_ROLES)

SITE_FILES = {".nojekyll", "index.html", "latest.json", "manifest.json"} | SVG_FILES

# Byte ceilings, enforced at render and re-enforced at validation. `.nojekyll`
# must be exactly empty, which the zero cap encodes.
FILE_CAPS = {
    ".nojekyll": 0,
    "index.html": 2 * 1024 * 1024,
    "latest.json": 32 * 1024 * 1024,
    "manifest.json": 1024 * 1024,
    **{name: 1024 * 1024 for name in SVG_FILES},
}

# manifest.json is deliberately absent from both maps below: it describes the
# other files and cannot describe itself.
MEDIA_TYPES = {
    ".nojekyll": "application/octet-stream",
    "index.html": "text/html; charset=utf-8",
    "latest.json": "application/json",
    **{name: "image/svg+xml" for name in SVG_FILES},
}
ROLES = {
    ".nojekyll": "github-pages-marker",
    "index.html": "report-index",
    "latest.json": "validated-latest-data",
    **SVG_ROLES,
}

MIN_RUNS = 4
HEX_64 = re.compile(r"\A[0-9a-f]{64}\Z")


class ReportError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise ReportError(message)


# ---------------------------------------------------------------- primitives


def compact_json(value: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, no spaces, trailing newline.

    Rendering has to be byte-reproducible or the manifest digest is meaningless.
    """
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return (text + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def escaped(value: object) -> str:
    return html.escape(str(value), quote=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_command(repository: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, check=False
    )
    if result.returncode != 0:
        fail(f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


# ------------------------------------------------------------ SVG rendering
#
# Hand-written SVG, fixed pixel layout, one dark theme. The panels are hotlinked from the raw
# data-branch URL, so they must carry no script, no external reference and no
# event handler; validate_svg enforces it.
#
# Every bar is directly labelled: a hotlinked SVG has no hover layer, so the
# labels are the only value channel in the README. The dashboard table is the
# accessible twin.

# The dark panel palette of zackees/mimalloc-pprof (ci/benchmark_report.py,
# SCALING_INK / SCALING_SERIES), so both projects' charts read as one system.
# There, the project's own allocator is blue and upstream mimalloc is green;
# here llvm-ld is the same blue and stock (upstream) lld-link the same green.
# Corpus size is ordinal, so the overview uses a light-to-dark ramp around
# that blue.
PALETTES = {
    "dark": {
        "background": "#0d1117",
        "plot": "#111823",
        "grid": "#1f2937",
        "axis": "#8b98ad",
        "title": "#e8eef7",
        "muted": "#7d8da5",
        "baseline": "#3fb950",
        "candidate": "#58a6ff",
        "ramp": ("#a5d6ff", "#58a6ff", "#1f6feb"),
    },
}

FONT_STACK = "system-ui,-apple-system,Segoe UI,Roboto,sans-serif"
# Approximate advance per character at 12px for the manual legend flow. The
# only text-metric assumption in the file; keep labels short.
CHAR_ADVANCE_12PX = 6.9

WIDTH = 1000
OVERVIEW_HEIGHT = 560
MODE_HEIGHT = 460


def svg_text(
    x: float,
    y: float,
    value: str,
    *,
    fill: str,
    size: float = 13,
    weight: str = "normal",
    anchor: str = "start",
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size:.0f}" '
        f'font-family="{FONT_STACK}" font-weight="{weight}" text-anchor="{anchor}">'
        f"{escaped(value)}</text>"
    )


def nice_ceiling(peak: float) -> float:
    """Round a positive axis maximum up to a readable step."""
    if peak <= 0:
        return 10.0
    magnitude = 10 ** math.floor(math.log10(peak))
    for factor in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        candidate = magnitude * factor
        if candidate >= peak:
            return float(candidate)
    return float(magnitude * 10)


def svg_open(width: int, height: int, title: str, ink: dict) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img">',
        f"<title>{escaped(title)}</title>",
        f'<rect width="{width}" height="{height}" fill="{ink["background"]}"/>',
    ]


def svg_close(parts: list[str]) -> bytes:
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def fmt_percent(value: float) -> str:
    """`+43%`, or one decimal for small values so a control row does not read as a flat 0."""
    if abs(value) < 10:
        return f"{value:+.1f}%"
    return f"{value:+.0f}%"


def fmt_ms(value: float) -> str:
    """Compact enough to sit over a 22px bar: `656`, `1163`, `12.1s`."""
    if value >= 10000:
        return f"{value / 1000:.1f}s"
    if value >= 100:
        return f"{value:.0f}"
    return f"{value:.1f}"


def fmt_axis_ms(value: float) -> str:
    if value == 0:
        return "0"
    if value >= 10000:
        return f"{value / 1000:g} s"
    return f"{value:,.0f} ms" if value >= 10 else f"{value:g} ms"


def draw_bar(parts: list[str], x: float, y_zero: float, y_end: float, width: float, color: str) -> None:
    """A bar from the zero line to y_end, rounded (4px) at the data end only."""
    height = abs(y_end - y_zero)
    if height < 0.5:
        parts.append(
            f'<rect x="{x:.1f}" y="{y_zero - 0.5:.1f}" width="{width:.1f}" height="1" fill="{color}"/>'
        )
        return
    radius = min(4.0, height, width / 2)
    right = x + width
    if y_end < y_zero:  # grows up
        path = (
            f"M{x:.1f} {y_zero:.1f}V{y_end + radius:.1f}"
            f"Q{x:.1f} {y_end:.1f} {x + radius:.1f} {y_end:.1f}"
            f"H{right - radius:.1f}Q{right:.1f} {y_end:.1f} {right:.1f} {y_end + radius:.1f}"
            f"V{y_zero:.1f}Z"
        )
    else:  # hangs below the zero line
        path = (
            f"M{x:.1f} {y_zero:.1f}V{y_end - radius:.1f}"
            f"Q{x:.1f} {y_end:.1f} {x + radius:.1f} {y_end:.1f}"
            f"H{right - radius:.1f}Q{right:.1f} {y_end:.1f} {right:.1f} {y_end - radius:.1f}"
            f"V{y_zero:.1f}Z"
        )
    parts.append(f'<path d="{path}" fill="{color}"/>')


def draw_whisker(parts: list[str], x: float, y_low: float, y_high: float, color: str) -> None:
    """Interquartile range: a 1px stem with 4px caps."""
    top, bottom = min(y_low, y_high), max(y_low, y_high)
    parts.append(
        f'<path d="M{x:.1f} {top:.1f}V{bottom:.1f}M{x - 4:.1f} {top:.1f}H{x + 4:.1f}'
        f'M{x - 4:.1f} {bottom:.1f}H{x + 4:.1f}" stroke="{color}" stroke-width="1" fill="none"/>'
    )


def draw_legend(
    parts: list[str], items: list[tuple[str, str]], x: float, y: float, ink: dict
) -> float:
    """Swatches with labels in a row; returns the x after the last item."""
    cursor = x
    for label, color in items:
        parts.append(
            f'<rect x="{cursor:.1f}" y="{y - 10:.1f}" width="12" height="12" rx="2" fill="{color}"/>'
        )
        parts.append(svg_text(cursor + 18, y, label, fill=ink["title"], size=12))
        cursor += 18 + CHAR_ADVANCE_12PX * len(label) + 22
    return cursor


def provenance_line(latest: dict) -> str:
    runner = latest["runner"]
    cores = f", {runner['cores']} cores" if runner["cores"] else ""
    return (
        f"run {latest['run']['run_id']} · source {latest['run']['source_sha'][:12]} · "
        f"{runner['cpu'] or 'unknown CPU'}{cores} · Linux, glibc malloc"
    )


def peak_threads_of(cells: list[dict]) -> int:
    return max(int(cell["threads"]) for cell in cells)


def mode_flags_text(mode: str) -> str:
    spec = MODES[mode]
    return f"objects: clang {' '.join(spec['cflags'])} · link: {' '.join(spec['link_flags'])}"


def overview_panel_svg(latest: dict, theme: str) -> bytes:
    """% less wall time per build mode at the highest measured thread count.

    One group per mode, one bar per corpus (ordinal ramp small -> large), each
    labelled with the percent and the paired times it came from.
    """
    ink = PALETTES[theme]
    cells = latest["cells"]
    peak = peak_threads_of(cells)
    title = "Link time saved by llvm-ld, by build mode"
    parts = svg_open(WIDTH, OVERVIEW_HEIGHT, title, ink)
    left, right, top, bottom = 84.0, 24.0, 118.0, 400.0
    plot_width = WIDTH - left - right
    plot_height = bottom - top

    # One bar per (mode, corpus): the cell at that mode's highest thread count.
    chosen: dict[str, list[dict]] = {}
    for mode in MODE_IDS:
        mode_cells = [cell for cell in cells if cell["mode"] == mode]
        top_threads = peak_threads_of(mode_cells)
        chosen[mode] = [cell for cell in mode_cells if int(cell["threads"]) == top_threads]
    values = [
        value
        for group in chosen.values()
        for cell in group
        for value in (cell["speedup_percent"], cell["speedup_percent_q1"], cell["speedup_percent_q3"])
    ]
    # Ticks on one readable step; room above the tallest bar for its two label lines.
    step = nice_ceiling(max(max(values + [0.0]) * 1.25, 4.0) / 4)
    ceiling = step * max(1, math.ceil(max(values + [0.0]) * 1.25 / step))
    trough = min(values + [0.0])
    floor = -step * math.ceil(-trough / step) if trough < 0 else 0.0

    def y_of(value: float) -> float:
        return top + (ceiling - value) / (ceiling - floor) * plot_height

    parts.append(svg_text(left - 36, 40, title, fill=ink["title"], size=20, weight="600"))
    parts.append(
        svg_text(
            left - 36,
            62,
            "vs. stock lld-link: the LLVM 23.1.0 payload before the patches · same runner, "
            "interleaved · byte-identical output",
            fill=ink["muted"],
            size=13,
        )
    )
    parts.append(
        svg_text(
            left - 36,
            80,
            f"paired median at {peak} threads (each mode's highest measured) · "
            "whiskers = interquartile range · higher is better",
            fill=ink["muted"],
            size=13,
        )
    )
    parts.append(
        f'<rect x="{left:.1f}" y="{top:.1f}" width="{plot_width:.1f}" height="{plot_height:.1f}" '
        f'fill="{ink["plot"]}" rx="6"/>'
    )
    for tick in range(round((ceiling - floor) / step) + 1):
        value = floor + step * tick
        y = y_of(value)
        parts.append(
            f'<line x1="{left:.1f}" y1="{y:.1f}" x2="{left + plot_width:.1f}" y2="{y:.1f}" '
            f'stroke="{ink["grid"]}" stroke-width="1"/>'
        )
        parts.append(svg_text(left - 10, y + 4, f"{value:g}%", fill=ink["axis"], size=12, anchor="end"))
    zero = y_of(0.0)
    parts.append(
        f'<line x1="{left:.1f}" y1="{zero:.1f}" x2="{left + plot_width:.1f}" y2="{zero:.1f}" '
        f'stroke="{ink["muted"]}" stroke-width="1"/>'
    )
    parts.append(svg_text(left, top - 10, "% less wall time (paired median)", fill=ink["muted"], size=11))

    group_width = plot_width / len(MODE_IDS)
    slot = 76.0
    bar_width = 24.0
    for index, mode in enumerate(MODE_IDS):
        centre = left + group_width * (index + 0.5)
        group = sorted(chosen[mode], key=lambda cell: CORPUS_IDS.index(cell["corpus"]))
        start = centre - slot * len(group) / 2
        for position, cell in enumerate(group):
            x_centre = start + slot * (position + 0.5)
            color = ink["ramp"][CORPUS_IDS.index(cell["corpus"])]
            value = cell["speedup_percent"]
            draw_bar(parts, x_centre - bar_width / 2, zero, y_of(value), bar_width, color)
            y_low, y_high = y_of(cell["speedup_percent_q1"]), y_of(cell["speedup_percent_q3"])
            draw_whisker(parts, x_centre, y_low, y_high, ink["muted"])
            pair = f"{fmt_ms(cell['baseline']['wall_ms'])}→{fmt_ms(cell['candidate']['wall_ms'])} ms"
            # Labels always sit above the bar, its whisker and the zero line, so a
            # negative (hanging) bar never pushes them into the group labels.
            label_top = min(y_of(value), y_high, y_low, zero) - 20
            parts.append(svg_text(x_centre, label_top, fmt_percent(value), fill=ink["title"], size=13, weight="700", anchor="middle"))
            parts.append(svg_text(x_centre, label_top + 14, pair, fill=ink["muted"], size=11, anchor="middle"))
        parts.append(svg_text(centre, bottom + 24, MODES[mode]["label"], fill=ink["title"], size=13, weight="700", anchor="middle"))
        spec = MODES[mode]
        parts.append(
            svg_text(centre, bottom + 40, " ".join(spec["cflags"]), fill=ink["muted"], size=11, anchor="middle")
        )
        parts.append(
            svg_text(centre, bottom + 54, " ".join(spec["link_flags"]), fill=ink["muted"], size=11, anchor="middle")
        )
        missing = [corpus for corpus in CORPUS_IDS if corpus not in {cell["corpus"] for cell in group}]
        if missing:
            parts.append(
                svg_text(centre, bottom + 70, f"{', '.join(missing)} not measured", fill=ink["muted"], size=11, anchor="middle")
            )
        elif not spec["pdb"]:
            parts.append(svg_text(centre, bottom + 70, "control: no PDB, expect ~0%", fill=ink["muted"], size=11, anchor="middle"))

    draw_legend(
        parts,
        [(CORPUS_LABELS[corpus], ink["ramp"][i]) for i, corpus in enumerate(CORPUS_IDS)],
        left - 36,
        OVERVIEW_HEIGHT - 44,
        ink,
    )
    parts.append(svg_text(left - 36, OVERVIEW_HEIGHT - 18, provenance_line(latest), fill=ink["muted"], size=11))
    return svg_close(parts)


def mode_panel_svg(latest: dict, mode: str, theme: str) -> bytes:
    """Paired link time (stock lld vs llvm-ld) per corpus facet and thread count."""
    ink = PALETTES[theme]
    spec = MODES[mode]
    cells = [cell for cell in latest["cells"] if cell["mode"] == mode]
    title = spec["label"]
    parts = svg_open(WIDTH, MODE_HEIGHT, f"{title}: link time, stock lld-link vs llvm-ld", ink)
    parts.append(svg_text(24, 36, title, fill=ink["title"], size=20, weight="600"))
    parts.append(svg_text(24, 58, mode_flags_text(mode), fill=ink["title"], size=12))
    parts.append(svg_text(24, 76, spec["note"], fill=ink["muted"], size=12))

    facet_top, facet_bottom = 132.0, 330.0
    facet_gap = 18.0
    outer_left, outer_right = 24.0, 24.0
    facet_width = (WIDTH - outer_left - outer_right - 2 * facet_gap) / 3
    bar_width, bar_gap = 22.0, 8.0
    for index, corpus in enumerate(CORPUS_IDS):
        fx = outer_left + index * (facet_width + facet_gap)
        axis_left = fx + 58
        plot_width = fx + facet_width - axis_left
        plot_height = facet_bottom - facet_top
        parts.append(svg_text(fx, facet_top - 18, CORPUS_LABELS[corpus], fill=ink["title"], size=13, weight="700"))
        parts.append(
            f'<rect x="{axis_left:.1f}" y="{facet_top:.1f}" width="{plot_width:.1f}" '
            f'height="{plot_height:.1f}" fill="{ink["plot"]}" rx="6"/>'
        )
        facet_cells = sorted(
            (cell for cell in cells if cell["corpus"] == corpus), key=lambda cell: int(cell["threads"])
        )
        if not facet_cells:
            parts.append(
                svg_text(
                    axis_left + plot_width / 2,
                    facet_top + plot_height / 2,
                    "not measured" + (": ThinLTO codegen" if spec["lto"] else ""),
                    fill=ink["muted"],
                    size=12,
                    anchor="middle",
                )
            )
            continue
        peak_ms = max(
            max(cell["baseline"]["wall_ms_q3"], cell["candidate"]["wall_ms_q3"], cell["baseline"]["wall_ms"])
            for cell in facet_cells
        )
        ceiling = nice_ceiling(peak_ms * 1.25)

        def y_of(value: float, ceiling: float = ceiling) -> float:
            return facet_bottom - value / ceiling * plot_height

        for step in range(3):
            value = ceiling * step / 2
            y = y_of(value)
            parts.append(
                f'<line x1="{axis_left:.1f}" y1="{y:.1f}" x2="{axis_left + plot_width:.1f}" '
                f'y2="{y:.1f}" stroke="{ink["grid"]}" stroke-width="1"/>'
            )
            parts.append(svg_text(axis_left - 6, y + 4, fmt_axis_ms(value), fill=ink["axis"], size=11, anchor="end"))
        slot = plot_width / len(facet_cells)
        for position, cell in enumerate(facet_cells):
            centre = axis_left + slot * (position + 0.5)
            label_tops = []
            for side, offset in (("baseline", -(bar_gap + bar_width) / 2), ("candidate", (bar_gap + bar_width) / 2)):
                data = cell[side]
                x_centre = centre + offset
                color = ink["baseline"] if side == "baseline" else ink["candidate"]
                draw_bar(parts, x_centre - bar_width / 2, facet_bottom, y_of(data["wall_ms"]), bar_width, color)
                draw_whisker(parts, x_centre, y_of(data["wall_ms_q1"]), y_of(data["wall_ms_q3"]), ink["muted"])
                label_y = min(y_of(data["wall_ms"]), y_of(data["wall_ms_q3"])) - 6
                label_tops.append(label_y)
                parts.append(svg_text(x_centre, label_y, fmt_ms(data["wall_ms"]), fill=ink["muted"], size=11, anchor="middle"))
            parts.append(
                svg_text(centre, min(label_tops) - 16, fmt_percent(cell["speedup_percent"]), fill=ink["title"], size=13, weight="700", anchor="middle")
            )
            threads = int(cell["threads"])
            parts.append(
                svg_text(centre, facet_bottom + 18, f"{threads} thread{'s' if threads != 1 else ''}", fill=ink["muted"], size=11, anchor="middle")
            )
        top_cell = facet_cells[-1]
        parts.append(
            svg_text(
                axis_left + plot_width / 2,
                facet_bottom + 38,
                f"at {int(top_cell['threads'])} threads: CPU {top_cell['cpu_delta_percent']:+.0f}% · "
                f"peak RSS {top_cell['rss_delta_percent']:+.1f}%",
                fill=ink["muted"],
                size=11,
                anchor="middle",
            )
        )

    runs = sorted({int(cell["runs"]) for cell in cells})
    runs_text = "/".join(str(value) for value in runs)
    end = draw_legend(
        parts,
        [("stock lld-link (LLVM 23.1.0, before the patches)", ink["baseline"]), ("llvm-ld", ink["candidate"])],
        outer_left,
        MODE_HEIGHT - 50,
        ink,
    )
    parts.append(
        svg_text(end, MODE_HEIGHT - 50, f"n = {runs_text} paired links per bar, medians; whiskers = interquartile range", fill=ink["muted"], size=11)
    )
    parts.append(svg_text(outer_left, MODE_HEIGHT - 22, provenance_line(latest) + " · shorter is better", fill=ink["muted"], size=11))
    return svg_close(parts)


# --------------------------------------------------------------- validation


def validate_svg(path: Path) -> None:
    """Panels are hotlinked into the README; they must be inert and scalable."""
    source = path.read_text(encoding="utf-8")
    if not source.startswith("<svg"):
        fail(f"{path}: expected a bare <svg> root element")
    if "viewBox=" not in source:
        fail(f"{path}: a viewBox is required so the panel scales in the README")
    for forbidden in ("<script", "<foreignObject", "xlink:href", "<image", "@import"):
        if forbidden in source:
            fail(f"{path}: forbidden construct {forbidden!r}")
    if re.search(r"\son[a-z]+\s*=", source, re.IGNORECASE):
        fail(f"{path}: inline event handlers are not allowed")
    if re.search(r"https?://(?!www\.w3\.org/)", source):
        fail(f"{path}: external references are not allowed")


def validate_html_links(path: Path, site: Path) -> None:
    """The page must be self-contained: it may link out, but never load out.

    A remote `<a href>` (the Actions run, say) navigates only when clicked and
    fetches nothing, so it is allowed. A remote `src=` or stylesheet `href`
    would make rendering the page depend on a third party, so it is not.
    """
    source = path.read_text(encoding="utf-8")
    if "@import" in source:
        fail(f"{path}: @import is not allowed")
    for tag in re.findall(r"<img\b[^>]*>", source, re.IGNORECASE):
        if not re.search(r"\balt=[\"'][^\"']+[\"']", tag, re.IGNORECASE):
            fail(f"{path}: every <img> needs non-empty alt text: {tag}")

    loading: list[str] = re.findall(r"\bsrc=[\"']([^\"']+)[\"']", source, re.IGNORECASE)
    # <picture><source srcset=...>: every candidate URL is a load, descriptors ("2x") are not.
    for srcset in re.findall(r"\bsrcset=[\"']([^\"']+)[\"']", source, re.IGNORECASE):
        loading += [entry.split()[0] for entry in srcset.split(",") if entry.strip()]
    loading += re.findall(r"<link\b[^>]*\bhref=[\"']([^\"']+)[\"']", source, re.IGNORECASE)
    for reference in loading:
        if re.match(r"\A[a-z]+:", reference, re.IGNORECASE) or reference.startswith("//"):
            fail(f"{path}: remote resource load is not allowed: {reference}")

    anchors = re.findall(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"']", source, re.IGNORECASE)
    for reference in loading + anchors:
        if reference.startswith("#"):
            continue
        if re.match(r"\A[a-z]+:", reference, re.IGNORECASE) or reference.startswith("//"):
            continue  # an outbound link, already constrained above for loads
        pure = PurePosixPath(reference)
        if pure.is_absolute() or ".." in pure.parts:
            fail(f"{path}: unsafe relative reference: {reference}")
        if not (site / reference).is_file():
            fail(f"{path}: reference does not resolve inside the site: {reference}")


def manifest_for(site: Path) -> dict:
    entries = []
    for name in sorted(SITE_FILES - {"manifest.json"}):
        path = site / name
        entries.append(
            {
                "path": name,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "media_type": MEDIA_TYPES[name],
                "role": ROLES[name],
            }
        )
    return {"schema_version": MANIFEST_SCHEMA, "files": entries}


def validate_site(site: Path, detached: Path | None = None) -> None:
    """The chokepoint. Every other command calls this first."""
    if not site.is_dir() or site.is_symlink():
        fail(f"{site}: expected a site directory")
    actual: set[str] = set()
    for root, dirs, files in os.walk(site):
        root_path = Path(root)
        if dirs:
            fail(f"{site}: subdirectories are not allowed: {sorted(dirs)}")
        for name in files:
            entry = root_path / name
            if entry.is_symlink() or not entry.is_file():
                fail(f"{entry}: only regular files may be published")
            actual.add(name)
    if actual != SITE_FILES:
        missing = sorted(SITE_FILES - actual)
        unexpected = sorted(actual - SITE_FILES)
        fail(f"{site}: file set mismatch (missing={missing} unexpected={unexpected})")
    for name in sorted(actual):
        size = (site / name).stat().st_size
        cap = FILE_CAPS[name]
        if size > cap or (cap == 0 and size != 0):
            fail(f"{site / name}: {size} bytes exceeds the {cap} byte cap")

    manifest = json.loads((site / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        fail("manifest.json: unexpected schema_version")
    expected_payload = SITE_FILES - {"manifest.json"}
    seen: set[str] = set()
    for index, item in enumerate(manifest.get("files", [])):
        if set(item) != {"path", "size", "sha256", "media_type", "role"}:
            fail(f"manifest.files[{index}]: unexpected field set")
        name = item["path"]
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or "\\" in name:
            fail(f"manifest.files[{index}]: unsafe path {name!r}")
        if name not in expected_payload or name in seen:
            fail(f"manifest.files[{index}]: unexpected or duplicate path {name!r}")
        seen.add(name)
        path = site / name
        if (
            item["size"] != path.stat().st_size
            or item["sha256"] != sha256_file(path)
            or item["media_type"] != MEDIA_TYPES[name]
            or item["role"] != ROLES[name]
        ):
            fail(f"{path}: manifest metadata or digest mismatch")
    if seen != expected_payload:
        fail("manifest.files: payload inventory is incomplete")

    for name in sorted(SVG_FILES):
        validate_svg(site / name)
    validate_html_links(site / "index.html", site)

    latest = json.loads((site / "latest.json").read_text(encoding="utf-8"))
    if latest.get("schema_version") != LATEST_SCHEMA:
        fail("latest.json: unexpected schema_version")
    for cell in latest.get("cells", []):
        if cell.get("mode") not in MODES:
            fail(f"latest.json: cell has unknown build mode {cell.get('mode')!r}")

    if detached is not None:
        expected = detached.read_text(encoding="ascii").strip()
        if not HEX_64.match(expected):
            fail(f"{detached}: expected a sha256 hex digest")
        if expected != sha256_file(site / "manifest.json"):
            fail(f"{detached}: detached digest does not match manifest.json")


# ------------------------------------------------------------------- render


def runner_fingerprint(runner: dict) -> str:
    return sha256_bytes(compact_json({k: v for k, v in sorted(runner.items())}))


def comparison_key_for(baseline_ref: str, cells: list[dict]) -> str:
    """Identifies the experiment: baseline, statistics and the measured matrix."""
    matrix: dict[str, dict[str, list]] = {}
    for mode in MODE_IDS:
        mode_cells = [cell for cell in cells if cell["mode"] == mode]
        matrix[mode] = {
            "corpora": sorted({cell["corpus"] for cell in mode_cells}),
            "threads": sorted({int(cell["threads"]) for cell in mode_cells}),
        }
    shape = {
        "baseline_ref": baseline_ref,
        "matrix": matrix,
        "statistics_version": STATISTICS_VERSION,
    }
    return sha256_bytes(compact_json(shape))


def modes_summary() -> list[dict]:
    return [
        {
            "id": mode,
            "label": MODES[mode]["label"],
            "cflags": list(MODES[mode]["cflags"]),
            "link_flags": list(MODES[mode]["link_flags"]),
            "pdb": bool(MODES[mode]["pdb"]),
            "note": MODES[mode]["note"],
        }
        for mode in MODE_IDS
    ]


def load_cells(cells_dir: Path) -> list[dict]:
    """Read the per-cell JSON that tests/perf/bench.py wrote."""
    cells = []
    for path in sorted(cells_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "baseline" not in raw:
            fail(f"{path}: cell has no baseline; bench.py must run with --baseline")
        mode = raw.get("mode")
        if mode not in MODES:
            fail(f"{path}: unknown or missing build mode {mode!r}")
        corpus = Path(raw["corpus"]).name
        if corpus not in CORPUS_IDS:
            fail(f"{path}: unknown corpus {corpus!r}")
        threads = raw.get("threads")
        if not threads:
            fail(f"{path}: cell has no explicit --threads value")
        if int(raw["runs"]) < MIN_RUNS:
            fail(f"{path}: {raw['runs']} runs; a published cell needs >= {MIN_RUNS} for its IQR")
        gate = raw["gate"]["candidate"]
        if (gate["pdb"] is not None) != bool(MODES[mode]["pdb"]):
            fail(f"{path}: PDB gate does not match mode {mode!r}")

        def side(data: dict) -> dict:
            return {
                "wall_ms": round(float(data["wall_ms"]), 3),
                "wall_ms_q1": round(float(data["wall_ms_q1"]), 3),
                "wall_ms_q3": round(float(data["wall_ms_q3"]), 3),
                "cpu_ms": round(float(data["cpu_ms"]), 3),
                "rss_mb": round(float(data["rss_mb"]), 3),
            }

        cells.append(
            {
                "mode": mode,
                "corpus": corpus,
                "threads": int(threads),
                "runs": int(raw["runs"]),
                "speedup_percent": round(float(raw["speedup_percent"]), 3),
                "speedup_percent_q1": round(float(raw["speedup_percent_q1"]), 3),
                "speedup_percent_q3": round(float(raw["speedup_percent_q3"]), 3),
                "paired_wall_ratio_median": round(float(raw["paired_wall_ratio_median"]), 6),
                "paired_wall_ratio_q1": round(float(raw["paired_wall_ratio_q1"]), 6),
                "paired_wall_ratio_q3": round(float(raw["paired_wall_ratio_q3"]), 6),
                "cpu_delta_percent": round(float(raw["cpu_delta_percent"]), 3),
                "rss_delta_percent": round(float(raw["rss_delta_percent"]), 3),
                "candidate": side(raw["candidate"]),
                "baseline": side(raw["baseline"]),
                "gate": {
                    "exe_sha256": gate["exe"],
                    "pdb_sha256": gate["pdb"],
                    "exe_bytes": int(gate["exe_bytes"]),
                    "pdb_bytes": None if gate["pdb_bytes"] is None else int(gate["pdb_bytes"]),
                },
            }
        )
    if not cells:
        fail(f"{cells_dir}: no measurement cells found")
    # The published file set is fixed, so every mode's panels must have data.
    missing = [mode for mode in MODE_IDS if not any(cell["mode"] == mode for cell in cells)]
    if missing:
        fail(f"{cells_dir}: no cells for build mode(s) {missing}")
    seen: set[tuple] = set()
    for cell in cells:
        identity = (cell["mode"], cell["corpus"], cell["threads"])
        if identity in seen:
            fail(f"{cells_dir}: duplicate cell {identity}")
        seen.add(identity)
    cells.sort(
        key=lambda cell: (MODE_IDS.index(cell["mode"]), CORPUS_IDS.index(cell["corpus"]), cell["threads"])
    )
    return cells


def picture(stem: str, alt: str) -> str:
    return f'<img src="{stem}-dark.svg" alt="{escaped(alt)}">'


def render_html(latest: dict) -> bytes:
    run = latest["run"]
    runner = latest["runner"]
    cells = latest["cells"]
    measured_threads = sorted({int(cell["threads"]) for cell in cells})
    peak_threads = measured_threads[-1]
    measured_threads_text = ", ".join(str(t) for t in measured_threads)
    cores_text = runner["cores"] if runner["cores"] else "unknown"
    if peak_threads < 16:
        regime_text = (
            f"this run's highest thread count cannot reproduce that regime, so "
            f"the speedup at {escaped(peak_threads)} threads is expected to be "
            "smaller."
        )
    else:
        regime_text = "this run reaches that regime."
    thread_cap_paragraph = (
        f"Thread counts measured this run: {escaped(measured_threads_text)}. "
        "The workflow measures 1, 2 and 4 threads plus the runner's core "
        f"count ({escaped(cores_text)}) when that is larger than 4, so the "
        f"overview compares modes at {escaped(peak_threads)} threads. The headline "
        "+46% in the project notes was measured at 16 threads on a local "
        f"workstation; {regime_text}"
    )
    allocator_paragraph = (
        "Allocator: on Linux the benchmarked llvm-ld-direct allocates "
        "through glibc malloc, because MI_MALLOC_OVERRIDE is defined only "
        "for WIN32 builds in CMakeLists.txt. The shipped Windows DLL "
        "allocates through mimalloc. These are parallelisation patches, and "
        "allocator behaviour under thread contention differs between glibc "
        "arenas and mimalloc."
    )
    platform_transfer_paragraph = (
        "Measured on Linux; the shipping target is Windows. That the "
        "relative speedup transfers to Windows is asserted, not measured: "
        "no Windows run of this paired ratio has been published yet, and "
        "at least one optimised phase is known to diverge "
        "(createFutureForFile is std::launch::deferred on Linux and "
        "std::launch::async under _WIN64)."
    )
    lto_modes = [mode for mode in MODE_IDS if MODES[mode]["lto"]]
    lto_paragraph = " ".join(
        f"{escaped(MODES[mode]['label'])} is measured on "
        f"{escaped(', '.join(sorted({c['corpus'] for c in cells if c['mode'] == mode}, key=CORPUS_IDS.index)))} "
        f"at the highest thread count only: {escaped(MODES[mode]['note'])}."
        for mode in lto_modes
    )
    mode_sections = "".join(
        f'<h2 id="{mode}">{escaped(MODES[mode]["label"])}</h2>\n'
        f"<p><code>{escaped(mode_flags_text(mode))}</code><br>{escaped(MODES[mode]['note'])}.</p>\n"
        + picture(
            f"link-speed-{mode}",
            f"{MODES[mode]['label']}: paired link time of stock lld-link and llvm-ld per corpus "
            "and thread count, with interquartile-range whiskers",
        )
        + "\n"
        for mode in MODE_IDS
    )

    def iqr(cell: dict) -> str:
        return (
            f"{cell['speedup_percent']:+.1f}% "
            f"({cell['speedup_percent_q1']:+.1f}..{cell['speedup_percent_q3']:+.1f})"
        )

    rows = "".join(
        "<tr>"
        f"<td>{escaped(MODES[cell['mode']]['label'])}</td>"
        f"<td>{escaped(CORPUS_LABELS[cell['corpus']])}</td>"
        f"<td>{cell['threads']}</td>"
        f"<td>{cell['runs']}</td>"
        f"<td>{escaped(iqr(cell))}</td>"
        f"<td>{cell['candidate']['wall_ms']:.1f}</td>"
        f"<td>{cell['baseline']['wall_ms']:.1f}</td>"
        f"<td>{cell['cpu_delta_percent']:+.1f}%</td>"
        f"<td>{cell['rss_delta_percent']:+.1f}%</td>"
        f"<td><code>{escaped(cell['gate']['exe_sha256'][:12])}</code></td>"
        f"<td><code>{escaped((cell['gate']['pdb_sha256'] or 'none')[:12])}</code></td>"
        "</tr>"
        for cell in cells
    )
    mode_nav = " &middot; ".join(
        f'<a href="#{mode}">{escaped(MODES[mode]["label"])}</a>' for mode in MODE_IDS
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>llvm-ld link speed</title>
<style>body{{font:15px {FONT_STACK};max-width:1100px;margin:auto;padding:24px;color:#e6edf3;background:#0d1117}}
table{{border-collapse:collapse;width:100%;margin:16px 0;font-size:13px}}th,td{{border:1px solid #30363d;padding:6px;text-align:left}}
img{{max-width:100%;height:auto}}code,pre{{overflow-wrap:anywhere;white-space:pre-wrap}}a{{color:#4493f8}}</style>
</head><body>
<h1>llvm-ld link speed</h1>
<p>How much faster llvm-ld links each kind of build, right now. Every number is
a paired A/B measured in one run on one machine: the current linker against a
baseline built from the payload as it stood before the link-speed patches,
linking the same corpora interleaved. Hosted runners are shared and noisy, so
times are only compared within a run, never across runs.</p>
<p>Every cell is gated on byte-identical output: the candidate's EXE (and PDB,
when the mode produces one) match the baseline's exactly, and both are
self-deterministic. A speedup that changes output bytes is a bug, not a result.</p>
<nav><a href="#overview">overview</a> &middot; {mode_nav} &middot;
<a href="#cells">all cells</a> &middot; <a href="#caveats">scope and caveats</a> &middot;
<a href="latest.json">validated latest data</a></nav>
<h2 id="overview">Overview</h2>
{picture("link-speed-overview", "Percent less link wall time with llvm-ld per build mode and corpus size, at the highest measured thread count")}
{mode_sections}<h2 id="cells">All cells</h2>
<table><thead><tr><th>Mode</th><th>Corpus</th><th>Threads</th><th>Runs</th><th>Saved (IQR)</th>
<th>llvm-ld ms</th><th>Stock ms</th><th>CPU</th><th>Peak RSS</th><th>EXE</th><th>PDB</th></tr></thead>
<tbody>{rows}</tbody></table>
<h2 id="caveats">Scope and caveats</h2>
<p>{thread_cap_paragraph}</p>
<p>{lto_paragraph}</p>
<p>{allocator_paragraph}</p>
<p>{platform_transfer_paragraph}</p>
<h2>Provenance</h2>
<p>Run {escaped(run["run_id"])} attempt {escaped(run["run_attempt"])};
source <code>{escaped(run["source_sha"])}</code>;
baseline <code>{escaped(latest["baseline"]["ref"])}</code>
({escaped(latest["baseline"]["description"])}).</p>
<p>Runner {escaped(runner["image"])}, {escaped(runner["cpu"])},
{escaped(runner["cores"])} cores, kernel {escaped(runner["kernel"])};
fingerprint <code>{escaped(runner["fingerprint_sha256"])}</code>.</p>
<p><a href="{escaped(latest["actions_run_url"])}">Actions run</a></p>
<h2>Reproduce</h2>
<pre><code>{escaped(latest["reproduction_command"])}</code></pre>
</body></html>
"""
    return document.encode("utf-8")


def ensure_empty_output(output: Path) -> None:
    if output.is_symlink():
        fail(f"{output}: refusing to render into a symlink")
    if output.exists():
        if any(output.iterdir()):
            fail(f"{output}: output directory must be empty")
    else:
        output.mkdir(parents=True)


def command_render(args: argparse.Namespace) -> int:
    cells = load_cells(args.cells_dir)
    runner = {
        "image": args.runner_image,
        "cpu": args.runner_cpu,
        "cores": args.runner_cores,
        "kernel": args.runner_kernel,
        "arch": args.runner_arch,
        "compiler": args.runner_compiler,
    }
    runner["fingerprint_sha256"] = runner_fingerprint(runner)
    latest = {
        "schema_version": LATEST_SCHEMA,
        "statistics_version": STATISTICS_VERSION,
        "comparison_key": comparison_key_for(args.baseline_ref, cells),
        "run": {
            "run_id": args.run_id,
            "run_attempt": args.run_attempt,
            "source_sha": args.source_sha,
            "generated_at_utc": utc_now(),
        },
        "runner": runner,
        "baseline": {
            "ref": args.baseline_ref,
            "description": "payload before the link-speed patches",
        },
        "modes": modes_summary(),
        "cells": cells,
        "actions_run_url": args.actions_run_url,
        "reproduction_command": (
            "python tests/perf/gen_corpus.py --out build-perf/corpus --mode release-pdb --profile large\n"
            "python tests/perf/bench.py --candidate build/llvm-ld-direct "
            "--baseline build-perf/baseline/llvm-ld-direct "
            "--corpus build-perf/corpus/release-pdb/large --threads 4 --runs 9"
        ),
    }

    output = args.output_dir
    ensure_empty_output(output)
    (output / ".nojekyll").write_bytes(b"")
    (output / "latest.json").write_bytes(compact_json(latest))
    for theme in THEMES:
        (output / overview_panel_name(theme)).write_bytes(overview_panel_svg(latest, theme))
        for mode in MODE_IDS:
            (output / mode_panel_name(mode, theme)).write_bytes(mode_panel_svg(latest, mode, theme))
    (output / "index.html").write_bytes(render_html(latest))
    (output / "manifest.json").write_bytes(compact_json(manifest_for(output)))

    digest = sha256_file(output / "manifest.json")
    digest_out = args.detached_digest_out
    # The detached digest seals the site, so it must not live inside it.
    try:
        digest_out.resolve().relative_to(output.resolve())
    except ValueError:
        pass
    else:
        fail(f"{digest_out}: detached digest must be outside the site tree")
    digest_out.parent.mkdir(parents=True, exist_ok=True)
    digest_out.write_text(digest + "\n", encoding="ascii", newline="\n")

    validate_site(output, digest_out)
    print(f"rendered {output} manifest={digest}")
    return 0


# ------------------------------------------------------------------ publish


def git_index_files(worktree: Path) -> set[str]:
    raw = git_command(worktree, "ls-files", "-z")
    return {name for name in raw.decode("utf-8").split("\0") if name}


def git_revision_files(repository: Path, revision: str) -> set[str]:
    raw = git_command(repository, "ls-tree", "-r", "--name-only", "-z", revision)
    return {name for name in raw.decode("utf-8").split("\0") if name}


def require_exact_files(actual: set[str], label: str) -> None:
    if actual != SITE_FILES:
        missing = sorted(SITE_FILES - actual)
        unexpected = sorted(actual - SITE_FILES)
        fail(f"{label}: file set mismatch (missing={missing} unexpected={unexpected})")


def command_prepare_branch(args: argparse.Namespace) -> int:
    site = args.site_dir.resolve()
    worktree = args.worktree.resolve()
    validate_site(site)
    if worktree == site:
        fail("publication worktree and site directory must be distinct")
    administrative = worktree / ".git"
    # A linked worktree has .git as a *file*; that is what makes `git rm -r .`
    # safe here, because repository administration lives elsewhere.
    if not administrative.is_file() or administrative.is_symlink():
        fail(f"{administrative}: expected a linked-worktree administrative file")
    top = Path(
        git_command(worktree, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    ).resolve()
    if top != worktree:
        fail(f"{worktree}: not the worktree root ({top})")
    if git_command(worktree, "status", "--porcelain=v1", "-z"):
        fail(f"{worktree}: publication worktree must start clean")

    git_command(worktree, "rm", "-r", "-f", "--ignore-unmatch", "--", ".")
    leftovers = sorted(p.name for p in worktree.iterdir() if p.name != ".git")
    if leftovers:
        fail(f"{worktree}: unexpected untracked files after git rm: {leftovers}")
    for name in sorted(SITE_FILES):
        shutil.copy2(site / name, worktree / name)
    git_command(worktree, "add", "-A")
    require_exact_files(git_index_files(worktree), "staged publication index")
    for name in SITE_FILES:
        if (worktree / name).read_bytes() != (site / name).read_bytes():
            fail(f"{worktree / name}: copied bytes differ from the sealed site")
    print(f"staged {len(SITE_FILES)} files in {worktree}")
    return 0


def command_validate_revision(args: argparse.Namespace) -> int:
    site = args.site_dir.resolve()
    validate_site(site)
    require_exact_files(git_revision_files(args.repository, args.revision), "revision")
    for name in sorted(SITE_FILES):
        published = git_command(args.repository, "show", f"{args.revision}:{name}")
        if published != (site / name).read_bytes():
            fail(f"{args.revision}:{name}: bytes differ from the sealed site")
    print(f"revision {args.revision} matches the sealed site")
    return 0


def command_audit_pages(args: argparse.Namespace) -> int:
    site = args.site_dir.resolve()
    validate_site(site)
    parsed = urllib.parse.urlparse(args.page_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        fail(f"{args.page_url!r}: expected a plain HTTPS Pages base URL")
    base = args.page_url.rstrip("/") + "/"
    version = sha256_file(site / "manifest.json")
    # .nojekyll is a Pages build marker and is not served.
    public = sorted(SITE_FILES - {".nojekyll"})
    last_error = "Pages payload did not match"
    for attempt in range(1, args.attempts + 1):
        try:
            for name in public:
                # The manifest digest is a cache-buster: a CDN must not be able
                # to answer with the previous deployment.
                url = urllib.parse.urljoin(base, name) + f"?manifest={version}"
                request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read(FILE_CAPS[name] + 1)
                if data != (site / name).read_bytes():
                    raise ReportError(f"{url}: deployed bytes differ from the sealed site")
            print(f"pages audit ok: {base}")
            return 0
        except (OSError, urllib.error.URLError, ReportError) as error:
            last_error = str(error)
            if attempt < args.attempts:
                time.sleep(args.delay_seconds)
    fail(f"Pages audit failed after {args.attempts} attempts: {last_error}")


def command_validate_site(args: argparse.Namespace) -> int:
    validate_site(args.site_dir.resolve(), args.detached_digest)
    print(f"site ok: {args.site_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="build the sealed site")
    render.add_argument("--cells-dir", type=Path, required=True)
    render.add_argument("--output-dir", type=Path, required=True)
    render.add_argument("--detached-digest-out", type=Path, required=True)
    render.add_argument("--baseline-ref", required=True)
    render.add_argument("--run-id", required=True)
    render.add_argument("--run-attempt", required=True)
    render.add_argument("--source-sha", required=True)
    render.add_argument("--actions-run-url", required=True)
    render.add_argument("--runner-image", default="")
    render.add_argument("--runner-cpu", default="")
    render.add_argument("--runner-cores", default="")
    render.add_argument("--runner-kernel", default="")
    render.add_argument("--runner-arch", default="")
    render.add_argument("--runner-compiler", default="")
    render.set_defaults(func=command_render)

    check = sub.add_parser("validate-site", help="re-check a rendered site")
    check.add_argument("--site-dir", type=Path, required=True)
    check.add_argument("--detached-digest", type=Path)
    check.set_defaults(func=command_validate_site)

    prepare = sub.add_parser("prepare-branch", help="stage the site into a worktree")
    prepare.add_argument("--worktree", type=Path, required=True)
    prepare.add_argument("--site-dir", type=Path, required=True)
    prepare.set_defaults(func=command_prepare_branch)

    revision = sub.add_parser("validate-revision", help="prove a commit matches the site")
    revision.add_argument("--repository", type=Path, required=True)
    revision.add_argument("--revision", required=True)
    revision.add_argument("--site-dir", type=Path, required=True)
    revision.set_defaults(func=command_validate_revision)

    pages = sub.add_parser("audit-pages", help="prove the deployment matches the site")
    pages.add_argument("--site-dir", type=Path, required=True)
    pages.add_argument("--page-url", required=True)
    pages.add_argument("--attempts", type=int, default=12)
    pages.add_argument("--delay-seconds", type=float, default=10.0)
    pages.set_defaults(func=command_audit_pages)

    args = parser.parse_args()
    try:
        return int(args.func(args))
    except ReportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
