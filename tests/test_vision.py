"""图像匹配单元测试: 合成图中定位已知模板 + 模板文件管理。"""
import io

import numpy as np
from PIL import Image

from narci import vision


def test_match_exact():
    rng = np.random.RandomState(42)
    big = (rng.rand(300, 400) * 255).astype(np.float32)
    tmpl = big[80:120, 50:110].copy()
    res = vision.match_template(big, tmpl, threshold=0.9)
    assert res is not None
    x, y, score = res
    assert (x, y) == (50, 80), f"期望(50,80), 实际({x},{y})"
    assert score > 0.99


def test_match_with_noise():
    rng = np.random.RandomState(7)
    big = (rng.rand(400, 500) * 200).astype(np.float32)
    tmpl = big[150:200, 200:280].copy()
    big_noisy = big + rng.randn(400, 500).astype(np.float32) * 8
    res = vision.match_template(big_noisy, tmpl, threshold=0.85)
    assert res is not None
    x, y, score = res
    assert abs(x - 200) <= 1 and abs(y - 150) <= 1, f"噪声下偏差过大: ({x},{y})"
    assert score > 0.85


def test_no_match_below_threshold():
    rng = np.random.RandomState(1)
    big = (rng.rand(200, 200) * 255).astype(np.float32)
    tmpl = (rng.rand(40, 40) * 255).astype(np.float32)
    assert vision.match_template(big, tmpl, threshold=0.95) is None


def test_template_larger_than_image():
    img = np.zeros((20, 20), dtype=np.float32)
    tmpl = np.zeros((30, 30), dtype=np.float32)
    assert vision.match_template(img, tmpl) is None


def test_templates_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "TEMPLATES_DIR", tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (255, 0, 0)).save(buf, "PNG")
    p = vision.save_template("btn.png", buf.getvalue())
    assert p.exists()
    lst = vision.list_templates()
    assert lst and lst[0]["name"] == "btn.png"
    assert vision.delete_template("btn.png")
    assert not vision.list_templates()
