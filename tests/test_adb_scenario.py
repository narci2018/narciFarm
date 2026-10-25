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
    deep = {"type": "back"}
    for _ in range(7):
        deep = {"type": "loop", "count": 1, "steps": [deep]}
    try:
        sc.validate_steps([deep])
        assert False
    except sc.ScenarioError:
        pass


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
