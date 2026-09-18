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

Subcommands
  render            build the sealed site from measurement cells + prior history
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

LATEST_SCHEMA = "llvm-ld-link-latest-v1"
HISTORY_SCHEMA = "llvm-ld-link-history-v1"
MANIFEST_SCHEMA = "llvm-ld-link-site-manifest-v1"
STATISTICS_VERSION = "paired-interleaved-median-v1"

# Corpus ids in publication order. The generator profiles live in
# tests/perf/gen_corpus.py; these are the ones the workflow measures.
CORPUS_IDS = ("small", "medium", "large")
CORPUS_LABELS = {
    "small": "small (64 objects)",
    "medium": "medium (512 objects)",
    "large": "large (2048 objects)",
}

THREADS_PANEL = "link-speedup-threads.svg"
HISTORY_PANEL = "link-speedup-history.svg"

SITE_FILES = {
    ".nojekyll",
    "index.html",
    "latest.json",
    "history.jsonl",
    "manifest.json",
    THREADS_PANEL,
    HISTORY_PANEL,
}

# Byte ceilings, enforced at render and re-enforced at validation. `.nojekyll`
# must be exactly empty, which the zero cap encodes.
FILE_CAPS = {
    ".nojekyll": 0,
    "index.html": 2 * 1024 * 1024,
    "latest.json": 32 * 1024 * 1024,
    "history.jsonl": 32 * 1024 * 1024,
    "manifest.json": 1024 * 1024,
    THREADS_PANEL: 1024 * 1024,
    HISTORY_PANEL: 1024 * 1024,
}

# manifest.json is deliberately absent from both maps below: it describes the
# other files and cannot describe itself.
MEDIA_TYPES = {
    ".nojekyll": "application/octet-stream",
    "index.html": "text/html; charset=utf-8",
    "latest.json": "application/json",
    "history.jsonl": "application/x-ndjson",
    THREADS_PANEL: "image/svg+xml",
    HISTORY_PANEL: "image/svg+xml",
}
ROLES = {
    ".nojekyll": "github-pages-marker",
    "index.html": "report-index",
    "latest.json": "validated-latest-data",
    "history.jsonl": "bounded-compatible-history",
    THREADS_PANEL: "speedup-by-threads-panel",
    HISTORY_PANEL: "speedup-history-panel",
}
SVG_FILES = frozenset({THREADS_PANEL, HISTORY_PANEL})

HISTORY_LIMIT = 1000
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
# Hand-written SVG, one dark theme, fixed pixel layout. The panels are
# hotlinked into the README from the raw data-branch URL, so they must carry no
# script, no external reference and no event handler; validate_svg enforces it.

INK = {
    "background": "#0d1117",
    "plot": "#111823",
    "grid": "#1f2937",
    "zero": "#30363d",
    "axis": "#8b98ad",
    "title": "#e8eef7",
    "muted": "#7d8da5",
}
# One fixed color per corpus, keyed by id so a corpus is the same color on
# every panel.
SERIES = {
    "small": "#e3b341",
    "medium": "#3fb950",
    "large": "#58a6ff",
}

FONT_STACK = "system-ui,-apple-system,Segoe UI,Roboto,sans-serif"
# Approximate advance per character at 12px for the manual legend flow. The
# only text-metric assumption in the file; keep labels short.
LEGEND_ADVANCE = 7.4

WIDTH = 1000
HEIGHT = 600
PLOT_TOP = 118
PLOT_LEFT = 92
PLOT_RIGHT = 40
# Deep enough for four stacked rows below the plot: tick labels (+26), the axis
# title (+48), the legend (height-54) and the provenance footer (height-22).
# With three corpora the legend runs ~530px wide, so it must not share a row
# with the centred axis title.
PLOT_BOTTOM = 140


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


def y_of(value: float, floor: float, ceiling: float, top: float, height: float) -> float:
    span = ceiling - floor
    if span <= 0:
        return top + height
    return top + height - (value - floor) / span * height


