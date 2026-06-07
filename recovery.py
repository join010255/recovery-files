"""
recovery.py — File recovery engine: signature carving + MFT/FAT data extraction.

Two recovery paths:
  1. Metadata-guided: CandidateFile already has a known offset from MFT/FAT.
     Read the data clusters directly and write to output directory.
  2. Signature carving: Raw byte scan across the entire disk looking for known
     magic bytes. Each hit spawns a CandidateFile with estimated boundaries.

All writes go to a user-specified output directory.
The source disk is NEVER written to.
"""

import os
import re
import struct
import threading
import hashlib
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable, Iterator
from pathlib import Path

import winapi
from scanner import CandidateFile, RecoveryMethod


# ─────────────────────────────────────────────────────────
# File signature database
# ─────────────────────────────────────────────────────────
@dataclass
class Signature:
    extension:   str              # e.g. ".jpg"
    magic:       bytes            # header bytes to match
    offset:      int = 0          # byte offset within file where magic appears
    max_size:    int = 50_000_000 # maximum expected file size in bytes (50 MB default)
    end_magic:   Optional[bytes] = None  # footer bytes (if known)
    category:    str = "Unknown"


SIGNATURES: List[Signature] = [
    # ── Images ──────────────────────────────────────────
    Signature(".jpg",  b"\xFF\xD8\xFF",            0, 15_000_000, b"\xFF\xD9",    "Image"),
    Signature(".png",  b"\x89PNG\r\n\x1a\n",       0, 15_000_000, b"\x00IEND\xaeB`\x82", "Image"),
    Signature(".gif",  b"GIF89a",                  0, 5_000_000,  b"\x00;",      "Image"),
    Signature(".gif",  b"GIF87a",                  0, 5_000_000,  None,          "Image"),
    Signature(".bmp",  b"BM",                      0, 20_000_000, None,          "Image"),
    Signature(".tiff", b"II*\x00",                 0, 50_000_000, None,          "Image"),
    Signature(".tiff", b"MM\x00*",                 0, 50_000_000, None,          "Image"),
    Signature(".webp", b"RIFF",                    0, 10_000_000, None,          "Image"),

    # ── Documents ────────────────────────────────────────
    Signature(".pdf",  b"%PDF-",                   0, 100_000_000, b"%%EOF",     "Document"),
    Signature(".docx", b"PK\x03\x04",              0, 50_000_000,  b"PK\x05\x06","Document"),  # ZIP-based
    Signature(".xlsx", b"PK\x03\x04",              0, 50_000_000,  b"PK\x05\x06","Document"),
    Signature(".pptx", b"PK\x03\x04",              0, 100_000_000, b"PK\x05\x06","Document"),
    Signature(".doc",  b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1", 0, 50_000_000, None, "Document"),
    Signature(".xls",  b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1", 0, 50_000_000, None, "Document"),
    Signature(".ppt",  b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1", 0, 50_000_000, None, "Document"),
    Signature(".rtf",  b"{\\rtf",                  0, 20_000_000,  None,         "Document"),

    # ── Archives ─────────────────────────────────────────
    Signature(".zip",  b"PK\x03\x04",              0, 500_000_000, b"PK\x05\x06","Archive"),
    Signature(".rar",  b"Rar!\x1a\x07\x00",        0, 500_000_000, None,          "Archive"),
    Signature(".rar",  b"Rar!\x1a\x07\x01\x00",    0, 500_000_000, None,          "Archive"),
    Signature(".7z",   b"7z\xbc\xaf'\x1c",         0, 500_000_000, None,          "Archive"),
    Signature(".gz",   b"\x1f\x8b\x08",            0, 500_000_000, None,          "Archive"),
    Signature(".tar",  b"ustar",                  257, 500_000_000, None,          "Archive"),

    # ── Video ────────────────────────────────────────────
    Signature(".mp4",  b"ftyp",                    4, 2_000_000_000, None,       "Video"),
    Signature(".avi",  b"RIFF",                    0, 2_000_000_000, None,       "Video"),
    Signature(".mkv",  b"\x1a\x45\xdf\xa3",        0, 2_000_000_000, None,       "Video"),
    Signature(".wmv",  b"\x30\x26\xB2\x75",        0, 500_000_000,  None,       "Video"),
    Signature(".mov",  b"ftyp",                    4, 2_000_000_000, None,       "Video"),

    # ── Audio ────────────────────────────────────────────
    Signature(".mp3",  b"\xFF\xFB",                0, 50_000_000,  None,        "Audio"),
    Signature(".mp3",  b"ID3",                     0, 50_000_000,  None,        "Audio"),
    Signature(".wav",  b"RIFF",                    0, 200_000_000, None,        "Audio"),
    Signature(".flac", b"fLaC",                    0, 200_000_000, None,        "Audio"),
    Signature(".ogg",  b"OggS",                    0, 100_000_000, None,        "Audio"),
    Signature(".aac",  b"\xFF\xF1",                0, 50_000_000,  None,        "Audio"),

    # ── Executables / System ─────────────────────────────
    Signature(".exe",  b"MZ",                      0, 200_000_000, None,        "Executable"),
    Signature(".dll",  b"MZ",                      0, 200_000_000, None,        "Executable"),
    Signature(".msi",  b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1", 0, 500_000_000, None, "Executable"),

    # ── Forensic / Data ──────────────────────────────────
    Signature(".db",   b"SQLite format 3\x00",     0, 500_000_000, None,        "Forensic"),
    Signature(".pst",  b"\x21\x42\x44\x4E",        0, 2_000_000_000, None,      "Forensic"),
    Signature(".eml",  b"From ",                   0, 10_000_000,  None,        "Forensic"),
]

# Build a fast lookup: first-byte → list of Signature objects
_SIG_INDEX: Dict[int, List[Signature]] = {}
for sig in SIGNATURES:
    if sig.offset == 0 and sig.magic:
        fb = sig.magic[0]
        _SIG_INDEX.setdefault(fb, []).append(sig)


# ─────────────────────────────────────────────────────────
# Confidence scoring
# ─────────────────────────────────────────────────────────
def score_confidence(
    candidate: CandidateFile,
    raw_data: Optional[bytes],
    sig: Optional[Signature] = None,
) -> float:
    """
    Compute a 0.0–1.0 recovery confidence score.

    Factors:
    - Method: MFT=+0.3, FAT=+0.2, Carving=+0.1
    - Header magic valid: +0.3
    - Footer magic valid: +0.2
    - Size plausible: +0.1
    - Filename has valid extension: +0.1
    """
    score = 0.0

    if candidate.method == RecoveryMethod.MFT:
        score += 0.3
    elif candidate.method == RecoveryMethod.FAT_DIR:
        score += 0.2
    else:
        score += 0.1

    if raw_data and sig:
        # Header check
        if raw_data[:len(sig.magic)] == sig.magic:
            score += 0.3
        # Footer check
        if sig.end_magic and sig.end_magic in raw_data[-128:]:
            score += 0.2
        # Size plausibility
        if 0 < len(raw_data) <= sig.max_size:
            score += 0.1

    if candidate.extension and candidate.extension.startswith("."):
        score += 0.1

    return min(1.0, score)


# ─────────────────────────────────────────────────────────
# Metadata-guided recovery
# ─────────────────────────────────────────────────────────
def recover_by_metadata(
    candidate: CandidateFile,
    drive_letter: str,
    output_dir: str,
    sector_size: int = 512,
) -> Tuple[bool, str]:
    """
    Attempt to recover a file whose offset is known from MFT or FAT metadata.
    Reads raw sectors starting at candidate.offset and writes to output_dir.

    Returns (success, output_path_or_error_message).
    """
    if candidate.offset <= 0 or candidate.size_bytes <= 0:
        return False, "No valid offset or size"

    handle = winapi.open_disk_handle(drive_letter)
    if handle is None:
        return False, "Cannot open disk (non-Windows or permission denied)"

    try:
        lba        = candidate.offset // sector_size
        num_bytes  = candidate.size_bytes
        # Round up to sector boundary for NO_BUFFERING reads
        read_size  = ((num_bytes + sector_size - 1) // sector_size) * sector_size
        max_sectors = min(read_size // sector_size, 200_000)  # cap at ~100 MB

        data = winapi.read_sectors(handle, lba, max_sectors, sector_size)
        if not data:
            return False, "Disk read returned empty data"

        # Trim to actual file size
        data = data[:num_bytes]

        # Find a matching signature to validate / refine confidence
        matched_sig = _match_signature(data[:32])
        candidate.confidence = score_confidence(candidate, data, matched_sig)

        out_path = _safe_output_path(output_dir, candidate.name, candidate.extension)
        _write_file(out_path, data)
        return True, out_path

    except Exception as e:
        return False, str(e)
    finally:
        winapi.close_disk_handle(handle)


# ─────────────────────────────────────────────────────────
# Signature carving engine
# ─────────────────────────────────────────────────────────
CARVE_CHUNK_BYTES = 4 * 1024 * 1024   # 4 MB read chunks
OVERLAP_BYTES     = 256               # overlap between chunks to catch split signatures

def carve_disk(
    drive_letter: str,
    total_bytes: int,
    output_dir: str,
    progress_cb: Callable[[int, int, str], None],
    stop_event:  threading.Event,
    sector_size: int = 512,
) -> Iterator[CandidateFile]:
    """
    Full-disk signature-based carving pass.
    Reads the disk in CARVE_CHUNK_BYTES chunks, scans each chunk for known
    file signatures, extracts candidate files, and writes recovered data.

    Yields CandidateFile objects as they are found.
    """
    handle = winapi.open_disk_handle(drive_letter)
    if handle is None:
        return
    try:
        offset       = 0
        carve_count  = 0
        prev_tail    = b""

        while offset < total_bytes and not stop_event.is_set():
            lba        = offset // sector_size
            sectors    = CARVE_CHUNK_BYTES // sector_size
            chunk      = winapi.read_sectors(handle, lba, sectors, sector_size)
            if not chunk:
                offset += CARVE_CHUNK_BYTES
                continue

            search_data = prev_tail + chunk

            # Scan for signatures
            for pos in range(len(search_data) - 16):
                if stop_event.is_set():
                    return

                fb = search_data[pos]
                if fb not in _SIG_INDEX:
                    continue

                for sig in _SIG_INDEX[fb]:
                    # Quick magic match
                    end = pos + len(sig.magic)
                    if search_data[pos:end] != sig.magic:
                        continue

                    # Calculate absolute disk offset
                    abs_offset = offset - len(prev_tail) + pos

                    # Extract file data up to max_size
                    extract_size = min(sig.max_size, total_bytes - abs_offset)
                    extract_lba  = abs_offset // sector_size
                    extract_sectors = min(
                        (extract_size + sector_size - 1) // sector_size,
                        sig.max_size // sector_size + 1,
                    )

                    raw = winapi.read_sectors(handle, extract_lba, extract_sectors, sector_size)
                    if not raw:
                        continue

                    # Find end marker if available
                    actual_size = _find_end(raw, sig, extract_size)
                    if actual_size < 8:
                        continue

                    raw = raw[:actual_size]
                    carve_count += 1

                    safe_name = f"carved_{carve_count:06d}{sig.extension}"
                    out_path  = _safe_output_path(output_dir, safe_name, sig.extension)
                    _write_file(out_path, raw)

                    cand = CandidateFile(
                        name       = safe_name,
                        extension  = sig.extension,
                        size_bytes = actual_size,
                        offset     = abs_offset,
                        method     = RecoveryMethod.CARVING,
                        confidence = score_confidence(
                            CandidateFile(
                                name=safe_name, extension=sig.extension,
                                size_bytes=actual_size, offset=abs_offset,
                                method=RecoveryMethod.CARVING,
                            ),
                            raw, sig,
                        ),
                    )
                    yield cand

                    progress_cb(
                        offset, total_bytes,
                        f"Carving… {carve_count} files found, {offset // (1024**2):.0f} MB scanned",
                    )
                    break  # Only first matching signature per position

            prev_tail = chunk[-OVERLAP_BYTES:]
            offset   += CARVE_CHUNK_BYTES

    finally:
        winapi.close_disk_handle(handle)


def _find_end(data: bytes, sig: Signature, max_size: int) -> int:
    """
    Determine the end boundary of a carved file.
    If the signature has a known end_magic footer, search for it.
    Otherwise return the minimum of max_size and len(data).
    """
    if sig.end_magic:
        idx = data.rfind(sig.end_magic, 0, min(len(data), max_size))
        if idx != -1:
            return idx + len(sig.end_magic)
    return min(len(data), max_size)


def _match_signature(header: bytes) -> Optional[Signature]:
    """Return the first Signature whose magic matches the start of header, or None."""
    if not header:
        return None
    fb = header[0]
    for sig in _SIG_INDEX.get(fb, []):
        if header[:len(sig.magic)] == sig.magic:
            return sig
    return None


# ─────────────────────────────────────────────────────────
# Output file helpers
# ─────────────────────────────────────────────────────────
def _safe_output_path(output_dir: str, name: str, ext: str) -> str:
    """
    Construct a safe, unique file path inside output_dir.
    Sanitises the filename and appends a counter if the file already exists.
    """
    # Sanitise filename — remove characters illegal in Windows filenames
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', name)
    if not safe_name or safe_name in (".", ".."):
        safe_name = "recovered_file"

    os.makedirs(output_dir, exist_ok=True)
    base = os.path.join(output_dir, safe_name)
    path = base

    counter = 1
    while os.path.exists(path):
        stem = os.path.splitext(safe_name)[0]
        path = os.path.join(output_dir, f"{stem}_{counter}{ext}")
        counter += 1

    return path


def _write_file(path: str, data: bytes) -> None:
    """Write bytes to path. Creates parent directories as needed."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ─────────────────────────────────────────────────────────
# High-level recovery orchestrator
# ─────────────────────────────────────────────────────────
def recover_candidate(
    candidate: CandidateFile,
    drive_letter: str,
    output_dir: str,
    sector_size: int = 512,
) -> Tuple[bool, str]:
    """
    Recover a single candidate file from disk.
    Tries metadata-guided recovery first; falls back to a targeted carve.
    Returns (success, output_path_or_error).
    """
    if candidate.method in (RecoveryMethod.MFT, RecoveryMethod.FAT_DIR):
        ok, result = recover_by_metadata(candidate, drive_letter, output_dir, sector_size)
        if ok:
            return True, result

    # Fallback: targeted re-carve at the known offset
    if candidate.offset > 0:
        handle = winapi.open_disk_handle(drive_letter)
        if handle:
            try:
                # Match extension to signature for size estimate
                sig = next(
                    (s for s in SIGNATURES if s.extension == candidate.extension),
                    None,
                )
                max_sz   = sig.max_size if sig else 10_000_000
                sectors  = min(max_sz // sector_size + 1, 20_000)
                lba      = candidate.offset // sector_size
                raw      = winapi.read_sectors(handle, lba, sectors, sector_size)
                if raw:
                    size = _find_end(raw, sig, max_sz) if sig else min(len(raw), max_sz)
                    raw  = raw[:size]
                    out  = _safe_output_path(output_dir, candidate.name, candidate.extension)
                    _write_file(out, raw)
                    return True, out
            finally:
                winapi.close_disk_handle(handle)

    return False, "Recovery failed: insufficient metadata and carving produced no data"
