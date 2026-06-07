"""
scanner.py — Disk scanning engine with NTFS MFT awareness and FAT32 support.

Responsibilities:
  1. Mount raw disk handle via winapi.py
  2. Detect filesystem type (NTFS / FAT32 / exFAT / RAW)
  3. Walk the NTFS Master File Table (MFT) to find deleted $FILE_NAME records
  4. Walk FAT32 directory entries to find deleted entries (marker 0xE5)
  5. Emit CandidateFile objects consumed by recovery.py

Threading model:
  Scanner runs in a background QThread. Progress signals are emitted after
  each MFT record or FAT cluster is processed so the GUI never blocks.
"""

import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, List, Callable, Iterator, Tuple
from enum import Enum, auto

import winapi


# ─────────────────────────────────────────────────────────
# Public data types
# ─────────────────────────────────────────────────────────
class RecoveryMethod(Enum):
    MFT        = auto()   # Found in NTFS MFT as a deleted record
    FAT_DIR    = auto()   # Found in FAT directory as 0xE5 entry
    CARVING    = auto()   # Found by signature scan (recovery.py)


@dataclass
class CandidateFile:
    """
    A potentially-recoverable file identified during scanning.
    Populated by scanner.py; confidence and binary content added by recovery.py.
    """
    name:           str             # Original filename (may be partial / reconstructed)
    extension:      str             # Lowercase extension, e.g. ".jpg"
    size_bytes:     int             # File size from metadata (0 if unknown)
    offset:         int             # Byte offset on disk where data begins (for carving)
    method:         RecoveryMethod  # How this candidate was identified
    path:           str = ""        # Original directory path if recoverable
    mft_record_no:  int = -1        # NTFS MFT record number (-1 = N/A)
    confidence:     float = 0.0     # 0.0–1.0 filled by recovery.py
    file_id:        str = ""        # Unique ID for display / deduplification

    def __post_init__(self):
        if not self.file_id:
            self.file_id = f"{self.method.name}_{self.offset}_{self.name}"


# ─────────────────────────────────────────────────────────
# Filesystem detection
# ─────────────────────────────────────────────────────────
NTFS_MAGIC   = b"NTFS    "   # at offset 3 in VBR
FAT32_MAGIC  = b"FAT32   "   # at offset 82 in VBR
FAT16_MAGIC  = b"FAT16   "
EXFAT_MAGIC  = b"EXFAT   "

def detect_filesystem(boot_sector: bytes) -> str:
    """
    Identify the filesystem from the Volume Boot Record (first 512 bytes).
    Returns one of: "NTFS", "FAT32", "FAT16", "exFAT", "Unknown".
    """
    if len(boot_sector) < 512:
        return "Unknown"
    if boot_sector[3:11] == NTFS_MAGIC:
        return "NTFS"
    if boot_sector[82:90] == FAT32_MAGIC:
        return "FAT32"
    if boot_sector[54:62] == FAT16_MAGIC:
        return "FAT16"
    if boot_sector[3:11] == EXFAT_MAGIC:
        return "exFAT"
    return "Unknown"


# ─────────────────────────────────────────────────────────
# NTFS BPB / geometry helpers
# ─────────────────────────────────────────────────────────
def parse_ntfs_bpb(boot_sector: bytes) -> dict:
    """
    Parse the NTFS BIOS Parameter Block from the VBR.
    Returns a dict with keys: bytes_per_sector, sectors_per_cluster,
    mft_start_lcn, mft_record_size.
    """
    # Offset reference: https://en.wikipedia.org/wiki/NTFS#Layout
    bps  = struct.unpack_from("<H", boot_sector, 0x0B)[0]   # bytes per sector
    spc  = struct.unpack_from("<B", boot_sector, 0x0D)[0]   # sectors per cluster
    mft_lcn = struct.unpack_from("<Q", boot_sector, 0x30)[0] # MFT start LCN
    mft_rec_raw = struct.unpack_from("<b", boot_sector, 0x40)[0]  # clusters per MFT record

    # If negative, it's a power-of-2 byte count (2^abs(value))
    if mft_rec_raw < 0:
        mft_record_size = 2 ** abs(mft_rec_raw)
    else:
        mft_record_size = mft_rec_raw * spc * bps

    return {
        "bytes_per_sector":     bps  if bps  else 512,
        "sectors_per_cluster":  spc  if spc  else 8,
        "mft_start_lcn":        mft_lcn,
        "mft_record_size":      mft_record_size if mft_record_size else 1024,
    }


