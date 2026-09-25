from __future__ import annotations

import io
import re
from fractions import Fraction
from math import ceil
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


def _selected_order_from_detections(lines, scores, boxes) -> dict | None:
    """Read the selected order using the game's labeled left/right columns."""
    if boxes is None or not (len(lines) == len(scores) == len(boxes)):
        return None
    numbers = []
    labels = {}
    for text, score, box in zip(lines, scores, boxes):
        if box is None or len(box) == 0:
            continue
        points = list(box)
        center_x = sum(float(point[0]) for point in points) / len(points)
        normalized = text.replace(" ", "")
        if score >= 0.7 and "我需要的" in normalized:
            labels["receive"] = center_x
        elif score >= 0.7 and ("我拥有的" in normalized or "我擁有的" in normalized):
            labels["pay"] = center_x
        match = re.fullmatch(r"\s*(\d[\d,]*)\s*", text)
        if not match or score < 0.7:
            continue
        numbers.append({
            "value": int(match.group(1).replace(",", "")),
            "score": score,
            "x": center_x,
            "y": sum(float(point[1]) for point in points) / len(points),
        })
    # The calibrated panel should contain exactly two order amounts on its
    # upper row and one gold fee below. Extra standalone integers are unsafe.
    if len(numbers) != 3:
        return None
    gold = max(numbers, key=lambda item: item["y"])
    amounts = [item for item in numbers if item is not gold]
    if gold["y"] <= max(item["y"] for item in amounts):
        return None
    if labels.keys() >= {"receive", "pay"}:
        # Bind values to the headings instead of trusting RapidOCR's output
        # order. In PoE 2, "I Want" is receive and "I Have" is pay.
        direct = (
            abs(amounts[0]["x"] - labels["receive"])
            + abs(amounts[1]["x"] - labels["pay"])
        )
        swapped = (
            abs(amounts[1]["x"] - labels["receive"])
            + abs(amounts[0]["x"] - labels["pay"])
        )
        receive, pay = amounts if direct <= swapped else amounts[::-1]
    else:
        # The game layout is fixed even when a localized heading is missed:
        # left is "I Want" (receive), right is "I Have" (pay).
        receive, pay = sorted(amounts, key=lambda item: item["x"])
    return {
        "receive": receive["value"],
        "pay": pay["value"],
        "gold": gold["value"],
        "confidence": round(min(item["score"] for item in numbers), 3),
    }


def _best_ladder_quote(lines, scores, boxes) -> dict | None:
    """Read the first executable ratio/stock row from the hovered market ladder."""
    if boxes is None or not (len(lines) == len(scores) == len(boxes)):
        return None
    ratios = []
    stocks = []
    for text, score, box in zip(lines, scores, boxes):
        if score < 0.7 or box is None or len(box) == 0:
            continue
        points = list(box)
        center_x = sum(float(point[0]) for point in points) / len(points)
        center_y = sum(float(point[1]) for point in points) / len(points)
        height = max(float(point[1]) for point in points) - min(float(point[1]) for point in points)
        ratio = re.fullmatch(r"\s*(\d[\d,]*(?:\.\d+)?)\s*[:：]\s*(\d[\d,]*(?:\.\d+)?)\s*", text)
        integer = re.fullmatch(r"\s*(\d[\d,]*)\s*", text)
        if ratio:
            ratios.append((center_y, center_x, height, ratio.groups(), score))
        elif integer:
            stocks.append((center_y, center_x, int(integer.group(1).replace(",", "")), score))
    for ratio_y, ratio_x, height, (left_text, right_text), ratio_score in sorted(ratios):
        same_row = [
            stock for stock in stocks
            if stock[1] > ratio_x and abs(stock[0] - ratio_y) <= max(10, height * 0.6)
        ]
        # RapidOCR can miss the first row's right-hand stock while still
        # detecting the same quantity already filled in above the ladder.
        if not same_row:
            same_row = [
                stock for stock in stocks
                if abs(stock[0] - ratio_y) <= max(10, height * 0.9)
            ]
        if not same_row:
            continue
        _, _, stock, stock_score = min(same_row, key=lambda item: abs(item[0] - ratio_y))
        left = Fraction(left_text.replace(",", ""))
        right = Fraction(right_text.replace(",", ""))
        if left <= 0 or right <= 0 or stock <= 0:
            continue
        return {
            "receive": stock,
            "pay": ceil(stock * right / left),
            "stock": stock,
            "ratio": f"{left_text}:{right_text}",
            "confidence": round(min(ratio_score, stock_score), 3),
        }
    return None


def resolve_capture_order(selected_order: dict | None, best_quote: dict | None) -> dict | None:
    """Prefer the quantities displayed in the selected exchange panel.

    The stock ROI may include the selected panel when it is calibrated too
    broadly.  In that case its market ratio and a nearby amount can look like a
    ladder row, and expanding that false "stock" by the ratio changes a shown
    65 -> 1 order into 4225 -> 65.  The selected panel is the authoritative
    source for the order the user is actually reviewing; a ladder quote is only
    a fallback when that panel could not be read at all.
    """
    if selected_order:
        return selected_order
    if best_quote:
        return {
            "pay": best_quote["pay"],
            "receive": best_quote["receive"],
            "gold": None,
            "confidence": best_quote["confidence"],
        }
    return None


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
    import numpy as np
    recognized = engine(np.asarray(enhanced))
    lines = list(recognized.txts or [])
    scores = list(recognized.scores or [])
    result = _parse(lines, scores)
    result["selected_order"] = _selected_order_from_detections(
        lines, scores, recognized.boxes
    )
    if result["selected_order"]:
        return result
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
    return result


def read_stock_region(data: bytes) -> dict:
    """Read the best ratio/stock row, or a region containing one stock integer.

    A separate calibration is required because the hovered market ladder moves
    with the exchange UI. A single-number region remains supported as fallback.
    """
    from rapidocr import RapidOCR
    image = _image(data)
    image = image.resize((max(160, image.width * 4), max(88, image.height * 4)))
    image = ImageOps.autocontrast(ImageEnhance.Contrast(image).enhance(1.5))
    import numpy as np
    result = RapidOCR()(np.asarray(image))
    lines = list(result.txts or [])
    scores = list(result.scores or [])
    ladder_quote = _best_ladder_quote(lines, scores, getattr(result, "boxes", None))
    if ladder_quote:
        return {
            "stock": ladder_quote["stock"],
            "confidence": ladder_quote["confidence"],
            "lines": lines,
            "best_quote": ladder_quote,
        }
    joined = "".join(lines).strip()
    match = re.fullmatch(r"(\d[\d,]*)", joined)
    value = int(match.group(1).replace(",", "")) if match else None
    confidence = min(scores) if scores else 0
    return {"stock": value if value and confidence >= 0.7 else None,
            "confidence": round(confidence, 3), "lines": lines, "best_quote": None}
