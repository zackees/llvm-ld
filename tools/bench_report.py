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

What is measured is every build type (Debug, Release, ThinLTO) linked both
without /debug and with /debug:full on the same objects, defined once in
tests/perf/gen_corpus.py (MODES, VARIANTS) and imported here. The site shows
only the current run, as one dark chart: rows are build types, columns are
corpus sizes, and each cell stacks the PDB's extra time on the link itself for
stock lld-link and llvm-ld.

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

LATEST_SCHEMA = "llvm-ld-link-latest-v3"
MANIFEST_SCHEMA = "llvm-ld-link-site-manifest-v3"
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
# How every corpus is linked: without /debug ("nopdb") and with /debug:full ("pdb").
VARIANTS: dict[str, dict] = _GENERATOR.VARIANTS
VARIANT_IDS = tuple(VARIANTS)

# Corpus ids in publication order. The generator profiles live in
# tests/perf/gen_corpus.py; these are the ones the workflow measures.
CORPUS_IDS = ("small", "medium", "large")
CORPUS_LABELS = {
    corpus: f"{corpus} · {_GENERATOR.PROFILES[corpus]['tus']} objects" for corpus in CORPUS_IDS
}

# One chart (#35). It keeps the published name of the former overview panel,
# which the README hotlinks, so no image breaks between a merge and the next
# publication.
CHART = "link-speed-overview-dark.svg"
SVG_ROLES = {CHART: "link-speed-chart"}
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
# One hand-written SVG, fixed pixel layout, dark theme. It is hotlinked from the
# raw data-branch URL, so it must carry no script, no external reference and no
# event handler; validate_svg enforces it. <pattern> (the PDB hatch) needs no
# href and is allowed.
#
# Layout (#35): rows are build types, columns are corpus sizes. Each cell holds
# two horizontal stacked bars, stock lld-link and llvm-ld, at the mode's highest
# measured thread count: a solid segment for the link without /debug and a
# hatched, dotted-outline segment for the extra time /debug:full adds on the
# same objects. Every value is directly labelled: a hotlinked SVG has no hover
# layer, and the dashboard table is the accessible twin.

# Surfaces and text are the dark panel palette of zackees/mimalloc-pprof
# (ci/benchmark_report.py SCALING_INK), so both projects' charts read as one
# system. Stock lld-link is a darkish blue and llvm-ld a whiter blue.
PALETTES = {
    "dark": {
        "background": "#0d1117",
        "plot": "#111823",
        "grid": "#1f2937",
        "axis": "#8b98ad",
        "title": "#e8eef7",
        "muted": "#7d8da5",
        "baseline": "#1f6feb",
        "candidate": "#a5d6ff",
    },
}
INK = PALETTES["dark"]
SIDES = (("baseline", "stock lld-link"), ("candidate", "llvm-ld"))

FONT_STACK = "system-ui,-apple-system,Segoe UI,Roboto,sans-serif"
# Approximate advance per character at 12px for manual text flow. The only
# text-metric assumption in the file; keep labels short.
CHAR_ADVANCE_12PX = 7.0

WIDTH = 1000
ROW_LABEL_WIDTH = 118
GRID_LEFT = 24
GRID_RIGHT = 16
COLUMN_GAP = 16
GRID_TOP = 150
ROW_HEIGHT = 138
BAR_HEIGHT = 20
BAR_GAP = 8
VALUE_LABEL_ROOM = 92