# ─────────────────────────────────────────────────────────
# MFT record attribute parser
# ─────────────────────────────────────────────────────────
MFT_SIGNATURE        = b"FILE"
ATTR_STANDARD_INFO   = 0x10
ATTR_ATTRIBUTE_LIST  = 0x20
ATTR_FILE_NAME       = 0x30
ATTR_DATA            = 0x80

FILE_FLAG_DIRECTORY   = 0x02   # In MFT record flags
MFT_RECORD_IN_USE     = 0x01   # bit 0 of record flags = in-use

def _apply_fixup(data: bytearray, sector_size: int) -> bytearray:
    """
    Apply the Update Sequence Array (USA) fixup to an MFT record.
    NTFS writes update sequence values at the end of each 512-byte block
    to detect torn writes; we must patch these before parsing attributes.
    """
    usa_offset = struct.unpack_from("<H", data, 4)[0]
    usa_count  = struct.unpack_from("<H", data, 6)[0]
    if usa_offset < 8 or usa_count < 2:
        return data
    seq_num = struct.unpack_from("<H", data, usa_offset)[0]
    for i in range(1, usa_count):
        pos = i * sector_size - 2
        if pos + 2 > len(data):
            break
        # Verify the sequence number matches before patching
        stored = struct.unpack_from("<H", data, pos)[0]
        if stored != seq_num:
            break  # Corrupt record
        real = struct.unpack_from("<H", data, usa_offset + i * 2)[0]
        struct.pack_into("<H", data, pos, real)
    return data


def parse_mft_record(raw: bytes, sector_size: int = 512) -> Optional[dict]:
    """
    Parse a single 1024-byte MFT record and extract key fields.
    Returns a dict or None if the record is invalid / in-use (not deleted).

    Keys in returned dict:
      is_deleted, is_directory, name, parent_ref, size_bytes, mft_record_no
    """
    if len(raw) < 48 or raw[:4] != MFT_SIGNATURE:
        return None

    data = _apply_fixup(bytearray(raw), sector_size)

    flags = struct.unpack_from("<H", data, 22)[0]
    is_deleted   = not bool(flags & MFT_RECORD_IN_USE)
    is_directory = bool(flags & FILE_FLAG_DIRECTORY)

    if not is_deleted:
        return None   # Only interested in deleted files

    record_no = struct.unpack_from("<I", data, 44)[0]

    # Walk attributes
    attr_offset = struct.unpack_from("<H", data, 20)[0]
    name        = ""
    parent_ref  = 0
    size_bytes  = 0

    while attr_offset + 4 <= len(data):
        attr_type = struct.unpack_from("<I", data, attr_offset)[0]
        if attr_type == 0xFFFFFFFF:
            break  # End of attributes marker
        attr_len = struct.unpack_from("<I", data, attr_offset + 4)[0]
        if attr_len < 8 or attr_offset + attr_len > len(data):
            break

        if attr_type == ATTR_FILE_NAME:
            # Non-resident flag: byte at offset+8
            non_resident = data[attr_offset + 8]
            if not non_resident:
                content_off = struct.unpack_from("<H", data, attr_offset + 20)[0]
                fn_off = attr_offset + content_off
                if fn_off + 66 <= len(data):
                    parent_ref = struct.unpack_from("<Q", data, fn_off)[0] & 0x0000FFFFFFFFFFFF
                    alloc_size = struct.unpack_from("<Q", data, fn_off + 40)[0]
                    real_size  = struct.unpack_from("<Q", data, fn_off + 48)[0]
                    size_bytes = real_size or alloc_size
                    name_len   = data[fn_off + 64]
                    name_type  = data[fn_off + 65]
                    # Prefer POSIX (0) or Win32 (1) names; skip DOS (2) or Win32+DOS (3) if we already have name
                    if name_len > 0 and (not name or name_type in (0, 1)):
                        raw_name = bytes(data[fn_off + 66: fn_off + 66 + name_len * 2])
                        try:
                            name = raw_name.decode("utf-16-le")
                        except UnicodeDecodeError:
                            name = raw_name.decode("utf-16-le", errors="replace")

        attr_offset += attr_len

    if not name:
        return None

    return {
        "is_deleted":    True,
        "is_directory":  is_directory,
        "name":          name,
        "parent_ref":    parent_ref,
        "size_bytes":    size_bytes,
        "mft_record_no": record_no,
    }


