"""narciFarm 桌面入口（PyInstaller 打包目标；开发时也可直接运行）。

无控制台形态：托盘图标常驻（打开管理台 / 关闭服务），日志写 data/narciFarm.log，
启动失败弹系统对话框，端口被占用提示"已在运行"。

用法:
    python main.py [--host 127.0.0.1] [--port 8555] [--no-browser] [--no-tray]
"""
import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser

import narci.config as cfg
from narci.events import bus

_shutdown_watchdog_started = False


def display_url(host: str, port: int) -> str:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{shown}:{port}"


def _fatal_box(msg: str) -> None:
    """无控制台模式下，致命错误用系统对话框呈现（用户关闭后进程继续退出流程）。"""
    print(f"[fatal] {msg}", file=sys.__stdout__ or sys.stderr)
    try:
        import ctypes
        done = threading.Event()

        def _show() -> None:
            try:
                ctypes.windll.user32.MessageBoxW(
                    0, f"{msg}\n\n详细日志: {cfg.DATA_DIR / 'narciFarm.log'}",
                    "narciFarm 启动失败", 0x10)
            except Exception:
                pass
            done.set()

        threading.Thread(target=_show, daemon=True, name="narci-fatalbox").start()
        done.wait(timeout=60)
    except Exception:
        pass


def _setup_file_logging() -> None:
    """uvicorn 自身错误也落入文件（无控制台时 stderr 不可见）。"""
    try:
        logging.basicConfig(
            filename=str(cfg.DATA_DIR / "narciFarm.log"),
            level=logging.WARNING,
            format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        )
    except Exception:
        pass


def _port_available(host: str, port: int) -> bool:
    probe = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((probe, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _make_tray(url: str, server_holder: dict):
    """构建托盘图标。依赖缺失或初始化失败时静默降级（返回 None）。"""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as e:
        bus.warn(f"托盘不可用（{e}），请通过网页「关闭服务」按钮退出")
        return None

    img = Image.new("RGB", (64, 64), (23, 28, 35))
    draw = ImageDraw.Draw(img)
    draw.ellipse([10, 10, 54, 54], fill=(77, 163, 255))
    draw.ellipse([26, 26, 38, 38], fill=(23, 28, 35))

    def open_ui(icon=None, item=None):
        webbrowser.open(url)

def quit_app(icon=None, item=None):
    server = server_holder.get("server")
    bus.info(f"托盘请求退出 (server={'已挂接' if server is not None else '缺失!'})")
    if server is not None:
        server.should_exit = True
    # 【关键新增】立即停止托盘图标，防止“服务停了图标还在”
    if icon is not None:
        icon.stop()
    from narci.main import start_shutdown_watchdog
    start_shutdown_watchdog()

    menu = pystray.Menu(
        pystray.MenuItem("打开管理台", open_ui, default=True),
        pystray.MenuItem("关闭服务", quit_app),
    )
    return pystray.Icon("narciFarm", img, "narciFarm", menu)


def main() -> None:
    parser = argparse.ArgumentParser(description="narciFarm 设备农场管理")
    parser.add_argument("--host", default=cfg.HOST)
    parser.add_argument("--port", type=int, default=cfg.PORT)
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘图标")
    args = parser.parse_args()

    cfg.HOST = args.host
    cfg.PORT = args.port

    if not _port_available(args.host, args.port):
        _fatal_box(f"端口 {args.port} 已被占用——narciFarm 可能已在运行，请查看系统托盘。")
        sys.exit(2)

    url = display_url(args.host, args.port)
    _setup_file_logging()

    server_holder: dict = {}

    tray = None
    if not args.no_tray:
        tray = _make_tray(url, server_holder)
        if tray is not None:
            threading.Thread(target=tray.run, daemon=True,
                             name="narci-tray").start()

    print(f"narciFarm 启动中: {url}", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    from narci.main import app
    import uvicorn
    config = uvicorn.Config(app, host=args.host, port=args.port,
                            log_level="warning", log_config=None)
    server = uvicorn.Server(config)
    server_holder["server"] = server
    app.state.server = server  # 供 /api/shutdown 优雅停机

    try:
        server.run()
    except Exception as e:  # noqa: BLE001 - 无控制台模式必须兜底呈现
        _fatal_box(f"服务异常退出: {type(e).__name__}: {e}")
        raise
    finally:
        bus.info("server.run 已返回，停止托盘图标")
        if tray is not None:
            ts = threading.Thread(target=tray.stop, daemon=True, name="narci-tray-stop")
            ts.start()
            ts.join(timeout=3)
        bus.info("进程即将退出")


if __name__ == "__main__":
    main()
