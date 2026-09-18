# PyInstaller spec for برنامج توزيع الأساتذة والحصص.
#
# Builds a single-file, double-clickable executable (no Python install, no
# terminal needed) for whichever OS this is run on. Build once per target
# OS (PyInstaller cannot cross-compile): see BUILD.md, or just push to
# GitHub and let .github/workflows/build.yml build both Windows and macOS
# automatically.
#
#   pyinstaller app.spec
#
# The result is dist/TeacherScheduler(.exe) - a single portable file.

import sys

block_cipher = None

# Heavy / non-trivial-to-discover dependencies: pull in every submodule,
# binary, and data file automatically rather than hand-listing hidden
# imports (ortools and ttkbootstrap in particular ship many optional
# submodules and non-.py data files that PyInstaller's static import
# scanner would otherwise miss).
from PyInstaller.utils.hooks import collect_all

datas = [
    ("assets/fonts/Amiri-Regular.ttf", "assets/fonts"),
    ("assets/fonts/Amiri-Bold.ttf", "assets/fonts"),
    ("assets/fonts/AMIRI-LICENSE.txt", "assets/fonts"),
    ("data/school_data_default.json", "data"),
]
binaries = []
hiddenimports = []

for pkg in ("ortools", "ttkbootstrap", "reportlab", "arabic_reshaper", "bidi", "PIL"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# ttkbootstrap's themed widget icons go through PIL.ImageTk, which lazily
# imports this small Tk-glue helper module in a way PyInstaller's static
# analysis does not always catch on its own (observed missing even with
# collect_all("PIL") above, on some Pillow/PyInstaller version pairs) -
# without it, the app crashes on startup while building its first themed
# icon (ModuleNotFoundError: No module named 'PIL._tkinter_finder').
hiddenimports += ["PIL._tkinter_finder"]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="TeacherScheduler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=(sys.platform == "darwin"),
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# On macOS, wrap the single-file executable in a real .app bundle so it is
# a normal double-clickable Mac application (Finder icon, no Terminal
# window) instead of a bare Unix executable. Ignored on other platforms.
if sys.platform == "darwin":
    app_bundle = BUNDLE(
        exe,
        name="TeacherScheduler.app",
        icon=None,
        bundle_identifier="com.techniaa.teacherscheduler",
        info_plist={
            "CFBundleName": "برنامج توزيع الأساتذة",
            "CFBundleDisplayName": "برنامج توزيع الأساتذة",
            "NSHighResolutionCapable": True,
        },
    )