def svg_open(width: int, height: int) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img">',
        f'<rect width="{width}" height="{height}" fill="{INK["background"]}"/>',
    ]


def svg_close(parts: list[str]) -> bytes:
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def draw_frame(
    parts: list[str],
    *,
    title: str,
    subtitle: str,
    left: float,
    top: float,
    plot_width: float,
    plot_height: float,
    floor: float,
    ceiling: float,
    y_label: str,
) -> None:
    """Plot background, horizontal gridlines with value labels, and titles."""
    parts.append(
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" '
        f'fill="{INK["plot"]}" rx="6"/>'
    )
    parts.append(svg_text(left - 12, top - 46, title, fill=INK["title"], size=20, weight="600"))
    parts.append(svg_text(left - 12, top - 24, subtitle, fill=INK["muted"], size=13))
    for step in range(5):
        value = floor + (ceiling - floor) * step / 4
        y = y_of(value, floor, ceiling, top, plot_height)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width:.1f}" y2="{y:.1f}" '
            f'stroke="{INK["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            svg_text(left - 12, y + 4, f"{value:g}", fill=INK["axis"], size=12, anchor="end")
        )
    # A zero rule, when the axis spans it, so a regression reads instantly.
    if floor < 0 < ceiling:
        zero = y_of(0.0, floor, ceiling, top, plot_height)
        parts.append(
            f'<line x1="{left}" y1="{zero:.1f}" x2="{left + plot_width:.1f}" y2="{zero:.1f}" '
            f'stroke="{INK["zero"]}" stroke-width="2"/>'
        )
    parts.append(
        svg_text(left - 12, top - 4, y_label, fill=INK["muted"], size=11, anchor="end")
    )


def draw_series(
    parts: list[str],
    points: list[tuple[float, float]],
    color: str,
    *,
    floor: float,
    ceiling: float,
    top: float,
    plot_height: float,
) -> None:
    if not points:
        return
    path = " ".join(
        f"{'M' if index == 0 else 'L'}{x:.1f} "
        f"{y_of(value, floor, ceiling, top, plot_height):.1f}"
        for index, (x, value) in enumerate(points)
    )
    parts.append(
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
    )
    for x, value in points:
        # Marker filled with the page background so the line reads through it.
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y_of(value, floor, ceiling, top, plot_height):.1f}" '
            f'r="4.5" fill="{INK["background"]}" stroke="{color}" stroke-width="2.5"/>'
        )


def draw_legend(parts: list[str], labels: list[tuple[str, str]], height: int) -> None:
    cursor = float(PLOT_LEFT)
    for label, color in labels:
        parts.append(
            f'<rect x="{cursor:.1f}" y="{height - 54}" width="22" height="4" rx="2" '
            f'fill="{color}"/>'
        )
        parts.append(svg_text(cursor + 30, height - 47, label, fill=INK["axis"], size=12))
        cursor += 34 + LEGEND_ADVANCE * len(label)


def footer(parts: list[str], text: str, height: int) -> None:
    parts.append(svg_text(PLOT_LEFT, height - 22, text, fill=INK["muted"], size=11))


