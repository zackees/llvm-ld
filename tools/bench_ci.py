#!/usr/bin/env python3
"""link-benchmark CI helpers (#50): exact-input key, zccache sessions, job timings, floors.

Subcommands
  pgo-key        print the exact-input key of the optimized (PGO + ThinLTO + BOLT) llvm-ld-direct:
                 a hash of every input that can change that binary, plus tool identities. When
                 main moves without touching them (docs, charts, workflows) the key is unchanged
                 and the whole build is skipped by restoring the cached result.
  zc-start       start a zccache stats session; prints the session id
  zc-end         end it and write {"hits", "misses", "compilations", ...} to --out
  timing         write one job's timing record (name, seconds, warm, detail) to --out
  floors         check tools/bench_floors.json against the job timings and the published
                 latest.json; print a report (and to $GITHUB_STEP_SUMMARY) and exit 1 on any
                 violation. CI-time floors apply only when every build job was warm.

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Everything that can change the optimized llvm-ld-direct. provenance/llvm-source-closure.json
# carries a hash of every payload file (tools/verify.py enforces it), so it stands in for the
# 5,600-file payload. The generator decides the training workload, so it is an input too.
PGO_KEY_PATHS = [
    "provenance/llvm-source-closure.json",
    "provenance/payload-prune.json",
    "CMakeLists.txt",
    "exports.map",
    "cmake",
    "src",
    "include",
    "tools/direct_runner.cpp",
    "tools/pgo_build.py",
    "tools/ci_build.py",
    "tests/perf/gen_corpus.py",
]


def hash_paths(root: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for rel in paths:
        target = root / rel
        files = sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else [target]
        for path in files:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes() if path.exists() else b"<missing>")
            digest.update(b"\0")
    return digest.hexdigest()


def pgo_key(root: Path, identities: list[str]) -> str:
    digest = hashlib.sha256(hash_paths(root, PGO_KEY_PATHS).encode())
    for identity in identities:
        digest.update(b"\0" + identity.encode())
    return f"pgo-bolt-{sys.platform}-{digest.hexdigest()[:24]}"


def cmd_pgo_key(args: argparse.Namespace) -> int:
    identities = []
    for command in args.identity or []:
        out = subprocess.run(command, shell=True, capture_output=True, text=True).stdout.strip()
        identities.append(out.splitlines()[0] if out else command)
    key = pgo_key(ROOT, identities)
    print(key)
    if args.github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"key={key}\n")
    return 0


def zccache(args: argparse.Namespace) -> list[str]:
    return (args.zccache or os.environ.get("ZCCACHE") or "zccache").split()


def cmd_zc_start(args: argparse.Namespace) -> int:
    extra = ["--journal", str(args.journal)] if args.journal else []
    out = subprocess.run([*zccache(args), "session-start", "--stats", *extra], capture_output=True,
                         text=True, check=True).stdout
    print(json.loads(out.strip().splitlines()[-1])["session_id"])
    return 0


def cmd_zc_end(args: argparse.Namespace) -> int:
    out = subprocess.run([*zccache(args), "session-end", "--json", args.session], capture_output=True,
                         text=True).stdout
    try:
        stats = json.loads(out)
    except ValueError:
        stats = {"status": "error", "raw": out[-400:]}
    args.out.write_text(json.dumps(stats, indent=1) + "\n")
    print(f"zccache: {stats.get('compilations')} compilations, {stats.get('hits')} hits, "
          f"{stats.get('misses')} misses, {stats.get('non_cacheable')} non-cacheable")
    return 0


def cmd_timing(args: argparse.Namespace) -> int:
    record = {
        "job": args.job,
        "seconds": round(time.time() - args.start, 1),
        "warm": args.warm == "true",
        "detail": args.detail or "",
    }
    if args.zccache_stats and args.zccache_stats.exists():
        stats = json.loads(args.zccache_stats.read_text())
        record["zccache"] = {k: stats.get(k) for k in ("compilations", "hits", "misses", "non_cacheable")}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=1) + "\n")
    print(json.dumps(record))
    return 0


# ------------------------------------------------------------------ floors


def load_timings(directory: Path) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(directory.rglob("timing-*.json"))]


def is_noisy(cell: dict, wall_iqr: float, speedup_iqr: float) -> bool:
    for side in ("baseline", "candidate"):
        data = cell[side]
        if data["wall_ms_q3"] - data["wall_ms_q1"] > wall_iqr * data["wall_ms"]:
            return True
    return cell["speedup_percent_q3"] - cell["speedup_percent_q1"] > speedup_iqr


def evaluate(floors: dict, timings: list[dict], latest: dict | None, total_seconds: float | None) -> tuple[list[str], list[str]]:
    """Returns (violations, report lines)."""
    violations: list[str] = []
    report: list[str] = []

    # CI time: only on warm runs, because a cold build legitimately takes much longer.
    ci = floors["ci_time"]
    build_jobs = [t for t in timings if t["job"] in ci["build_jobs"]]
    missing = sorted(set(ci["build_jobs"]) - {t["job"] for t in build_jobs})
    warm = bool(build_jobs) and not missing and all(t["warm"] for t in build_jobs)
    report.append(f"CI time: run is {'WARM' if warm else 'COLD'} "
                  f"({', '.join(f'{t['job']}={'warm' if t['warm'] else 'cold'}' for t in build_jobs)}"
                  f"{'; missing timings: ' + ', '.join(missing) if missing else ''})")
    for t in timings:
        limit = ci["job_max_minutes"].get(t["job"], ci["job_max_minutes"].get(t["job"].split(":")[0]))
        line = f"  {t['job']}: {t['seconds'] / 60:.1f} min" + (f" (floor {limit} min)" if limit else "")
        if t.get("zccache"):
            z = t["zccache"]
            line += f", zccache {z.get('hits')} hits / {z.get('misses')} misses"
        report.append(line)
        if warm and limit and t["seconds"] > limit * 60:
            violations.append(f"warm {t['job']} took {t['seconds'] / 60:.1f} min > {limit} min")
    if total_seconds is not None:
        cold = ci["cold_reference_minutes"]
        improvement = 100 * (1 - total_seconds / 60 / cold)
        report.append(f"  whole run: {total_seconds / 60:.1f} min "
                      f"({improvement:+.0f}% vs the {cold}-min cold reference; floor "
                      f"{ci['total_max_minutes']} min = {100 * (1 - ci['total_max_minutes'] / cold):.0f}% faster)")
        if warm and total_seconds > ci["total_max_minutes"] * 60:
            violations.append(f"warm run took {total_seconds / 60:.1f} min > {ci['total_max_minutes']} min")
    if not warm:
        report.append("  (cold run: CI-time floors are reported, not enforced)")

    # Link speed: every run; noisy cells are skipped, and too many noisy cells is itself a failure.
    if latest is not None:
        link = floors["link_speed"]
        cells = latest["cells"]
        peak = {}
        for cell in cells:
            peak[cell["mode"]] = max(peak.get(cell["mode"], 0), int(cell["threads"]))
        noisy = [c for c in cells if is_noisy(c, link["noisy_wall_iqr"], link["noisy_speedup_iqr"])]
        report.append(f"link speed: {len(cells)} cells, {len(noisy)} noisy (max {link['max_noisy_cells']})")
        if len(noisy) > link["max_noisy_cells"]:
            violations.append(f"{len(noisy)} noisy cells > {link['max_noisy_cells']}")
        for rule in link["min_speedup_percent"]:
            matched = [c for c in cells
                       if c["mode"] in rule["modes"] and c["corpus"] in rule["corpora"]
                       and c["variant"] == rule["variant"] and int(c["threads"]) == peak[c["mode"]]]
            if not matched:
                report.append(f"  {rule['name']}: no matching cells")
                continue
            for cell in matched:
                where = f"{cell['mode']}/{cell['corpus']}/{cell['variant']}/t{cell['threads']}"
                if cell in noisy:
                    report.append(f"  {rule['name']} {where}: {cell['speedup_percent']:+.1f}% (noisy, skipped)")
                    continue
                ok = cell["speedup_percent"] >= rule["floor"]
                report.append(f"  {rule['name']} {where}: {cell['speedup_percent']:+.1f}% "
                              f"(floor {rule['floor']:+.0f}%) {'ok' if ok else 'BELOW FLOOR'}")
                if not ok:
                    violations.append(f"{where} speedup {cell['speedup_percent']:+.1f}% < {rule['floor']}%")
    return violations, report


def cmd_floors(args: argparse.Namespace) -> int:
    floors = json.loads(args.floors.read_text())
    timings = load_timings(args.timings) if args.timings and args.timings.exists() else []
    latest = json.loads(args.latest.read_text()) if args.latest and args.latest.exists() else None
    violations, report = evaluate(floors, timings, latest, args.total_seconds)
    text = "\n".join(["### Benchmark floors (#50)", "```", *report, "```"] +
                     ([f"**{len(violations)} floor violation(s):**", *[f"- {v}" for v in violations]]
                      if violations else ["All floors met."]))
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write(text + "\n")
    return 1 if violations else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    key = sub.add_parser("pgo-key")
    key.add_argument("--identity", action="append", help="a shell command whose first output line identifies a tool")
    key.add_argument("--github-output", action="store_true")
    key.set_defaults(func=cmd_pgo_key)
    start = sub.add_parser("zc-start")
    start.add_argument("--journal", type=Path)
    start.add_argument("--zccache")
    start.set_defaults(func=cmd_zc_start)
    end = sub.add_parser("zc-end")
    end.add_argument("--session", required=True)
    end.add_argument("--out", type=Path, required=True)
    end.add_argument("--zccache")
    end.set_defaults(func=cmd_zc_end)
    timing = sub.add_parser("timing")
    timing.add_argument("--job", required=True)
    timing.add_argument("--start", type=float, required=True, help="epoch seconds the job's work started")
    timing.add_argument("--warm", choices=["true", "false"], required=True)
    timing.add_argument("--detail")
    timing.add_argument("--zccache-stats", type=Path)
    timing.add_argument("--out", type=Path, required=True)
    timing.set_defaults(func=cmd_timing)
    check = sub.add_parser("floors")
    check.add_argument("--floors", type=Path, default=ROOT / "tools" / "bench_floors.json")
    check.add_argument("--timings", type=Path)
    check.add_argument("--latest", type=Path)
    check.add_argument("--total-seconds", type=float)
    check.set_defaults(func=cmd_floors)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
