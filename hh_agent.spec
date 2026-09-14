# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for HH Agent Launcher."""
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = []
datas += collect_data_files("playwright")

binaries = []
binaries += collect_dynamic_libs("playwright")

# UI templates
datas += [("ui/app.html", "ui"), ("ui/wizard.html", "ui"), ("ui/run.html", "ui"), ("ui/loader.html", "ui"), ("ui/theme.js", "ui")]

# Ядро hh_applicant_tool в сборку НЕ входит: оно скачивается при первом
# запуске. PyInstaller его импорты не видит, поэтому всё, что ядру нужно,
# перечислено здесь вручную. Список сверен с реальными import'ами ядра 1.8.28.
core_runtime = [
    "PIL",
    "PIL.Image",
    "argparse",
    "ast",
    "asyncio",
    "base64",
    "bottle",
    "collections",
    "contextlib",
    "csv",
    "ctypes",
    "dataclasses",
    "datetime",
    "email",
    "email.message",
    "email.mime.multipart",
    "email.mime.text",
    "email.utils",
    "enum",
    "functools",
    "gzip",
    "hashlib",
    "html",
    "http",
    "http.client",
    "http.cookiejar",
    "importlib",
    "importlib.metadata",
    "itertools",
    "json",
    "logging.handlers",
    "pkgutil",
    "platform",
    "playwright",
    "playwright.__main__",
    "playwright.async_api",
    "playwright.sync_api",
    "playwright._impl._driver",
    "pprint",
    "prettytable",
    "requests",
    "runpy",
    "secrets",
    "signal",
    "smtplib",
    "snowballstemmer",
    "sqlite3",
    "struct",
    "subprocess",
    "tomllib",
    "urllib.parse",
    "urllib3",
    "uuid",
    "webview",
    "zlib",
]

# Наши собственные модули — они импортируются по имени из ядра/патчей.
app_modules = [
    "app_api",
    "app_main",
    "core_manager",
    "engine",
    "hh_patch",
    "letters",
    "profiles",
    "run_monitor",
    "stopwords",
    "telebot",
    "telegram_bot",
]

hiddenimports = core_runtime + app_modules

if sys.platform == "win32":
    hiddenimports += [
        "clr_loader",
        "pythonnet",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
    ]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
else:
    hiddenimports += ["webview.platforms.gtk", "webview.platforms.qt"]

excludes = [
    "tkinter", 
    "unittest", 
    "pydoc_data",
    "hh_applicant_tool"
]

a = Analysis(
    ["app_main.py"],
    pathex=["."],
    binaries=binaries,
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
    icon='icon.ico' if sys.platform == 'win32' else None,
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
