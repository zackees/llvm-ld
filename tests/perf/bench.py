#!/usr/bin/env python3
"""Link-speed benchmark with a byte-identity gate.

Runs a candidate linker binary and a baseline binary over the same corpus, interleaved, and
reports wall / CPU / peak-RSS medians plus the paired ratio, with interquartile ranges. Before
timing, it links twice with each and requires the EXE (and the PDB, when the corpus's build mode
produces one) to be byte-identical and self-deterministic: a speedup that changes output bytes is
a bug, not a win. The corpus directory's manifest.json (tests/perf/gen_corpus.py) says whether the
mode produces a PDB. Works with any binary that takes `winlink lld-link <args>` (llvm-ld-direct,
llvm-ld-runner) or a bare lld-link (--bare).

Usage:
  bench.py --candidate build/llvm-ld-direct --baseline build-perf/baseline/llvm-ld-direct \
           --corpus build-perf/corpus/release-pdb/medium [--runs 10] [--threads N] [--extra /opt:noicf ...]
  bench.py --candidate ... --corpus ... --time     # lld /time phase breakdown (candidate only)
"""
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, resource, shutil, statistics, subprocess, sys, tempfile, time

def sha(p: pathlib.Path) -> str: return hashlib.sha256(p.read_bytes()).hexdigest()

def run(exe: str, bare: bool, rsp: pathlib.Path, out: pathlib.Path, extra: list[str], cwd: pathlib.Path,
        capture: bool = False, pdb: bool = True) -> dict:
    pdb_arg = [f"/pdb:{out.with_suffix('.pdb')}"] if pdb else []
    argv = [exe] + ([] if bare else ["winlink", "lld-link"]) + [f"@{rsp}", f"/out:{out}", *pdb_arg, *extra]
    t0 = time.perf_counter()
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.PIPE if capture else subprocess.DEVNULL)
    _, status, ru = os.wait4(proc.pid, 0)
    wall = time.perf_counter() - t0
    stdout = proc.stdout.read().decode(errors="replace") if capture else ""
    stderr = proc.stderr.read().decode(errors="replace") if capture else ""
    if status != 0:
        raise SystemExit(f"link failed ({status}) for {exe}:\n{stderr or '(stderr not captured; rerun with --time)'}")
    return dict(wall=wall, cpu=ru.ru_utime + ru.ru_stime, rss=ru.ru_maxrss * 1024, stdout=stdout, stderr=stderr)

def quartiles(values: list[float]) -> tuple[float, float, float]:
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, q2, q3

