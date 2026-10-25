"""进程内事件总线：日志环形缓冲 + 文件落盘 + WebSocket 订阅分发。"""
import asyncio
import threading
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator

from .config import DATA_DIR

_RING_SIZE = 800
_LOG_FILE = DATA_DIR / "narciFarm.log"
_LOG_ROTATE_BYTES = 5 * 1024 * 1024
_file_lock = threading.Lock()


def _rotate_if_needed() -> None:
    try:
        if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _LOG_ROTATE_BYTES:
            old = _LOG_FILE.with_suffix(".log.old")
            if old.exists():
                old.unlink()
            _LOG_FILE.rename(old)
    except OSError:
        pass


def _append_file(line: str) -> None:
    with _file_lock:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


_rotate_if_needed()


class EventBus:
    def __init__(self) -> None:
        self._ring: deque[dict] = deque(maxlen=_RING_SIZE)
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, level: str, msg: str, **extra) -> dict:
        event = {"ts": time.time(), "level": level, "msg": msg, **extra}
        self._ring.append(event)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["ts"]))
        _append_file(f"[{stamp}] [{level}] {msg}")
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        print(f"[{level}] {msg}", flush=True)
        return event

    def info(self, msg: str, **extra) -> dict:
        return self.publish("info", msg, **extra)

    def warn(self, msg: str, **extra) -> dict:
        return self.publish("warn", msg, **extra)

    def error(self, msg: str, **extra) -> dict:
        return self.publish("error", msg, **extra)

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def recent(self, limit: int = 200) -> list[dict]:
        return list(self._ring)[-limit:]

    async def stream(self, q: asyncio.Queue) -> AsyncIterator[dict]:
        while True:
            yield await q.get()


bus = EventBus()
