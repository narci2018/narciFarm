"""图像匹配增强 / OCR 结构化 单元测试。"""
import sys
import types

import numpy as np
import pytest
from PIL import Image

from narci import vision


def _noise(h, w, seed=0):
    rng = np.random.RandomState(seed)
    return rng.randint(0, 255, (h, w)).astype(np.float32)


def test_match_all_single_result_at_known_location():
    img = _noise(120, 120, seed=1)
    tmpl = img[40:70, 40:70].copy()
    res = vision.match_all(img, tmpl, threshold=0.8)
    assert len(res) == 1
    assert res[0]["x"] == 40 and res[0]["y"] == 40
    assert res[0]["score"] > 0.9
    assert res[0]["center"] == (55, 55)


def test_match_all_multiple_results_and_nms():
    img = _noise(140, 220, seed=2)
    tmpl = _noise(24, 24, seed=99)
    img[30:54, 30:54] = tmpl
    img[80:104, 160:184] = tmpl
    res = vision.match_all(img, tmpl, threshold=0.8, max_results=5)
    assert len(res) == 2
    xs = sorted(r["x"] for r in res)
    assert xs == [30, 160]
    # 得分降序
    assert res[0]["score"] >= res[1]["score"]


def test_match_all_region_restricts_search():
    img = _noise(140, 220, seed=3)
    tmpl = _noise(20, 20, seed=7)
    img[20:40, 20:40] = tmpl      # 区域内
    img[80:100, 160:180] = tmpl   # 区域外
    res = vision.match_all(img, tmpl, threshold=0.8, region=(0, 0, 100, 100))
    assert len(res) == 1
    assert res[0]["x"] == 20


def test_match_all_scales_detects_resized_template():
    img = _noise(200, 200, seed=4)
    base = _noise(20, 20, seed=11)
    img[60:100, 60:100] = np.asarray(
        Image.fromarray(base.astype(np.uint8)).resize((40, 40), Image.BILINEAR),
        dtype=np.float32,
    )
    res = vision.match_all(img, base, threshold=0.75, scales=[1.0, 2.0])
    assert res, "多尺度应匹配到放大后的模板"
    assert any(r["scale"] == 2.0 for r in res)


def test_match_all_below_threshold_returns_empty():
    img = _noise(80, 80, seed=5)
    tmpl = _noise(20, 20, seed=6)
    assert vision.match_all(img, tmpl, threshold=0.99) == []


def test_match_all_rejects_uniform_template():
    img = _noise(80, 80, seed=7)
    tmpl = np.full((20, 20), 128.0, dtype=np.float32)
    assert vision.match_all(img, tmpl, threshold=0.5) == []


def test_match_all_max_results_caps_output():
    img = _noise(200, 200, seed=8)
    tmpl = _noise(16, 16, seed=21)
    for y in range(0, 160, 40):
        for x in range(0, 160, 40):
            img[y:y + 16, x:x + 16] = tmpl
    res = vision.match_all(img, tmpl, threshold=0.8, max_results=3)
    assert len(res) == 3


def test_ocr_find_returns_structure(monkeypatch, tmp_path):
    fake = types.ModuleType("pytesseract")

    def image_to_data(img, lang=None, output_type=None):
        return {
            "text": ["你好", "", "世界"],
            "conf": [95.5, -1, 88.0],
            "left": [10, 0, 50],
            "top": [20, 0, 30],
            "width": [30, 0, 40],
            "height": [15, 0, 18],
        }

    fake.image_to_data = image_to_data
    fake.Output = types.SimpleNamespace(DICT="dict")
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(vision, "ocr_available", lambda: True)

    p = tmp_path / "shot.png"
    Image.new("RGB", (200, 100), (255, 255, 255)).save(p)

    rows = vision.ocr_find(str(p))
    assert [r["text"] for r in rows] == ["你好", "世界"]
    assert rows[0]["bounds"] == (10, 20, 40, 35)
    assert rows[0]["center"] == (25, 27)
    assert rows[0]["confidence"] == 95.5


def test_ocr_find_region_offsets_coordinates(monkeypatch, tmp_path):
    fake = types.ModuleType("pytesseract")
    fake.image_to_data = lambda img, lang=None, output_type=None: {
        "text": ["字"], "conf": [90.0], "left": [5], "top": [7],
        "width": [20], "height": [12],
    }
    fake.Output = types.SimpleNamespace(DICT="dict")
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(vision, "ocr_available", lambda: True)

    p = tmp_path / "shot.png"
    Image.new("RGB", (400, 400), (255, 255, 255)).save(p)

    rows = vision.ocr_find(str(p), region=(100, 200, 300, 350))
    assert rows[0]["bounds"] == (105, 207, 125, 219)
    assert rows[0]["center"] == (115, 213)


def test_ocr_text_joins_words(monkeypatch, tmp_path):
    fake = types.ModuleType("pytesseract")
    fake.image_to_data = lambda img, lang=None, output_type=None: {
        "text": ["abc", "def"], "conf": [90.0, 91.0], "left": [0, 10],
        "top": [0, 0], "width": [10, 10], "height": [10, 10],
    }
    fake.Output = types.SimpleNamespace(DICT="dict")
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(vision, "ocr_available", lambda: True)

    p = tmp_path / "shot.png"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(p)
    assert vision.ocr_text(str(p)) == "abc def"


def test_ocr_text_returns_none_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "ocr_available", lambda: False)
    p = tmp_path / "shot.png"
    Image.new("RGB", (10, 10), (255, 255, 255)).save(p)
    assert vision.ocr_text(str(p)) is None
