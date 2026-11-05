# -*- mode: python ; coding: utf-8 -*-
# Personal Workspace 打包配置（onefile，窗口版无控制台）。
#   python -m PyInstaller --noconfirm --clean PersonalWorkspace.spec
# 注意：重新打包前先关掉正在运行的 PersonalWorkspace.exe（Windows 文件锁）。
import os

ROOT = SPECPATH

a = Analysis(
    ['run.py'],
    pathex=[os.path.join(ROOT, 'src')],
    binaries=[],
    datas=[
        (os.path.join(ROOT, 'src', 'personal_workspace', 'webui'), 'webui'),
        (os.path.join(ROOT, 'ico.ico'), '.'),
    ],
    hiddenimports=[
        'personal_workspace',
        'personal_workspace.core',
        'personal_workspace.db',
        'personal_workspace.server',
        'personal_workspace.notify',
        'webview',
        'webview.platforms.winforms',
        'clr',
        'clr_loader',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='PersonalWorkspace',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, 'ico.ico'),
)
