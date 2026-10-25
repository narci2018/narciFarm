"""协议层单元测试：帧头解析、控制消息编码与 scrcpy v5.0.1 源码逐字节对齐。"""
import struct

from narci import scrcpy_protocol as sp


def test_parse_session_packet():
    # 按文档构造: [u32 flags(最高位=1, 末位=client_resized)][u32 width][u32 height]
    buf = struct.pack(">III", 0x80000000 | 0, 1080, 2400)
    pkt = sp.parse_packet_header(buf)
    assert pkt.kind == "session"
    assert (pkt.width, pkt.height) == (1080, 2400)


def test_parse_config_packet():
    # Streamer.writeFrameMeta: config 时 ptsAndFlags = PACKET_FLAG_CONFIG(1<<62), size
    buf = struct.pack(">QI", 1 << 62, 25)
    pkt = sp.parse_packet_header(buf)
    assert pkt.kind == "config"
    assert pkt.size == 25


def test_parse_frame_packet():
    pts = 123456
    buf = struct.pack(">QI", pts | (1 << 61), 1024)
    pkt = sp.parse_packet_header(buf)
    assert pkt.kind == "frame"
    assert pkt.pts == pts
    assert pkt.key_frame is True
    assert pkt.size == 1024
    # 非关键帧
    buf2 = struct.pack(">QI", pts, 64)
    pkt2 = sp.parse_packet_header(buf2)
    assert pkt2.key_frame is False and pkt2.pts == pts


def test_encode_inject_keycode_layout():
    # ControlMessageReader.parseInjectKeycode: action u8, keycode i32, repeat i32, metaState i32
    buf = sp.encode_inject_keycode(sp.KEY_ACTION_DOWN, 4, 0, 0)
    assert buf[0] == 0
    action, keycode, repeat, meta = struct.unpack(">Biii", buf[1:])
    assert (action, keycode, repeat, meta) == (0, 4, 0, 0)
    assert len(buf) == 14


def test_encode_inject_touch_layout():
    # parseInjectTouchEvent: action u8, pointerId i64, Position(x i32,y i32,w u16,h u16),
    # pressure i16, actionButton i32, buttons i32
    buf = sp.encode_inject_touch(sp.ACTION_DOWN, 100, 200, 1080, 2400, pressure=1.0)
    assert buf[0] == 2
    action, pointer_id = struct.unpack_from(">Bq", buf, 1)
    x, y = struct.unpack_from(">ii", buf, 10)
    w, h = struct.unpack_from(">HH", buf, 18)
    (pressure_raw,) = struct.unpack_from(">H", buf, 22)
    action_button, buttons = struct.unpack_from(">ii", buf, 24)
    assert (action, pointer_id, x, y, w, h, action_button, buttons) == (0, -1, 100, 200, 1080, 2400, 0, 0)
    assert pressure_raw == 0xFFFF
    assert len(buf) == 32


def test_encode_inject_scroll_layout():
    # parseInjectScrollEvent: Position(12B), h i16, v i16, buttons i32
    # Reader 侧: hScroll = i16FixedPointToFloat(short) * 16 → 发送 -1.0 应编码为 -1/16*32768 = -2048
    buf = sp.encode_inject_scroll(10, 20, 1080, 2400, 0.0, -1.0)
    assert buf[0] == 3
    x, y = struct.unpack_from(">ii", buf, 1)
    w, h = struct.unpack_from(">HH", buf, 9)
    hs, vs = struct.unpack_from(">hh", buf, 13)
    (buttons,) = struct.unpack_from(">i", buf, 17)
    assert (x, y, w, h, buttons) == (10, 20, 1080, 2400, 0)
    assert vs == -2048
    assert len(buf) == 21


def test_encode_set_clipboard_layout():
    # parseSetClipboard: sequence i64, paste u8, string(u32 len + utf8)
    buf = sp.encode_set_clipboard("你好", paste=True, sequence=7)
    assert buf[0] == 9
    seq, paste = struct.unpack_from(">qB", buf, 1)
    (n,) = struct.unpack_from(">I", buf, 10)
    assert seq == 7 and paste == 1 and n == 6
    assert buf[14:].decode("utf-8") == "你好"


def test_encode_inject_text_layout():
    buf = sp.encode_inject_text("hi")
    assert buf[0] == 1
    (n,) = struct.unpack_from(">I", buf, 1)
    assert n == 2 and buf[5:] == b"hi"


def test_inject_text_too_long():
    try:
        sp.encode_inject_text("a" * 301)
        assert False, "应抛出超长异常"
    except ValueError:
        pass


def test_parse_device_message_clipboard():
    # DeviceMessageWriter: type u8 + u32 len + text
    raw = b"\x00" + struct.pack(">I", 5) + "hello".encode()
    msg_type, text, seq = sp.parse_device_message(raw)
    assert msg_type == 0 and text == "hello" and seq == 0


def test_parse_device_message_ack():
    raw = b"\x01" + struct.pack(">q", 42)
    msg_type, text, seq = sp.parse_device_message(raw)
    assert msg_type == 1 and text is None and seq == 42


def test_parse_avcc_nals():
    nal1, nal2 = b"\x67abcd", b"\x68ef"
    payload = struct.pack(">I", len(nal1)) + nal1 + struct.pack(">I", len(nal2)) + nal2
    nals = sp.parse_avcc_nals(payload)
    assert nals == [nal1, nal2]
