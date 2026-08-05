# -*- mode: python ; coding: utf-8 -*-
"""Воспроизводимая Windows one-file сборка с идентификаторами релиза из CI."""

import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata
from project_meta import PROJECT_REPOSITORY, PROJECT_VERSION

root = Path(SPECPATH)
meta_dir = root / "build" / "pyinstaller-meta"
meta_dir.mkdir(parents=True, exist_ok=True)

app_version = os.environ.get("APP_VERSION", f"{PROJECT_VERSION}-dev").strip()
app_repository = os.environ.get("APP_REPOSITORY", PROJECT_REPOSITORY).strip()
app_server_url = os.environ.get("APP_SERVER_URL", "https://github.com").strip()
app_api_url = os.environ.get("APP_API_URL", "https://api.github.com").strip()

(meta_dir / "_build_info.py").write_text(
    "\n".join([
        f"APP_VERSION = {app_version!r}",
        f"APP_REPOSITORY = {app_repository!r}",
        f"APP_SERVER_URL = {app_server_url!r}",
        f"APP_API_URL = {app_api_url!r}",
        "",
    ]),
    encoding="utf-8",
)

match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-dev)?", app_version)
parts = tuple(int(value) for value in match.groups()) if match else (0, 0, 0)
version_file = meta_dir / "windows-version.txt"
version_file.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({parts[0]}, {parts[1]}, {parts[2]}, 0),
    prodvers=({parts[0]}, {parts[1]}, {parts[2]}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'ShikiUpdatesBot contributors'),
        StringStruct('Comments', '{app_server_url.rstrip('/')}/{app_repository}'),
        StringStruct('FileDescription', 'Shikimori activity Telegram bot'),
        StringStruct('FileVersion', '{app_version}'),
        StringStruct('InternalName', 'ShikiUpdatesBot'),
        StringStruct('LegalCopyright', 'Copyright (C) 2026 WorgaNomoR'),
        StringStruct('OriginalFilename', 'ShikiUpdatesBot.exe'),
        StringStruct('ProductName', 'ShikiUpdatesBot'),
        StringStruct('ProductVersion', '{app_version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)""",
    encoding="utf-8",
)

datas = collect_data_files("pytrovich")
datas += copy_metadata("aiogram")
datas += copy_metadata("aiohttp")
datas += copy_metadata("pytrovich")

a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root), str(meta_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=["_build_info"],
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
    a.binaries,
    a.datas,
    [],
    name="ShikiUpdatesBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(root / "assets" / "ShikiUpdatesBot.ico"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(version_file),
)
