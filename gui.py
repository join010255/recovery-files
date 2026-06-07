"""
gui.py — Professional file recovery GUI built with PySide6 (falls back to PyQt6).

Layout:
  ┌──────────────────────────────────────────────────────────────┐
  │  HEADER: logo + title                                        │
  ├─────────────────────┬────────────────────────────────────────┤
  │  LEFT PANEL         │  RIGHT PANEL                           │
  │  • Drive selector   │  • Results table                       │
  │  • Scan type        │  • File detail panel                   │
  │  • Output dir       │                                        │
  │  • Start / Stop     │                                        │
  │  • Progress bar     │                                        │
  │  • Stats            │                                        │
  └─────────────────────┴────────────────────────────────────────┘
  • Status bar at bottom

Threading model:
  ScanWorker runs in a QThread and emits signals consumed by the GUI.
  All disk I/O stays off the main thread; GUI never blocks.
"""

import os
import sys
import time
import threading
from typing import List, Optional

# ── Qt import with graceful fallback ──────────────────────
try:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
        QProgressBar, QFileDialog, QHeaderView, QSplitter, QFrame,
        QTextEdit, QGroupBox, QLineEdit, QCheckBox, QAbstractItemView,
        QSizePolicy, QStatusBar, QMessageBox, QStyle,
    )
    from PySide6.QtCore import (
        Qt, QThread, Signal, QSize, QTimer, QRunnable, QThreadPool,
    )
    from PySide6.QtGui import (
        QColor, QFont, QIcon, QPalette, QPixmap, QLinearGradient,
        QBrush, QPainter, QFontDatabase,
    )
    QT_BACKEND = "PySide6"
except ImportError:
    from PyQt6.QtWidgets import (                                    # type: ignore
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
        QProgressBar, QFileDialog, QHeaderView, QSplitter, QFrame,
        QTextEdit, QGroupBox, QLineEdit, QCheckBox, QAbstractItemView,
        QSizePolicy, QStatusBar, QMessageBox, QStyle,
    )
    from PyQt6.QtCore import (                                       # type: ignore
        Qt, QThread, pyqtSignal as Signal, QSize, QTimer,
        QRunnable, QThreadPool,
    )
    from PyQt6.QtGui import (                                        # type: ignore
        QColor, QFont, QIcon, QPalette, QPixmap, QLinearGradient,
        QBrush, QPainter, QFontDatabase,
    )
    QT_BACKEND = "PyQt6"

import winapi
import scanner
import recovery


