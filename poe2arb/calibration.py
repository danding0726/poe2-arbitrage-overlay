from __future__ import annotations

from copy import deepcopy


CAPTURE_PRESETS = {
    (1920, 1080): {
        "roi": {
            "bbox": [588, 160, 1328, 345],
            "screen": [1920, 1080],
            "layout": "selected_trade_v1",
            "preset": True,
        },
    },
    # Reference: centered Currency Exchange panel at 2560x1440, 100% UI scale.
    # The stock box contains the full hovered ladder, excluding the market
    # ratio above it and the selected order below it.
    (2560, 1440): {
        "roi": {
            "bbox": [784, 213, 1771, 460],
            "screen": [2560, 1440],
            "layout": "selected_trade_v1",
            "preset": True,
        },
        "stock_roi": {
            "bbox": [1135, 300, 1430, 525],
            "screen": [2560, 1440],
            "layout": "stock_ladder_v3",
            "preset": True,
        },
    },
}


def capture_preset(screen: tuple[int, int]) -> dict:
    """Return independent ROI dictionaries for an exact reference layout."""
    return deepcopy(CAPTURE_PRESETS.get(tuple(screen), {}))


def calibration_guides(screen: tuple[int, int]) -> dict[str, list[int]]:
    """Scale reference boxes for display only; never use them as OCR presets."""
    width, height = screen
    if width <= 0 or height <= 0 or width * 9 != height * 16:
        return {}
    exact = CAPTURE_PRESETS.get(tuple(screen), {})
    reference = CAPTURE_PRESETS[2560, 1440]
    scale_x, scale_y = width / 2560, height / 1440
    guides = {}
    for key in ("roi", "stock_roi"):
        if key in exact:
            guides[key] = exact[key]["bbox"].copy()
        else:
            left, top, right, bottom = reference[key]["bbox"]
            guides[key] = [int(left * scale_x + 0.5), int(top * scale_y + 0.5),
                           int(right * scale_x + 0.5), int(bottom * scale_y + 0.5)]
    return guides
