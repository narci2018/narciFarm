"""断言增强：匹配模式 / 数值比较 / 软断言。"""
import asyncio

import pytest

from narci import scenario


# ---------- 匹配模式 ----------

@pytest.mark.parametrize("actual,expected,mode,want", [
    ("设置", "设置", "exact", True),
    ("设置", "设", "exact", False),
    ("设置页面", "设置", "contains", True),
    ("设置页面", "页面", "contains", True),
    ("设置页面", "取消", "contains", False),
    ("版本 1.2.3", r"版本 \d+\.\d+", "regex", True),
    ("版本 abc", r"版本 \d+", "regex", False),
    ("确定按钮", "确定", "startswith", True),
    ("确定按钮", "按钮", "endswith", True),
])
def test_text_matches_modes(actual, expected, mode, want):
    assert scenario._text_matches(actual, expected, mode) is want


def test_text_matches_rejects_bad_regex():
    with pytest.raises(scenario.ScenarioError):
        scenario._text_matches("x", "([", "regex")


def test_text_matches_rejects_unknown_mode():
    with pytest.raises(scenario.ScenarioError):
        scenario._text_matches("x", "y", "nope")


# ---------- 数值比较 ----------

@pytest.mark.parametrize("actual,expected,op,want", [
    ("5", "5", "eq", True),
    ("5", "6", "eq", False),
    ("5", "6", "ne", True),
    ("10", "6", "gt", True),
    ("10", "10", "gte", True),
    ("3", "6", "lt", True),
    ("3", "3", "lte", True),
    ("abc", "abc", "eq", True),
    ("abc", "abd", "eq", False),
    ("abc123", r"\d+", "regex", True),
    ("hello world", "world", "contains", True),
])
def test_compare_operators(actual, expected, op, want):
    ok, _ = scenario._compare(actual, expected, op)
    assert ok is want


def test_compare_non_numeric_ordering_rejected():
    ok, detail = scenario._compare("abc", "abd", "gt")
    assert ok is False
    assert "非数值" in detail


def test_compare_rejects_unknown_op():
    with pytest.raises(scenario.ScenarioError):
        scenario._compare(1, 2, "~~")


# ---------- 软断言抛错类型 ----------

def test_raise_assert_hard_by_default():
    with pytest.raises(scenario.ScenarioError) as ei:
        scenario._raise_assert({}, "失败")
    assert not isinstance(ei.value, scenario.SoftAssertionError)


def test_raise_assert_soft_when_flag_set():
    with pytest.raises(scenario.SoftAssertionError):
        scenario._raise_assert({"soft": True}, "失败")
    with pytest.raises(scenario.SoftAssertionError):
        scenario._raise_assert({"soft": "1"}, "失败")


# ---------- 步骤执行 ----------

def _run(monkeypatch, steps):
    async def fake_size(serial):
        return (1080, 2400)

    monkeypatch.setattr(scenario.adb, "get_screen_size", fake_size)
    run_id = "t"
    state = {"status": "running", "done": 0, "current": 0}
    scenario._runs[run_id] = {"run_id": run_id, "devices": {}}
    abort = asyncio.Event()

    async def runner():
        await scenario._run_step_list(steps, "S", (1080, 2400), run_id, abort, state, {})

    asyncio.run(runner())
    scenario._runs.pop(run_id, None)
    return state


def test_assert_value_passes(monkeypatch):
    state = _run(monkeypatch, [{"type": "assert_value", "actual": "10", "expected": "6", "op": "gt"}])
    assert state["results"][0]["met"] is True


def test_assert_value_hard_failure_aborts(monkeypatch):
    with pytest.raises(scenario.ScenarioError):
        _run(monkeypatch, [
            {"type": "assert_value", "actual": "3", "expected": "6", "op": "gt"},
            {"type": "wait", "ms": 0},
        ])


def test_assert_value_soft_failure_continues(monkeypatch):
    state = _run(monkeypatch, [
        {"type": "assert_value", "actual": "3", "expected": "6", "op": "gt", "soft": True},
        {"type": "wait", "ms": 0},
    ])
    assert state["done"] == 2
    rec = state["results"][0]
    assert rec["soft_fail"] is True
    assert rec["skipped"] is False
    assert "不成立" in rec["error"]


def test_assert_text_regex_mode(monkeypatch):
    async def fake_dump(serial, timeout=25):
        return [
            {"text": "版本 1.2.3", "content_desc": "", "center": (10, 20), "id": "", "class": ""},
            {"text": "其他", "content_desc": "", "center": (30, 40), "id": "", "class": ""},
        ]

    monkeypatch.setattr(scenario.adb, "dump_hierarchy", fake_dump)
    state = _run(monkeypatch, [
        {"type": "assert_text", "text": r"版本 \d+\.\d+", "match": "regex"},
    ])
    assert state["results"][0]["met"] is True
    assert state["results"][0]["match"] == "regex"


def test_assert_text_soft_absent(monkeypatch):
    async def fake_dump(serial, timeout=25):
        return [{"text": "已存在", "content_desc": "", "center": (1, 2), "id": "", "class": ""}]

    monkeypatch.setattr(scenario.adb, "dump_hierarchy", fake_dump)
    state = _run(monkeypatch, [
        {"type": "assert_text", "text": "已存在", "expect": "absent", "soft": True},
        {"type": "wait", "ms": 0},
    ])
    assert state["done"] == 2
    assert state["results"][0]["soft_fail"] is True