# ─────────────────────────────────────────────────────────
# Colour palette & stylesheet
# ─────────────────────────────────────────────────────────
DARK_BG     = "#0D1117"
PANEL_BG    = "#161B22"
BORDER      = "#30363D"
ACCENT      = "#238636"
ACCENT_HOV  = "#2EA043"
DANGER      = "#DA3633"
DANGER_HOV  = "#F85149"
TEXT_PRI    = "#E6EDF3"
TEXT_SEC    = "#8B949E"
TEXT_DIM    = "#484F58"
HIGHLIGHT   = "#1F6FEB"
ROW_ALT     = "#1A2030"
AMBER       = "#D29922"

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {DARK_BG};
    color: {TEXT_PRI};
    font-family: "Segoe UI", "Consolas", monospace;
    font-size: 13px;
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 10px;
    padding: 8px;
    font-weight: 600;
    color: {TEXT_SEC};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_SEC};
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
}}
QLabel {{
    color: {TEXT_PRI};
}}
QLabel#dimLabel {{
    color: {TEXT_DIM};
    font-size: 11px;
}}
QComboBox {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 10px;
    color: {TEXT_PRI};
    min-height: 28px;
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    color: {TEXT_PRI};
    selection-background-color: {HIGHLIGHT};
}}
QPushButton {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 7px 16px;
    color: {TEXT_PRI};
    font-weight: 500;
    min-height: 30px;
}}
QPushButton:hover {{
    border-color: {TEXT_SEC};
    background-color: #1C2128;
}}
QPushButton#btnScan {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: #FFFFFF;
    font-weight: 700;
    font-size: 14px;
    min-height: 40px;
    border-radius: 6px;
}}
QPushButton#btnScan:hover {{
    background-color: {ACCENT_HOV};
}}
QPushButton#btnScan:disabled {{
    background-color: {TEXT_DIM};
    border-color: {TEXT_DIM};
    color: {DARK_BG};
}}
QPushButton#btnStop {{
    background-color: {DANGER};
    border-color: {DANGER};
    color: #FFFFFF;
    font-weight: 700;
    font-size: 14px;
    min-height: 40px;
    border-radius: 6px;
}}
QPushButton#btnStop:hover {{
    background-color: {DANGER_HOV};
}}
QPushButton#btnRecover {{
    background-color: {HIGHLIGHT};
    border-color: {HIGHLIGHT};
    color: #FFFFFF;
    font-weight: 600;
    min-height: 36px;
    border-radius: 5px;
}}
QPushButton#btnRecover:hover {{
    background-color: #388BFD;
}}
QPushButton#btnRecover:disabled {{
    background-color: {TEXT_DIM};
    border-color: {TEXT_DIM};
    color: {DARK_BG};
}}
QTableWidget {{
    background-color: {PANEL_BG};
    alternate-background-color: {ROW_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    gridline-color: {BORDER};
    color: {TEXT_PRI};
    selection-background-color: {HIGHLIGHT};
}}
QTableWidget::item {{
    padding: 4px 8px;
    border: none;
}}
QHeaderView::section {{
    background-color: #0D1117;
    color: {TEXT_SEC};
    border: none;
    border-bottom: 2px solid {BORDER};
    border-right: 1px solid {BORDER};
    padding: 6px 10px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}
QProgressBar {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    color: {TEXT_PRI};
    font-size: 11px;
    min-height: 20px;
}}
QProgressBar::chunk {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {ACCENT}, stop:1 {HIGHLIGHT});
    border-radius: 3px;
}}
QTextEdit {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    color: {TEXT_PRI};
    font-family: "Consolas", monospace;
    font-size: 12px;
    padding: 6px;
}}
QLineEdit {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 10px;
    color: {TEXT_PRI};
    min-height: 28px;
}}
QLineEdit:focus {{
    border-color: {HIGHLIGHT};
}}
QCheckBox {{
    spacing: 6px;
    color: {TEXT_PRI};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {PANEL_BG};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QStatusBar {{
    background-color: #010409;
    color: {TEXT_SEC};
    border-top: 1px solid {BORDER};
    font-size: 11px;
}}
QSplitter::handle {{
    background-color: {BORDER};
    width: 1px;
}}
QScrollBar:vertical {{
    background-color: {DARK_BG};
    width: 10px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background-color: {BORDER};
    border-radius: 5px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {TEXT_DIM};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""


# ─────────────────────────────────────────────────────────
# Column indices
# ─────────────────────────────────────────────────────────
COL_NAME       = 0
COL_TYPE       = 1
COL_CATEGORY   = 2
COL_PATH       = 3
COL_SIZE       = 4
COL_METHOD     = 5
COL_CONFIDENCE = 6
COLUMNS = ["File Name", "Type", "Category", "Path", "Size", "Method", "Confidence"]


# ─────────────────────────────────────────────────────────
# Worker thread
# ─────────────────────────────────────────────────────────
class ScanWorker(QThread):
    """
    Background worker: runs the disk scan + optional carving.
    Emits Qt signals consumed by MainWindow slots.
    """
    sig_progress  = Signal(int, int, str)          # (value, maximum, message)
    sig_found     = Signal(object)                  # CandidateFile
    sig_finished  = Signal(int, float)              # (total_found, elapsed_seconds)
    sig_error     = Signal(str)                     # error message

    def __init__(
        self,
        drive: winapi.DriveInfo,
        do_carve: bool,
        output_dir: str,
    ):
        super().__init__()
        self.drive      = drive
        self.do_carve   = do_carve
        self.output_dir = output_dir
        self._stop      = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        start    = time.time()
        found    = 0
        try:
            # Phase 1: filesystem metadata scan
            self.sig_progress.emit(0, 100, f"Phase 1: Scanning {self.drive.fs_type} metadata…")
            for cand in scanner.scan_drive(self.drive, self._emit_progress, self._stop):
                if self._stop.is_set():
                    break
                self.sig_found.emit(cand)
                found += 1

            # Phase 2: signature carving (if requested)
            if self.do_carve and not self._stop.is_set():
                self.sig_progress.emit(0, 100, "Phase 2: Carving disk for file signatures…")
                for cand in recovery.carve_disk(
                    self.drive.letter,
                    self.drive.total_bytes,
                    self.output_dir,
                    self._emit_progress,
                    self._stop,
                    self.drive.sector_size,
                ):
                    if self._stop.is_set():
                        break
                    self.sig_found.emit(cand)
                    found += 1

        except PermissionError as e:
            self.sig_error.emit(
                f"Permission denied: {e}\n\nPlease run the application as Administrator."
            )
        except Exception as e:
            self.sig_error.emit(str(e))
        finally:
            elapsed = time.time() - start
            self.sig_finished.emit(found, elapsed)

    def _emit_progress(self, value: int, maximum: int, msg: str) -> None:
        self.sig_progress.emit(value, maximum, msg)


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _confidence_color(score: float) -> QColor:
    if score >= 0.8:
        return QColor("#2EA043")
    if score >= 0.5:
        return QColor("#D29922")
    return QColor("#F85149")


def _ext_category(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    IMAGE  = {"jpg","jpeg","png","gif","bmp","tiff","webp"}
    DOC    = {"pdf","doc","docx","txt","rtf","xls","xlsx","ppt","pptx"}
    ARCH   = {"zip","rar","7z","tar","gz"}
    VIDEO  = {"mp4","avi","mkv","mov","wmv"}
    AUDIO  = {"mp3","wav","aac","flac","ogg"}
    EXE    = {"exe","dll","sys","msi","bat","cmd"}
    FOREN  = {"db","sqlite","log","pst","eml"}
    if ext in IMAGE:  return "🖼  Image"
    if ext in DOC:    return "📄 Document"
    if ext in ARCH:   return "🗜  Archive"
    if ext in VIDEO:  return "🎬 Video"
    if ext in AUDIO:  return "🎵 Audio"
    if ext in EXE:    return "⚙  Executable"
    if ext in FOREN:  return "🔍 Forensic"
    return "📁 Other"


# ─────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PhoenixRecover — Professional File Recovery")
        self.setMinimumSize(1200, 760)
        self.resize(1400, 860)

        self._worker:    Optional[ScanWorker]       = None
        self._drives:    List[winapi.DriveInfo]      = []
        self._candidates: List[recovery.CandidateFile] = []

        self._build_ui()
        self._apply_style()
        self._refresh_drives()

        # Periodically re-check admin status in the status bar
        self._admin_timer = QTimer(self)
        self._admin_timer.timeout.connect(self._update_admin_status)
        self._admin_timer.start(5000)
        self._update_admin_status()

    # ── UI construction ───────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([320, 880])
        splitter.setHandleWidth(1)
        root_layout.addWidget(splitter, 1)

        # Status bar
        self.statusBar().showMessage("Ready — select a drive and press Scan")

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(56)
        header.setObjectName("header")
        header.setStyleSheet(
            f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 #010409, stop:0.5 {PANEL_BG}, stop:1 #010409);"
            f"border-bottom: 1px solid {BORDER};"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)

        # Icon + title
        icon_lbl = QLabel("🔥")
        icon_lbl.setStyleSheet("font-size: 24px;")

        title_lbl = QLabel("PhoenixRecover")
        title_lbl.setStyleSheet(
            f"font-size: 20px; font-weight: 800; "
            f"color: {TEXT_PRI}; letter-spacing: -0.5px;"
        )

        sub_lbl = QLabel("Professional File Recovery System")
        sub_lbl.setStyleSheet(f"color: {TEXT_SEC}; font-size: 12px; margin-left: 12px;")

        self._admin_badge = QLabel()
        self._admin_badge.setStyleSheet(
            "font-size: 11px; padding: 2px 8px; border-radius: 10px;"
        )

        hl.addWidget(icon_lbl)
        hl.addWidget(title_lbl)
        hl.addWidget(sub_lbl)
        hl.addStretch()
        hl.addWidget(self._admin_badge)
        return header

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(280)
        panel.setMaximumWidth(380)
        panel.setStyleSheet(f"background-color: {PANEL_BG}; border-right: 1px solid {BORDER};")
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(16, 16, 16, 16)
        vl.setSpacing(14)

        # ── Drive selector ──
        drv_grp = QGroupBox("Target Drive")
        drv_lay = QVBoxLayout(drv_grp)
        self._combo_drives = QComboBox()
        self._combo_drives.currentIndexChanged.connect(self._on_drive_changed)
        self._btn_refresh = QPushButton("⟳  Refresh Drives")
        self._btn_refresh.clicked.connect(self._refresh_drives)

        self._lbl_drive_info = QLabel("Select a drive above")
        self._lbl_drive_info.setObjectName("dimLabel")
        self._lbl_drive_info.setWordWrap(True)

        drv_lay.addWidget(self._combo_drives)
        drv_lay.addWidget(self._lbl_drive_info)
        drv_lay.addWidget(self._btn_refresh)

        # ── Scan options ──
        opt_grp = QGroupBox("Scan Options")
        opt_lay = QVBoxLayout(opt_grp)
        self._chk_carve = QCheckBox("Enable signature carving (deeper, slower)")
        self._chk_carve.setChecked(True)
        self._chk_ntfs  = QCheckBox("NTFS MFT analysis")
        self._chk_ntfs.setChecked(True)
        self._chk_ntfs.setEnabled(False)  # always on
        opt_lay.addWidget(self._chk_carve)
        opt_lay.addWidget(self._chk_ntfs)

        # ── Output directory ──
        out_grp = QGroupBox("Recovery Output Directory")
        out_lay = QVBoxLayout(out_grp)
        out_row = QHBoxLayout()
        default_out = os.path.join(os.path.expanduser("~"), "PhoenixRecover_Output")
        self._edit_outdir = QLineEdit(default_out)
        btn_browse = QPushButton("…")
        btn_browse.setFixedWidth(36)
        btn_browse.clicked.connect(self._browse_output)
        out_row.addWidget(self._edit_outdir)
        out_row.addWidget(btn_browse)
        out_lay.addLayout(out_row)

        # ── Controls ──
        self._btn_scan = QPushButton("⚡  Start Scan")
        self._btn_scan.setObjectName("btnScan")
        self._btn_scan.clicked.connect(self._start_scan)

        self._btn_stop = QPushButton("⏹  Stop Scan")
        self._btn_stop.setObjectName("btnStop")
        self._btn_stop.clicked.connect(self._stop_scan)
        self._btn_stop.setEnabled(False)

        # ── Progress ──
        prog_grp = QGroupBox("Scan Progress")
        prog_lay = QVBoxLayout(prog_grp)
        self._progress   = QProgressBar()
        self._progress.setRange(0, 100)
        self._lbl_status = QLabel("Idle")
        self._lbl_status.setObjectName("dimLabel")
        self._lbl_status.setWordWrap(True)
        prog_lay.addWidget(self._progress)
        prog_lay.addWidget(self._lbl_status)

        # ── Stats ──
        stats_grp = QGroupBox("Session Statistics")
        stats_lay = QVBoxLayout(stats_grp)
        self._lbl_stats = QLabel("Files found: 0\nMFT records: 0\nCarved: 0")
        self._lbl_stats.setObjectName("dimLabel")
        self._lbl_stats.setWordWrap(True)
        self._stat_total  = 0
        self._stat_mft    = 0
        self._stat_carved = 0
        stats_lay.addWidget(self._lbl_stats)

        vl.addWidget(drv_grp)
        vl.addWidget(opt_grp)
        vl.addWidget(out_grp)
        vl.addWidget(self._btn_scan)
        vl.addWidget(self._btn_stop)
        vl.addWidget(prog_grp)
        vl.addWidget(stats_grp)
        vl.addStretch()
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(12, 12, 12, 12)
        vl.setSpacing(8)

        # ── Toolbar row ──
        toolbar = QHBoxLayout()

        lbl_results = QLabel("Recovered File Candidates")
        lbl_results.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {TEXT_PRI};")

        self._btn_recover_sel = QPushButton("💾  Recover Selected")
        self._btn_recover_sel.setObjectName("btnRecover")
        self._btn_recover_sel.clicked.connect(self._recover_selected)
        self._btn_recover_sel.setEnabled(False)

        self._btn_recover_all = QPushButton("💾  Recover All")
        self._btn_recover_all.setObjectName("btnRecover")
        self._btn_recover_all.clicked.connect(self._recover_all)
        self._btn_recover_all.setEnabled(False)

        btn_clear = QPushButton("🗑  Clear")
        btn_clear.clicked.connect(self._clear_results)

        self._lbl_count = QLabel("0 files")
        self._lbl_count.setStyleSheet(f"color: {TEXT_SEC}; font-size: 12px;")

        toolbar.addWidget(lbl_results)
        toolbar.addStretch()
        toolbar.addWidget(self._lbl_count)
        toolbar.addWidget(self._btn_recover_sel)
        toolbar.addWidget(self._btn_recover_all)
        toolbar.addWidget(btn_clear)
        vl.addLayout(toolbar)

        # ── Results table ──
        self._table = QTableWidget()
        self._table.setColumnCount(len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(COL_NAME,       220)
        self._table.setColumnWidth(COL_TYPE,        60)
        self._table.setColumnWidth(COL_CATEGORY,   110)
        self._table.setColumnWidth(COL_PATH,       200)
        self._table.setColumnWidth(COL_SIZE,        90)
        self._table.setColumnWidth(COL_METHOD,      90)
        self._table.setColumnWidth(COL_CONFIDENCE,  90)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.setRowCount(0)

        # ── Detail panel ──
        detail_frame = QFrame()
        detail_frame.setFixedHeight(120)
        detail_frame.setStyleSheet(
            f"background: {PANEL_BG}; border: 1px solid {BORDER}; border-radius: 6px;"
        )
        dl = QHBoxLayout(detail_frame)
        dl.setContentsMargins(12, 8, 12, 8)

        self._lbl_detail = QLabel("Select a file in the table to see details.")
        self._lbl_detail.setStyleSheet(f"color: {TEXT_SEC}; font-size: 12px;")
        self._lbl_detail.setWordWrap(True)
        self._lbl_detail.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self._txt_log = QTextEdit()
        self._txt_log.setReadOnly(True)
        self._txt_log.setPlaceholderText("Scan log output…")
        self._txt_log.setFixedWidth(260)

        dl.addWidget(self._lbl_detail, 2)
        dl.addWidget(self._txt_log, 1)

        vl.addWidget(self._table, 1)
        vl.addWidget(detail_frame)
        return panel

    def _apply_style(self):
        self.setStyleSheet(STYLESHEET)

    # ── Drive management ──────────────────────────────────
    def _refresh_drives(self):
        self._drives = winapi.enumerate_drives()
        self._combo_drives.clear()
        for d in self._drives:
            self._combo_drives.addItem(d.display_name)
        if self._drives:
            self._on_drive_changed(0)

    def _on_drive_changed(self, idx: int):
        if 0 <= idx < len(self._drives):
            d = self._drives[idx]
            self._lbl_drive_info.setText(
                f"FS: {d.fs_type}   Size: {_fmt_size(d.total_bytes)}\n"
                f"Free: {_fmt_size(d.free_bytes)}   Sector: {d.sector_size}B"
            )

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select Output Directory",
                                                 self._edit_outdir.text())
        if path:
            self._edit_outdir.setText(path)

    # ── Scan control ─────────────────────────────────────
    def _start_scan(self):
        if not self._drives:
            QMessageBox.warning(self, "No Drives", "No drives available to scan.")
            return

        idx = self._combo_drives.currentIndex()
        if idx < 0:
            return

        drive = self._drives[idx]
        outdir = self._edit_outdir.text().strip()
        if not outdir:
            QMessageBox.warning(self, "Output Required",
                                "Please specify an output directory for recovered files.")
            return

        if not winapi.is_admin():
            ret = QMessageBox.question(
                self, "Administrator Required",
                "Raw disk access requires Administrator privileges.\n\n"
                "In demo/simulation mode, scan will run without actual disk access.\n"
                "Continue in simulation mode?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return

        os.makedirs(outdir, exist_ok=True)

        # Reset stats
        self._stat_total = self._stat_mft = self._stat_carved = 0
        self._candidates.clear()

        self._btn_scan.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_recover_sel.setEnabled(False)
        self._btn_recover_all.setEnabled(False)
        self._table.setRowCount(0)
        self._progress.setValue(0)
        self._txt_log.clear()

        self._worker = ScanWorker(drive, self._chk_carve.isChecked(), outdir)
        self._worker.sig_progress.connect(self._on_progress)
        self._worker.sig_found.connect(self._on_candidate_found)
        self._worker.sig_finished.connect(self._on_scan_finished)
        self._worker.sig_error.connect(self._on_scan_error)
        self._worker.start()

        self._log(f"Scan started on {drive.letter}: ({drive.fs_type}) "
                  f"— carving {'ON' if self._chk_carve.isChecked() else 'OFF'}")

    def _stop_scan(self):
        if self._worker:
            self._worker.stop()
            self._log("Stop requested — finishing current sector…")
        self._btn_stop.setEnabled(False)

    # ── Worker signal handlers ────────────────────────────
    def _on_progress(self, value: int, maximum: int, msg: str):
        if maximum > 0:
            pct = min(100, int(value / maximum * 100))
            self._progress.setValue(pct)
        self._lbl_status.setText(msg[:100])
        self.statusBar().showMessage(msg)

    def _on_candidate_found(self, cand: recovery.CandidateFile):
        self._candidates.append(cand)
        self._stat_total += 1

        if cand.method == scanner.RecoveryMethod.MFT:
            self._stat_mft += 1
        elif cand.method == scanner.RecoveryMethod.CARVING:
            self._stat_carved += 1

        self._update_stats()
        self._add_table_row(cand)
        self._btn_recover_all.setEnabled(True)

    def _on_scan_finished(self, total: int, elapsed: float):
        self._progress.setValue(100)
        msg = (f"Scan complete — {total:,} candidates found in {elapsed:.1f}s")
        self._lbl_status.setText(msg)
        self.statusBar().showMessage(msg)
        self._log(msg)
        self._btn_scan.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._lbl_count.setText(f"{total:,} files")

    def _on_scan_error(self, err: str):
        self._log(f"ERROR: {err}")
        QMessageBox.critical(self, "Scan Error", err)
        self._btn_scan.setEnabled(True)
        self._btn_stop.setEnabled(False)

    # ── Table management ──────────────────────────────────
    def _add_table_row(self, cand: recovery.CandidateFile):
        self._table.setSortingEnabled(False)
        row = self._table.rowCount()
        self._table.insertRow(row)

        ext = cand.extension.lstrip(".")
        items = [
            QTableWidgetItem(cand.name),
            QTableWidgetItem(ext.upper() or "?"),
            QTableWidgetItem(_ext_category(ext)),
            QTableWidgetItem(cand.path or "—"),
            QTableWidgetItem(_fmt_size(cand.size_bytes) if cand.size_bytes else "?"),
            QTableWidgetItem(cand.method.name),
            QTableWidgetItem(f"{cand.confidence*100:.0f}%"),
        ]

        # Colour the confidence cell
        conf_item = items[COL_CONFIDENCE]
        conf_item.setForeground(_confidence_color(cand.confidence))

        # Colour method badge
        method_item = items[COL_METHOD]
        if cand.method == scanner.RecoveryMethod.MFT:
            method_item.setForeground(QColor(HIGHLIGHT))
        elif cand.method == scanner.RecoveryMethod.FAT_DIR:
            method_item.setForeground(QColor(AMBER))
        else:
            method_item.setForeground(QColor(TEXT_SEC))

        # Store candidate ref in first item
        items[COL_NAME].setData(Qt.ItemDataRole.UserRole, cand)

        for col, item in enumerate(items):
            self._table.setItem(row, col, item)

        self._table.setSortingEnabled(True)

        # Auto-scroll only if near bottom
        self._table.scrollToBottom()

    def _on_selection_changed(self):
        selected = self._table.selectedItems()
        if not selected:
            self._lbl_detail.setText("Select a file in the table to see details.")
            self._btn_recover_sel.setEnabled(False)
            return

        row = self._table.currentRow()
        name_item = self._table.item(row, COL_NAME)
        if name_item is None:
            return

        cand: Optional[recovery.CandidateFile] = name_item.data(Qt.ItemDataRole.UserRole)
        if cand:
            self._lbl_detail.setText(
                f"<b>{cand.name}</b><br>"
                f"Method: {cand.method.name}  |  "
                f"Confidence: {cand.confidence*100:.0f}%  |  "
                f"Size: {_fmt_size(cand.size_bytes)}<br>"
                f"Disk offset: {cand.offset:,} bytes  |  "
                f"MFT record: {cand.mft_record_no if cand.mft_record_no >= 0 else 'N/A'}<br>"
                f"Path: {cand.path or 'Unknown'}"
            )
        self._btn_recover_sel.setEnabled(True)

    def _clear_results(self):
        self._table.setRowCount(0)
        self._candidates.clear()
        self._stat_total = self._stat_mft = self._stat_carved = 0
        self._update_stats()
        self._btn_recover_sel.setEnabled(False)
        self._btn_recover_all.setEnabled(False)
        self._lbl_count.setText("0 files")
        self._lbl_detail.setText("Select a file in the table to see details.")

    # ── Recovery actions ──────────────────────────────────
    def _recover_selected(self):
        selected_rows = list({idx.row() for idx in self._table.selectedIndexes()})
        if not selected_rows:
            return

        drive_idx = self._combo_drives.currentIndex()
        if drive_idx < 0:
            return

        drive  = self._drives[drive_idx]
        outdir = self._edit_outdir.text().strip()
        ok_count = 0

        for row in selected_rows:
            item = self._table.item(row, COL_NAME)
            if item is None:
                continue
            cand = item.data(Qt.ItemDataRole.UserRole)
            if cand is None:
                continue
            success, result = recovery.recover_candidate(
                cand, drive.letter, outdir, drive.sector_size
            )
            if success:
                ok_count += 1
                self._log(f"Recovered: {os.path.basename(result)}")
            else:
                self._log(f"Failed: {cand.name} — {result}")

        QMessageBox.information(
            self, "Recovery Complete",
            f"Successfully recovered {ok_count}/{len(selected_rows)} file(s) to:\n{outdir}"
        )

    def _recover_all(self):
        if not self._candidates:
            return
        ret = QMessageBox.question(
            self, "Recover All Files",
            f"Recover all {len(self._candidates):,} candidate files?\n"
            f"This may take a while for large scans.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        drive_idx = self._combo_drives.currentIndex()
        if drive_idx < 0:
            return

        drive  = self._drives[drive_idx]
        outdir = self._edit_outdir.text().strip()
        ok_count = 0

        for cand in self._candidates:
            success, result = recovery.recover_candidate(
                cand, drive.letter, outdir, drive.sector_size
            )
            if success:
                ok_count += 1

        QMessageBox.information(
            self, "Recovery Complete",
            f"Recovered {ok_count:,}/{len(self._candidates):,} files to:\n{outdir}"
        )
        self._log(f"Batch recovery: {ok_count}/{len(self._candidates)} files recovered.")

    # ── Misc helpers ──────────────────────────────────────
    def _update_stats(self):
        self._lbl_stats.setText(
            f"Files found: {self._stat_total:,}\n"
            f"  MFT/FAT: {self._stat_mft:,}\n"
            f"  Carved:  {self._stat_carved:,}"
        )
        self._lbl_count.setText(f"{self._stat_total:,} files")

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._txt_log.append(f"[{ts}] {msg}")

    def _update_admin_status(self):
        if winapi.is_admin():
            self._admin_badge.setText("🔓 Administrator")
            self._admin_badge.setStyleSheet(
                f"font-size: 11px; padding: 2px 10px; border-radius: 10px;"
                f"background: {ACCENT}; color: white;"
            )
        else:
            self._admin_badge.setText("🔒 Limited Access")
            self._admin_badge.setStyleSheet(
                f"font-size: 11px; padding: 2px 10px; border-radius: 10px;"
                f"background: {AMBER}; color: #0D1117;"
            )

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
        event.accept()


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PhoenixRecover")
    app.setOrganizationName("PhoenixSoftware")

    # High-DPI support
    try:
        app.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough   # type: ignore
        )
    except AttributeError:
        pass

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