def summarize(candidate: list[dict], baseline: list[dict]) -> dict:
    """Paired statistics over interleaved samples (dicts with wall/cpu/rss in seconds and bytes).

    Medians, plus q1/q3 of each side's wall time and of the paired wall ratio. The speedup is
    derived from the paired median ratio, so speedup = 100 * (1 - median ratio) holds exactly and
    the speedup's q1 comes from the ratio's q3 (a larger ratio is a smaller speedup).
    """
    if len(candidate) != len(baseline) or len(candidate) < 4:
        raise ValueError("need at least 4 paired samples for quartiles")
    def side(samples: list[dict]) -> dict:
        q1, _, q3 = quartiles([s["wall"] * 1000 for s in samples])
        return dict(wall_ms=statistics.median(s["wall"] for s in samples) * 1000,
                    wall_ms_q1=q1, wall_ms_q3=q3,
                    cpu_ms=statistics.median(s["cpu"] for s in samples) * 1000,
                    rss_mb=statistics.median(s["rss"] for s in samples) / 1e6)
    c, b = side(candidate), side(baseline)
    ratios = [x["wall"] / y["wall"] for x, y in zip(candidate, baseline)]
    r1, _, r3 = quartiles(ratios)
    median_ratio = statistics.median(ratios)
    return dict(candidate=c, baseline=b,
                paired_wall_ratio_median=median_ratio, paired_wall_ratio_q1=r1, paired_wall_ratio_q3=r3,
                speedup_percent=100 * (1 - median_ratio),
                speedup_percent_q1=100 * (1 - r3), speedup_percent_q3=100 * (1 - r1),
                rss_delta_percent=100 * (c["rss_mb"] / b["rss_mb"] - 1),
                cpu_delta_percent=100 * (c["cpu_ms"] / b["cpu_ms"] - 1))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True); ap.add_argument("--baseline")
    ap.add_argument("--corpus", type=pathlib.Path, required=True)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--threads", type=int)
    ap.add_argument("--extra", nargs="*", default=[])
    ap.add_argument("--bare", action="store_true", help="binaries are bare lld-link, not the llvm-ld runner")
    ap.add_argument("--time", action="store_true", help="print lld /time output for the candidate and exit")
    ap.add_argument("--json", type=pathlib.Path)
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--max-rss-regress", type=float, default=5.0,
                    help="flag a peak-RSS increase above this percent (default 5)")
    a = ap.parse_args()
    corpus = a.corpus.resolve(); rsp = corpus / "link.rsp"
    manifest_path = corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    # Corpora from before build modes existed carry no "pdb" key; they always linked with /debug.
    has_pdb = bool(manifest.get("pdb", True))
    mode = manifest.get("mode")
    if a.json and a.baseline and a.runs < 4:
        raise SystemExit("--json needs --runs >= 4: a published cell must carry an interquartile range")
    extra = list(a.extra) + ([f"/threads:{a.threads}"] if a.threads else [])
    cand = str(pathlib.Path(a.candidate).resolve()); base = str(pathlib.Path(a.baseline).resolve()) if a.baseline else None
    # Comparing a binary against itself yields a meaningless ~0% and every
    # correctness gate still passes, because the outputs really are identical.
    # This happens for real: forget to rebuild after restoring sources and the
    # candidate silently *is* the baseline.
    if base and sha(pathlib.Path(cand)) == sha(pathlib.Path(base)):
        raise SystemExit(
            f"candidate and baseline are the same binary ({cand} == {base}); "
            "rebuild one of them before measuring"
        )
    work = pathlib.Path(tempfile.mkdtemp(prefix="llvm-ld-bench-"))
    try:
        if a.time:
            r = run(cand, a.bare, rsp, work / "t.exe", extra + ["/time"], corpus, capture=True, pdb=has_pdb)
            print(r["stdout"] + r["stderr"]); print(f"wall {r['wall']*1000:.0f} ms  cpu {r['cpu']*1000:.0f} ms  rss {r['rss']/1e6:.0f} MB")
            return 0
        # Byte-identity gate. Every link in this script writes to the same output path: lld embeds
        # the PDB basename in the EXE debug directory and the full command line (including /out:) in
        # the PDB's linker module, so distinct names would differ by construction.
        out = work / "out.exe"; pdb = work / "out.pdb"
        def link_and_hash(exe):
            pdb.unlink(missing_ok=True)
            run(exe, a.bare, rsp, out, extra, corpus, pdb=has_pdb)
            # A mode without a PDB must not produce one either: a stray PDB on one side only
            # would mean the two binaries disagree about what the link does.
            if pdb.exists() != has_pdb:
                raise SystemExit(f"{exe}: expected {'a' if has_pdb else 'no'} PDB for this build mode")
            if not has_pdb:
                return dict(exe=sha(out), pdb=None, exe_bytes=out.stat().st_size, pdb_bytes=None)
            return dict(exe=sha(out), pdb=sha(pdb), exe_bytes=out.stat().st_size, pdb_bytes=pdb.stat().st_size)
        gate = {}
        for name, exe in (("candidate", cand), ("baseline", base)):
            if not exe: continue
            gate[name] = link_and_hash(exe)
            if name == "candidate":
                shutil.copy(out, work / "candidate.exe")
                if has_pdb: shutil.copy(pdb, work / "candidate.pdb")
            # Self-determinism: a second link must reproduce the first.
            again = link_and_hash(exe)
            if again["exe"] != gate[name]["exe"] or again["pdb"] != gate[name]["pdb"]:
                raise SystemExit(f"{name} is not self-deterministic on this corpus "
                                 f"(exe {'same' if again['exe'] == gate[name]['exe'] else 'differs'}, "
                                 f"pdb {'same' if again['pdb'] == gate[name]['pdb'] else 'differs'})")
        if base and not a.no_gate:
            for k in (("exe", "pdb") if has_pdb else ("exe",)):
                if gate["candidate"][k] != gate["baseline"][k]:
                    shutil.copy(work / f"candidate.{k}", corpus / f"mismatch-candidate.{k}")
                    shutil.copy(work / f"out.{k}", corpus / f"mismatch-baseline.{k}")
                    raise SystemExit(f"BYTE-IDENTITY GATE FAILED: {k} differs (saved to {corpus}/mismatch-*.{k})")
            pdb_note = f", pdb {gate['candidate']['pdb_bytes']} B" if has_pdb else " (no PDB in this mode)"
            print(f"gate ok: exe {gate['candidate']['exe_bytes']} B{pdb_note} identical to baseline")
        samples = {"candidate": [], "baseline": []}
        for i in range(a.runs):
            order = [("candidate", cand), ("baseline", base)] if i % 2 == 0 else [("baseline", base), ("candidate", cand)]
            for name, exe in order:
                if exe: samples[name].append(run(exe, a.bare, rsp, out, extra, corpus, pdb=has_pdb))
        def med(name, key): return statistics.median(s[key] for s in samples[name])
        result = dict(corpus=str(corpus), mode=mode, runs=a.runs, threads=a.threads, extra=extra, gate=gate,
                      candidate=dict(wall_ms=med("candidate", "wall") * 1000, cpu_ms=med("candidate", "cpu") * 1000, rss_mb=med("candidate", "rss") / 1e6))
        line = f"candidate: wall {result['candidate']['wall_ms']:.1f} ms  cpu {result['candidate']['cpu_ms']:.1f} ms  rss {result['candidate']['rss_mb']:.0f} MB"
        if base:
            # Peak RSS is reported as a delta, not just two numbers: a change that buys wall
            # time by holding more memory is a trade to make deliberately, and this linker runs
            # as a library inside someone else's process.
            if a.runs >= 4:
                result.update(summarize(samples["candidate"], samples["baseline"]))
            else:
                result["baseline"] = dict(wall_ms=med("baseline", "wall") * 1000, cpu_ms=med("baseline", "cpu") * 1000, rss_mb=med("baseline", "rss") / 1e6)
                ratios = [c["wall"] / b["wall"] for c, b in zip(samples["candidate"], samples["baseline"])]
                result["paired_wall_ratio_median"] = statistics.median(ratios)
                result["speedup_percent"] = 100 * (1 - result["paired_wall_ratio_median"])
                result["rss_delta_percent"] = 100 * (result["candidate"]["rss_mb"] / result["baseline"]["rss_mb"] - 1)
                result["cpu_delta_percent"] = 100 * (result["candidate"]["cpu_ms"] / result["baseline"]["cpu_ms"] - 1)
            line += (f"\nbaseline:  wall {result['baseline']['wall_ms']:.1f} ms  cpu {result['baseline']['cpu_ms']:.1f} ms  rss {result['baseline']['rss_mb']:.0f} MB"
                     f"\nspeedup: {result['speedup_percent']:+.1f}% wall (paired median ratio {result['paired_wall_ratio_median']:.3f}, n={a.runs})"
                     f"\ncpu: {result['cpu_delta_percent']:+.1f}%   peak rss: {result['rss_delta_percent']:+.1f}%")
            if result["rss_delta_percent"] > a.max_rss_regress:
                line += (f"\nWARNING: peak RSS regressed {result['rss_delta_percent']:+.1f}% "
                         f"(> {a.max_rss_regress:.0f}%); justify the memory cost against the wall-time win")
        print(line)
        if a.json: a.json.write_text(json.dumps(result, indent=2) + "\n")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)

if __name__ == "__main__": sys.exit(main())
