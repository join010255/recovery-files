"""
main.py — Application entry point for PhoenixRecover.

Responsibilities:
  1. Check for required Python packages and print friendly error if missing
  2. Check for Administrator privileges on Windows; offer UAC elevation
  3. Launch the Qt GUI
"""

import sys
import os
import platform

# ── Dependency check ──────────────────────────────────────
REQUIRED_PACKAGES = {
    "PySide6": "pip install PySide6",
    # PyQt6 is an acceptable fallback; we try PySide6 first in gui.py
}

def _check_dependencies():
    missing = []
    try:
        import PySide6  # noqa
        return  # PySide6 available — good
    except ImportError:
        pass

    try:
        import PyQt6    # noqa
        return  # PyQt6 available — acceptable fallback
    except ImportError:
        missing.append("PySide6 (or PyQt6)")

    if missing:
        print("=" * 60)
        print("PhoenixRecover — Missing Dependencies")
        print("=" * 60)
        for pkg in missing:
            hint = REQUIRED_PACKAGES.get(pkg.split()[0], f"pip install {pkg}")
            print(f"  ✗ {pkg}")
            print(f"    Install: {hint}")
        print()
        print("Install all at once:")
        print("  pip install PySide6")
        print("=" * 60)
        sys.exit(1)


def _check_admin_windows():
    """On Windows, warn if not running as Administrator."""
    if platform.system() != "Windows":
        return  # Not applicable on Linux/macOS

    try:
        import ctypes
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False

    if not is_admin:
        print("=" * 60)
        print("⚠  WARNING: Not running as Administrator")
        print("=" * 60)
        print(
            "Raw disk access requires Administrator privileges.\n"
            "The application will start in limited mode.\n"
            "Some drives may be inaccessible for scanning.\n\n"
            "Right-click the application and choose 'Run as Administrator'\n"
            "to enable full disk scanning capabilities."
        )
        print("=" * 60)
        print()


def main():
    _check_dependencies()
    _check_admin_windows()

    # Import after dependency check
    from gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
