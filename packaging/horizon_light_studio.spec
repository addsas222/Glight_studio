# -*- mode: python ; coding: utf-8 -*-
"""凌日光影棚 —— PyInstaller 打包配置（Windows / macOS / Linux 通用）。

用法（在仓库根目录执行）：
  Windows : packaging\\build_windows.bat
  macOS   : packaging/build_macos.sh
  Linux   : packaging/build_linux.sh
或手动：
  pyinstaller packaging/horizon_light_studio.spec --noconfirm

打包对象是 **桌面启动器 webui/desktop.py**（WebUI 外壳）：
  - 启动本地服务（webui/api.py）并自动打开界面
  - 界面静态资源来自 webui/frontend/dist（随包分发，**运行期不需要 Node**）

平台产物：
  Windows : dist/HorizonLightStudio.exe                （单文件绿色版 .exe）
  macOS   : dist/凌日光影棚.app                     （再由脚本封装为 .dmg）
  Linux   : dist/HorizonLightStudio/                    （再由脚本封装为 .AppImage）
另附命令行渲染器 dist/hls_cli[.exe]（图片批处理，可选）。

依赖：ffmpeg 由 imageio-ffmpeg 提供（pip 包内自带静态二进制），
PyInstaller 会自动收集；视频功能因此无需系统安装 ffmpeg。
"""
import os
import sys

ROOT = os.path.dirname(SPECPATH)          # SPECPATH 由 PyInstaller 注入
ICONS = os.path.join(SPECPATH, "icons")

APP_NAME = "HorizonLightStudio"
APP_DISPLAY = "凌日光影棚"

FRONTEND_DIST = os.path.join(ROOT, "webui", "frontend", "dist")

# 随包分发的数据：文档 + 本地服务代码 + 前端构建产物
datas = [
    (os.path.join(ROOT, "README.md"), "."),
    (os.path.join(ROOT, "docs"), "docs"),
    (os.path.join(ROOT, "server"), "server"),
]
if os.path.isdir(FRONTEND_DIST):
    datas.append((FRONTEND_DIST, os.path.join("webui", "frontend", "dist")))

CORE_HIDDEN = [
    "core", "core.ai_backend", "core.builtin_depth", "core.cache",
    "core.degrade", "core.pipeline", "core.presets", "core.reference",
    "core.render", "core.types", "core.video", "core.paths",
    # 这些模块只通过函数内的惰性 import 触达（try/except → 503 降级），
    # 必须显式列出，否则打包后对应功能会静默 503。
    "core.autolight", "core.theme", "core.plugins", "core.workflow",
    "core.cues", "core.harvest", "core.strokes",
]
APP_HIDDEN = CORE_HIDDEN + [
    "webui", "webui.api", "webui.desktop",
    # WebUI 运行时
    "fastapi", "starlette", "uvicorn", "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.protocols",
    "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "anyio", "pydantic",
    # 视频：随包静态 ffmpeg
    "imageio_ffmpeg",
]

if sys.platform == "win32":
    icon_file = os.path.join(ICONS, "appicon.ico")
    version_file = os.path.join(SPECPATH, "version_info.txt")
elif sys.platform == "darwin":
    # BUNDLE 需要 .icns；build_macos.sh 会先用 sips+iconutil 生成它。
    # 尚未生成时退回 PNG（PyInstaller 可自动转换，但质量略差）。
    _icns = os.path.join(ICONS, "appicon.icns")
    icon_file = _icns if os.path.exists(_icns) else os.path.join(ICONS, "appicon.png")
    version_file = None
else:
    icon_file = os.path.join(ICONS, "appicon.png")
    version_file = None

# ---------------------------------------------------------------- 桌面软件


# ffmpeg 二进制：显式收集，不依赖 PyInstaller 版本自带的 hook。
# （imageio-ffmpeg 的 hook 在各版本间行为不一致，漏收会让视频功能在用户机上直接失败。）
_binaries = []
try:
    import imageio_ffmpeg
    _ff = imageio_ffmpeg.get_ffmpeg_exe()
    if _ff and os.path.isfile(_ff):
        _binaries.append((_ff, "."))
        print("[spec] 已纳入 ffmpeg 二进制:", _ff)
except Exception as _e:                      # pragma: no cover
    print("[spec] 警告：未能定位 ffmpeg（视频功能将不可用）:", _e)


def gui_app(*, onefile: bool):
    a = Analysis(
        [os.path.join(ROOT, "webui", "desktop.py")],
        pathex=[ROOT],
        binaries=list(_binaries),
        datas=datas,
        hiddenimports=APP_HIDDEN,
        hookspath=[],
        runtime_hooks=[],
        excludes=["tkinter", "matplotlib", "scipy", "IPython", "pytest"],
        noarchive=False,
    )
    pyz = PYZ(a.pure)
    kw = dict(
        name=APP_NAME,
        console=False,
        icon=icon_file if os.path.exists(icon_file) else None,
        version=version_file if version_file and os.path.exists(version_file) else None,
    )
    if onefile:
        return EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [], **kw)
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **kw)
    return exe, COLLECT(exe, a.binaries, a.zipfiles, a.datas, name=APP_NAME)


# macOS 需要 .app 目录才能封装 .dmg；Windows 交付单文件 .exe；Linux 用 onedir 组装 AppDir。
if sys.platform == "darwin":
    _exe, _coll = gui_app(onefile=False)
    app = BUNDLE(
        _coll,
        name=APP_DISPLAY + ".app",
        icon=icon_file if os.path.exists(icon_file) else None,
        bundle_identifier="studio.horizonlight.app",
        info_plist={
            "CFBundleName": APP_DISPLAY,
            "CFBundleDisplayName": APP_DISPLAY,
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSRequiresAquaSystemAppearance": False,
        },
    )
elif sys.platform == "win32":
    gui_app(onefile=True)
else:
    gui_app(onefile=False)

# ---------------------------------------------------------------- 命令行渲染器
# 图片批处理用；不含 WebUI，体积更小。

cli_a = Analysis(
    [os.path.join(ROOT, "hls_cli.py")],
    pathex=[ROOT],
    datas=[],
    hiddenimports=CORE_HIDDEN,
    excludes=["tkinter", "fastapi", "uvicorn", "starlette", "pydantic",
              "imageio_ffmpeg", "matplotlib", "scipy"],
    noarchive=False,
)
cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(
    cli_pyz, cli_a.scripts, cli_a.binaries, cli_a.zipfiles, cli_a.datas, [],
    name="hls_cli",
    console=True,
    icon=icon_file if (icon_file.endswith(".ico") and os.path.exists(icon_file)) else None,
)
