#!/usr/bin/env python3
"""Compare an llvm-ld link against the same link by the official release lld-link.exe (issue #30).

The two linkers are built from the same llvmorg-23.1.0 source, and the outputs are byte-identical
except for exactly one explained build-configuration difference plus the fields derived from it:

1. Section-contribution DataCrc of chunks with no contents: uninitialized-data (BSS) chunks and
   zero-size chunks. lld computes it with JamCRC(0) over the chunk's contents, which for these
   is an empty, null-data ArrayRef. This repo
   builds LLVM with LLVM_ENABLE_ZLIB=OFF (CMakeLists.txt), where llvm::crc32 returns the CRC
   unchanged for empty input, so DataCrc = 0. The release is built with zlib, and zlib's
   crc32(crc, NULL, 0) returns 0 for a null buffer regardless of `crc`, so JamCRC's final
   inversion yields DataCrc = 0xFFFFFFFF. Our link-speed patches are not involved: the same
   JamCRC(0)/getContents() call is upstream's, and our slice-by-8 llvm::crc32 matches upstream's
   non-zlib implementation.
2. The build-id fields derived from those bytes (lld/COFF/PDB.cpp and Writer.cpp writeBuildId).
   The PDB GUID is a hash of the PDB contents (so it covers the CRCs), with GUID[8:16] fixed to
   "LLD PDB."; the PDB info stream Signature is GUID[0:4]; the image's CodeView RSDS record
   copies the GUID; and /Brepro sets the COFF TimeDateStamp and every debug-directory
   TimeDateStamp to xxh3 of the image, which contains the GUID. Each of those relationships is
   checked, so these fields are verified to hold hashes rather than just being masked.

Every other byte must be equal. Only DataCrc pairs of empty-content (BSS or zero-size) chunks
that are exactly (ours 0, release 0xFFFFFFFF) are normalized; any other CRC difference fails.
stdlib only.

Exit status: 0 identical or identical modulo the explained fields, 1 different, 2 usage/parse error.
"""
from __future__ import annotations

import argparse
import pathlib
import struct
import sys

IMAGE_SCN_CNT_UNINITIALIZED_DATA = 0x00000080
IMAGE_DEBUG_TYPE_CODEVIEW = 2
MSF_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"
PDB_INFO_STREAM = 1
PDB_DBI_STREAM = 3
DBI_HEADER_SIZE = 64
SC_VER60 = 0xEFFE0000 + 19970605
SC_V2 = 0xEFFE0000 + 20140516


class CompareError(Exception):
    pass