def threads_panel_svg(latest: dict) -> bytes:
    """Speedup versus thread count, one line per corpus.

    x is log2-spaced because the thread points are powers of two.
    """
    cells = latest["cells"]
    thread_points = sorted({int(cell["threads"]) for cell in cells})
    parts = svg_open(WIDTH, HEIGHT)
    plot_width = WIDTH - PLOT_LEFT - PLOT_RIGHT
    plot_height = HEIGHT - PLOT_TOP - PLOT_BOTTOM

    values = [float(cell["speedup_percent"]) for cell in cells]
    peak = max(values + [0.0])
    trough = min(values + [0.0])
    ceiling = nice_ceiling(peak * 1.12)
    floor = 0.0 if trough >= 0 else -nice_ceiling(abs(trough) * 1.2)

    draw_frame(
        parts,
        title="Link speedup after the PDB-emission work",
        subtitle=(
            "paired against the payload before the patches, same runner, "
            "byte-identical EXE and PDB"
        ),
        left=PLOT_LEFT,
        top=PLOT_TOP,
        plot_width=plot_width,
        plot_height=plot_height,
        floor=floor,
        ceiling=ceiling,
        y_label="% faster",
    )

    def x_of(threads: int) -> float:
        if len(thread_points) < 2:
            return PLOT_LEFT + plot_width / 2
        lo = math.log2(thread_points[0])
        hi = math.log2(thread_points[-1])
        return PLOT_LEFT + (math.log2(threads) - lo) / (hi - lo) * plot_width

    for threads in thread_points:
        x = x_of(threads)
        parts.append(
            f'<line x1="{x:.1f}" y1="{PLOT_TOP}" x2="{x:.1f}" '
            f'y2="{PLOT_TOP + plot_height}" stroke="{INK["grid"]}" stroke-width="1"/>'
        )
        label = "max" if threads == thread_points[-1] and threads > 8 else str(threads)
        parts.append(
            svg_text(
                x,
                PLOT_TOP + plot_height + 26,
                label,
                fill=INK["title"],
                size=13,
                weight="600",
                anchor="middle",
            )
        )
    parts.append(
        svg_text(
            PLOT_LEFT + plot_width / 2,
            PLOT_TOP + plot_height + 48,
            "linker threads (/threads:)",
            fill=INK["muted"],
            size=12,
            anchor="middle",
        )
    )

    legend: list[tuple[str, str]] = []
    for corpus in CORPUS_IDS:
        points = sorted(
            (x_of(int(cell["threads"])), float(cell["speedup_percent"]))
            for cell in cells
            if cell["corpus"] == corpus
        )
        if not points:
            continue
        draw_series(
            parts,
            points,
            SERIES[corpus],
            floor=floor,
            ceiling=ceiling,
            top=PLOT_TOP,
            plot_height=plot_height,
        )
        legend.append((CORPUS_LABELS[corpus], SERIES[corpus]))
    draw_legend(parts, legend, HEIGHT)
    footer(
        parts,
        f"run {latest['run']['run_id']} - source {latest['run']['source_sha'][:12]} - "
        f"{latest['runner']['cpu']} - Linux, glibc malloc - higher is better",
        HEIGHT,
    )
    return svg_close(parts)


