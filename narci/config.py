"""narciFarm 全局配置。所有路径/端口/码率均可通过环境变量覆盖。"""
import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("NARCI_DATA_DIR", BASE_DIR / "data"))
CAPTURES_DIR = Path(os.environ.get("NARCI_CAPTURES_DIR", BASE_DIR / "captures"))
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "narci.db"

ASSETS_DIR = BASE_DIR / "assets"
SERVER_JAR = Path(os.environ.get("NARCI_SERVER_JAR", ASSETS_DIR / "scrcpy-server.jar"))
SERVER_VERSION = os.environ.get("NARCI_SERVER_VERSION", "5.0.1")
REMOTE_JAR_PATH = os.environ.get("NARCI_REMOTE_JAR", "/data/local/tmp/narci_scrcpy_server.jar")

ADB_PATH = (
    os.environ.get("NARCI_ADB")
    or shutil.which("adb")
    or r"C:\tools\platform-tools\adb.exe"
)

HOST = os.environ.get("NARCI_HOST", "127.0.0.1")
PORT = int(os.environ.get("NARCI_PORT", "8555"))

# 默认投屏参数
MAX_SIZE = int(os.environ.get("NARCI_MAX_SIZE", "1280"))
VIDEO_BITRATE = int(os.environ.get("NARCI_BITRATE", "8000000"))
MAX_FPS = int(os.environ.get("NARCI_FPS", "30"))

# 设备轮询间隔（秒）
DEVICE_REFRESH_INTERVAL = float(os.environ.get("NARCI_REFRESH_INTERVAL", "5"))
# 总览页快照刷新间隔（秒，由前端读取）
SNAPSHOT_INTERVAL = float(os.environ.get("NARCI_SNAPSHOT_INTERVAL", "4"))

DEVICE_POLL_CONCURRENCY = int(os.environ.get("NARCI_POLL_CONCURRENCY", "8"))
LAN_SCAN_CONCURRENCY = int(os.environ.get("NARCI_LAN_SCAN_CONCURRENCY", "64"))

for _d in (DATA_DIR, CAPTURES_DIR, UPLOADS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
