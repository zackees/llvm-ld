#!/usr/bin/env python3
"""Triage a byte-diff between two PE (or PDB) outputs from the gold link test.

Diagnostic only: exits non-zero on its OWN errors (bad args, unreadable files), never because
the two inputs differ. stdlib-only, network-free, no pefile dependency - parses PE/COFF headers
with struct directly.
"""
from __future__ import annotations
import argparse, pathlib, shutil, struct, subprocess, sys
from typing import NamedTuple

class Section(NamedTuple):
    name: str
    virtual_address: int
    raw_offset: int
    raw_size: int

class PeLayout(NamedTuple):
    sections: list[Section]
    size_of_headers: int

def parse_pe_sections(data: bytes) -> PeLayout:
    if len(data) < 0x40 or data[0:2] != b"MZ":
        raise ValueError("not a PE file (missing MZ magic)")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if len(data) < pe_offset + 24 or data[pe_offset:pe_offset + 4] != b"PE\x00\x00":
        raise ValueError("not a PE file (missing PE\\0\\0 signature)")
    coff_off = pe_offset + 4
    machine, num_sections, _ts, _symtab, _numsyms, opt_size, _chars = struct.unpack_from(
        "<HHIIIHH", data, coff_off)
    opt_off = coff_off + 20
    if opt_size < 2:
        raise ValueError("optional header too small")
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic not in (0x10B, 0x20B):
        raise ValueError(f"unexpected optional header magic {magic:#x}")
    # SizeOfHeaders sits at the same +60 offset in both PE32 and PE32+: PE32 has a 4-byte
    # BaseOfData field that PE32+ lacks, but PE32+'s ImageBase is 8 bytes instead of 4, so the
    # Windows-specific fields (and SizeOfHeaders within them) start at the same offset either way.
    size_of_headers = struct.unpack_from("<I", data, opt_off + 60)[0]
    sec_off = opt_off + opt_size
    sections = []
    for i in range(num_sections):
        rec = data[sec_off + i * 40: sec_off + (i + 1) * 40]
        if len(rec) < 40:
            break
        name = rec[0:8].rstrip(b"\x00").decode("latin1")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", rec, 8)
        sections.append(Section(name, vaddr, rawptr, rawsize))
    return PeLayout(sections, size_of_headers)

def locate_offset(layout: PeLayout, offset: int) -> str:
    if offset < layout.size_of_headers:
        return f"PE/COFF header region (offset < SizeOfHeaders={layout.size_of_headers:#x})"
    for s in layout.sections:
        if s.raw_offset <= offset < s.raw_offset + s.raw_size:
            rel = offset - s.raw_offset
            return f"section '{s.name}' (file offset {s.raw_offset:#x}..{s.raw_offset + s.raw_size:#x}, +{rel:#x})"
    return "outside any mapped section (trailing data / certificate table / padding)"

def first_diff(a: bytes, b: bytes) -> tuple[int, int] | None:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            count = sum(1 for j in range(n) if a[j] != b[j]) + abs(len(a) - len(b))
            return i, count
    if len(a) != len(b):
        return n, abs(len(a) - len(b))
    return None