def history_panel_svg(rows: list[dict], comparison_key: str) -> bytes:
    """Speedup at the highest measured thread count over time, per corpus.

    Only rows sharing the current comparison key are connected: a different
    baseline or corpus set is a different experiment, not a later data point.
    """
    compatible = [row for row in rows if row.get("comparison_key") == comparison_key]
    parts = svg_open(WIDTH, HEIGHT)
    plot_width = WIDTH - PLOT_LEFT - PLOT_RIGHT
    plot_height = HEIGHT - PLOT_TOP - PLOT_BOTTOM

    series: dict[str, list[tuple[float, float]]] = {}
    for index, row in enumerate(compatible):
        for corpus, value in sorted(row.get("peak_speedup_percent", {}).items()):
            series.setdefault(corpus, []).append((float(index), float(value)))

    values = [value for points in series.values() for _, value in points]
    peak_value = max(values + [0.0])
    trough = min(values + [0.0])
    ceiling = nice_ceiling(peak_value * 1.12) if values else 10.0
    floor = 0.0 if trough >= 0 else -nice_ceiling(abs(trough) * 1.2)

    peak = None
    if compatible and "peak_threads" in compatible[-1]:
        peak = int(compatible[-1]["peak_threads"])
    if peak is not None:
        subtitle = (
            f"{len(compatible)} run(s) sharing one baseline and corpus set; "
            f"at {peak} threads, the highest measured (capped by runner cores)"
        )
    else:
        subtitle = (
            f"{len(compatible)} run(s) sharing one baseline and corpus set; "
            "at the highest measured thread count"
        )

    draw_frame(
        parts,
        title="Link speedup over time",
        subtitle=subtitle,
        left=PLOT_LEFT,
        top=PLOT_TOP,
        plot_width=plot_width,
        plot_height=plot_height,
        floor=floor,
        ceiling=ceiling,
        y_label="% faster",
    )

    span = max(len(compatible) - 1, 1)

    def x_of(index: float) -> float:
        # A lone run would otherwise pin to the left edge and read like a
        # truncated series; centre it until there is a second point.
        if len(compatible) == 1:
            return PLOT_LEFT + plot_width / 2
        return PLOT_LEFT + index / span * plot_width

    legend: list[tuple[str, str]] = []
    for corpus in CORPUS_IDS:
        points = [(x_of(index), value) for index, value in series.get(corpus, [])]
        if not points:
            continue
        draw_series(
            parts,
            points,
            SERIES[corpus],
            floor=floor,
            ceiling=ceiling,
            top=PLOT_TOP,
            plot_height=plot_height,
        )
        legend.append((CORPUS_LABELS[corpus], SERIES[corpus]))
    draw_legend(parts, legend, HEIGHT)

    if compatible:
        first = compatible[0]["run"]["generated_at_utc"][:10]
        last = compatible[-1]["run"]["generated_at_utc"][:10]
        parts.append(
            svg_text(PLOT_LEFT, PLOT_TOP + plot_height + 26, first, fill=INK["axis"], size=12)
        )
        parts.append(
            svg_text(
                PLOT_LEFT + plot_width,
                PLOT_TOP + plot_height + 26,
                last,
                fill=INK["axis"],
                size=12,
                anchor="end",
            )
        )
    footer(
        parts,
        "one point per published run - highest measured thread count - higher is better",
        HEIGHT,
    )
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
    rows = read_history_text((site / "history.jsonl").read_text(encoding="utf-8"), site)
    if len(rows) > HISTORY_LIMIT:
        fail(f"history.jsonl: history exceeds {HISTORY_LIMIT} rows")
    if rows != sorted(rows, key=history_sort_key):
        fail("history.jsonl: rows are not in canonical sort order")
    reject_duplicate_history(rows)

    if detached is not None:
        expected = detached.read_text(encoding="ascii").strip()
        if not HEX_64.match(expected):
            fail(f"{detached}: expected a sha256 hex digest")
        if expected != sha256_file(site / "manifest.json"):
            fail(f"{detached}: detached digest does not match manifest.json")


# ------------------------------------------------------------------ history


def history_sort_key(row: dict) -> tuple:
    run = row["run"]
    return (run["generated_at_utc"], str(run["run_id"]), int(run["run_attempt"]))


def reject_duplicate_history(rows: list[dict]) -> None:
    seen = set()
    for row in rows:
        run = row["run"]
        identity = (str(run["run_id"]), int(run["run_attempt"]))
        if identity in seen:
            fail(f"history: duplicate run {identity}")
        seen.add(identity)


def read_history_text(text: str, where: Path) -> list[dict]:
    rows = []
    if text and not text.endswith("\n"):
        fail(f"{where}: history must end with a newline")
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            fail(f"{where}: blank line at {number}")
        row = json.loads(line)
        if row.get("history_schema_version") != HISTORY_SCHEMA:
            fail(f"{where}: unexpected history_schema_version at line {number}")
        rows.append(row)
    return rows


def read_history(path: Path, initialize: bool) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        if not initialize:
            fail(f"{path}: history is absent; pass --initialize-history explicitly")
        return []
    return read_history_text(path.read_text(encoding="utf-8"), path)


