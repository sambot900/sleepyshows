# -*- mode: python ; coding: utf-8 -*-


import os
import sys


entry_script = os.path.join('src', 'main.py')

binaries = []
if sys.platform.startswith('linux'):
    # Linux-only runtime dependency (bundled so the frozen app works on systems
    # without the matching lib installed).
    binaries.append((os.path.join('libs', 'libxcb-cursor.so.0'), '.'))

icon_files = None
if sys.platform.startswith('win'):
    icon_path = os.path.join('assets', 'sleepy-ico.ico')
    if os.path.exists(icon_path):
        icon_files = [icon_path]


a = Analysis(
    [entry_script],
    pathex=[],
    binaries=binaries,
    datas=[('assets', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SleepyShows',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_files,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SleepyShows',
)
