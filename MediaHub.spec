# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('C:/Users/Сергей Уткин/Documents/deepseek-harness/default-workspace/mediahub/web', 'web')]
binaries = [('C:/Users/Сергей Уткин/Documents/deepseek-harness/default-workspace/mediahub/vendor/ffmpeg/ffmpeg.exe', 'vendor/ffmpeg')]
hiddenimports = ['pystray._win32', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on']
hiddenimports += collect_submodules('mediahub')
tmp_ret = collect_all('pillow_heif')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pypdfium2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pypdfium2_raw')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['C:/Users/Сергей Уткин/Documents/deepseek-harness/default-workspace/mediahub/tools/mediahub_app.py'],
    pathex=['C:/Users/Сергей Уткин/Documents/deepseek-harness/default-workspace/mediahub'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='MediaHub',
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
    icon=['C:/Users/Сергей Уткин/Documents/deepseek-harness/default-workspace/mediahub/build/icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MediaHub',
)
