"""AI 工作群 · 桌面版入口。

它做的事很少，只负责把「浏览器里那套」包成一个双击即用的程序：

1. 找一个空闲的本地端口（不写死 8801，避免和已开的服务撞车）；
2. 在后台线程里起 uvicorn（复用 group.app:app，界面与逻辑一行不改）；
3. 等端口真正可连之后打开界面，按可靠性依次尝试：
   a. Chromium 的 `--app` 模式 → 无地址栏的独立窗口（**默认，最兼容**）；
   b. pywebview 原生窗口 → 需 .NET 6+ 运行时，多数机器没有；
   c. 系统默认浏览器 → 保底方案。
4. 主线程留着等退出；按 Ctrl+C 即结束。

诊断信息都会打到控制台，方便「双击后没反应」时排查——
这正是 PyInstaller 打包最常见的翻车方式（失败后静默退出、什么都没有）。

用法：
    python desktop.py                     # 正常启动（应用窗口）
    python desktop.py --browser           # 强制用系统默认浏览器
    python desktop.py --native-window     # 强制尝试 pywebview 原生窗口
    python desktop.py --no-window         # 只起服务不开界面（自动化验证）
    python desktop.py --port 8899         # 指定端口
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 冻结后模块在包内，无需再插 sys.path；源码运行时补上项目根
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def find_free_port(preferred: int = 0) -> int:
    """找一个可用端口。preferred=0 表示交给系统随机分配。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", preferred))
        except OSError:
            s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_ready(port: int, timeout: float = 30.0) -> bool:
    """轮询直到端口可连（或超时）。比 sleep 固定秒数可靠。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def start_server(port: int) -> threading.Thread:
    """后台线程起 uvicorn。"""
    import uvicorn

    from group.app import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def run() -> None:
        try:
            server.run()
        except Exception as exc:  # noqa: BLE001 - 桌面端绝不允许静默退出
            print(f"[desktop] 服务启动失败：{type(exc).__name__}: {exc}", flush=True)

    t = threading.Thread(target=run, name="uvicorn", daemon=True)
    t.start()
    return t


def _find_chromium() -> str | None:
    """找到可用的 Chromium 内核浏览器（Edge / Chrome）。

    为什么用它而不是 pywebview：pywebview 在 Windows 上依赖 pythonnet 3.x + .NET 6+
    运行时，而多数机器只装了 .NET Framework 4.x，实测直接报
    「Failed to resolve Python.Runtime.Loader.Initialize」。
    用浏览器自带的 --app 模式开窗口零依赖、任何 Windows 都能跑，
    出来的就是一个没有地址栏的独立窗口，观感与原生程序一致。
    """
    import os

    candidates = [
        # 常见安装位置
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    # Edge 的 EdgeCore 目录（部分系统的 Edge 装在这里）与注册表登记
    import glob

    for pattern in (
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\EdgeCore\*\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]  # 版本号最大者

    try:
        import winreg

        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    path, _ = winreg.QueryValueEx(key, "")
                    if os.path.isfile(path):
                        return path
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return None


def open_app_window(url: str, title: str = "AI 工作群 · 多模型协作") -> bool:
    """用 Chromium 的 --app 模式开一个无地址栏窗口。成功返回 True。"""
    import subprocess
    import tempfile

    exe = _find_chromium()
    if not exe:
        return False

    # 独立 user-data-dir：避免和用户日常浏览器抢会话，也避免「已存在实例」导致窗口不弹。
    # 必须用绝对路径——相对路径会落到项目目录里，污染交付物。
    profile_dir = os.path.join(tempfile.gettempdir(), "ai-group-window-profile")
    os.makedirs(profile_dir, exist_ok=True)
    cmd = [
        exe,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--window-size=1280,860",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
    ]
    try:
        print(f"[desktop] 用应用窗口打开（{os.path.basename(exe)}）", flush=True)
        # 不等待：窗口由用户关闭，服务继续跑
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[desktop] 应用窗口启动失败：{type(exc).__name__}: {exc}", flush=True)
        return False


def open_pywebview_window(url: str) -> bool:
    """可选：用 pywebview 开真·原生窗口（需要 .NET 6+ 运行时）。"""
    try:
        import webview  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(f"[desktop] pywebview 不可用：{exc}", flush=True)
        return False

    try:
        webview.create_window("AI 工作群 · 多模型协作", url, width=1360, height=880, min_size=(900, 600))
        webview.start()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[desktop] pywebview 窗口启动失败：{type(exc).__name__}: {exc}", flush=True)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 工作群 桌面版")
    parser.add_argument("--port", type=int, default=0, help="监听端口（0=自动选择空闲端口）")
    parser.add_argument("--no-window", action="store_true", help="只起服务、不开界面（自动化验证用）")
    parser.add_argument("--browser", action="store_true", help="强制用系统默认浏览器打开")
    parser.add_argument(
        "--native-window",
        action="store_true",
        help="强制尝试 pywebview 原生窗口（需要 .NET 6+ 运行时，多数机器没有）。",
    )
    args = parser.parse_args()

    port = find_free_port(args.port)
    url = f"http://127.0.0.1:{port}/"

    # 配置与数据位置先打出来：路径问题是打包后最常见的事故来源
    try:
        from group.config import CONFIG_DIR, PROJECT_ROOT, RESOURCE_ROOT, WEB_DIR

        print(f"[desktop] 数据目录  : {PROJECT_ROOT}", flush=True)
        print(f"[desktop] 资源目录  : {RESOURCE_ROOT}", flush=True)
        print(f"[desktop] 配置目录  : {CONFIG_DIR}  (存在={CONFIG_DIR.is_dir()})", flush=True)
        print(f"[desktop] 静态资源  : {WEB_DIR}  (存在={WEB_DIR.is_dir()})", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[desktop] 载入配置失败：{type(exc).__name__}: {exc}", flush=True)
        return 2

    print(f"[desktop] 启动服务：{url}", flush=True)
    start_server(port)

    if not wait_until_ready(port):
        print("[desktop] 服务在 30 秒内没有就绪，退出。", flush=True)
        return 3
    print("[desktop] 服务就绪。", flush=True)

    if args.no_window:
        print("[desktop] --no-window：服务已在运行，按 Ctrl+C 结束。", flush=True)
    elif args.native_window and open_pywebview_window(url):
        print("[desktop] pywebview 窗口已关闭，退出。", flush=True)
        return 0
    elif not args.browser and not args.native_window and open_app_window(url):
        # 应用窗口：独立窗口、无地址栏，观感接近原生程序；窗口由用户自行关闭
        pass
    else:
        print(f"[desktop] 用系统默认浏览器打开：{url}", flush=True)
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] 打开浏览器失败（请手动访问上面的地址）：{exc}", flush=True)

    print("[desktop] 服务运行中，按 Ctrl+C 结束。", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[desktop] 已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
