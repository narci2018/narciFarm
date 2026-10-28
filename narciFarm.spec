# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件 exe，内置前端资源、scrcpy-server.jar 与（可选）adb。

构建: pyinstaller narciFarm.spec --noconfirm
产物: dist/narciFarm.exe
"""
import os
import sys

# Anaconda 的 Python 依赖 conda 管理的 DLL（Library/bin），PyInstaller 不一定收集。
# _ctypes 等扩展在冻结后报 "DLL load failed" 就是缺这些；python.org 官方版无此问题，此处为空操作。
extra_binaries = []
if sys.platform == "win32":
    import glob as _glob
    lib_bin = os.path.join(sys.prefix, "Library", "bin")
    if os.path.isdir(lib_bin):
        for pattern in ("ffi*.dll", "libffi*.dll", "zlib*.dll", "liblzma*.dll",
                        "libbz2*.dll", "sqlite3*.dll", "libcrypto*.dll", "libssl*.dll",
                        "tiff*.dll", "jpeg*.dll", "libjpeg*.dll", "libpng*.dll",
                        "libwebp*.dll", "libdeflate*.dll", "jbig*.dll", "lcms*.dll",
                        "openjp*.dll", "libsharpyuv*.dll"):
            for p in _glob.glob(os.path.join(lib_bin, pattern)):
                extra_binaries.append((p, "."))

datas = [
    ("static", "static"),
    ("assets", "assets"),
]
# CI 构建前会把 platform-tools 的 adb 三件套放进 build_stage/adb（本地可手动放置）
adb_stage = os.path.join("build_stage", "adb")
if os.path.isdir(adb_stage):
    datas.append((adb_stage, os.path.join("tools", "adb")))

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
    "pystray._win32",
]

excludes = [
    "tkinter", "matplotlib", "pandas", "scipy",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "jedi",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=extra_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="narciFarm",
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
)
