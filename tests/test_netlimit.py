"""弱网模拟：节流代理 + 设备代理设置 测试。"""
import asyncio
import io
import http.client

import pytest

from narci import adb, netlimit


# ---------- RateLimiter ----------

def test_rate_limiter_is_accurate():
    async def main():
        limiter = netlimit.RateLimiter(rate_kbps=128)   # 128 kbps = 16384 B/s
        t0 = asyncio.get_event_loop().time()
        for _ in range(10):
            await limiter.charge(1638)
        return asyncio.get_event_loop().time() - t0

    elapsed = asyncio.run(main())
    assert elapsed >= 0.9   # 10 * 1638 / 16384 ≈ 1.0s
    assert elapsed < 3.0


def test_rate_limiter_no_wait_when_idle():
    async def main():
        limiter = netlimit.RateLimiter(rate_kbps=100000)
        t0 = asyncio.get_event_loop().time()
        await limiter.charge(10)
        return asyncio.get_event_loop().time() - t0

    assert asyncio.run(main()) < 0.2


# ---------- 代理生命周期 ----------

def test_proxy_start_and_stop():
    async def main():
        proxy = netlimit.ThrottleProxy(rate_kbps=100000)
        port = await proxy.start()
        assert port > 0
        proxy.stop()
        return port

    asyncio.run(main())


def test_proxy_passes_http_request(monkeypatch):
    """代理应把普通 HTTP 请求转发到目标并限速返回。"""

    class FakeResponse:
        status = 200
        reason = "OK"

        def __init__(self):
            self._done = False

        def getheaders(self):
            return [("Content-Type", "text/plain")]

        def read(self, n):
            if self._done:
                return b""
            self._done = True
            return b"hello"

    class FakeConn:
        def __init__(self, *a, **k):
            self.requested = None

        def request(self, method, path, body=None, headers=None):
            self.requested = (method, path)

        def getresponse(self):
            return FakeResponse()

    async def main():
        monkeypatch.setattr(http.client, "HTTPConnection", FakeConn)
        proxy = netlimit.ThrottleProxy(rate_kbps=100000)
        port = await proxy.start()

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET http://example.com/path HTTP/1.1\r\nHost: example.com\r\n\r\n")
        await writer.drain()
        data = b""
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=3)
                if not chunk:
                    break
                data += chunk
                if b"hello" in data:
                    break
        except asyncio.TimeoutError:
            pass
        writer.close()
        proxy.stop()
        return data

    data = asyncio.run(main())
    assert b"200" in data
    assert b"hello" in data


def test_proxy_handles_bad_request():
    async def main():
        proxy = netlimit.ThrottleProxy(rate_kbps=100000)
        port = await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.close()
        proxy.stop()

    asyncio.run(main())   # 不应抛异常


# ---------- 设备代理设置 ----------

def test_set_and_clear_proxy(monkeypatch):
    calls = []

    async def fake_ok(args, timeout=20, device=None):
        calls.append(" ".join(args))
        return b""

    monkeypatch.setattr(adb, "_run_ok", fake_ok)
    asyncio.run(adb.set_global_proxy("S", "10.0.0.5", 8888))
    asyncio.run(adb.clear_global_proxy("S"))
    assert any("http_proxy 10.0.0.5:8888" in c for c in calls)
    assert any("http_proxy :0" in c for c in calls)


def test_get_global_proxy(monkeypatch):
    async def fake_shell(serial, cmd, timeout=30):
        return 0, "10.0.0.5:8888"

    monkeypatch.setattr(adb, "shell", fake_shell)
    assert asyncio.run(adb.get_global_proxy("S")) == "10.0.0.5:8888"
