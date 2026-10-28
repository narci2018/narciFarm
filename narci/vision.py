"""图像匹配(Airtest 式模板匹配, numpy FFT-NCC)与 OCR(可选 Tesseract)。

匹配支持单结果(兼容旧接口)与多结果/多尺度/区域限定(match_all)。
"""
import contextlib
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


def _ncc_map(img: np.ndarray, tmpl: np.ndarray) -> Optional[np.ndarray]:
    """计算归一化互相关图(与模板等尺寸偏移)。模板/窗口接近纯色时返回 None。"""
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
    return ncc


def _top_peaks(ncc: np.ndarray, h: int, w: int, max_results: int) -> list:
    """从 NCC 图按得分降序取前 N 个互不重叠的峰值，返回 [(x, y, score, bounds)]。"""
    work = ncc.copy()
    out: list = []
    for _ in range(max(1, max_results)):
        y, x = np.unravel_index(int(np.argmax(work)), work.shape)
        score = float(work[y, x])
        if score <= 0:
            break
        out.append((int(x), int(y), round(min(1.0, max(0.0, score)), 4),
                    (int(x), int(y), int(x) + w, int(y) + h)))
        # 抑制该匹配周围区域，避免同一目标被重复检出
        y1, y2 = max(0, y - h // 2), min(work.shape[0], y + h + h // 2)
        x1, x2 = max(0, x - w // 2), min(work.shape[1], x + w + w // 2)
        work[y1:y2, x1:x2] = -1.0
    return out


def _overlap_ratio(a: tuple, b: tuple) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / min(area_a, area_b)


def _dedupe(items: list, iou_threshold: float = 0.3) -> list:
    """跨尺度去重：保留得分最高且不与已保留项重叠的结果。"""
    kept: list = []
    for item in sorted(items, key=lambda x: -x[2]):
        if all(_overlap_ratio(item[3], k[3]) <= iou_threshold for k in kept):
            kept.append(item)
    return kept


def match_template(img: np.ndarray, tmpl: np.ndarray, threshold: float = 0.8) -> Optional[tuple[int, int, float]]:
    """在 img 中匹配 tmpl(均为灰度 float32)。返回 (x, y, score) 或 None。

    x/y 为匹配区域左上角坐标, score 为归一化互相关(0~1)。
    需要多结果/多尺度/区域限定时用 match_all()。
    """
    ncc = _ncc_map(img, tmpl)
    if ncc is None:
        return None
    h, w = tmpl.shape
    peaks = _top_peaks(ncc, h, w, 1)
    if not peaks:
        return None
    x, y, score, _ = peaks[0]
    if score < threshold:
        return None
    return x, y, score


def _resize_gray(arr: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """用双线性插值把灰度数组缩放到指定尺寸。"""
    h, w = arr.shape
    if out_h == h and out_w == w:
        return arr
    return np.asarray(
        Image.fromarray(arr.astype(np.uint8)).resize((out_w, out_h), Image.BILINEAR),
        dtype=np.float32,
    )


def match_all(img: np.ndarray, tmpl: np.ndarray, threshold: float = 0.8,
              max_results: int = 1, region: Optional[tuple] = None,
              scales: Optional[list] = None, iou_threshold: float = 0.3) -> list:
    """多结果 / 多尺度 / 区域限定的模板匹配。

    返回 [{"x","y","center","score","bounds","scale"}, ...]，按得分降序，最多 max_results 条。
    region 为 (x1,y1,x2,y2) 时只在该区域内搜索；scales 为缩放比例列表（默认 [1.0]）。
    """
    if scales is None:
        scales = [1.0]
    max_results = max(1, int(max_results))
    H, W = img.shape
    h, w = tmpl.shape
    if h < 2 or w < 2 or H < 2 or W < 2:
        return []
    offset = (0, 0)
    search = img
    if region:
        x1, y1, x2, y2 = (int(v) for v in region)
        x1, x2 = max(0, min(x1, x2)), min(W, max(x1, x2))
        y1, y2 = max(0, min(y1, y2)), min(H, max(y1, y2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            return []
        search = img[y1:y2, x1:x2]
        offset = (x1, y1)

    found: list = []
    seen_scales = set()
    for sc in scales:
        try:
            scale = float(sc)
        except (TypeError, ValueError):
            continue
        if scale <= 0 or scale in seen_scales:
            continue
        seen_scales.add(scale)
        if abs(scale - 1.0) < 1e-6:
            t = tmpl
        else:
            t = _resize_gray(tmpl, max(2, int(round(h * scale))), max(2, int(round(w * scale))))
        th, tw = t.shape
        if th > search.shape[0] or tw > search.shape[1]:
            continue
        ncc = _ncc_map(search, t)
        if ncc is None:
            continue
        for x, y, score, bounds in _top_peaks(ncc, th, tw, max_results):
            if score < threshold:
                continue
            ox, oy = x + offset[0], y + offset[1]
            found.append((ox, oy, score, (ox, oy, ox + tw, oy + th), round(scale, 3)))

    deduped = _dedupe(found, iou_threshold)
    deduped.sort(key=lambda f: -f[2])
    out = []
    for (x, y, score, bounds, scale) in deduped[:max_results]:
        x1, y1, x2, y2 = bounds
        out.append({"x": x, "y": y, "center": ((x1 + x2) // 2, (y1 + y2) // 2),
                    "score": score, "bounds": bounds, "scale": scale})
    return out


async def match_on_device(serial: str, tmpl_path: str, threshold: float = 0.8,
                          max_results: int = 1, region: Optional[tuple] = None,
                          scales: Optional[list] = None) -> Optional[tuple[int, int, float]]:
    """截屏后在屏幕上匹配模板, 返回第一个匹配区域的中心坐标。"""
    res = await match_all_on_device(serial, tmpl_path, threshold, max_results=max_results,
                                    region=region, scales=scales)
    if not res:
        return None
    return res[0]["center"][0], res[0]["center"][1], res[0]["score"]


async def match_all_on_device(serial: str, tmpl_path: str, threshold: float = 0.8,
                              max_results: int = 1, region: Optional[tuple] = None,
                              scales: Optional[list] = None) -> list:
    """截屏后做多结果/多尺度匹配，返回 match_all() 的节点列表。"""
    import io
    from . import adb
    data = await adb.screenshot_bytes(serial)
    img = Image.open(io.BytesIO(data))
    tmpl = Image.open(tmpl_path)
    return match_all(_gray(img), _gray(tmpl), threshold, max_results=max_results,
                     region=region, scales=scales)


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


def ocr_find(image_path: str, region: Optional[tuple] = None,
             lang: str = "chi_sim+eng") -> list[dict]:
    """OCR 并返回每个识别词的文本 / 边界框 / 中心点 / 置信度。

    返回 [{"text": str, "bounds": (x1,y1,x2,y2), "center": (x,y), "confidence": float}, ...]
    未安装 Tesseract 或无识别结果时返回 []。region 为 (x1,y1,x2,y2) 时只识别该区域，
    返回的坐标仍换算回整屏坐标系。
    """
    if not ocr_available():
        return []
    try:
        import pytesseract
        from pytesseract import Output
    except Exception:
        return []
    try:
        img = Image.open(image_path)
        offset = (0, 0)
        if region:
            x1, y1, x2, y2 = (int(v) for v in region)
            x1, x2 = max(0, min(x1, x2)), min(img.size[0], max(x1, x2))
            y1, y2 = max(0, min(y1, y2)), min(img.size[1], max(y1, y2))
            img = img.crop((x1, y1, x2, y2))
            offset = (x1, y1)
        data = pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT)
    except Exception:
        return []
    out: list[dict] = []
    for i in range(len(data.get("text", []))):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][i])
        except (TypeError, ValueError):
            confidence = -1.0
        x = int(data["left"][i]) + offset[0]
        y = int(data["top"][i]) + offset[1]
        w = int(data["width"][i])
        h = int(data["height"][i])
        out.append({
            "text": text,
            "bounds": (x, y, x + w, y + h),
            "center": (x + w // 2, y + h // 2),
            "confidence": round(confidence, 2),
        })
    return out


def ocr_text(image_path: str) -> Optional[str]:
    """整屏 OCR 文本（需系统安装 Tesseract; 未安装返回 None）。

    保留旧语义：多词以空格连接。需要坐标级结果请用 ocr_find()。
    """
    try:
        results = ocr_find(image_path)
    except Exception:
        return None
    if not results:
        return None
    return " ".join(r["text"] for r in results).strip()


async def ocr_find_on_device(serial: str, target: Optional[str] = None,
                             region: Optional[tuple] = None,
                             lang: str = "chi_sim+eng") -> list[dict]:
    """截屏后做 OCR。给定 target 时按文本包含关系过滤（返回匹配项）。"""
    import tempfile
    from . import adb
    data = await adb.screenshot_bytes(serial)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(data)
        tmp_path = f.name
    try:
        results = ocr_find(tmp_path, region=region, lang=lang)
    finally:
        with contextlib.suppress(Exception):
            os.unlink(tmp_path)
    if target:
        return [r for r in results if target in r["text"]]
    return results


def ocr_available() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False
