# -*- mode: python ; coding: utf-8 -*-

import os
import sys

MAIN_SCRIPT = os.path.join('src', 'main.py')
APP_ICON = os.path.join('assets', 'sleepy-ico.ico')
HOOKS_DIR = os.path.abspath('hooks')
EXE_ICON = [APP_ICON] if (sys.platform.startswith('win') or sys.platform == 'darwin') else None

a = Analysis(
    [MAIN_SCRIPT],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets')],
    hiddenimports=[],
    hookspath=[HOOKS_DIR],
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
    icon=EXE_ICON,
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
