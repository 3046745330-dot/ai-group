# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 ai-group 打成双击即用的桌面程序。

设计要点（都是打包后真会踩的坑）：
1. **onedir 而非 onefile**：onefile 每次启动都要把几十 MB 解包到临时目录，
   启动慢且更容易被杀软误报；onedir 启动快、便于排查。
2. **web/ 与 config/ 作为数据文件**：
   - web/  是只读资源，解包到 _MEIPASS 下由 app.py 挂载静态目录；
   - config/ 首次运行时会被 config.py 复制一份到 exe 同级，方便用户改模型配置。
3. **uvicorn 必须显式声明隐藏导入**：它用字符串动态导入
   loops / protocols / lifespan 等实现，PyInstaller 静态分析看不到，
   漏了就会「exe 起来后立刻退出」且几乎不报错——这是本项目最容易翻车的地方。
4. **排除无关大包**：当前 venv 是 campus-market 的，里面还有 sqlalchemy/pandas 等，
   不排除的话包体会白胖几十 MB。
"""

from pathlib import Path

project_root = Path(SPECPATH)          # noqa: F821 - PyInstaller 注入
app_name = "ai-group-desktop"

# 需要随包分发的数据：源目录 -> 包内目标目录
datas = [
    (str(project_root / "web"), "web"),
    (str(project_root / "config"), "config"),
]

# uvicorn 的动态导入（缺一不可）
hidden_imports = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "websockets",
    "websockets.legacy",
    "wsproto",
    "h11",
    "anyio",
    "anyio._backends._asyncio",
    "starlette",
    "fastapi",
    "pydantic",
    "yaml",
    "openai",
    "httpx",
    "httpcore",
    # 本项目自身模块
    "group",
    "group.app",
    "group.config",
    "group.engine",
    "group.llm",
    "group.ag2_adapter",
]

# 与桌面端无关、且体积可观的包（venv 是复用的，必须显式排除）
excludes = [
    "numpy", "pandas", "matplotlib", "scipy", "PIL", "Pillow",
    "sqlalchemy", "aiosqlite", "alembic", "passlib", "bcrypt",
    "pytest", "IPython", "notebook", "tkinter", "PyQt5", "PySide2",
    "docx", "pptx", "openpyxl", "xlsxwriter", "lxml",
    "pyinstaller", "setuptools", "pip",
    # pywebview 及其 .NET 依赖：Windows 上需要 .NET 6+ 运行时才能用，
    # 多数机器只有 .NET Framework 4.x，实测报 Loader.Initialize 失败。
    # 默认走 Chromium 的 --app 模式开窗口（零依赖），故这里一并排除以免包体虚胖。
    "webview", "pythonnet", "clr_loader", "clr", "proxy_tools", "bottle",
]

a = Analysis(                                    # noqa: F821
    [str(project_root / "desktop.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)                                # noqa: F821

exe = EXE(                                       # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX 压缩极易触发杀软误报，关掉
    console=True,            # 保留控制台：打包失败/配置错误时用户能看到原因
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(                                  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=app_name,
)