def history_row(latest: dict) -> dict:
    """Compact projection of latest.json: enough to plot, nothing bulky."""
    peak_threads = max(int(cell["threads"]) for cell in latest["cells"])
    peak: dict[str, float] = {}
    for cell in latest["cells"]:
        if int(cell["threads"]) == peak_threads:
            peak[cell["corpus"]] = float(cell["speedup_percent"])
    return {
        "history_schema_version": HISTORY_SCHEMA,
        "statistics_version": STATISTICS_VERSION,
        "comparison_key": latest["comparison_key"],
        "run": latest["run"],
        "runner_fingerprint_sha256": latest["runner"]["fingerprint_sha256"],
        "peak_threads": peak_threads,
        "peak_speedup_percent": peak,
        "cells": [
            {
                "corpus": cell["corpus"],
                "threads": int(cell["threads"]),
                "speedup_percent": float(cell["speedup_percent"]),
                "cpu_delta_percent": float(cell["cpu_delta_percent"]),
                "rss_delta_percent": float(cell["rss_delta_percent"]),
            }
            for cell in latest["cells"]
        ],
    }


def merge_history(rows: list[dict], current: dict) -> list[dict]:
    combined = list(rows)
    run = current["run"]
    identity = (str(run["run_id"]), int(run["run_attempt"]))
    for row in combined:
        existing = row["run"]
        if (str(existing["run_id"]), int(existing["run_attempt"])) == identity:
            fail(f"history append: run {identity} is already published")
    combined.append(current)
    reject_duplicate_history(combined)
    combined.sort(key=history_sort_key)
    # Rolling window: the branch is a publication surface, not an archive.
    return combined[-HISTORY_LIMIT:]


# ------------------------------------------------------------------- render


def runner_fingerprint(runner: dict) -> str:
    return sha256_bytes(compact_json({k: v for k, v in sorted(runner.items())}))


def comparison_key_for(baseline_ref: str, cells: list[dict]) -> str:
    """Identifies the experiment, so history only connects like with like."""
    shape = {
        "baseline_ref": baseline_ref,
        "corpora": sorted({cell["corpus"] for cell in cells}),
        "threads": sorted({int(cell["threads"]) for cell in cells}),
        "statistics_version": STATISTICS_VERSION,
    }
    return sha256_bytes(compact_json(shape))


