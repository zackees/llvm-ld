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

RT_MANIFEST = 24

def rva_to_offset(layout: PeLayout, rva: int) -> int:
    for s in layout.sections:
        if s.virtual_address <= rva < s.virtual_address + s.raw_size:
            return s.raw_offset + (rva - s.virtual_address)
    raise ValueError(f"RVA {rva:#x} not covered by any section")

def find_rsrc_section(layout: PeLayout) -> Section:
    for s in layout.sections:
        if s.name == ".rsrc":
            return s
    raise ValueError("no .rsrc section (PE has no embedded resources)")

# Minimal IMAGE_RESOURCE_DIRECTORY walk, three levels deep (Type -> Name -> Language), matching
# the layout lld-link's writeResEntryHeader produces: RT_MANIFEST at the type level, the manifest
# ID (1 for the default EXE resource) at the name level, and a single language leaf below that.
# Named entries (high bit set on Id/NameOffset) are skipped since resources of interest here are
# always numeric IDs.
def read_resource_directory(data: bytes, rsrc: Section, dir_offset: int) -> list[tuple[int, int]]:
    base = rsrc.raw_offset
    named_count, id_count = struct.unpack_from("<HH", data, base + dir_offset + 12)
    entries = []
    entry_off = base + dir_offset + 16 + named_count * 8
    for i in range(id_count):
        eid, value = struct.unpack_from("<II", data, entry_off + i * 8)
        entries.append((eid, value))
    return entries

def extract_manifest_bytes(data: bytes, layout: PeLayout, resource_id: int | None) -> bytes:
    rsrc = find_rsrc_section(layout)
    type_entries = read_resource_directory(data, rsrc, 0)
    type_match = [v for (eid, v) in type_entries if eid == RT_MANIFEST]
    if not type_match:
        raise ValueError(f"no RT_MANIFEST (type {RT_MANIFEST}) entry in .rsrc directory")
    # High bit of the subdirectory offset marks "points to another directory" rather than a leaf.
    name_dir_offset = type_match[0] & 0x7FFFFFFF
    name_entries = read_resource_directory(data, rsrc, name_dir_offset)
    if resource_id is not None:
        name_match = [v for (eid, v) in name_entries if eid == resource_id]
        if not name_match:
            ids = ", ".join(str(eid) for eid, _ in name_entries)
            raise ValueError(f"resource id {resource_id} not found under RT_MANIFEST (have: {ids})")
    else:
        if not name_entries:
            raise ValueError("RT_MANIFEST type directory has no name entries")
        name_match = [name_entries[0][1]]
    lang_dir_offset = name_match[0] & 0x7FFFFFFF
    lang_entries = read_resource_directory(data, rsrc, lang_dir_offset)
    if not lang_entries:
        raise ValueError("RT_MANIFEST name directory has no language entries")
    leaf_offset = lang_entries[0][1]
    if leaf_offset & 0x80000000:
        raise ValueError("expected a leaf data entry at the language level, found another subdirectory")
    leaf_base = rsrc.raw_offset + leaf_offset
    data_rva, data_size = struct.unpack_from("<II", data, leaf_base)
    file_off = rva_to_offset(layout, data_rva)
    return data[file_off:file_off + data_size]

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
    p.add_argument("right", type=pathlib.Path, nargs="?")
    p.add_argument("--left-pdb", type=pathlib.Path, default=None)
    p.add_argument("--right-pdb", type=pathlib.Path, default=None)
    p.add_argument("--extract-manifest", action="store_true",
                    help="extract the embedded RT_MANIFEST resource from 'left' instead of diffing "
                         "left vs right; writes to --out (or stdout, text-decoded, if omitted)")
    p.add_argument("--resource-id", type=int, default=None,
                    help="manifest resource (name) id to extract; defaults to the first one found "
                         "(the default EXE manifest resource id is 1)")
    p.add_argument("--out", type=pathlib.Path, default=None,
                    help="--extract-manifest: file to write the extracted manifest bytes to")
    args = p.parse_args()

    if args.extract_manifest:
        if not args.left.is_file():
            print(f"error: not a file: {args.left}", file=sys.stderr)
            return 1
        try:
            data = args.left.read_bytes()
            layout = parse_pe_sections(data)
            manifest = extract_manifest_bytes(data, layout, args.resource_id)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.out:
            args.out.write_bytes(manifest)
            print(f"wrote {len(manifest)} bytes to {args.out}")
        else:
            sys.stdout.write(manifest.decode("utf-8", errors="replace"))
        return 0

    if args.right is None:
        print("error: 'right' is required unless --extract-manifest is given", file=sys.stderr)
        return 1

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
