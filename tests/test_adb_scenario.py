"""adb 解析 / 场景校验 / 调度计算 的单元测试。"""
import time

from narci import adb
from narci import scenario as sc
from narci.scheduler import next_run_for


def test_parse_devices_list():
    sample = """List of devices attached
ABC123               device product:foo model:SM_G991B device:bar transport_id:1
192.168.1.5:5555     device product:baz model:Redmi_K40 transport_id:2
XYZ789               unauthorized transport_id:3
emulator-5554        offline transport_id:4

"""
    devs = adb.parse_devices_list(sample)
    assert len(devs) == 4
    assert devs[0]["serial"] == "ABC123" and devs[0]["state"] == "device"
    assert devs[0]["model"] == "SM G991B"
    assert devs[1]["serial"] == "192.168.1.5:5555" and devs[1]["model"] == "Redmi K40"
    assert devs[2]["state"] == "unauthorized"
    assert devs[3]["state"] == "offline"


def test_escape_input_text():
    assert adb._escape_input_text("hello world") == 'input text "hello%sworld"'
    assert adb._escape_input_text('a"b\\c$d`e') == 'input text "a\\"b\\\\c\\$d\\`e"'


def test_parse_bounds():
    assert adb._parse_bounds("[100,200][300,400]") == (200, 300)


def test_validate_steps():
    sc.validate_steps([{"type": "tap", "x_pct": 0.5, "y_pct": 0.5}, {"type": "wait", "ms": 500}])
    try:
        sc.validate_steps([{"type": "nope"}])
        assert False
    except sc.ScenarioError:
        pass
    sc.validate_steps([{"type": "loop", "count": 2, "steps": [{"type": "back"}]}])
    # v0.4.0 新步骤类型
    sc.validate_steps([
        {"type": "wait_text", "text": "设置", "timeout_ms": 5000, "interval_ms": 1000},
        {"type": "assert_text", "text": "WLAN", "expect": "exists"},
        {"type": "assert_text", "text": "蓝牙", "expect": "absent"},
        {"type": "rand_wait", "min_ms": 200, "max_ms": 800},
        {"type": "start_activity", "component": "com.example/.MainActivity"},
    ])
    deep = {"type": "back"}
    for _ in range(7):
        deep = {"type": "loop", "count": 1, "steps": [deep]}
    try:
        sc.validate_steps([deep])
        assert False
    except sc.ScenarioError:
        pass


def test_execute_assert_text(monkeypatch):
    import asyncio

    async def fake_find_text(serial, text, timeout=25):
        return (100, 200) if text == "存在" else None

    monkeypatch.setattr(sc.adb, "find_text", fake_find_text)
    assert asyncio.run(sc._execute_step(
        {"type": "assert_text", "text": "存在", "expect": "exists"}, "S", (0, 0), "t"))["met"]
    assert asyncio.run(sc._execute_step(
        {"type": "assert_text", "text": "不存在", "expect": "absent"}, "S", (0, 0), "t"))["met"]
    for step in ({"type": "assert_text", "text": "不存在", "expect": "exists"},
                 {"type": "assert_text", "text": "存在", "expect": "absent"}):
        try:
            asyncio.run(sc._execute_step(step, "S", (0, 0), "t"))
            assert False, "断言应失败"
        except sc.ScenarioError:
            pass


def test_execute_rand_wait(monkeypatch):
    import asyncio

    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(sc.asyncio, "sleep", fake_sleep)
    r = asyncio.run(sc._execute_step({"type": "rand_wait", "min_ms": 100, "max_ms": 200}, "S", (0, 0), "t"))
    assert 0.1 <= slept[0] <= 0.2
    assert 100 <= r["ms"] <= 200


def test_screen_awake_parses(monkeypatch):
    import asyncio

    async def fake_run_ok(args, timeout=20, device=None):
        return b"  mWakefulness=Asleep\r\n"

    monkeypatch.setattr(adb, "_run_ok", fake_run_ok)
    assert asyncio.run(adb.screen_awake("S")) is False

    async def fake_run_ok_awake(args, timeout=20, device=None):
        return b"  mWakefulness=Awake\r\n"

    monkeypatch.setattr(adb, "_run_ok", fake_run_ok_awake)
    assert asyncio.run(adb.screen_awake("S")) is True


def test_screen_awake_falls_back_without_grep(monkeypatch):
    """设备 shell 没有 grep / 无匹配时，退化为拉全量 dumpsys 本地解析。"""
    import asyncio

    seen = []

    async def fake_run_ok(args, timeout=20, device=None):
        seen.append(args[1])
        if "grep" in args[1]:
            raise adb.AdbError("grep: not found")
        return b"mWakefulness=Asleep"

    monkeypatch.setattr(adb, "_run_ok", fake_run_ok)
    assert asyncio.run(adb.screen_awake("S")) is False
    assert len(seen) == 2


def test_ensure_awake_wakes_when_asleep(monkeypatch):
    import asyncio

    adb._awake_cache.clear()
    woke = []

    async def fake_asleep(serial):
        return False

    async def fake_wake(serial):
        woke.append(serial)

    monkeypatch.setattr(adb, "screen_awake", fake_asleep)
    monkeypatch.setattr(adb, "wake_up", fake_wake)

    note = asyncio.run(adb.ensure_awake("S1"))
    assert note and "熄屏" in note
    assert woke == ["S1"]
    # TTL 内再次注入：不重复探测、不重复唤醒、不重复返回 note
    assert asyncio.run(adb.ensure_awake("S1")) is None
    assert woke == ["S1"]
    adb._awake_cache.clear()


def test_ensure_awake_passthrough_and_probe_failure(monkeypatch):
    import asyncio

    adb._awake_cache.clear()

    async def fake_awake(serial):
        return True

    monkeypatch.setattr(adb, "screen_awake", fake_awake)
    assert asyncio.run(adb.ensure_awake("S2")) is None  # 亮屏：直接放行

    adb._awake_cache.clear()

    async def fake_unknown(serial):
        return None  # 探测失败

    monkeypatch.setattr(adb, "screen_awake", fake_unknown)
    assert asyncio.run(adb.ensure_awake("S3")) is None  # 探测失败不阻塞、不报错
    assert not adb._awake_cache  # 且不留下误导性缓存
    adb._awake_cache.clear()


def test_coords_pct_vs_px():
    step = {"x_pct": 0.5, "y_pct": 0.25}
    assert sc._coords(step, (1080, 2400)) == (540, 600)
    step2 = {"x": 10, "y": 20}
    assert sc._coords(step2, (1080, 2400)) == (10, 20)


def test_next_run_interval():
    now = time.time()
    nxt = next_run_for("interval", "600", now)
    assert nxt - now == 600


def test_next_run_daily():
    import datetime
    now = time.time()
    nxt = next_run_for("daily", "23:59", now)
    d = datetime.datetime.fromtimestamp(nxt)
    assert d.hour == 23 and d.minute == 59 and nxt > now