def svg_text(
    x: float,
    y: float,
    value: str,
    *,
    fill: str,
    size: float = 12,
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


def fmt_time(ms: float, seconds: bool = False) -> str:
    if seconds or ms >= 10000:
        return f"{ms / 1000:.1f} s"
    return f"{ms:,.0f} ms" if ms >= 100 else f"{ms:.1f} ms"


def fmt_tick(value: float) -> str:
    if value == 0:
        return "0"
    return f"{value / 1000:g} s" if value >= 10000 else f"{value:,.0f}"


def hatch_pattern(pattern_id: str, color: str) -> str:
    """Cross-hatch over a faint tint of the side's color."""
    return (
        f'<pattern id="{pattern_id}" width="7" height="7" patternUnits="userSpaceOnUse">'
        f'<rect width="7" height="7" fill="{color}" fill-opacity="0.14"/>'
        f'<path d="M0 7L7 0M-2 2L2 -2M5 9L9 5M0 0L7 7M-2 5L2 9M5 -2L9 2" '
        f'stroke="{color}" stroke-width="1.1"/></pattern>'
    )


def provenance_line(latest: dict) -> str:
    runner = latest["runner"]
    cores = f", {runner['cores']} cores" if runner["cores"] else ""
    return (
        f"run {latest['run']['run_id']} · source {latest['run']['source_sha'][:12]} · "
        f"{runner['cpu'] or 'unknown CPU'}{cores} · Linux, glibc malloc · shorter is better"
    )


def peak_threads_of(cells: list[dict]) -> int:
    return max(int(cell["threads"]) for cell in cells)


def mode_flags_text(mode: str) -> str:
    spec = MODES[mode]
    return f"objects: clang {' '.join(spec['cflags'])} · link: {' '.join(spec['link_flags'])}"


def chart_cells(latest: dict) -> dict[tuple[str, str], dict[str, dict]]:
    """(mode, corpus) -> {variant: cell} at each mode's highest measured thread count."""
    chosen: dict[tuple[str, str], dict[str, dict]] = {}
    for mode in MODE_IDS:
        mode_cells = [cell for cell in latest["cells"] if cell["mode"] == mode]
        top = peak_threads_of(mode_cells)
        for cell in mode_cells:
            if int(cell["threads"]) == top:
                chosen.setdefault((mode, cell["corpus"]), {})[cell["variant"]] = cell
    return chosen


# A measurement is too noisy to derive anything from when either linker's wall
# IQR exceeds this fraction of its median, or the paired-speedup IQR is wider
# than NOISY_SPEEDUP_IQR percentage points (#38: a hosted runner can go bimodal).
NOISY_WALL_IQR = 0.25
NOISY_SPEEDUP_IQR = 25.0


def cell_is_noisy(cell: dict) -> bool:
    for side, _ in SIDES:
        data = cell[side]
        if data["wall_ms_q3"] - data["wall_ms_q1"] > NOISY_WALL_IQR * data["wall_ms"]:
            return True
    return cell["speedup_percent_q3"] - cell["speedup_percent_q1"] > NOISY_SPEEDUP_IQR


def pdb_is_visible(pair: dict[str, dict]) -> bool:
    """The PDB segment is drawn only when it clears run-to-run noise on both sides:
    the PDB cell's q1 above the no-PDB cell's q3, for stock and for llvm-ld."""
    return all(
        pair["pdb"][side]["wall_ms_q1"] > pair["nopdb"][side]["wall_ms_q3"] for side, _ in SIDES
    )


def link_speed_svg(latest: dict) -> bytes:
    cells = chart_cells(latest)
    peak = peak_threads_of(latest["cells"])
    height = GRID_TOP + ROW_HEIGHT * len(MODE_IDS) + 40
    title = "How long a link takes, and where llvm-ld saves the time"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
        f'width="{WIDTH}" height="{height}" role="img">',
        f"<title>{escaped(title)}: stock lld-link vs llvm-ld, link plus PDB, by build type and corpus size</title>",
        "<defs>",
        hatch_pattern("hatch-baseline", INK["baseline"]),
        hatch_pattern("hatch-candidate", INK["candidate"]),
        hatch_pattern("hatch-legend", INK["axis"]),
        "</defs>",
        f'<rect width="{WIDTH}" height="{height}" fill="{INK["background"]}"/>',
        svg_text(24, 36, title, fill=INK["title"], size=20, weight="600"),
        svg_text(
            24, 58,
            "solid = the link itself (no /debug) · hatched = extra time to write the PDB "
            "(/debug:full) · same objects both ways",
            fill=INK["title"], size=12,
        ),
        svg_text(
            24, 76,
            f"{peak} threads (each build type's highest measured) · paired interleaved medians · "
            "every output byte-identical between the two linkers",
            fill=INK["muted"], size=12,
        ),
    ]

    # Legend first, above the grid.
    ly, lx = 104.0, 24.0
    for side, name in SIDES:
        parts.append(f'<rect x="{lx:.1f}" y="{ly - 10:.1f}" width="12" height="12" rx="2" fill="{INK[side]}"/>')
        parts.append(svg_text(lx + 18, ly, name, fill=INK["title"]))
        lx += 18 + CHAR_ADVANCE_12PX * len(name) + 24
    parts.append(f'<rect x="{lx:.1f}" y="{ly - 10:.1f}" width="22" height="12" fill="{INK["axis"]}"/>')
    parts.append(svg_text(lx + 28, ly, "link", fill=INK["title"]))
    lx += 28 + CHAR_ADVANCE_12PX * 4 + 24
    parts.append(
        f'<rect x="{lx + 0.75:.1f}" y="{ly - 9.25:.1f}" width="20.5" height="10.5" fill="url(#hatch-legend)" '
        f'stroke="{INK["axis"]}" stroke-width="1.5" stroke-dasharray="3 2"/>'
    )
    parts.append(svg_text(lx + 28, ly, "+ PDB (extra time for /debug:full)", fill=INK["title"]))

    column_width = (WIDTH - GRID_LEFT - ROW_LABEL_WIDTH - GRID_RIGHT - 2 * COLUMN_GAP) / len(CORPUS_IDS)

    def column_x(index: int) -> float:
        return GRID_LEFT + ROW_LABEL_WIDTH + index * (column_width + COLUMN_GAP)

    for index, corpus in enumerate(CORPUS_IDS):
        parts.append(svg_text(column_x(index), GRID_TOP - 12, CORPUS_LABELS[corpus], fill=INK["title"], size=13, weight="700"))

    # Non-LTO build types share one ms axis per column so their rows compare
    # directly; an LTO row runs codegen (~100x longer) and gets its own axis.
    shared_ceiling = {}
    for corpus in CORPUS_IDS:
        totals = [
            pair["pdb"][side]["wall_ms"]
            for (mode, cell_corpus), pair in cells.items()
            if cell_corpus == corpus and not MODES[mode]["lto"]
            for side, _ in SIDES
        ]
        shared_ceiling[corpus] = nice_ceiling(max(totals) * 1.02) if totals else 10.0

    for row, mode in enumerate(MODE_IDS):
        ry = GRID_TOP + row * ROW_HEIGHT
        parts.append(svg_text(GRID_LEFT, ry + 34, MODES[mode]["label"], fill=INK["title"], size=15, weight="700"))
        parts.append(svg_text(GRID_LEFT, ry + 52, "clang " + " ".join(
            flag for flag in MODES[mode]["cflags"] if flag != "-gcodeview"), fill=INK["muted"], size=11))
        for column, corpus in enumerate(CORPUS_IDS):
            cx = column_x(column)
            cell_height = ROW_HEIGHT - 14
            parts.append(
                f'<rect x="{cx:.1f}" y="{ry:.1f}" width="{column_width:.1f}" height="{cell_height}" '
                f'fill="{INK["plot"]}" rx="6"/>'
            )
            pair = cells.get((mode, corpus))
            if pair is None:
                parts.append(svg_text(cx + column_width / 2, ry + cell_height / 2 + 4,
                                      "not measured" + (": ThinLTO codegen is too slow" if MODES[mode]["lto"] else ""),
                                      fill=INK["muted"], size=11, anchor="middle"))
                continue
            if MODES[mode]["lto"]:
                ceiling = nice_ceiling(max(pair["pdb"][side]["wall_ms"] for side, _ in SIDES) * 1.02)
            else:
                ceiling = shared_ceiling[corpus]
            seconds = ceiling >= 10000
            x0 = cx + 10
            span = column_width - 20 - VALUE_LABEL_ROOM

            def x_of(value: float, x0: float = x0, span: float = span, ceiling: float = ceiling) -> float:
                return x0 + value / ceiling * span

            axis_y = ry + ROW_HEIGHT - 34
            for tick in range(3):
                value = ceiling * tick / 2
                parts.append(
                    f'<line x1="{x_of(value):.1f}" y1="{ry + 12:.1f}" x2="{x_of(value):.1f}" y2="{axis_y:.1f}" '
                    f'stroke="{INK["grid"]}" stroke-width="1"/>'
                )
                parts.append(svg_text(x_of(value), axis_y + 13, fmt_tick(value), fill=INK["axis"], size=10, anchor="middle"))

            noisy = cell_is_noisy(pair["nopdb"]) or cell_is_noisy(pair["pdb"])
            visible = not noisy and pdb_is_visible(pair)
            for index, (side, _) in enumerate(SIDES):
                by = ry + 22 + index * (BAR_HEIGHT + BAR_GAP)
                total = pair["pdb"][side]["wall_ms"]
                base = pair["nopdb"][side]["wall_ms"] if visible else total
                color = INK[side]
                parts.append(
                    f'<rect x="{x0:.1f}" y="{by:.1f}" width="{x_of(base) - x0:.1f}" height="{BAR_HEIGHT}" fill="{color}"/>'
                )
                if visible:
                    parts.append(
                        f'<rect x="{x_of(base) + 0.75:.1f}" y="{by + 0.75:.1f}" '
                        f'width="{max(x_of(total) - x_of(base) - 1.5, 0.0):.1f}" height="{BAR_HEIGHT - 1.5}" '
                        f'fill="url(#hatch-{side})" stroke="{color}" stroke-width="1.5" stroke-dasharray="3 2"/>'
                    )
                label = fmt_time(total, seconds)
                bold = "700" if side == "candidate" else "normal"
                parts.append(svg_text(x_of(total) + 6, by + 14, label, fill=INK["title"], size=12, weight=bold))
                if side == "candidate":
                    change = -pair["pdb"]["speedup_percent"]
                    # Bold digits run wider than the plain-text advance.
                    parts.append(svg_text(x_of(total) + 14 + CHAR_ADVANCE_12PX * 1.1 * len(label), by + 14,
                                          f"{change:+.0f}%", fill=INK["candidate"], size=12, weight="700"))
            if noisy:
                note = "timing noisy on this runner: split not shown"
            elif visible:
                pdb_stock = pair["pdb"]["baseline"]["wall_ms"] - pair["nopdb"]["baseline"]["wall_ms"]
                pdb_ours = pair["pdb"]["candidate"]["wall_ms"] - pair["nopdb"]["candidate"]["wall_ms"]
                # The link-only change is reported only when its paired IQR
                # excludes zero; hosted runners occasionally go bimodal on
                # short links, and a median then says nothing.
                nopdb = pair["nopdb"]
                if nopdb["speedup_percent_q1"] <= 0 <= nopdb["speedup_percent_q3"]:
                    link_text = "link within noise"
                else:
                    link_text = f"link {-nopdb['speedup_percent']:+.0f}%"
                note = (
                    f"PDB {fmt_time(pdb_stock, seconds)} → {fmt_time(pdb_ours, seconds)} "
                    f"({100 * (pdb_ours / pdb_stock - 1):+.0f}%) · {link_text}"
                )
            else:
                note = "PDB cost within noise" + (": codegen dominates" if MODES[mode]["lto"] else "")
            parts.append(svg_text(x0, ry + 22 + 2 * (BAR_HEIGHT + BAR_GAP) + 6, note, fill=INK["muted"], size=11))

    parts.append(svg_text(24, height - 16, provenance_line(latest), fill=INK["muted"], size=11))
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


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
            "variants": sorted({cell["variant"] for cell in mode_cells}),
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
            "note": MODES[mode]["note"],
        }
        for mode in MODE_IDS
    ]