def _u32(data: bytes | bytearray, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _put_u32(data: bytearray, off: int, value: int) -> None:
    struct.pack_into("<I", data, off, value)


# --- PE ------------------------------------------------------------------------------------------

def pe_build_id_fields(data: bytes) -> dict:
    """Locate the /Brepro-derived fields of a PE32+ image. Returns file offsets and values."""
    if data[:2] != b"MZ":
        raise CompareError("not a PE file")
    pe = _u32(data, 0x3C)
    if data[pe:pe + 4] != b"PE\x00\x00":
        raise CompareError("missing PE signature")
    coff = pe + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    if struct.unpack_from("<H", data, opt)[0] != 0x20B:
        raise CompareError("not a PE32+ image")
    debug_rva, debug_size = struct.unpack_from("<II", data, opt + 112 + 6 * 8)
    sections = []
    for i in range(num_sections):
        s = opt + opt_size + 40 * i
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, s + 8)
        sections.append((va, max(vsize, raw_size), raw_ptr))

    def rva_to_off(rva: int) -> int:
        for va, size, raw_ptr in sections:
            if va <= rva < va + size:
                return raw_ptr + (rva - va)
        raise CompareError(f"RVA {rva:#x} not in any section")

    fields = {"timestamp_off": coff + 4, "timestamp": _u32(data, coff + 4),
              "debug_timestamp_offs": [], "guid_off": None, "guid": None}
    if debug_size:
        base = rva_to_off(debug_rva)
        for i in range(debug_size // 28):
            entry = base + 28 * i
            fields["debug_timestamp_offs"].append(entry + 4)
            dtype = _u32(data, entry + 12)
            if dtype == IMAGE_DEBUG_TYPE_CODEVIEW:
                cv = _u32(data, entry + 24)
                if data[cv:cv + 4] != b"RSDS":
                    raise CompareError("CodeView debug entry is not RSDS")
                fields["guid_off"] = cv + 4
                fields["guid"] = bytes(data[cv + 4:cv + 20])
    return fields


def check_pe_build_id(name: str, data: bytes, f: dict) -> None:
    ts = f["timestamp"]
    for off in f["debug_timestamp_offs"]:
        if _u32(data, off) != ts:
            raise CompareError(f"{name}: debug directory TimeDateStamp != COFF TimeDateStamp")
    if f["guid"] is not None:
        if f["guid"][8:] != b"LLD PDB.":
            raise CompareError(f"{name}: RSDS GUID is not an lld content-hash GUID")


def normalize_pe(data: bytearray, f: dict) -> None:
    _put_u32(data, f["timestamp_off"], 0)
    for off in f["debug_timestamp_offs"]:
        _put_u32(data, off, 0)
    if f["guid_off"] is not None:
        data[f["guid_off"]:f["guid_off"] + 16] = bytes(16)


# --- PDB (MSF) -----------------------------------------------------------------------------------

class Msf:
    def __init__(self, data: bytes):
        if data[:32] != MSF_MAGIC:
            raise CompareError("not an MSF 7.00 PDB")
        self.bs = _u32(data, 32)
        num_dir_bytes = _u32(data, 44)
        block_map = _u32(data, 52)
        n_dir_blocks = -(-num_dir_bytes // self.bs)
        dir_blocks = struct.unpack_from(f"<{n_dir_blocks}I", data, block_map * self.bs)
        directory = b"".join(data[b * self.bs:(b + 1) * self.bs] for b in dir_blocks)[:num_dir_bytes]
        n_streams = _u32(directory, 0)
        sizes = struct.unpack_from(f"<{n_streams}I", directory, 4)
        pos = 4 + 4 * n_streams
        self.streams: list[tuple[int, list[int]]] = []
        for size in sizes:
            size = 0 if size == 0xFFFFFFFF else size
            n = -(-size // self.bs)
            self.streams.append((size, list(struct.unpack_from(f"<{n}I", directory, pos))))
            pos += 4 * n
        self.data = data

    def file_offset(self, stream: int, off: int) -> int:
        size, blocks = self.streams[stream]
        if off >= size:
            raise CompareError(f"offset {off} past end of stream {stream}")
        return blocks[off // self.bs] * self.bs + off % self.bs

    def u32(self, stream: int, off: int) -> int:
        return _u32(self.data, self.file_offset(stream, off))

    def read(self, stream: int, off: int, n: int) -> bytes:
        return bytes(self.data[self.file_offset(stream, off + i)] for i in range(n))


def pdb_info_fields(msf: Msf) -> dict:
    # PDB info stream: Version, Signature, Age, GUID[16].
    return {"signature": msf.u32(PDB_INFO_STREAM, 4), "guid": msf.read(PDB_INFO_STREAM, 12, 16)}


def _contrib(msf: Msf, off: int) -> dict:
    # SectionContrib: ISect u16, pad u16, Off i32, Size i32, Characteristics u32, Imod u16,
    # pad u16, DataCrc u32, RelocCrc u32.
    return {"size": msf.u32(PDB_DBI_STREAM, off + 8),
            "characteristics": msf.u32(PDB_DBI_STREAM, off + 12),
            "crc_off": msf.file_offset(PDB_DBI_STREAM, off + 20),
            "data_crc": msf.u32(PDB_DBI_STREAM, off + 20)}


def pdb_section_contribs(msf: Msf) -> list[dict]:
    """Every SectionContrib in the DBI stream: the copy embedded in each module-info record (the
    module's first contribution), then the section-contribution substream."""
    mod_info_size = msf.u32(PDB_DBI_STREAM, 24)
    sc_size = msf.u32(PDB_DBI_STREAM, 28)
    contribs = []
    # Module-info records: Mod u32, SC (28 bytes), then 32 more header bytes, then the module and
    # object file names (NUL-terminated), padded to 4 bytes.
    off = DBI_HEADER_SIZE
    while off < DBI_HEADER_SIZE + mod_info_size:
        contribs.append(_contrib(msf, off + 4))
        off += 64
        for _ in range(2):
            while msf.read(PDB_DBI_STREAM, off, 1) != b"\0":
                off += 1
            off += 1
        off = (off + 3) & ~3
    base = DBI_HEADER_SIZE + mod_info_size
    version = msf.u32(PDB_DBI_STREAM, base)
    entry_size = {SC_VER60: 28, SC_V2: 32}.get(version)
    if entry_size is None:
        raise CompareError(f"unknown section contribution version {version:#x}")
    for off in range(base + 4, base + sc_size, entry_size):
        contribs.append(_contrib(msf, off))
    return contribs


def compare_pair(ours_img: pathlib.Path, release_img: pathlib.Path,
                 ours_pdb: pathlib.Path | None, release_pdb: pathlib.Path | None) -> list[str]:
    """Returns the explained differences; raises CompareError on any unexplained one."""
    a_img, b_img = bytearray(ours_img.read_bytes()), bytearray(release_img.read_bytes())
    explained: list[str] = []
    if a_img == b_img and (ours_pdb is None or ours_pdb.read_bytes() == release_pdb.read_bytes()):
        return explained
    if len(a_img) != len(b_img):
        raise CompareError(f"image sizes differ: {len(a_img)} vs {len(b_img)}")
    fa, fb = pe_build_id_fields(a_img), pe_build_id_fields(b_img)
    check_pe_build_id(str(ours_img), a_img, fa)
    check_pe_build_id(str(release_img), b_img, fb)

    if ours_pdb is not None:
        a_pdb, b_pdb = bytearray(ours_pdb.read_bytes()), bytearray(release_pdb.read_bytes())
        if len(a_pdb) != len(b_pdb):
            raise CompareError(f"PDB sizes differ: {len(a_pdb)} vs {len(b_pdb)}")
        ma, mb = Msf(bytes(a_pdb)), Msf(bytes(b_pdb))
        for name, msf, f in ((ours_pdb, ma, fa), (release_pdb, mb, fb)):
            info = pdb_info_fields(msf)
            if info["guid"] != f["guid"] or info["signature"] != _u32(info["guid"], 0):
                raise CompareError(f"{name}: PDB GUID/Signature do not match its image's RSDS GUID")
        sca, scb = pdb_section_contribs(ma), pdb_section_contribs(mb)
        if len(sca) != len(scb):
            raise CompareError("section contribution counts differ")
        empty_crcs = 0
        for ca, cb in zip(sca, scb):
            if ca["data_crc"] == cb["data_crc"]:
                continue
            empty = ca["size"] == 0 or ca["characteristics"] & IMAGE_SCN_CNT_UNINITIALIZED_DATA
            if (ca["characteristics"] == cb["characteristics"] and ca["size"] == cb["size"] and empty
                    and ca["data_crc"] == 0 and cb["data_crc"] == 0xFFFFFFFF):
                _put_u32(a_pdb, ca["crc_off"], 0)
                _put_u32(b_pdb, cb["crc_off"], 0)
                empty_crcs += 1
                continue
            raise CompareError(
                f"unexplained section contribution DataCrc difference: {ca['data_crc']:#x} vs "
                f"{cb['data_crc']:#x}, size {ca['size']}, characteristics {ca['characteristics']:#x}")
        for pdb, msf in ((a_pdb, ma), (b_pdb, mb)):
            _put_u32(pdb, msf.file_offset(PDB_INFO_STREAM, 4), 0)
            for i in range(16):
                pdb[msf.file_offset(PDB_INFO_STREAM, 12 + i)] = 0
        if a_pdb != b_pdb:
            first = next(i for i in range(len(a_pdb)) if a_pdb[i] != b_pdb[i])
            raise CompareError(f"PDBs differ outside the explained fields (first at file offset {first:#x})")
        if empty_crcs:
            explained.append(f"{empty_crcs} empty-content (BSS/zero-size) section-contribution DataCrc(s): "
                             "0 (LLVM_ENABLE_ZLIB=OFF) vs 0xFFFFFFFF (zlib)")

    normalize_pe(a_img, fa)
    normalize_pe(b_img, fb)
    if a_img != b_img:
        first = next(i for i in range(len(a_img)) if a_img[i] != b_img[i])
        raise CompareError(f"images differ outside the /Brepro build-id fields (first at {first:#x})")
    if fa["timestamp"] != fb["timestamp"]:
        explained.append("/Brepro build id (COFF/debug TimeDateStamp, RSDS and PDB GUID/Signature)")
    return explained


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ours", type=pathlib.Path, help="image linked by llvm-ld-direct")
    ap.add_argument("release", type=pathlib.Path, help="same link by the release lld-link.exe")
    ap.add_argument("--ours-pdb", type=pathlib.Path)
    ap.add_argument("--release-pdb", type=pathlib.Path)
    args = ap.parse_args(argv)
    if (args.ours_pdb is None) != (args.release_pdb is None):
        ap.error("--ours-pdb and --release-pdb go together")
    try:
        explained = compare_pair(args.ours, args.release, args.ours_pdb, args.release_pdb)
    except CompareError as exc:
        print(f"DIFFERENT: {args.ours} vs {args.release}: {exc}")
        return 1
    except (OSError, struct.error, IndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if explained:
        print(f"IDENTICAL modulo explained differences: {args.ours} vs {args.release}")
        for line in explained:
            print(f"  - {line}")
    else:
        print(f"IDENTICAL: {args.ours} vs {args.release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
