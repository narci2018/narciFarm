"""scrcpy v5.0.1 协议编解码（纯函数，依据 refs/scrcpy 中对应版本源码实现）。

来源对照：
- 帧头/会话包:  server/.../device/Streamer.java
- 控制消息:     server/.../control/ControlMessageReader.java + ControlMessage.java
- 设备回传:     server/.../control/DeviceMessageWriter.java
- 压力/滚轮定点: server/.../util/Binary.java
"""
import struct
from dataclasses import dataclass
from typing import Optional

# ---- 常量（与 ControlMessage.java 一致）----
TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_INJECT_SCROLL_EVENT = 3
TYPE_BACK_OR_SCREEN_ON = 4
TYPE_EXPAND_NOTIFICATION_PANEL = 5
TYPE_EXPAND_SETTINGS_PANEL = 6
TYPE_COLLAPSE_PANELS = 7
TYPE_GET_CLIPBOARD = 8
TYPE_SET_CLIPBOARD = 9
TYPE_SET_DISPLAY_POWER = 10
TYPE_ROTATE_DEVICE = 11
TYPE_START_APP = 16
TYPE_RESET_VIDEO = 17

# MotionEvent action
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

# KeyEvent action
KEY_ACTION_DOWN = 0
KEY_ACTION_UP = 1

POINTER_ID_MOUSE = -1

# 视频流包头标志（Streamer.java）
FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY_FRAME = 1 << 61

CODEC_H264 = 0x68323634  # "h264"
CODEC_H265 = 0x68323635
CODEC_AV1 = 0x00617631

DEVICE_META_LEN = 64

INJECT_TEXT_MAX_BYTES = 300


@dataclass
class PacketHeader:
    kind: str            # "session" | "config" | "frame"
    width: int = 0       # session
    height: int = 0      # session
    pts: int = 0         # frame（微秒）
    key_frame: bool = False
    size: int = 0        # config / frame 的载荷长度


def parse_packet_header(buf: bytes) -> PacketHeader:
    """解析视频流上的 12 字节包头（会话包与媒体包共用判别位：最高位）。"""
    if len(buf) != 12:
        raise ValueError(f"包头必须为 12 字节, 实际 {len(buf)}")
    value, size = struct.unpack(">QI", buf)
    if value & FLAG_SESSION:
        # 会话包: [u32 flags(含最高位与 client-resized 标志)][u32 width][u32 height]
        width, height = struct.unpack(">ii", buf[4:12])
        return PacketHeader(kind="session", width=width, height=height)
    if value & FLAG_CONFIG:
        return PacketHeader(kind="config", size=size)
    pts = value & ~(FLAG_SESSION | FLAG_CONFIG | FLAG_KEY_FRAME)
    return PacketHeader(kind="frame", pts=pts, key_frame=bool(value & FLAG_KEY_FRAME), size=size)


# ---- 控制消息编码（client -> device）----

def _u16_fixed_point(value: float) -> int:
    """Binary.u16FixedPointToFloat 的逆变换：0..1 -> 0..0xFFFF。"""
    v = max(0.0, min(1.0, value))
    if v >= 1.0:
        return 0xFFFF
    return int(v * 65536) & 0xFFFF


def _i16_fixed_point_scroll(value: float) -> int:
    """ControlMessageReader 对滚轮先 i16FixedPointToFloat 再 *16，此处取逆。

    即 encode(v) 满足: (decode(enc)) * 16 == v, decode(x) = x / 32768。
    """
    scaled = value / 16.0
    if scaled >= 1.0:
        return 0x7FFF
    if scaled <= -1.0:
        return -0x8000
    return int(scaled * 32768)


def encode_inject_keycode(action: int, keycode: int, repeat: int = 0, meta_state: int = 0) -> bytes:
    return struct.pack(">BBiii", TYPE_INJECT_KEYCODE, action, keycode, repeat, meta_state)


