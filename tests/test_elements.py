"""控件树 / 多维元素选择器 单元测试。"""
import asyncio
import xml.etree.ElementTree as ET

from narci import adb

SAMPLE_XML = (
    '<hierarchy>'
    '<node text="设置" resource-id="com.x:id/title" class="android.widget.TextView" '
    'content-desc="" package="com.x" bounds="[0,0][200,80]" clickable="true" enabled="true"/>'
    '<node text="" resource-id="com.x:id/btn_ok" class="android.widget.Button" '
    'content-desc="确定" package="com.x" bounds="[200,100][400,180]" clickable="true"/>'
    '<node text="取消" resource-id="com.x:id/btn_cancel" class="android.widget.Button" '
    'content-desc="" package="com.x" bounds="[420,100][620,180]" clickable="true"/>'
    '</hierarchy>'
)


def _fake_dump(root):
    async def _dump(serial, timeout=25):
        return root
    return _dump


def test_parse_selector_defaults():
    kind, value, contains, index = adb.parse_selector({"kind": "id", "value": "com.x:id/btn"})
    assert (kind, value, contains, index) == ("id", "com.x:id/btn", False, 0)


def test_parse_selector_fuzzy_and_index():
    kind, value, contains, index = adb.parse_selector(
        {"kind": "text", "value": "设置", "contains": True, "index": 2})
    assert (kind, value, contains, index) == ("text", "设置", True, 2)


def test_parse_selector_legacy_text_field():
    kind, value, _, _ = adb.parse_selector({"kind": "desc", "text": "确定"})
    assert (kind, value) == ("desc", "确定")


def test_parse_selector_rejects_empty():
    try:
        adb.parse_selector({"kind": "text", "value": "  "})
        assert False, "空 value 应被拒绝"
    except adb.AdbError:
        pass


def test_dump_hierarchy_returns_nodes(monkeypatch):
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(adb, "_dump_xml", _fake_dump(root))
    nodes = asyncio.run(adb.dump_hierarchy("S"))
    assert len(nodes) == 3
    first = nodes[0]
    assert first["text"] == "设置"
    assert first["id"] == "com.x:id/title"
    assert first["center"] == (100, 40)
    assert first["clickable"] is True


def test_find_by_text_exact_and_contains(monkeypatch):
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(adb, "_dump_xml", _fake_dump(root))
    assert len(asyncio.run(adb.find_elements("S", "text", "设置"))) == 1
    assert len(asyncio.run(adb.find_elements("S", "text", "设", contains=True))) == 1


def test_find_by_id_and_desc(monkeypatch):
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(adb, "_dump_xml", _fake_dump(root))
    assert len(asyncio.run(adb.find_elements("S", "id", "com.x:id/btn_ok"))) == 1
    assert len(asyncio.run(adb.find_elements("S", "desc", "确定"))) == 1


def test_find_by_class_and_index(monkeypatch):
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(adb, "_dump_xml", _fake_dump(root))
    buttons = asyncio.run(adb.find_elements("S", "class", "android.widget.Button"))
    assert len(buttons) == 2
    second = asyncio.run(adb.find_element("S", "class", "android.widget.Button", index=1))
    assert second["text"] == "取消"


def test_find_by_xpath(monkeypatch):
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(adb, "_dump_xml", _fake_dump(root))
    els = asyncio.run(adb.find_elements("S", "xpath", ".//node[@text='取消']"))
    assert len(els) == 1
    assert els[0]["text"] == "取消"


def test_find_rejects_unknown_kind(monkeypatch):
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(adb, "_dump_xml", _fake_dump(root))
    try:
        asyncio.run(adb.find_elements("S", "bogus", "x"))
        assert False, "未知维度应被拒绝"
    except adb.AdbError:
        pass


def test_find_element_out_of_range_returns_none(monkeypatch):
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(adb, "_dump_xml", _fake_dump(root))
    assert asyncio.run(adb.find_element("S", "text", "设置", index=5)) is None


# ---------- 命中测试与选择器生成（控件级录制） ----------

def test_find_element_at_picks_smallest_containing_node(monkeypatch):
    xml = (
        '<hierarchy>'
        '<node text="" resource-id="com.x:id/root" class="android.widget.FrameLayout" '
        'content-desc="" package="com.x" bounds="[0,0][1000,2000]"/>'
        '<node text="" resource-id="com.x:id/btn" class="android.widget.Button" '
        'content-desc="" package="com.x" bounds="[100,100][300,200]"/>'
        '<node text="" resource-id="com.x:id/inner" class="android.view.View" '
        'content-desc="" package="com.x" bounds="[150,120][200,150]"/>'
        '</hierarchy>'
    )
    root = ET.fromstring(xml)
    monkeypatch.setattr(adb, "_dump_xml", _fake_dump(root))
    el = asyncio.run(adb.find_element_at("S", 170, 130))
    assert el["id"] == "com.x:id/inner"
    assert asyncio.run(adb.find_element_at("S", 500, 500))["id"] == "com.x:id/root"
    assert asyncio.run(adb.find_element_at("S", 5000, 5000)) is None


def test_selector_from_element_priority_order():
    assert adb.selector_from_element({"id": "com.x:id/a", "desc": "d", "text": "t", "class": "c"}) == {
        "kind": "id", "value": "com.x:id/a", "contains": False, "index": 0}
    assert adb.selector_from_element({"id": "", "desc": "d", "text": "t", "class": "c"})["kind"] == "desc"
    assert adb.selector_from_element({"id": "", "desc": "", "text": "t", "class": "c"})["kind"] == "text"
    assert adb.selector_from_element({"id": "", "desc": "", "text": "", "class": "c"})["kind"] == "class"
    assert adb.selector_from_element({"id": "", "desc": "", "text": "", "class": ""}) is None


def test_selector_from_element_ignores_blank_values():
    assert adb.selector_from_element({"id": "   ", "desc": "确定", "text": "", "class": ""})["kind"] == "desc"
