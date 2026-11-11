# PyInstaller recipe for the packaged Cell Counter app.
# Build it with build_windows.bat on Windows (see BUILD_WINDOWS.md).
# The result is a folder, dist/Cell Counter/, holding "Cell Counter.exe" and its
# libraries; share the whole folder (zipped). A folder starts much faster than a
# single self-unpacking .exe.

import sys

block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[],
    # stats_view is imported only when the Cell sizes window opens, and matplotlib
    # picks its Qt backend at run time, so name them here to be sure they're included.
    hiddenimports=["stats_view", "matplotlib.backends.backend_qtagg"],
    hookspath=[],
    runtime_hooks=[],
    # Not used by the app; leaving them out keeps the download smaller.
    excludes=["tkinter", "PyQt5", "PyQt6", "IPython", "jupyter", "notebook", "pandas",
              "matplotlib.tests", "numpy.tests", "scipy.tests", "skimage.data"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Cell Counter",
    console=False,            # a normal windowed app, no black console window
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Cell Counter", upx=False)

if sys.platform == "darwin":  # trial builds on the Mac produce a .app as well
    app = BUNDLE(coll, name="Cell Counter.app", bundle_identifier="au.edu.uq.cellcounter")
