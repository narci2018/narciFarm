"""进程内事件总线：日志环形缓冲 + WebSocket 订阅分发。"""
import asyncio
import time
from collections import deque
from typing import AsyncIterator

_RING_SIZE = 800


class EventBus:
    def __init__(self) -> None:
        self._ring: deque[dict] = deque(maxlen=_RING_SIZE)
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, level: str, msg: str, **extra) -> dict:
        event = {"ts": time.time(), "level": level, "msg": msg, **extra}
        self._ring.append(event)
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
