# 🔥 PhoenixRecover
### Professional Windows File Recovery System

A professional-grade deleted file recovery tool for Windows featuring NTFS MFT analysis, FAT32 directory scanning, full-disk signature carving, and a modern dark-themed GUI.

---

## ✨ Features

| Category | Capability |
|---|---|
| **Filesystem Support** | NTFS (MFT-aware), FAT32 directory walk, RAW (carving fallback) |
| **Recovery Methods** | MFT metadata, FAT directory entries, signature-based carving |
| **File Types** | 40+ types: images, documents, archives, video, audio, executables, forensic |
| **GUI** | PySide6/PyQt6 dark-theme interface with real-time progress |
| **Safety** | Read-only disk access — source disk is never modified |
| **Performance** | Multithreaded scan; GUI remains responsive at all times |

---

## 🗂 Project Structure

```
file_recovery_tool/
├── main.py          # Entry point — dependency & admin check, launches GUI
├── gui.py           # Qt GUI layer — MainWindow, ScanWorker thread, stylesheet
├── scanner.py       # Disk scanner — NTFS MFT parser, FAT32 directory walk
├── recovery.py      # Recovery engine — signature carving, file extraction
├── winapi.py        # Windows API wrappers — ctypes bindings to Win32
└── requirements.txt # Python dependencies
```

---

## ⚙️ Architecture

```
main.py
  └─► gui.py  (QMainWindow + ScanWorker QThread)
        ├─► scanner.py  ──► winapi.py  (raw disk I/O)
        │     • NTFS: reads MFT, parses FILE records, finds deleted entries
        │     • FAT32: reads dir clusters, finds 0xE5-prefixed entries
        │
        └─► recovery.py ──► winapi.py  (raw sector reads)
              • Carves signatures across full disk
              • Validates header/footer magic
              • Scores confidence (0–100%)
              • Writes recovered files to output directory
```

### Threading Model

```
Main Thread (Qt event loop)
  └─ ScanWorker (QThread)
       ├─ scanner.scan_drive()     → yields CandidateFile objects
       ├─ recovery.carve_disk()    → yields CandidateFile objects
       └─ sig_found signal ──────► GUI adds row to table (thread-safe)
```

---

## 🚀 Installation

### Prerequisites
- **Python 3.10+**
- **Windows 10/11** (required for raw disk access via Win32 API)
- **Administrator privileges** (required for `CreateFile` on `\\.\X:`)

### Install dependencies

```bash
pip install -r requirements.txt
```

Or manually:

```bash
pip install PySide6
```

### Run (as Administrator)

```cmd
# Right-click terminal → "Run as Administrator", then:
python main.py
```

Or create a shortcut with "Run as Administrator" enabled.

---

## 🖥 Usage

1. **Launch** the application as Administrator
2. **Select** a drive from the dropdown (Fixed or Removable drives are listed)
3. **Configure** options:
   - Enable/disable signature carving (deeper but slower)
   - Set output directory for recovered files
4. **Click "Start Scan"** — the scan runs in the background
5. **Review** candidates in the results table:
   - Sort by confidence, size, type, or method
   - Click a row to see detailed metadata
6. **Recover** selected files or all files to the output directory

---

## 🔍 Recovery Methods Explained

### 1. NTFS MFT Analysis (highest confidence)
- Reads the Master File Table directly from disk
- Parses FILE records with `IN_USE` bit = 0 (deleted)
- Extracts original filename, size, and cluster offset
- Applies Update Sequence Array (USA) fixup before parsing
- Confidence: **70–100%**

### 2. FAT32 Directory Walk
- Reads directory clusters looking for entries starting with `0xE5` (deleted marker)
- Reconstructs 8.3 filenames (first character replaced with `?`)
- Follows subdirectory chains
- Confidence: **60–90%**

### 3. Signature-Based Carving (deepest recovery)
- Scans the raw disk in 4 MB chunks
- Matches 40+ known file magic bytes (headers) and footers
- Extracts files at every signature match
- Used as fallback for unknown filesystems or heavily fragmented volumes
- Confidence: **40–80%** depending on header + footer match

---

## 📦 Supported File Types

| Category | Extensions |
|---|---|
| Images | JPG, PNG, GIF, BMP, TIFF, WEBP |
| Documents | PDF, DOC/DOCX, XLS/XLSX, PPT/PPTX, RTF, TXT |
| Archives | ZIP, RAR, 7Z, TAR, GZ |
| Video | MP4, AVI, MKV, MOV, WMV |
| Audio | MP3, WAV, AAC, FLAC, OGG |
| Executables | EXE, DLL, SYS, MSI |
| Forensic | SQLite DB, PST, EML |

---

## 🛡 Safety Guarantees

- **Read-only access**: `CreateFile` is opened with `GENERIC_READ` only — no write flag
- **No filesystem modifications**: the tool never calls `WriteFile`, `SetEndOfFile`, or any write API on the source disk
- **Separate output**: all recovered data is written to the user-specified output directory on a different path
- **FILE_FLAG_NO_BUFFERING**: bypasses OS cache, ensuring raw sector reads without side effects

---

## 🔧 Extending the Tool

### Add a new file signature

In `recovery.py`, add a `Signature` entry to the `SIGNATURES` list:

```python
Signature(
    extension = ".xyz",
    magic     = b"\xAB\xCD\xEF",    # Header magic bytes
    offset    = 0,                   # Offset where magic appears
    max_size  = 10_000_000,          # Max expected file size (bytes)
    end_magic = b"\xFF\xFE",         # Footer bytes (optional)
    category  = "Custom",
)
```

### Support a new filesystem

Create a new generator function in `scanner.py`:

```python
def scan_exfat(drive_letter, progress_cb, stop_event, sector_size) -> Iterator[CandidateFile]:
    ...
```

Then add it to the `scan_drive()` dispatcher:

```python
elif fs == "exFAT":
    yield from scan_exfat(drive.letter, progress_cb, stop_event, drive.sector_size)
```

---

## ⚠️ Limitations

- Heavily fragmented files may be partially recovered (non-contiguous clusters)
- Overwritten sectors cannot be recovered (no quantum effects)
- MFT record reuse may produce false positives — review confidence scores
- Very large disks (> 4 TB) may take significant time to carve
- EXE/DLL recovery may flag many false positives due to `MZ` header prevalence

---

## 📄 License

For professional and personal use. Do not use for data destruction, evidence tampering, or any illegal purpose. Always obtain proper authorisation before scanning disks you do not own.
