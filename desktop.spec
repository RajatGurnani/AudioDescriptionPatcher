# PyInstaller spec for the desktop app - local builds only.
#
#   .venv\Scripts\pip install -r requirements-desktop.txt
#   .venv\Scripts\pyinstaller desktop.spec --noconfirm
#   -> dist\ADPatcher\ADPatcher.exe   (one-folder build; zip the folder
#      to move it to another PC - no Python needed there)
#
# One-folder, not one-file: one-file unpacks ~100 MB to %TEMP% on every
# launch (slow, and a common antivirus false positive).

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [
    ("static", "static"),                   # web UI assets (uPlot, icon)
    ("LICENSE", "."),
    ("THIRD_PARTY_NOTICES.txt", "."),
    ("licenses", "licenses"),               # ffmpeg GPLv3 text + notice
]
datas += collect_data_files("imageio_ffmpeg")   # the ffmpeg.exe binary

# MIT/BSD-style licences of every bundled Python package must travel
# with the binary: copy each installed distribution's licence files to
# licenses/python/<package>/
import importlib.metadata as _md
import os as _os
for _dist in _md.distributions():
    _name = _dist.metadata["Name"] or "unknown"
    for _f in _dist.files or []:
        _base = _os.path.basename(str(_f)).upper()
        if _base.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            _src = str(_dist.locate_file(_f))
            if _os.path.isfile(_src):
                datas.append((_src, f"licenses/python/{_name}"))

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    datas=datas,
    # uvicorn picks its loop/protocol implementations by name at runtime
    hiddenimports=collect_submodules("uvicorn") + ["multipart",
                                                   "python_multipart"],
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2",
              "PySide6"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ADPatcher",
    icon="static/icon.ico",
    console=False,          # GUI app: no console window
    upx=False,              # UPX-packed exes trip antivirus heuristics
)
coll = COLLECT(exe, a.binaries, a.datas, name="ADPatcher", upx=False)
