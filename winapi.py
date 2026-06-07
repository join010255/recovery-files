"""
winapi.py — Windows API wrappers for low-level disk and filesystem access.

Provides ctypes-based bindings to Win32 APIs needed for:
- Enumerating logical drives and volumes
- Opening raw disk handles (read-only)
- Reading raw sectors
- Querying volume geometry and filesystem type
- MFT record access (NTFS)

All disk access is strictly READ-ONLY. No writes are performed.
"""

import ctypes
import ctypes.wintypes as wintypes
import struct
import platform
import sys
from typing import Optional, List, Tuple, Dict
from dataclasses import dataclass

# ─────────────────────────────────────────────────────────
# Platform guard — graceful degradation on non-Windows
# ─────────────────────────────────────────────────────────
IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    kernel32 = ctypes.windll.kernel32   # type: ignore
    advapi32 = ctypes.windll.advapi32   # type: ignore

# ─────────────────────────────────────────────────────────
# Win32 constants
# ─────────────────────────────────────────────────────────
GENERIC_READ            = 0x80000000
FILE_SHARE_READ         = 0x00000001
FILE_SHARE_WRITE        = 0x00000002
OPEN_EXISTING           = 3
FILE_FLAG_NO_BUFFERING  = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
INVALID_HANDLE_VALUE    = ctypes.c_void_p(-1).value

IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_STORAGE_QUERY_PROPERTY     = 0x002D1400

# Drive types (GetDriveTypeW)
DRIVE_REMOVABLE  = 2
DRIVE_FIXED      = 3
DRIVE_REMOTE     = 4
DRIVE_CDROM      = 5
DRIVE_RAMDISK    = 6

DRIVE_TYPE_NAMES = {
    DRIVE_REMOVABLE: "Removable",
    DRIVE_FIXED:     "Fixed",
    DRIVE_REMOTE:    "Network",
    DRIVE_CDROM:     "CD-ROM",
    DRIVE_RAMDISK:   "RAM Disk",
}

# ─────────────────────────────────────────────────────────
# Structures
# ─────────────────────────────────────────────────────────
class DISK_GEOMETRY(ctypes.Structure):
    """Win32 DISK_GEOMETRY structure — describes physical disk layout."""
    _fields_ = [
        ("Cylinders",         ctypes.c_int64),
        ("MediaType",         ctypes.c_uint),
        ("TracksPerCylinder", ctypes.c_ulong),
        ("SectorsPerTrack",   ctypes.c_ulong),
        ("BytesPerSector",    ctypes.c_ulong),
    ]

class DISK_GEOMETRY_EX(ctypes.Structure):
    """Extended geometry — includes DiskSize (total bytes)."""
    _fields_ = [
        ("Geometry",  DISK_GEOMETRY),
        ("DiskSize",  ctypes.c_int64),
        ("Data",      ctypes.c_byte * 1),
    ]

# ─────────────────────────────────────────────────────────
# Data classes exposed to upper layers
# ─────────────────────────────────────────────────────────
@dataclass
class DriveInfo:
    """Metadata about a logical drive available for scanning."""
    letter:      str          # e.g. "C"
    label:       str          # volume label or empty string
    fs_type:     str          # "NTFS", "FAT32", "exFAT", "Unknown"
    drive_type:  str          # "Fixed", "Removable", …
    total_bytes: int          # total capacity in bytes
    free_bytes:  int          # free space in bytes
    sector_size: int = 512    # bytes per physical sector

    @property
    def display_name(self) -> str:
        label = f" [{self.label}]" if self.label else ""
        size_gb = self.total_bytes / (1024 ** 3)
        return f"{self.letter}:{label} — {self.fs_type} — {size_gb:.1f} GB ({self.drive_type})"


# ─────────────────────────────────────────────────────────
# Drive enumeration
# ─────────────────────────────────────────────────────────
def enumerate_drives() -> List[DriveInfo]:
    """
    Return a list of DriveInfo objects for all accessible logical drives.
    Uses GetLogicalDriveStringsW, GetDriveTypeW, GetVolumeInformationW,
    and GetDiskFreeSpaceExW.
    Falls back to a safe mock list on non-Windows systems (for UI testing).
    """
    if not IS_WINDOWS:
        return _mock_drives()

    drives: List[DriveInfo] = []
    buf = ctypes.create_unicode_buffer(256)
    kernel32.GetLogicalDriveStringsW(ctypes.sizeof(buf), buf)

    # GetLogicalDriveStrings returns null-separated drive strings
    raw = buf.value
    drive_strings = []
    i = 0
    while i < len(raw):
        end = raw.find('\0', i)
        if end == -1:
            break
        s = raw[i:end]
        if s:
            drive_strings.append(s)
        i = end + 1

    for ds in drive_strings:
        letter = ds[0].upper()
        drive_type_id = kernel32.GetDriveTypeW(ds)
        drive_type = DRIVE_TYPE_NAMES.get(drive_type_id, "Unknown")

        # Skip CD-ROM and network drives — no block-level access needed
        if drive_type_id in (DRIVE_CDROM, DRIVE_REMOTE):
            continue

        # Volume information
        vol_label   = ctypes.create_unicode_buffer(256)
        fs_name     = ctypes.create_unicode_buffer(256)
        serial      = wintypes.DWORD(0)
        max_comp    = wintypes.DWORD(0)
        fs_flags    = wintypes.DWORD(0)

        ok = kernel32.GetVolumeInformationW(
            ds, vol_label, 256,
            ctypes.byref(serial),
            ctypes.byref(max_comp),
            ctypes.byref(fs_flags),
            fs_name, 256,
        )
        label   = vol_label.value if ok else ""
        fs_type = fs_name.value   if ok else "Unknown"

        # Disk space
        free_bytes_caller   = ctypes.c_int64(0)
        total_bytes         = ctypes.c_int64(0)
        total_free_bytes    = ctypes.c_int64(0)
        kernel32.GetDiskFreeSpaceExW(
            ds,
            ctypes.byref(free_bytes_caller),
            ctypes.byref(total_bytes),
            ctypes.byref(total_free_bytes),
        )

        # Sector size via GetDiskFreeSpaceW
        sectors_per_cluster = wintypes.DWORD(0)
        bytes_per_sector    = wintypes.DWORD(0)
        free_clusters       = wintypes.DWORD(0)
        total_clusters      = wintypes.DWORD(0)
        kernel32.GetDiskFreeSpaceW(
            ds,
            ctypes.byref(sectors_per_cluster),
            ctypes.byref(bytes_per_sector),
            ctypes.byref(free_clusters),
            ctypes.byref(total_clusters),
        )
        sector_size = bytes_per_sector.value or 512

        drives.append(DriveInfo(
            letter      = letter,
            label       = label,
            fs_type     = fs_type,
            drive_type  = drive_type,
            total_bytes = total_bytes.value,
            free_bytes  = total_free_bytes.value,
            sector_size = sector_size,
        ))

    return drives


