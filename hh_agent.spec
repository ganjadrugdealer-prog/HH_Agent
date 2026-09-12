# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for HH Agent Launcher."""
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files("playwright")

# UI templates
datas += [("ui/app.html", "ui"), ("ui/wizard.html", "ui"), ("ui/run.html", "ui"), ("ui/loader.html", "ui"), ("ui/theme.js", "ui")]

hiddenimports = [
    "snowballstemmer",
    "webview",
    "webview.platforms.edgechromium",
    "clr_loader",
    "bottle",
    "prettytable",
    "PIL",
    "PIL.Image",
    "smtplib",
    "email",
    "email.message",
    "email.utils",
    "email.mime.text",
    "email.mime.multipart",
    "sqlite3",
    "html",
    "logging.handlers",
    "csv",
    "ctypes",
    "runpy",
    "secrets",
    "tomllib",
    "urllib.parse",
    "ast",
    "asyncio",
    "app_api",
    "app_main",
    "engine",
    "letters",
    "hh_patch",
    "run_monitor",
    "stopwords",
    "profiles",
    "requests",
    "core_manager",
    "telebot",
    "telegram_bot"
]

excludes = [
    "tkinter", 
    "unittest", 
    "pydoc_data",
    "hh_applicant_tool"
]

a = Analysis(
    ["app_main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name="HH-Agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    icon='icon.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='HH-Agent.app',
        icon='icon.icns',
        bundle_identifier='com.hhagent.app',
        info_plist={
            'NSHighResolutionCapable': 'True',
            'LSBackgroundOnly': 'False',
        }
    )