def encode_inject_text(text: str) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) > INJECT_TEXT_MAX_BYTES:
        raise ValueError(f"INJECT_TEXT 最大 {INJECT_TEXT_MAX_BYTES} 字节, 实际 {len(raw)}")
    return struct.pack(">BI", TYPE_INJECT_TEXT, len(raw)) + raw


def encode_inject_touch(action: int, x: int, y: int, screen_w: int, screen_h: int,
                        pressure: float = 1.0, pointer_id: int = POINTER_ID_MOUSE,
                        action_button: int = 0, buttons: int = 0) -> bytes:
    """触控注入。x/y 为 screen_w×screen_h 坐标系下的像素坐标（服务端做映射）。

    压力为 u16 定点（Java 端 readShort 后按无符号解释，线格式用 H 打包）。
    """
    return struct.pack(
        ">BBqiiHHHii",
        TYPE_INJECT_TOUCH_EVENT, action, pointer_id, int(x), int(y),
        int(screen_w) & 0xFFFF, int(screen_h) & 0xFFFF,
        _u16_fixed_point(pressure), action_button, buttons,
    )


def encode_inject_scroll(x: int, y: int, screen_w: int, screen_h: int,
                         h_scroll: float = 0.0, v_scroll: float = 0.0, buttons: int = 0) -> bytes:
    """滚轮注入。h_scroll/v_scroll 实际范围 [-16, 16]，正值向上滚动。"""
    return struct.pack(
        ">BiiHHhhi",
        TYPE_INJECT_SCROLL_EVENT, int(x), int(y),
        int(screen_w) & 0xFFFF, int(screen_h) & 0xFFFF,
        _i16_fixed_point_scroll(h_scroll), _i16_fixed_point_scroll(v_scroll), buttons,
    )


def encode_back_or_screen_on(action: int = ACTION_DOWN) -> bytes:
    return struct.pack(">BB", TYPE_BACK_OR_SCREEN_ON, action)


def encode_expand_notification_panel() -> bytes:
    return struct.pack(">B", TYPE_EXPAND_NOTIFICATION_PANEL)


def encode_collapse_panels() -> bytes:
    return struct.pack(">B", TYPE_COLLAPSE_PANELS)


def encode_rotate_device() -> bytes:
    return struct.pack(">B", TYPE_ROTATE_DEVICE)


def encode_get_clipboard(copy_key: int = 0) -> bytes:
    return struct.pack(">BB", TYPE_GET_CLIPBOARD, copy_key)


def encode_set_clipboard(text: str, paste: bool = False, sequence: int = 0) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">BqB", TYPE_SET_CLIPBOARD, sequence, 1 if paste else 0) + \
        struct.pack(">I", len(raw)) + raw


def encode_set_display_power(on: bool) -> bytes:
    return struct.pack(">BB", TYPE_SET_DISPLAY_POWER, 1 if on else 0)


# ---- 设备回传（device -> client）----

def parse_device_message(buf: bytes) -> tuple[int, Optional[str], int]:
    """返回 (type, clipboard_text 或 None, sequence)。类型: 0=剪贴板, 1=ACK, 2=UHID 输出。"""
    if not buf:
        raise ValueError("空设备消息")
    msg_type = buf[0]
    text, sequence = None, 0
    if msg_type == 0:  # TYPE_CLIPBOARD: u32 len + utf8
        (n,) = struct.unpack_from(">I", buf, 1)
        text = buf[5:5 + n].decode("utf-8", "replace")
    elif msg_type == 1:  # TYPE_ACK_CLIPBOARD: i64
        (sequence,) = struct.unpack_from(">q", buf, 1)
    return msg_type, text, sequence


def parse_avcc_nals(payload: bytes) -> list[bytes]:
    """把 MediaCodec 输出（AVCC: 4 字节大端长度前缀 + NAL）拆成 NAL 列表。"""
    nals = []
    offset = 0
    while offset + 4 <= len(payload):
        (n,) = struct.unpack_from(">I", payload, offset)
        offset += 4
        if n == 0 or offset + n > len(payload):
            break
        nals.append(payload[offset:offset + n])
        offset += n
    return nals