# ─────────────────────────────────────────────────────────
# NTFS scanner
# ─────────────────────────────────────────────────────────
def scan_ntfs_mft(
    drive_letter: str,
    progress_cb:  Callable[[int, int, str], None],
    stop_event:   threading.Event,
    sector_size:  int = 512,
) -> Iterator[CandidateFile]:
    """
    Walk the NTFS MFT on drive_letter and yield CandidateFile for every
    deleted non-directory entry found.

    progress_cb(processed, total, message) is called periodically.
    stop_event is checked after each record; set it to abort the scan.
    """
    handle = winapi.open_disk_handle(drive_letter)
    if handle is None:
        # Simulation / non-Windows: yield nothing
        return

    try:
        boot_raw = winapi.read_sectors(handle, 0, 1, sector_size)
        if not boot_raw:
            return

        bpb            = parse_ntfs_bpb(boot_raw)
        bps            = bpb["bytes_per_sector"]
        spc            = bpb["sectors_per_cluster"]
        mft_lcn        = bpb["mft_start_lcn"]
        mft_rec_size   = bpb["mft_record_size"]
        cluster_bytes  = bps * spc

        mft_byte_offset = mft_lcn * cluster_bytes
        mft_lba         = mft_byte_offset // bps

        # Read the first MFT record ($MFT itself) to get the MFT's own data runs
        # and thus the total number of MFT records we need to process.
        # For simplicity we scan up to 4 M records (4 GB MFT at 1024 bytes/record)
        MAX_RECORDS = 4_000_000
        processed   = 0

        sectors_per_record = max(1, mft_rec_size // bps)

        progress_cb(0, MAX_RECORDS, "Scanning NTFS MFT for deleted files…")

        while not stop_event.is_set():
            lba = mft_lba + processed * sectors_per_record
            raw = winapi.read_sectors(handle, lba, sectors_per_record, bps)
            if not raw or len(raw) < mft_rec_size:
                break

            record = parse_mft_record(raw[:mft_rec_size], bps)
            if record and not record["is_directory"]:
                name = record["name"]
                ext  = os.path.splitext(name)[1].lower()
                byte_off = lba * bps  # approximate; carver will refine
                yield CandidateFile(
                    name          = name,
                    extension     = ext,
                    size_bytes    = record["size_bytes"],
                    offset        = byte_off,
                    method        = RecoveryMethod.MFT,
                    mft_record_no = record["mft_record_no"],
                    confidence    = 0.7,  # will be refined by recovery.py
                )

            processed += 1
            if processed % 1000 == 0:
                progress_cb(processed, MAX_RECORDS, f"MFT: checked {processed:,} records…")

            # Heuristic stop: if we've seen 10000 consecutive unreadable sectors, MFT ended
            if len(raw) == 0:
                break

    finally:
        winapi.close_disk_handle(handle)


# ─────────────────────────────────────────────────────────
# FAT32 scanner
# ─────────────────────────────────────────────────────────
FAT32_DELETED_MARKER = 0xE5

def _parse_fat32_bpb(boot: bytes) -> dict:
    """Extract FAT32 BPB fields from the VBR."""
    bps       = struct.unpack_from("<H", boot, 11)[0]
    spc       = struct.unpack_from("<B", boot, 13)[0]
    reserved  = struct.unpack_from("<H", boot, 14)[0]
    num_fats  = struct.unpack_from("<B", boot, 16)[0]
    fat_size  = struct.unpack_from("<I", boot, 36)[0]   # sectors per FAT (FAT32)
    root_clus = struct.unpack_from("<I", boot, 44)[0]   # root dir first cluster

    fat_start    = reserved
    data_start   = reserved + num_fats * fat_size
    cluster_size = bps * spc

    return {
        "bps":          bps or 512,
        "spc":          spc or 8,
        "fat_start":    fat_start,
        "data_start":   data_start,
        "root_cluster": root_clus,
        "cluster_size": cluster_size or 4096,
    }


def scan_fat32(
    drive_letter: str,
    progress_cb:  Callable[[int, int, str], None],
    stop_event:   threading.Event,
    sector_size:  int = 512,
) -> Iterator[CandidateFile]:
    """
    Walk FAT32 directory clusters looking for entries with the 0xE5 deleted marker.
    Yields CandidateFile for each deleted regular file found.
    """
    handle = winapi.open_disk_handle(drive_letter)
    if handle is None:
        return
    try:
        boot = winapi.read_sectors(handle, 0, 1, sector_size)
        if not boot:
            return

        bpb = _parse_fat32_bpb(boot)
        bps          = bpb["bps"]
        data_start   = bpb["data_start"]
        root_cluster = bpb["root_cluster"]
        cluster_size = bpb["cluster_size"]
        spc          = bpb["spc"]

        clusters_to_scan = [root_cluster]
        visited          = set()
        total_found      = 0

        progress_cb(0, 1000, "Scanning FAT32 directory entries…")

        while clusters_to_scan and not stop_event.is_set():
            cluster = clusters_to_scan.pop(0)
            if cluster in visited or cluster < 2 or cluster >= 0x0FFFFFF8:
                continue
            visited.add(cluster)

            lba = (data_start + (cluster - 2) * spc)
            raw = winapi.read_sectors(handle, lba, spc, bps)
            if not raw:
                continue

            # Each FAT directory entry is 32 bytes
            for off in range(0, len(raw), 32):
                entry = raw[off:off+32]
                if len(entry) < 32:
                    break
                first_byte = entry[0]
                attrs      = entry[11]

                # 0xE5 = deleted; skip LFN (0x0F) and empty (0x00)
                if first_byte == 0x00:
                    break  # No more entries in this cluster
                if attrs == 0x0F:
                    continue  # Long file name entry
                if first_byte != FAT32_DELETED_MARKER:
                    if attrs & 0x10:  # Directory — add cluster to scan queue
                        clus_hi = struct.unpack_from("<H", entry, 20)[0]
                        clus_lo = struct.unpack_from("<H", entry, 26)[0]
                        child_cluster = (clus_hi << 16) | clus_lo
                        clusters_to_scan.append(child_cluster)
                    continue

                # Reconstruct 8.3 filename (first char replaced by 0xE5, mark as '?')
                raw_name = entry[1:8].rstrip(b' ')
                raw_ext  = entry[8:11].rstrip(b' ')
                try:
                    name_str = ('?' + raw_name.decode('ascii', errors='replace')).strip()
                    ext_str  = raw_ext.decode('ascii', errors='replace').strip()
                except Exception:
                    name_str = "UNKNOWN"
                    ext_str  = ""

                filename  = f"{name_str}.{ext_str}" if ext_str else name_str
                ext       = f".{ext_str.lower()}" if ext_str else ""
                size_b    = struct.unpack_from("<I", entry, 28)[0]
                clus_hi   = struct.unpack_from("<H", entry, 20)[0]
                clus_lo   = struct.unpack_from("<H", entry, 26)[0]
                file_clus = (clus_hi << 16) | clus_lo

                byte_off = (data_start + (file_clus - 2) * spc) * bps if file_clus >= 2 else 0

                total_found += 1
                yield CandidateFile(
                    name       = filename,
                    extension  = ext,
                    size_bytes = size_b,
                    offset     = byte_off,
                    method     = RecoveryMethod.FAT_DIR,
                    confidence = 0.6,
                )

                if total_found % 100 == 0:
                    progress_cb(total_found, total_found + 100, f"FAT32: found {total_found:,} deleted entries…")

    finally:
        winapi.close_disk_handle(handle)


# ─────────────────────────────────────────────────────────
# Dispatcher — chooses scanner based on filesystem
# ─────────────────────────────────────────────────────────
def scan_drive(
    drive: winapi.DriveInfo,
    progress_cb: Callable[[int, int, str], None],
    stop_event:  threading.Event,
) -> Iterator[CandidateFile]:
    """
    High-level entry point. Selects NTFS or FAT32 scanner.
    Caller iterates results; each is a CandidateFile ready for recovery.py.
    """
    fs = drive.fs_type.upper()
    progress_cb(0, 1, f"Opening {drive.letter}: ({fs})…")

    if fs == "NTFS":
        yield from scan_ntfs_mft(drive.letter, progress_cb, stop_event, drive.sector_size)
    elif fs in ("FAT32", "FAT16"):
        yield from scan_fat32(drive.letter, progress_cb, stop_event, drive.sector_size)
    else:
        progress_cb(0, 1, f"Filesystem '{fs}' not directly supported; falling back to carving only…")
        # Carving-only mode: recovery.py will scan the raw disk for signatures.
        # We yield nothing here; recovery.py drives the carving pass.