def load_cells(cells_dir: Path) -> list[dict]:
    """Read the per-cell JSON that tests/perf/bench.py wrote."""
    cells = []
    for path in sorted(cells_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "baseline" not in raw:
            fail(f"{path}: cell has no baseline; bench.py must run with --baseline")
        corpus = Path(raw["corpus"]).name
        if corpus not in CORPUS_IDS:
            fail(f"{path}: unknown corpus {corpus!r}")
        threads = raw.get("threads")
        if not threads:
            fail(f"{path}: cell has no explicit --threads value")
        cells.append(
            {
                "corpus": corpus,
                "threads": int(threads),
                "runs": int(raw["runs"]),
                "speedup_percent": round(float(raw["speedup_percent"]), 3),
                "paired_wall_ratio_median": round(float(raw["paired_wall_ratio_median"]), 6),
                "cpu_delta_percent": round(float(raw["cpu_delta_percent"]), 3),
                "rss_delta_percent": round(float(raw["rss_delta_percent"]), 3),
                "candidate": {
                    "wall_ms": round(float(raw["candidate"]["wall_ms"]), 3),
                    "cpu_ms": round(float(raw["candidate"]["cpu_ms"]), 3),
                    "rss_mb": round(float(raw["candidate"]["rss_mb"]), 3),
                },
                "baseline": {
                    "wall_ms": round(float(raw["baseline"]["wall_ms"]), 3),
                    "cpu_ms": round(float(raw["baseline"]["cpu_ms"]), 3),
                    "rss_mb": round(float(raw["baseline"]["rss_mb"]), 3),
                },
                "gate": {
                    "exe_sha256": raw["gate"]["candidate"]["exe"],
                    "pdb_sha256": raw["gate"]["candidate"]["pdb"],
                    "exe_bytes": int(raw["gate"]["candidate"]["exe_bytes"]),
                    "pdb_bytes": int(raw["gate"]["candidate"]["pdb_bytes"]),
                },
            }
        )
    if not cells:
        fail(f"{cells_dir}: no measurement cells found")
    cells.sort(key=lambda cell: (CORPUS_IDS.index(cell["corpus"]), cell["threads"]))
    return cells


def render_html(latest: dict) -> bytes:
    run = latest["run"]
    runner = latest["runner"]
    measured_threads = sorted({int(cell["threads"]) for cell in latest["cells"]})
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
        f"history panel tracks {escaped(peak_threads)} threads. The headline "
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
    rows = "".join(
        "<tr>"
        f"<td>{escaped(CORPUS_LABELS[cell['corpus']])}</td>"
        f"<td>{cell['threads']}</td>"
        f"<td>{cell['speedup_percent']:+.1f}%</td>"
        f"<td>{cell['candidate']['wall_ms']:.1f}</td>"
        f"<td>{cell['baseline']['wall_ms']:.1f}</td>"
        f"<td>{cell['cpu_delta_percent']:+.1f}%</td>"
        f"<td>{cell['rss_delta_percent']:+.1f}%</td>"
        f"<td><code>{escaped(cell['gate']['exe_sha256'][:12])}</code></td>"
        "</tr>"
        for cell in latest["cells"]
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>llvm-ld link speed</title>
<style>body{{font:15px {FONT_STACK};max-width:1100px;margin:auto;padding:24px;color:#182334}}
table{{border-collapse:collapse;width:100%;margin:16px 0}}th,td{{border:1px solid #ccd4dd;padding:7px;text-align:left}}
img{{max-width:100%;height:auto}}code,pre{{overflow-wrap:anywhere;white-space:pre-wrap}}small{{color:#596575}}</style>
</head><body>
<h1>llvm-ld link speed</h1>
<p>Every number is a paired A/B measured in one run on one machine: the current
linker against a baseline built from the payload as it stood before the
link-speed patches, linking the same corpora interleaved. Hosted runners are
shared and noisy, so absolute wall time across runs is not published.</p>
<p>Every cell is gated on byte-identical output: the candidate's EXE and PDB
match the baseline's exactly, and both are self-deterministic. A speedup that
changes output bytes is a bug, not a result.</p>
<nav><a href="latest.json">validated latest data</a> &middot;
<a href="history.jsonl">compact history</a> &middot;
<a href="#caveats">scope and caveats</a></nav>
<h2 id="threads">Speedup by thread count</h2>
<img src="{THREADS_PANEL}" alt="Link speedup versus linker thread count, one line per corpus; higher is better">
<h2 id="history">Speedup over time</h2>
<img src="{HISTORY_PANEL}" alt="Link speedup over published runs at the highest measured thread count, one line per corpus">
<h2>Cells</h2>
<table><thead><tr><th>Corpus</th><th>Threads</th><th>Speedup</th><th>Candidate ms</th>
<th>Baseline ms</th><th>CPU</th><th>Peak RSS</th><th>Output EXE</th></tr></thead>
<tbody>{rows}</tbody></table>
<h2 id="caveats">Scope and caveats</h2>
<p>{thread_cap_paragraph}</p>
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
        "cells": cells,
        "actions_run_url": args.actions_run_url,
        "reproduction_command": (
            "python tests/perf/gen_corpus.py --out build-perf/corpus --profile large && "
            "python tests/perf/bench.py --candidate build/llvm-ld-direct "
            "--baseline build-perf/baseline/llvm-ld-direct --corpus build-perf/corpus/large"
        ),
    }

    rows = read_history(args.history_in, args.initialize_history)
    rows = merge_history(rows, history_row(latest))

    output = args.output_dir
    ensure_empty_output(output)
    (output / ".nojekyll").write_bytes(b"")
    (output / "latest.json").write_bytes(compact_json(latest))
    (output / "history.jsonl").write_bytes(b"".join(compact_json(row) for row in rows))
    (output / THREADS_PANEL).write_bytes(threads_panel_svg(latest))
    (output / HISTORY_PANEL).write_bytes(history_panel_svg(rows, latest["comparison_key"]))
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
    render.add_argument("--history-in", type=Path, required=True)
    render.add_argument("--output-dir", type=Path, required=True)
    render.add_argument("--detached-digest-out", type=Path, required=True)
    render.add_argument("--initialize-history", action="store_true")
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
