from __future__ import annotations

import io
import re
from PIL import Image, ImageEnhance, ImageOps


def _image(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    if image.width < 30 or image.height < 20 or image.width * image.height > 12_000_000:
        raise ValueError("截图区域尺寸无效")
    return image


def _recognized(engine, image: Image.Image) -> tuple[list[str], list[float]]:
    import numpy as np
    result = engine(np.asarray(image))
    return list(result.txts or []), list(result.scores or [])


def _parse(lines: list[str], scores: list[float]) -> dict:
    ratios = []
    numbers = []
    for line in lines:
        # Only accept an isolated integer ratio. Decimal or merged text must not
        # be silently converted into a false executable order.
        match = re.fullmatch(r"\s*(\d[\d,]*)\s*[:：/]\s*(\d[\d,]*)\s*", line)
        if match:
            a, b = (int(value.replace(",", "")) for value in match.groups())
            if a and b:
                ratios.append({"left": a, "right": b})
        for number in re.finditer(r"(?<![\d.])\d[\d,]*(?![\d.])", line):
            value = int(number.group().replace(",", ""))
            if value not in numbers:
                numbers.append(value)
    return {"lines": lines, "confidence": round(min(scores), 3) if scores else 0, "ratios": ratios, "numbers": numbers}


def read_image(data: bytes) -> dict:
    from rapidocr import RapidOCR
    image = _image(data)
    image = ImageOps.autocontrast(ImageEnhance.Contrast(image).enhance(1.5))
    lines, scores = _recognized(RapidOCR(), image)
    return _parse(lines, scores)


def read_exchange_panel(data: bytes) -> dict:
    """Read the selected trade panel above the listings, scaled to a calibrated ROI."""
    from rapidocr import RapidOCR
    image = _image(data)
    engine = RapidOCR()
    enhanced = ImageOps.autocontrast(ImageEnhance.Contrast(image).enhance(1.5))
    lines, scores = _recognized(engine, enhanced)
    result = _parse(lines, scores)
    values = []
    value_scores = []
    # Fractions measured from the selected panel [588,160,1328,345].
    # Left is "I need" (receive), right is "I have" (pay), lower middle is gold.
    for box in ((240, 60, 325, 105), (420, 60, 505, 105), (335, 115, 440, 150)):
        x0, y0, x1, y1 = box
        region = image.crop((
            round(x0 * image.width / 740), round(y0 * image.height / 185),
            round(x1 * image.width / 740), round(y1 * image.height / 185),
        ))
        region = region.resize((max(160, region.width * 4), max(88, region.height * 4)))
        region = ImageOps.autocontrast(region)
        region_lines, region_scores = _recognized(engine, region)
        match = re.fullmatch(r"\s*(\d[\d,]*)\s*", "".join(region_lines))
        values.append(int(match.group(1).replace(",", "")) if match else None)
        value_scores.append(min(region_scores) if region_scores else 0)
    if values[0] and values[1] and values[2] is not None and min(value_scores) >= 0.7:
        result["selected_order"] = {
            "receive": values[0], "pay": values[1], "gold": values[2],
            "confidence": round(min(value_scores), 3),
        }
    else:
        result["selected_order"] = None
    return result


def read_stock_region(data: bytes) -> dict:
    """Read a calibrated region containing only the available target-item count.

    A separate calibration is required because stock placement differs by market
    view. Reject ratios, merged labels and ambiguous multiple values.
    """
    from rapidocr import RapidOCR
    image = _image(data)
    image = image.resize((max(160, image.width * 4), max(88, image.height * 4)))
    image = ImageOps.autocontrast(ImageEnhance.Contrast(image).enhance(1.5))
    lines, scores = _recognized(RapidOCR(), image)
    joined = "".join(lines).strip()
    match = re.fullmatch(r"(\d[\d,]*)", joined)
    value = int(match.group(1).replace(",", "")) if match else None
    confidence = min(scores) if scores else 0
    return {"stock": value if value and confidence >= 0.7 else None,
            "confidence": round(confidence, 3), "lines": lines}
