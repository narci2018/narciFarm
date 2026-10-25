"""下载 scrcpy-server jar 到 assets/。

用法:
    py -3 scripts/fetch_scrcpy_server.py
可选环境变量:
    NARCI_SERVER_VERSION  版本号，默认 5.0.1
    NARCI_PROXY           代理，如 socks5h://127.0.0.1:10808
"""
import os
import sys
import urllib.request
from pathlib import Path

VERSION = os.environ.get("NARCI_SERVER_VERSION", "5.0.1")
PROXY = os.environ.get("NARCI_PROXY", "")
DEST = Path(__file__).resolve().parent.parent / "assets" / "scrcpy-server.jar"
URL = f"https://github.com/Genymobile/scrcpy/releases/download/v{VERSION}/scrcpy-server-v{VERSION}"


def main() -> int:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if DEST.exists() and DEST.stat().st_size > 100_000:
        print(f"已存在: {DEST} ({DEST.stat().st_size} 字节)")
        return 0
    print(f"下载 {URL}")
    if PROXY:
        handler = urllib.request.ProxyHandler({
            "http": PROXY.replace("socks5h://", "socks5://"),
            "https": PROXY.replace("socks5h://", "socks5://"),
        })
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    with opener.open(URL, timeout=120) as resp, open(DEST, "wb") as f:
        f.write(resp.read())
    print(f"完成: {DEST} ({DEST.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
