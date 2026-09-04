# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""Воспроизводимая Windows one-file сборка с идентификаторами релиза из CI."""

import os
import re
import sys
import tempfile
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    copy_metadata,
)

root = Path(SPECPATH)
sys.path.insert(0, str(root))

from project_meta import PROJECT_REPOSITORY, PROJECT_VERSION  # noqa: E402


def atomic_write_text(path: Path, content: str) -> None:
    """Атомарно опубликовать генерируемый текстовый файл сборки."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


meta_dir = root / "build" / "pyinstaller-meta"
meta_dir.mkdir(parents=True, exist_ok=True)

app_version = os.environ.get("APP_VERSION", f"{PROJECT_VERSION}-dev").strip()
app_repository = os.environ.get("APP_REPOSITORY", PROJECT_REPOSITORY).strip()
app_server_url = os.environ.get("APP_SERVER_URL", "https://github.com").strip()
app_api_url = os.environ.get("APP_API_URL", "https://api.github.com").strip()

atomic_write_text(
    meta_dir / "_build_info.py",
    "\n".join([
        f"APP_VERSION = {app_version!r}",
        f"APP_REPOSITORY = {app_repository!r}",
        f"APP_SERVER_URL = {app_server_url!r}",
        f"APP_API_URL = {app_api_url!r}",
        "",
    ]),
)

match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-dev)?", app_version)
parts = tuple(int(value) for value in match.groups()) if match else (0, 0, 0)
version_file = meta_dir / "windows-version.txt"
atomic_write_text(
    version_file,
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
)

datas = collect_data_files("pytrovich")
datas += copy_metadata("aiogram")
datas += copy_metadata("aiohttp")
datas += copy_metadata("pytrovich")
datas.append((str(root / "assets" / "info-preview.png"), "assets"))
datas.append((str(root / "examples" / "facts.json"), "examples"))

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
