# -*- coding: utf-8 -*-
"""凌日光影棚 —— 桌面软件启动器。

以独立进程启动本地服务并打开软件界面，是打包成
Windows .exe / macOS .app / Linux AppImage 的入口。

用法：
  python -m webui.desktop                # 启动并自动打开界面
  python -m webui.desktop --no-browser   # 只启动服务（供浏览器手动访问）
  python -m webui.desktop --port 8899    # 指定端口

说明：
  - 引擎（core/）完全本地运行；本启动器不需要联网、不需要 Node。
  - 界面由 webui/frontend/dist 提供，随软件一起分发。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEFAULT_PORT = 8756
# 收到退出请求后最多再等这么久（秒）：既作为 uvicorn 优雅关停的上限，
# 也作为主线程等待服务线程的上限。超过就强制结束进程 —— 打包版是窗口子系统，
# 没有控制台，用户无法用 Ctrl+C 收场，只能去任务管理器杀进程。
SHUTDOWN_GRACE = 5.0


def _port_available(port: int) -> bool:
    """端口是否真的空闲。

    注意：**不要**设置 SO_REUSEADDR——在 Windows 上它的语义是
    「允许绑定已被占用的端口」，会让探测恒为成功，从而失去回退作用。
    这里用「bind + connect」双重确认。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    # bind 成功后再试连一次，避免某些平台的语义差异
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
        c.settimeout(0.3)
        if c.connect_ex(("127.0.0.1", port)) == 0:
            return False
    return True


def _free_port(preferred: int) -> int:
    """优先使用 preferred，被占用时向后找一个可用端口。"""
    for port in range(preferred, preferred + 40):
        if _port_available(port):
            return port
    raise RuntimeError("找不到可用端口（已尝试 %d~%d）" % (preferred, preferred + 39))


def _wait_ready(url: str, timeout: float = 30.0) -> bool:
    import urllib.error
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _log_path() -> str:
    """启动日志路径（数据目录下，失败则退回临时目录）。"""
    try:
        from core.paths import data_dir
        d = data_dir()
    except Exception:
        d = os.path.join(os.path.expanduser("~"), ".horizon_light_studio")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        import tempfile
        d = tempfile.gettempdir()
    return os.path.join(d, "startup.log")


def _make_stdio_safe() -> None:
    """把 None 的 stdout/stderr 接到日志文件。

    打包版是 PyInstaller 的 **窗口子系统**（console=False）：在没有控制台时
    `sys.stdout` / `sys.stderr` 为 **None**，任何 `print()` 或
    `sys.stderr.write()` 都会抛 AttributeError。这会让「双击没反应」——
    而且 uvicorn 默认的日志处理器也挂在 stderr 上，服务线程会在首次输出时
    直接死掉。因此这里先把两个流兜住，并返回日志文件句柄。
    """
    p = _log_path()
    try:
        sink = open(p, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def _log(message: str) -> None:
    """同时写 stderr 与启动日志（两者都不可用时静默，绝不抛异常）。"""
    try:
        sys.stderr.write(message + "\n")
    except Exception:
        pass
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def _fail(title: str, message: str) -> None:
    """失败时给出**可见**反馈：日志 + 原生对话框（无控制台环境下）。"""
    _log("错误：" + message)
    detail = ("%s\n\n详细日志：%s" % (message, _log_path()))
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, detail, title, 0x10)
            return
        except Exception:
            pass
    elif sys.platform == "darwin":
        # macOS .app 没有控制台，用 osascript 弹原生对话框（不依赖 tkinter）
        try:
            import subprocess
            subprocess.run(["osascript", "-e",
                            'display alert "%s" message "%s" as critical'
                            % (title, detail.replace('"', "'"))],
                           timeout=15, check=False)
            return
        except Exception:
            pass
    try:
        sys.stderr.write(detail + "\n")
    except Exception:
        pass