def _mock_drives() -> List[DriveInfo]:
    """Demo drives for non-Windows development / UI testing."""
    return [
        DriveInfo("C", "System",    "NTFS",  "Fixed",     512*1024**3, 120*1024**3, 512),
        DriveInfo("D", "Data",      "NTFS",  "Fixed",     1024*1024**3, 400*1024**3, 512),
        DriveInfo("E", "USB Drive", "FAT32", "Removable", 32*1024**3,  10*1024**3, 512),
    ]


# ─────────────────────────────────────────────────────────
# Raw disk handle management
# ─────────────────────────────────────────────────────────
def open_disk_handle(drive_letter: str) -> Optional[int]:
    """
    Open a raw, read-only handle to a logical volume via the UNC path syntax
    (backslash backslash dot backslash DriveLetter colon).

    The handle is opened with FILE_SHARE_READ | FILE_SHARE_WRITE and
    FILE_FLAG_NO_BUFFERING so reads bypass the OS page cache and always
    land on sector boundaries (required for raw sector I/O).

    Returns the integer handle value, or None on failure.
    IMPORTANT: Caller must close the handle with close_disk_handle().
    """
    if not IS_WINDOWS:
        return None  # Simulation mode

    path = "\\\\.\\{}:".format(drive_letter)
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )

    if handle == INVALID_HANDLE_VALUE:
        err = kernel32.GetLastError()
        raise OSError(f"Cannot open drive {drive_letter}: Win32 error {err}. "
                      f"Run as Administrator for raw disk access.")
    return handle


def close_disk_handle(handle: int) -> None:
    """Close a raw disk handle obtained from open_disk_handle()."""
    if IS_WINDOWS and handle and handle != INVALID_HANDLE_VALUE:
        kernel32.CloseHandle(handle)


def read_sectors(handle: int, lba: int, count: int, sector_size: int = 512) -> bytes:
    """
    Read `count` sectors starting at logical block address `lba`.

    Uses SetFilePointerEx for 64-bit seek (required for disks > 2 TB)
    then ReadFile. Both the offset and the read length MUST be multiples
    of the physical sector size when FILE_FLAG_NO_BUFFERING is set.

    Returns raw bytes, or empty bytes on failure.
    """
    if not IS_WINDOWS:
        return b'\x00' * (count * sector_size)  # Simulation mode

    byte_offset = lba * sector_size
    buf_size    = count * sector_size
    buf         = ctypes.create_string_buffer(buf_size)
    bytes_read  = wintypes.DWORD(0)

    # SetFilePointerEx: move to absolute position (method=0 → FILE_BEGIN)
    offset_li = ctypes.c_int64(byte_offset)
    ok = kernel32.SetFilePointerEx(handle, offset_li, None, 0)
    if not ok:
        return b''

    ok = kernel32.ReadFile(handle, buf, buf_size, ctypes.byref(bytes_read), None)
    if not ok or bytes_read.value == 0:
        return b''

    return bytes(buf.raw[:bytes_read.value])


def get_disk_geometry(handle: int) -> Tuple[int, int]:
    """
    Query physical disk geometry via IOCTL_DISK_GET_DRIVE_GEOMETRY_EX.
    Returns (total_bytes, bytes_per_sector).
    Falls back to (0, 512) on failure.
    """
    if not IS_WINDOWS:
        return (0, 512)

    geom   = DISK_GEOMETRY_EX()
    bytes_returned = wintypes.DWORD(0)
    ok = kernel32.DeviceIoControl(
        handle,
        IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
        None, 0,
        ctypes.byref(geom), ctypes.sizeof(geom),
        ctypes.byref(bytes_returned),
        None,
    )
    if ok:
        return (geom.DiskSize, geom.Geometry.BytesPerSector)
    return (0, 512)


# ─────────────────────────────────────────────────────────
# Privilege escalation helper
# ─────────────────────────────────────────────────────────
def is_admin() -> bool:
    """Return True if the current process has Administrator privileges."""
    if not IS_WINDOWS:
        return True  # Treat non-Windows as having access for testing
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def request_admin_relaunch() -> None:
    """Re-launch the current script with UAC elevation (ShellExecuteW runas)."""
    if not IS_WINDOWS:
        return
    params = " ".join(sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{sys.argv[0]}" {params}', None, 1
    )
    sys.exit(0)
