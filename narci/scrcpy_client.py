"""scrcpy 客户端：负责推 server、建隧道、解析视频流、发送注入消息。

每个在线设备的投屏会话对应一个 ScrcpyClient 实例，由 StreamHub 管理引用计数。
"""
import asyncio
import random
import socket
import struct
import time
from typing import Awaitable, Callable, Optional

from . import adb
from .config import (MAX_FPS, MAX_SIZE, REMOTE_JAR_PATH, SERVER_JAR,
                     SERVER_VERSION, VIDEO_BITRATE)
from .events import bus
from .scrcpy_protocol import (DEVICE_META_LEN, PacketHeader,
                              parse_device_message, parse_packet_header)

_open_server_jars: set[str] = set()  # 已推送过 jar 的设备 serial


class ScrcpyClientError(Exception):
    pass


async def _free_port() -> int:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def _connect_with_retry(port: int, *, expect_dummy: bool,
                              attempts: int = 25, delay: float = 0.4) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """连接 adb forward 端口。设备端 server 未就绪时 adb 会立即断开，需重试。

    expect_dummy: tunnel_forward 模式下 server 会在第一个 socket 上先写一个 0x00。
    """
    last_err: Optional[Exception] = None
    for _ in range(attempts):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            if expect_dummy:
                dummy = await asyncio.wait_for(reader.readexactly(1), timeout=3)
                if dummy != b"\x00":
                    raise ScrcpyClientError(f"握手字节异常: {dummy!r}")
            return reader, writer
        except (ConnectionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            last_err = e
            await asyncio.sleep(delay)
    raise ScrcpyClientError(f"连接 scrcpy 隧道失败(127.0.0.1:{port}): {last_err}")


class ScrcpyClient:
    """单设备 scrcpy 会话。回调（若设置）:

    on_session(width, height) / on_config(payload) / on_frame(pts, key_frame, payload)
    on_closed(reason)
    """

    def __init__(self, serial: str, *, max_size: int = MAX_SIZE,
                 video_bit_rate: int = VIDEO_BITRATE, max_fps: int = MAX_FPS,
                 control: bool = True) -> None:
        self.serial = serial
        self.max_size = max_size
        self.video_bit_rate = video_bit_rate
        self.max_fps = max_fps
        self.control_enabled = control

        self.device_name = ""
        self.width = 0
        self.height = 0
        self.frames = 0
        self.started_at = 0.0

        self.on_session: Optional[Callable[[int, int], Awaitable[None]]] = None
        self.on_config: Optional[Callable[[bytes], Awaitable[None]]] = None
        self.on_frame: Optional[Callable[[int, bool, bytes], Awaitable[None]]] = None
        self.on_closed: Optional[Callable[[str], Awaitable[None]]] = None

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._video: Optional[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = None
        self._control: Optional[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = None
        self._port = 0
        self._scid = 0
        self._tasks: list[asyncio.Task] = []
        self._send_lock = asyncio.Lock()
        self._closing = False
        # 标记 scrcpy server 是否已完全就绪（视频流开始发送数据）
        self._ready = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._video is not None and not self._closing

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self.running:
            return
        if not SERVER_JAR.exists():
            raise ScrcpyClientError(f"缺少 scrcpy-server: {SERVER_JAR}，请先运行 scripts/fetch_scrcpy_server.py")

        self._scid = random.getrandbits(31)
        self._port = await _free_port()
        socket_name = f"scrcpy_{self._scid:08x}"

        if self.serial not in _open_server_jars:
            await adb.push(self.serial, str(SERVER_JAR), REMOTE_JAR_PATH, timeout=60)
            _open_server_jars.add(self.serial)

        await adb.add_forward(self.serial, self._port, socket_name)

        server_args = (
            f"CLASSPATH={REMOTE_JAR_PATH} app_process / com.genymobile.scrcpy.Server {SERVER_VERSION} "
            f"tunnel_forward=true audio=false video=true control={'true' if self.control_enabled else 'false'} "
            f"scid={self._scid:x} log_level=warn "
            f"max_size={self.max_size} video_bit_rate={self.video_bit_rate} max_fps={self.max_fps} "
            f"cleanup=false power_on=false clipboard_autosync=false "
            f"send_dummy_byte=true send_device_meta=true send_stream_meta=true send_frame_meta=true"
        )
        self._proc = await asyncio.create_subprocess_exec(
            adb.ADB_PATH, "-s", self.serial, "shell", server_args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=adb.CREATE_NO_WINDOW,
        )
        self._tasks.append(asyncio.create_task(self._pump_server_logs()))

        try:
            self._video = await _connect_with_retry(self._port, expect_dummy=True)
            if self.control_enabled:
                # server 在 accept 视频 socket 后才 accept 控制 socket，紧随其后连接即可
                self._control = await _connect_with_retry(self._port, expect_dummy=False, attempts=15)
            meta = await asyncio.wait_for(self._video[0].readexactly(DEVICE_META_LEN), timeout=5)
            self.device_name = meta.rstrip(b"\x00").decode("utf-8", "replace")
            codec = await asyncio.wait_for(self._video[0].readexactly(4), timeout=5)
            if codec != b"h264" and codec != b"\x68\x32\x36\x34":
                bus.warn(f"[{self.serial}] 视频编码非 h264: {codec!r}")
        except Exception:
            await self.stop("启动失败")
            raise

        self.started_at = time.time()
        self._tasks.append(asyncio.create_task(self._read_video_loop()))
        if self._control:
            self._tasks.append(asyncio.create_task(self._read_control_loop()))

        # 提示服务端已开始推流（第一帧/配置到达时 _ready.set()）；
        # 不在此阻塞等待——若服务端慢则前端 WebSocket 一连上就会收数据，
        # 否则快速报错并降级，避免 Event.wait(timeout=...) 这类不兼容调用
        bus.info(f"[{self.serial}] 投屏会话已建立 (scid={self._scid:08x}, name={self.device_name!r})")

    async def stop(self, reason: str = "手动停止") -> None:
        if self._closing:
            return
        self._closing = True
        for pair in (self._video, self._control):
            if pair:
                try:
                    pair[1].close()
                except Exception:
                    pass
        self._video = self._control = None
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        for t in self._tasks:
            t.cancel()
        if self._port:
            await adb.remove_forward(self.serial, self._port)
        self._tasks.clear()
        bus.info(f"[{self.serial}] 投屏会话关闭: {reason}")
        if self.on_closed:
            try:
                await self.on_closed(reason)
            except Exception:
                pass

    async def _pump_server_logs(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                low = text.lower()
                if any(k in low for k in ("error", "exception", "could not inject", "denied", "security")):
                    bus.error(f"[{self.serial}][server] {text}  ← 若为注入被拒，多为厂商安全限制(如MIUI「USB调试(安全设置)」)")
                else:
                    bus.warn(f"[{self.serial}][server] {text}")
            rc = await self._proc.wait()
            if rc not in (0, -15, 130) and not self._closing:
                bus.error(f"[{self.serial}] scrcpy server 退出 rc={rc}")
                if self.on_closed and not self._closing:
                    await self.on_closed(f"server 退出 rc={rc}")
        except asyncio.CancelledError:
            pass

    # ---------- 视频流 ----------

    async def _read_video_loop(self) -> None:
        assert self._video
        reader = self._video[0]
        try:
            while True:
                header = await reader.readexactly(12)
                pkt: PacketHeader = parse_packet_header(header)
                if pkt.kind == "session":
                    self.width, self.height = pkt.width, pkt.height
                    if self.on_session:
                        await self.on_session(pkt.width, pkt.height)
                elif pkt.kind == "config":
                    payload = await reader.readexactly(pkt.size)
                    if self.on_config:
                        await self.on_config(payload)
                    # scrcpy server 已发送视频配置（SPS/PPS），表示服务端已完全就绪
                    self._ready.set()
                else:
                    payload = await reader.readexactly(pkt.size)
                    self.frames += 1
                    if self.on_frame:
                        await self.on_frame(pkt.pts, pkt.key_frame, payload)
                    # 第一帧到达也表示服务器已完全就绪
                    if self.frames == 1:
                        self._ready.set()
        except asyncio.IncompleteReadError:
            if not self._closing:
                await self.stop("视频流中断")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._closing:
                await self.stop(f"视频流异常: {e}")

    # ---------- 控制通道（设备回传读取） ----------

    async def _read_control_loop(self) -> None:
        assert self._control
        reader = self._control[0]
        try:
            while True:
                msg_type = (await reader.readexactly(1))[0]
                if msg_type == 0:      # 剪贴板
                    (n,) = struct.unpack(">I", await reader.readexactly(4))
                    text = (await reader.readexactly(n)).decode("utf-8", "replace")
                    bus.info(f"[{self.serial}] 设备剪贴板: {text[:80]!r}")
                elif msg_type == 1:    # ACK
                    await reader.readexactly(8)
                elif msg_type == 2:    # UHID 输出
                    await reader.readexactly(2)
                    (n,) = struct.unpack(">H", await reader.readexactly(2))
                    await reader.readexactly(n)
                else:
                    bus.warn(f"[{self.serial}] 未知设备消息类型 {msg_type}")
        except asyncio.IncompleteReadError:
            if not self._closing:
                await self.stop("控制通道中断")
        except asyncio.CancelledError:
            pass

    # ---------- 发送 ----------

    async def send_control(self, payload: bytes) -> None:
        if not self._control:
            raise ScrcpyClientError("控制通道未启用")
        async with self._send_lock:
            self._control[1].write(payload)
            await self._control[1].drain()

    def fps(self) -> float:
        if not self.started_at:
            return 0.0
        elapsed = time.time() - self.started_at
        return self.frames / elapsed if elapsed > 0 else 0.0


class StreamHub:
    """多设备投屏会话管理（引用计数 + 观看者扇出，支持同一设备多窗口观看）。"""

    def __init__(self) -> None:
        self._clients: dict[str, ScrcpyClient] = {}
        self._refs: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._subscribers: dict[str, set] = {}

    def _lock(self, serial: str) -> asyncio.Lock:
        if serial not in self._locks:
            self._locks[serial] = asyncio.Lock()
        return self._locks[serial]

    def get(self, serial: str) -> Optional[ScrcpyClient]:
        return self._clients.get(serial)

    def refs(self, serial: str) -> int:
        return self._refs.get(serial, 0)

    def subscribe(self, serial: str) -> asyncio.Queue:
        """注册一个观看者队列；客户端回调会把会话/帧数据扇出到所有队列。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=240)
        self._subscribers.setdefault(serial, set()).add(q)
        return q

    def unsubscribe(self, serial: str, q: asyncio.Queue) -> None:
        self._subscribers.get(serial, set()).discard(q)

    async def acquire(self, serial: str, **kwargs) -> ScrcpyClient:
        async with self._lock(serial):
            client = self._clients.get(serial)
            # 复用已有 client（无论 running 与否，尝试恢复连接）
            if client:
                if client.running:
                    self._refs[serial] += 1
                    return client
                # client 存在但未 running（如断线后重连），尝试 restart
                try:
                    await client.stop("恢复中")
                except Exception:
                    pass
                client = None
            client = ScrcpyClient(serial, **kwargs)

            def _fanout(item: tuple) -> None:
                for q in list(self._subscribers.get(serial, ())):
                    try:
                        q.put_nowait(item)
                    except asyncio.QueueFull:
                        pass  # 慢观看者丢帧，不影响其他观看者

            async def _on_session(w: int, h: int) -> None:
                _fanout(("json", {"type": "session", "width": w, "height": h}))

            async def _on_config(payload: bytes) -> None:
                _fanout(("bin", b"\x03" + payload))

            async def _on_frame(pts: int, key_frame: bool, payload: bytes) -> None:
                _fanout(("bin", b"\x01" + struct.pack(">BQ", 1 if key_frame else 0, pts) + payload))

            client.on_session = _on_session
            client.on_config = _on_config
            client.on_frame = _on_frame

            async def _closed(reason: str) -> None:
                async with self._lock(serial):
                    if self._clients.get(serial) is client:
                        self._clients.pop(serial, None)
                        self._refs.pop(serial, None)
                    for q in list(self._subscribers.pop(serial, ())):
                        try:
                            q.put_nowait(("json", {"type": "error", "msg": f"投屏会话结束: {reason}"}))
                        except asyncio.QueueFull:
                            pass

            client.on_closed = _closed
            await client.start()
            self._clients[serial] = client
            self._refs[serial] = 1
            return client

    async def release(self, serial: str) -> None:
        async with self._lock(serial):
            n = self._refs.get(serial, 1) - 1
            if n <= 0:
                client = self._clients.pop(serial, None)
                self._refs.pop(serial, None)
                if client:
                    await client.stop("无观看者")
            else:
                self._refs[serial] = n

    async def stop_all(self) -> None:
        for serial in list(self._clients):
            client = self._clients.get(serial)
            if client:
                await client.stop("服务关闭")
        self._clients.clear()
        self._refs.clear()
        self._subscribers.clear()


hub = StreamHub()
