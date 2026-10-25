"""narciFarm 桌面入口（PyInstaller 打包目标；开发时也可直接运行）。

用法:
    python main.py [--host 127.0.0.1] [--port 8555] [--no-browser]
"""
import argparse
import threading
import webbrowser

import uvicorn

import narci.config as cfg


def display_url(host: str, port: int) -> str:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{shown}:{port}"


def main() -> None:
    parser = argparse.ArgumentParser(description="narciFarm 设备农场管理")
    parser.add_argument("--host", default=cfg.HOST)
    parser.add_argument("--port", type=int, default=cfg.PORT)
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    cfg.HOST = args.host
    cfg.PORT = args.port

    url = display_url(args.host, args.port)
    print(f"narciFarm 启动中: {url} （数据目录与日志见 exe 同级目录）", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    from narci.main import app
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    app.state.server = server  # 供 /api/shutdown 优雅停机
    server.run()


if __name__ == "__main__":
    main()