def run_tool(args: list[str]) -> str | None:
    exe = shutil.which(args[0])
    if not exe:
        return None
    try:
        proc = subprocess.run([exe] + args[1:], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return f"<{args[0]} failed to run: {e}>"
    return proc.stdout + proc.stderr

def print_section(title: str) -> None:
    print(f"\n=== {title} ===")

def triage_pe(left: pathlib.Path, right: pathlib.Path) -> None:
    a, b = left.read_bytes(), right.read_bytes()
    print_section(f"byte diff: {left.name} vs {right.name}")
    if len(a) != len(b):
        print(f"size differs: {len(a)} vs {len(b)} bytes")
    diff = first_diff(a, b)
    if diff is None:
        print("files are byte-identical")
        return
    offset, count = diff
    print(f"first differing byte at offset {offset:#x} ({offset}); total differing bytes: {count}")
    try:
        layout = parse_pe_sections(a)
        print(f"maps to: {locate_offset(layout, offset)}")
        print("sections:")
        for s in layout.sections:
            print(f"  {s.name:<10} raw={s.raw_offset:#x}+{s.raw_size:#x} vaddr={s.virtual_address:#x}")
    except ValueError as e:
        print(f"could not parse PE headers: {e}")

    print_section("llvm-readobj file-headers")
    for f in (left, right):
        out = run_tool(["llvm-readobj", "--file-headers", "--coff-debug-directory", "--sections", str(f)])
        print(f"--- {f.name} ---")
        print(out if out is not None else "<llvm-readobj not found on PATH>")

    print_section("llvm-objdump disassembly (informational, may be large)")
    out_a = run_tool(["llvm-objdump", "-d", "--no-show-raw-insn", str(left)])
    out_b = run_tool(["llvm-objdump", "-d", "--no-show-raw-insn", str(right)])
    if out_a is None or out_b is None:
        print("<llvm-objdump not found on PATH>")
    elif out_a == out_b:
        print("disassembly is textually identical (difference is likely non-code: PDB GUID, padding, table ordering)")
    else:
        print("disassembly differs; run llvm-objdump manually on both files for a full diff")

def triage_pdb(left: pathlib.Path, right: pathlib.Path, first_offset: int | None) -> None:
    print_section(f"PDB triage: {left.name} vs {right.name}")
    pdbutil = shutil.which("llvm-pdbutil")
    if not pdbutil:
        print("<llvm-pdbutil not found on PATH; skipping PDB triage>")
        return
    if first_offset is not None:
        print_section("llvm-pdbutil explain (at PE diff offset, informational only)")
        for f in (left, right):
            out = run_tool(["llvm-pdbutil", "explain", f"--offset={first_offset}", str(f)])
            print(f"--- {f.name} ---")
            print(out)
    print_section("ranked likely causes (check confirming each)")
    causes = [
        ("differing working directory at link time",
         "S_ENVBLOCK 'cwd' field in the '* Linker *' module, via `llvm-pdbutil dump --symbols` grepped for ENVBLOCK"),
        ("differing argv[0] (stock lld-link.exe path vs runner path)",
         "S_ENVBLOCK 'exe' field in the same module"),
        ("differing flags (missing /brepro, /manifest:no, or /lldignoreenv on one side)",
         "S_ENVBLOCK 'cmd' field in the same module"),
        ("differing input object/library paths",
         "`llvm-pdbutil dump --modules` module source file list"),
        ("leaked SOURCE_DATE_EPOCH or LLD_REPRODUCE from the invoking environment",
         "re-run with both vars explicitly cleared; /lldignoreenv does not cover them"),
    ]
    for i, (cause, check) in enumerate(causes, 1):
        print(f"{i}. {cause}\n   confirm via: {check}")
    print_section("llvm-pdbutil pdb2yaml (informational dump, no diff subcommand exists at this LLVM version)")
    for f in (left, right):
        out = run_tool(["llvm-pdbutil", "pdb2yaml", str(f)])
        print(f"--- {f.name} (first 4000 chars) ---")
        print((out or "<failed>")[:4000])

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("left", type=pathlib.Path)
    p.add_argument("right", type=pathlib.Path)
    p.add_argument("--left-pdb", type=pathlib.Path, default=None)
    p.add_argument("--right-pdb", type=pathlib.Path, default=None)
    args = p.parse_args()

    for f in (args.left, args.right):
        if not f.is_file():
            print(f"error: not a file: {f}", file=sys.stderr)
            return 1

    triage_pe(args.left, args.right)

    if args.left_pdb and args.right_pdb:
        a, b = args.left.read_bytes(), args.right.read_bytes()
        diff = first_diff(a, b)
        triage_pdb(args.left_pdb, args.right_pdb, diff[0] if diff else None)
    elif args.left_pdb or args.right_pdb:
        print("\nwarning: only one of --left-pdb/--right-pdb given; skipping PDB triage", file=sys.stderr)

    return 0

if __name__ == "__main__":
    sys.exit(main())
