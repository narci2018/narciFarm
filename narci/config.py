"""narciFarm 全局配置。所有路径/端口/码率均可通过环境变量覆盖。

支持 PyInstaller 冻结运行：
- 只读资源（static/assets）在 _MEIPASS 解包目录；
- 可写数据（data/captures）放在 exe 同级目录，避免写进临时解包目录。
"""
import os
import shutil
import sys
from pathlib import Path


def _resource_base() -> Path:
    """只读资源根：开发时 = 项目根；冻结后 = 解包目录(_MEIPASS)。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def _writable_base() -> Path:
    """可写数据根：冻结后 = exe 所在目录；开发时 = 项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _resource_base()
WRITABLE_DIR = _writable_base()

DATA_DIR = Path(os.environ.get("NARCI_DATA_DIR", WRITABLE_DIR / "data"))
CAPTURES_DIR = Path(os.environ.get("NARCI_CAPTURES_DIR", WRITABLE_DIR / "captures"))
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "narci.db"

STATIC_DIR = BASE_DIR / "static"
ASSETS_DIR = BASE_DIR / "assets"
SERVER_JAR = Path(os.environ.get("NARCI_SERVER_JAR", ASSETS_DIR / "scrcpy-server.jar"))
SERVER_VERSION = os.environ.get("NARCI_SERVER_VERSION", "5.0.1")
REMOTE_JAR_PATH = os.environ.get("NARCI_REMOTE_JAR", "/data/local/tmp/narci_scrcpy_server.jar")


def _find_adb() -> str:
    env = os.environ.get("NARCI_ADB")
    if env:
        return env
    bundled = BASE_DIR / "tools" / "adb" / ("adb.exe" if os.name == "nt" else "adb")
    if bundled.exists():
        return str(bundled)
    found = shutil.which("adb")
    if found:
        return found
    return r"C:\tools\platform-tools\adb.exe"


ADB_PATH = _find_adb()

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

# 使用分析 / 会话回放（PostHog，可选）：项目 key 由前端 /api/settings 取用。
# 留空 = 不启用（前端不加载 SDK、不发任何请求）；key 是公开的写入令牌，不是机密。
POSTHOG_KEY = os.environ.get("NARCI_POSTHOG_KEY", "").strip()
POSTHOG_HOST = os.environ.get("NARCI_POSTHOG_HOST", "https://us.i.posthog.com").strip()

for _d in (DATA_DIR, CAPTURES_DIR, UPLOADS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
