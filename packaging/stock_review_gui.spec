# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Stock Review desktop app.

Build from the project root with the build helper (recommended)::

    powershell -ExecutionPolicy Bypass -File packaging/build_exe.ps1

...or directly::

    .venv313\\Scripts\\pyinstaller.exe packaging/stock_review_gui.spec --noconfirm

Produces ONE one-folder bundle in ``dist/Stock Review/`` that contains THREE
executables sharing a single ``_internal/``:

    * ``Stock Review.exe``           -> the windowed Tkinter GUI (console=False)
    * ``Stock Review Terminal.exe``  -> the real CLI with ASCII animations
                                        (console=True)
    * ``Stock Review Board.exe``     -> the dark "Live board" window (console=False)

Both freeze the same ``packaging/frozen_entry.py``, which picks GUI vs CLI from
the running exe's own filename — so there is exactly one Analysis, one shared
bundle, and zero chance the GUI and CLI drift apart. One-folder is chosen over
one-file: faster to start and far less flaky with heavy scientific deps.
"""
import os
from PyInstaller.utils.hooks import collect_all

# SPECPATH is injected by PyInstaller = the folder holding this spec (packaging/).
# Anchor everything to the project root so the build works no matter what the
# current working directory is when pyinstaller is invoked.
ROOT = os.path.dirname(SPECPATH)
ICON = os.path.join(SPECPATH, "stock_review.ico")

# curl_cffi ships a compiled libcurl + cacert bundle that PyInstaller does not
# pick up automatically; yfinance pulls a few dynamic imports. collect_all grabs
# their submodules, data files and binaries in one shot.
datas, binaries, hiddenimports = [], [], []
for _pkg in ("curl_cffi", "yfinance"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# Ship the window icon inside the bundle so gui.py can set it at runtime
# (resolved via sys._MEIPASS). Lands at the root of _internal/.
datas += [(ICON, ".")]

# yfinance's optional/lazy dependencies that static analysis can miss.
hiddenimports += [
    "peewee", "frozendict", "multitasking", "platformdirs",
    "websockets", "bs4",
]

# The GUI imports these inside run() (a nested relative import PyInstaller's
# graph normally follows anyway) — list them explicitly so a frozen build can
# never drop the Win98 theme or the in-app ASCII animation.
hiddenimports += [
    "stock_analyzer.review.gui_theme",
    "stock_analyzer.review.gui_anim",
    "stock_analyzer.review.gui_board",
]

a = Analysis(
    [os.path.join(SPECPATH, "frozen_entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim weight: these are installed in the venv for the *other* pipeline
    # (indicators/scoring/storage) but the review/GUI path never imports them.
    excludes=["scipy", "ta", "sqlalchemy", "matplotlib", "PIL", "pytest",
              "IPython", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Two executables, one shared bundle. They differ ONLY by name + console flag;
# frozen_entry.py reads the running exe's name to decide GUI vs CLI at startup.
exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Stock Review",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed GUI (no console). Flip to True to debug.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
exe_terminal = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Stock Review Terminal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # real console => stderr.isatty() => ASCII animations
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
# Third entry: the dark "Live board" window (gui_board). Windowed, like the GUI.
exe_board = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Stock Review Board",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
coll = COLLECT(
    exe_gui,
    exe_terminal,
    exe_board,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Stock Review",
)
