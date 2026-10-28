"""图像匹配(Airtest 式模板匹配, numpy FFT-NCC)与 OCR(可选 Tesseract)。"""
import os
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

TEMPLATES_DIR = Path(os.environ.get("NARCI_TEMPLATES_DIR", Path(__file__).resolve().parent.parent / "data" / "templates"))
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


def _gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float32)


def _win_sum(ii: np.ndarray, out_h: int, out_w: int, h: int, w: int) -> np.ndarray:
    """积分图窗口求和(向量化)。ii 尺寸 (out_h+h, out_w+w)。"""
    return ii[h:h + out_h, w:w + out_w] - ii[0:out_h, w:w + out_w] - ii[h:h + out_h, 0:out_w] + ii[0:out_h, 0:out_w]


def match_template(img: np.ndarray, tmpl: np.ndarray, threshold: float = 0.8) -> Optional[tuple[int, int, float]]:
    """在 img 中匹配 tmpl(均为灰度 float32)。返回 (x, y, score) 或 None。

    x/y 为匹配区域左上角坐标, score 为归一化互相关(0~1)。
    """
    H, W = img.shape
    h, w = tmpl.shape
    if h > H or w > W or h < 2 or w < 2:
        return None

    # 1) 互相关: corr = IFFT(FFT(img) * conj(FFT(tmpl)))  —— float64 降低浮点误差
    img64 = img.astype(np.float64)
    img_pad = np.pad(img64, ((0, h - 1), (0, w - 1)))
    corr = np.fft.irfft2(np.fft.rfft2(img_pad) * np.fft.rfft2(tmpl.astype(np.float64), s=img_pad.shape).conj(),
                         s=img_pad.shape)
    corr = corr[:H - h + 1, :W - w + 1]

    # 2) 局部均值/方差(积分图)
    n = h * w
    ii = np.pad(img.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    ii2 = np.pad((img ** 2).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    out_h, out_w = H - h + 1, W - w + 1
    s1 = _win_sum(ii, out_h, out_w, h, w)
    s2 = _win_sum(ii2, out_h, out_w, h, w)

    t_mean = float(tmpl.mean())
    t_sq = float((tmpl ** 2).sum())
    t_std = np.sqrt(max(t_sq / n - t_mean ** 2, 1e-9))
    i_mean = s1 / n
    i_std = np.sqrt(np.maximum(s2 / n - i_mean ** 2, 0))

    # 方差守卫: 模板或窗口接近均匀(纯色)时 NCC 不可靠, 直接判否
    if t_std < 1.0:
        return None
    valid = i_std >= 5.0
    if not valid.any():
        return None
    ncc = np.full(corr.shape, -1.0, dtype=np.float64)
    numerator = corr - n * t_mean * i_mean
    denominator = t_std * i_std * n + 1e-9
    ncc[valid] = numerator[valid] / denominator[valid]

    y, x = np.unravel_index(np.argmax(ncc), ncc.shape)
    score = min(1.0, max(0.0, float(ncc[y, x])))
    if score < threshold:
        return None
    return int(x), int(y), round(score, 4)


async def match_on_device(serial: str, tmpl_path: str, threshold: float = 0.8) -> Optional[tuple[int, int, float]]:
    """截屏后在屏幕上匹配模板, 返回匹配区域中心坐标。"""
    from . import adb
    data = await adb.screenshot_bytes(serial)
    img = Image.open(__import__("io").BytesIO(data))
    tmpl = Image.open(tmpl_path)
    res = match_template(_gray(img), _gray(tmpl), threshold)
    if res is None:
        return None
    x, y, score = res
    th, tw = tmpl.size[1], tmpl.size[0]
    return x + tw // 2, y + th // 2, score


def resolve_template(name: str) -> str:
    """模板名 -> 完整路径。"""
    return str(TEMPLATES_DIR / name)


def list_templates() -> list[dict]:
    out = []
    for p in sorted(TEMPLATES_DIR.glob("*")):
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp"):
            try:
                with Image.open(p) as im:
                    out.append({"name": p.name, "width": im.size[0], "height": im.size[1]})
            except Exception:
                continue
    return out


def save_template(name: str, data: bytes) -> Path:
    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
        name += ".png"
    p = TEMPLATES_DIR / name
    p.write_bytes(data)
    return p


def delete_template(name: str) -> bool:
    p = TEMPLATES_DIR / name
    if p.exists():
        p.unlink()
        return True
    return False


def ocr_text(image_path: str) -> Optional[str]:
    """OCR(需系统安装 Tesseract; 未安装返回 None)。"""
    try:
        import pytesseract
        return pytesseract.image_to_string(Image.open(image_path), lang="chi_sim+eng").strip()
    except Exception:
        return None


def ocr_available() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False