def variants_summary() -> list[dict]:
    return [
        {
            "id": variant,
            "label": VARIANTS[variant]["label"],
            "link_flags": list(VARIANTS[variant]["link_flags"]),
            "pdb": bool(VARIANTS[variant]["pdb"]),
        }
        for variant in VARIANT_IDS
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
        variant = raw.get("variant")
        if variant not in VARIANTS:
            fail(f"{path}: unknown or missing link variant {variant!r}")
        gate = raw["gate"]["candidate"]
        if (gate["pdb"] is not None) != bool(VARIANTS[variant]["pdb"]):
            fail(f"{path}: PDB gate does not match variant {variant!r}")

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
                "variant": variant,
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
                # Raw per-link wall times in execution order, for diagnosing noisy cells.
                "samples_ms": {
                    name: [round(float(v), 3) for v in values]
                    for name, values in sorted(raw.get("samples_ms", {}).items())
                },
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
    # Every row of the chart must have data.
    missing = [mode for mode in MODE_IDS if not any(cell["mode"] == mode for cell in cells)]
    if missing:
        fail(f"{cells_dir}: no cells for build mode(s) {missing}")
    seen: set[tuple] = set()
    for cell in cells:
        identity = (cell["mode"], cell["corpus"], cell["threads"], cell["variant"])
        if identity in seen:
            fail(f"{cells_dir}: duplicate cell {identity}")
        seen.add(identity)
    # The chart stacks the PDB's extra time on the link without it, so every
    # measured point needs both variants.
    for mode, corpus, threads, _ in seen:
        for variant in VARIANT_IDS:
            if (mode, corpus, threads, variant) not in seen:
                fail(f"{cells_dir}: {mode}/{corpus}/t{threads} has no {variant!r} cell")
    cells.sort(
        key=lambda cell: (
            MODE_IDS.index(cell["mode"]),
            CORPUS_IDS.index(cell["corpus"]),
            cell["threads"],
            VARIANT_IDS.index(cell["variant"]),
        )
    )
    return cells


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
        f"count ({escaped(cores_text)}) when that is larger than 4; the chart "
        f"shows each build type at its highest, {escaped(peak_threads)} threads here. The headline "
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
    mode_items = "".join(
        f"<li><b>{escaped(MODES[mode]['label'])}</b>: <code>{escaped(mode_flags_text(mode))}</code>; "
        f"{escaped(MODES[mode]['note'])}.</li>"
        for mode in MODE_IDS
    )
    variant_items = "".join(
        f"<li><b>{escaped(VARIANTS[variant]['label'])}</b>: "
        f"<code>{escaped(' '.join(VARIANTS[variant]['link_flags']) or 'no /debug')}</code></li>"
        for variant in VARIANT_IDS
    )

    def iqr(cell: dict) -> str:
        return (
            f"{cell['speedup_percent']:+.1f}% "
            f"({cell['speedup_percent_q1']:+.1f}..{cell['speedup_percent_q3']:+.1f})"
        )

    rows = "".join(
        "<tr>"
        f"<td>{escaped(MODES[cell['mode']]['label'])}</td>"
        f"<td>{escaped(VARIANTS[cell['variant']]['label'])}</td>"
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
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>llvm-ld link speed</title>
<style>body{{font:15px {FONT_STACK};max-width:1100px;margin:auto;padding:24px;color:#e6edf3;background:#0d1117}}
table{{border-collapse:collapse;width:100%;margin:16px 0;font-size:13px}}th,td{{border:1px solid #30363d;padding:6px;text-align:left}}
img{{max-width:100%;height:auto}}code,pre{{overflow-wrap:anywhere;white-space:pre-wrap}}a{{color:#4493f8}}</style>
</head><body>
<h1>llvm-ld link speed</h1>
<p>How long a link takes, and where llvm-ld saves the time, right now. Every number is
a paired A/B measured in one run on one machine: the current linker against a
baseline built from the payload as it stood before the link-speed patches,
linking the same corpora interleaved. Hosted runners are shared and noisy, so
times are only compared within a run, never across runs.</p>
<p>Every cell is gated on byte-identical output: the candidate's EXE (and PDB,
when the variant writes one) match the baseline's exactly, and both are
self-deterministic. A speedup that changes output bytes is a bug, not a result.</p>
<nav><a href="#overview">chart</a> &middot; <a href="#cells">all cells</a> &middot;
<a href="#caveats">scope and caveats</a> &middot; <a href="latest.json">validated latest data</a></nav>
<h2 id="overview">Link time, and what the PDB adds</h2>
<img src="{CHART}" alt="Link time of stock lld-link and llvm-ld by build type and corpus size, split into the link itself and the extra time the PDB adds">
<p>Each corpus is linked twice with the same objects, once per variant, so the hatched segment is only the PDB's extra time:</p>
<ul>{variant_items}</ul>
<p>Build types:</p>
<ul>{mode_items}</ul>
<h2 id="cells">All cells</h2>
<table><thead><tr><th>Build</th><th>Variant</th><th>Corpus</th><th>Threads</th><th>Runs</th><th>Saved (IQR)</th>
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
        "variants": variants_summary(),
        "cells": cells,
        "actions_run_url": args.actions_run_url,
        "reproduction_command": (
            "python tests/perf/gen_corpus.py --out build-perf/corpus --mode release --profile large\n"
            "python tests/perf/bench.py --candidate build/llvm-ld-direct "
            "--baseline build-perf/baseline/llvm-ld-direct "
            "--corpus build-perf/corpus/release/large --variant pdb --threads 4 --runs 9"
        ),
    }

    output = args.output_dir
    ensure_empty_output(output)
    (output / ".nojekyll").write_bytes(b"")
    (output / "latest.json").write_bytes(compact_json(latest))
    (output / CHART).write_bytes(link_speed_svg(latest))
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