def main(argv=None) -> int:
    # 必须在任何 print/日志之前：窗口子系统下 stdout/stderr 可能为 None
    _make_stdio_safe()
    try:
        return _run(argv)
    except SystemExit:
        raise
    except BaseException:
        import traceback
        tb = traceback.format_exc()
        _log("未捕获异常：\n" + tb)
        _fail("凌日光影棚 启动失败",
              "发生未预期的错误，程序已退出。\n\n%s" % tb.strip().splitlines()[-1])
        return 1


def _run(argv=None) -> int:
    ap = argparse.ArgumentParser(description="凌日光影棚 桌面软件")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true",
                    help="不自动打开界面（仅启动本地服务）")
    args = ap.parse_args(argv)

    try:
        from webui.api import app
    except Exception as e:                     # pragma: no cover - 打包环境诊断
        _fail("凌日光影棚 启动失败", "无法加载引擎接口：%s" % e)
        return 2

    import uvicorn

    try:
        port = _free_port(args.port)
    except RuntimeError as e:
        _fail("凌日光影棚 启动失败", str(e))
        return 4
    url = "http://%s:%d/" % (args.host, port)

    # uvicorn 默认把 StreamHandler 挂在 stderr 上，而窗口子系统下 stderr 可能
    # 是 None → 服务线程首次输出即崩溃。因此关掉默认日志配置（log_config=None），
    # 诊断信息由本模块的 _log 写入 startup.log。
    config = uvicorn.Config(app, host=args.host, port=port,
                            log_level="warning", access_log=False,
                            log_config=None,
                            # uvicorn 默认无限期等待未完成的连接/任务；一旦有请求
                            # 卡住，server.run() 就永不返回，下面的强制退出也就
                            # 到不了。给优雅关停一个上限。
                            timeout_graceful_shutdown=SHUTDOWN_GRACE)
    server = uvicorn.Server(config)
    # 暴露给 /api/shutdown（打包版没有控制台窗口，必须有界面内的退出入口）
    app.state.server = server
    serve_error = []

    def _serve():
        try:
            server.run()
        except Exception as e:                 # pragma: no cover
            import traceback
            serve_error.append(e)
            _log("服务异常退出：%s\n%s" % (e, traceback.format_exc()))

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    if not _wait_ready(url + "api/state"):
        reason = serve_error[0] if serve_error else "就绪探测超时（30 秒）"
        _fail("凌日光影棚 启动失败",
              "本地服务未能启动：%s\n\n常见原因：端口被占用或依赖缺失"
              "（fastapi / uvicorn）。" % reason)
        return 3

    _log("已启动：%s" % url)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        # 显式上限：不能只写 `while t.is_alive(): t.join(0.5)`。
        # 若 uvicorn 卡在关停流程（等未完成连接/任务）而不返回，该循环永不结束，
        # 后面的 os._exit 永远到不了 —— 症状正是「HTTP 已断但进程仍活」。
        # 因此一旦收到退出请求，就只再等 SHUTDOWN_GRACE 秒。
        deadline = None
        while t.is_alive():
            t.join(0.5)
            if getattr(server, "should_exit", False):
                if deadline is None:
                    deadline = time.time() + SHUTDOWN_GRACE
                elif time.time() > deadline:
                    _log("服务未在 %.0f 秒内结束，强制退出" % SHUTDOWN_GRACE)
                    break
    except KeyboardInterrupt:
        _log("正在关闭…")
    finally:
        server.should_exit = True
        t.join(SHUTDOWN_GRACE)
    # 强制结束进程。走到这里 HTTP 已不再响应，但**解释器退出**仍可能卡住：
    # API 层的 asyncio.to_thread 用的是 ThreadPoolExecutor，其工作线程是非守护
    # 线程，而 concurrent.futures 注册了 atexit 钩子（_python_exit）去**无限期**
    # join 它们 —— 只要某个工作线程停在阻塞的系统调用里（ONNX 会话加载、
    # ffmpeg 子进程读管道），join 就永不返回，表现为「接口已 202 退出、
    # 任务管理器里进程还在」。该竞态偶发（同一负载复跑多次才遇到一次）。
    # 本地桌面应用此时已无任何需要清理的状态（导出文件都显式 close 写入、
    # 日志每次开关文件、缓存按需落盘），因此强制结束是唯一可靠的做法。
    _log("已退出")
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
