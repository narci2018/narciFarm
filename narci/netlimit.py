"""弱网模拟：按速率限制设备流量的本地代理（真机无需 root）。

原理：把设备的全局 HTTP 代理指向本机代理端口，代理在转发时按目标速率节流。
- 普通 HTTP 请求：解析后转发，响应体按速率写出；
- HTTPS CONNECT：建立隧道后对隧道字节流按速率收发（不做中间人解密）。

支持带宽（rate_kbps）、固定延迟（latency_ms）与丢包率（loss_pct，仅 HTTP 层模拟）。
"""
import asyncio
import contextlib
import http.client
import random
import time
from typing import Optional
from urllib.parse import urlsplit


class RateLimiter:
    """串行速率限制：把通过的数据摊到目标速率上。"""

    def __init__(self, rate_kbps: float):
        self._bytes_per_sec = max(1.0, float(rate_kbps) * 1024.0 / 8.0)
        self._lock = asyncio.Lock()
        self._next_free = time.monotonic()

    async def charge(self, n: int) -> None:
        if n <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next_free < now:
                self._next_free = now
            self._next_free += n / self._bytes_per_sec
            wait = self._next_free - now
        if wait > 0:
            await asyncio.sleep(wait)


class ThrottleProxy:
    """极简节流代理。不是通用代理，仅用于弱网测试。"""

    def __init__(self, rate_kbps: float = 500.0, latency_ms: int = 0,
                 loss_pct: float = 0.0, host: str = "0.0.0.0"):
        self.rate_kbps = max(1.0, float(rate_kbps))
        self.latency_ms = max(0, int(latency_ms))
        self.loss_pct = max(0.0, min(95.0, float(loss_pct)))
        self.host = host
        self._server: Optional[asyncio.AbstractServer] = None
        self._limiter = RateLimiter(self.rate_kbps)
        self._conns: set = set()

    @property
    def port(self) -> int:
        if not self._server:
            return 0
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> int:
        """启动代理，返回监听端口。"""
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        return self.port

    def stop(self) -> None:
        if self._server:
            self._server.close()
            self._server = None
        for c in list(self._conns):
            with contextlib.suppress(Exception):
                c.close()
        self._conns.clear()

    # ---------- 连接处理 ----------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._conns.add(writer)
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not line:
                return
            if line.upper().startswith(b"CONNECT"):
                await self._handle_connect(reader, writer, line)
            else:
                await self._handle_http(reader, writer, line)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            self._conns.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle_connect(self, reader, writer, line: bytes) -> None:
        """HTTPS 隧道：不做解密，只对隧道字节流限速。"""
        parts = line.split()
        if len(parts) < 2:
            return
        target = parts[1].decode("latin-1", "replace")
        host, _, port_s = target.partition(":")
        port = int(port_s or 443)
        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except (OSError, asyncio.TimeoutError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(
            self._pump(reader, up_writer, down=False),
            self._pump(up_reader, writer, down=True),
            return_exceptions=True,
        )

    async def _pump(self, src, dst, down: bool) -> None:
        """在两个流之间搬运数据并按速率节流。"""
        try:
            while True:
                chunk = await src.read(16384)
                if not chunk:
                    break
                if self._drop():
                    continue
                if self.latency_ms and down:
                    await asyncio.sleep(self.latency_ms / 1000.0)
                await self._limiter.charge(len(chunk))
                dst.write(chunk)
                await dst.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                if hasattr(dst, "write_eof"):
                    dst.write_eof()

    def _drop(self) -> bool:
        return self.loss_pct > 0 and random.uniform(0, 100) < self.loss_pct

    async def _handle_http(self, reader, writer, line: bytes) -> None:
        """普通 HTTP：解析请求行与头，转发并限速返回。"""
        try:
            parts = line.split()
            if len(parts) < 2:
                return
            method = parts[0].decode("latin-1", "replace")
            url = parts[1].decode("latin-1", "replace")
            headers = []
            while True:
                h = await asyncio.wait_for(reader.readline(), timeout=15)
                if h in (b"\r\n", b"\n", b""):
                    break
                headers.append(h)
            parsed = urlsplit(url if "://" in url else "http://" + url)
            host = parsed.hostname or ""
            port = parsed.port or 443
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            if not host:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                return
            if self._drop():
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\n"
                             b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            conn = (http.client.HTTPSConnection(host, port, timeout=30)
                    if url.startswith("https") else
                    http.client.HTTPConnection(host, port, timeout=30))
            body = await self._read_body(reader, headers)
            if self.latency_ms:
                await asyncio.sleep(self.latency_ms / 1000.0)
            try:
                conn.request(method, path, body=body, headers=self._clean_headers(headers))
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n"
                             b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            out = [f"HTTP/1.1 {resp.status} {resp.reason}\r\n".encode("latin-1")]
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                out.append(f"{k}: {v}\r\n".encode("latin-1"))
            out.append(b"Connection: close\r\n\r\n")
            writer.write(b"".join(out))
            await writer.drain()
            while True:
                chunk = await asyncio.to_thread(resp.read, 65536)
                if not chunk:
                    break
                await self._limiter.charge(len(chunk))
                writer.write(chunk)
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass

    @staticmethod
    def _clean_headers(raw: list) -> dict:
        out = {}
        for h in raw:
            try:
                k, v = h.decode("latin-1").split(":", 1)
            except ValueError:
                continue
            k = k.strip()
            if k.lower() in ("proxy-connection", "proxy-authorization", "host", "connection"):
                continue
            out[k] = v.strip()
        return out

    @staticmethod
    async def _read_body(reader, headers: list) -> bytes:
        length = 0
        for h in headers:
            if h.lower().startswith(b"content-length:"):
                try:
                    length = int(h.split(b":", 1)[1].strip())
                except ValueError:
                    length = 0
        if length <= 0:
            return b""
        try:
            return await asyncio.wait_for(reader.readexactly(length), timeout=30)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return b""
