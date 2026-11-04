"""Deterministic rectangular heatmap layout; rendering and fetching are separate."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TileRect:
    tile: object
    x: float
    y: float
    width: float
    height: float


def layout_tiles(tiles, width, height, limit=200):
    if width <= 0 or height <= 0 or not 1 <= limit <= 500:
        return ()
    rows = sorted(
        (t for t in tiles if t.amount_yuan is not None and t.amount_yuan > 0),
        key=lambda t: (-t.amount_yuan, t.code),
    )[:limit]
    result = []

    def split(items, x, y, w, h):
        if not items:
            return
        if len(items) == 1:
            result.append(TileRect(items[0], x, y, w, h))
            return
        total = sum(t.amount_yuan for t in items)
        accumulated, pivot = 0, 1
        for i, item in enumerate(items[:-1], 1):
            accumulated += item.amount_yuan
            pivot = i
            if accumulated >= total / 2:
                break
        fraction = accumulated / total
        if w >= h:
            split(items[:pivot], x, y, w * fraction, h)
            split(items[pivot:], x + w * fraction, y, w * (1 - fraction), h)
        else:
            split(items[:pivot], x, y, w, h * fraction)
            split(items[pivot:], x, y + h * fraction, w, h * (1 - fraction))

    split(rows, 0, 0, width, height)
    return tuple(result)
