#!/usr/bin/env python3
"""Refresh provenance/llvm-source-closure.json entries for the patched payload files.

The closure is the SHA-256 of every file under llvm-project/. It is derived mechanically from
build evidence (tools/audit_build.py) and normally never edited by hand. The one legitimate
reason for a hash to change without a payload re-import is a deliberate local patch to an
upstream file, and every such file must be declared in provenance/payload-prune.json under
`patched`. This tool recomputes the hashes of exactly those declared files and refuses to touch
anything else, so an undeclared edit still fails tools/verify.py.

Usage: refresh_patched_closure.py [--check]
"""
from __future__ import annotations
import argparse, hashlib, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report stale hashes without rewriting")
    a = ap.parse_args()
    closure_path = ROOT / "provenance" / "llvm-source-closure.json"
    closure = json.loads(closure_path.read_text(encoding="utf-8"))
    prune = json.loads((ROOT / "provenance" / "payload-prune.json").read_text(encoding="utf-8"))
    patched = [entry["path"] for entry in prune.get("patched", [])]
    files = closure["files"]
    stale = []
    for rel in patched:
        if rel not in files:
            raise SystemExit(f"patched file is not in the closure: {rel}")
        actual = hashlib.sha256((ROOT / "llvm-project" / rel).read_bytes()).hexdigest()
        if files[rel] != actual:
            stale.append(rel)
            if not a.check:
                files[rel] = actual
    # Everything not declared as patched must still match; otherwise a stray edit is hiding.
    undeclared = []
    for rel, want in files.items():
        if rel in patched:
            continue
        path = ROOT / "llvm-project" / rel
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != want:
            undeclared.append(rel)
    if undeclared:
        raise SystemExit("undeclared payload edits (add to payload-prune.json `patched` or revert): "
                         + ", ".join(undeclared[:20]))
    if a.check:
        if stale:
            print("stale closure hashes: " + ", ".join(stale)); return 1
        print("closure hashes up to date"); return 0
    if stale:
        closure_path.write_text(json.dumps(closure, indent=2) + "\n", encoding="utf-8")
        print("refreshed: " + ", ".join(stale))
    else:
        print("nothing to refresh")
    return 0

if __name__ == "__main__":
    sys.exit(main())
