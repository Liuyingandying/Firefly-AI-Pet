"""Generate ``assets/firefly.ico`` from the idle animation's first frame.

Fully offline and dependency-free beyond the project's existing Qt (PySide6)
runtime: no Pillow, no online service, no newly generated art. The character
GIF ships on a pure-black background (verified: every border pixel is opaque
black), so we flood-fill that connected black region to transparent, crop to
the opaque silhouette, and bake a multi-size ICO.

Run once (regenerates the committed asset):
    .venv\\Scripts\\python.exe tools\\generate_firefly_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

os_env = __import__("os")
os_env.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter

PROJECT_DIR = Path(__file__).resolve().parent.parent
SOURCE_GIF = PROJECT_DIR / "assets" / "animations" / "idle.gif"
OUT_ICO = PROJECT_DIR / "assets" / "firefly.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)
BG_THRESHOLD = 30  # max(r,g,b) below this counts as the black background


def _load_first_frame() -> QImage:
    reader = QImageReader(str(SOURCE_GIF))
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise RuntimeError(f"Could not read first frame of {SOURCE_GIF}")
    return image.convertToFormat(QImage.Format_RGBA8888)


def _remove_background(image: QImage, threshold: int = BG_THRESHOLD) -> QImage:
    """Flood-fill the near-black region connected to the border to transparent.

    Only background pixels are cleared; any dark pixel enclosed by the character
    (eyes, body segments) is unreachable from the border and preserved.
    """
    w, h = image.width(), image.height()
    rgb = [[None] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            c = image.pixelColor(x, y)
            rgb[y][x] = (c.red(), c.green(), c.blue())

    is_bg = [[False] * w for _ in range(h)]
    stack: list[tuple[int, int]] = []
    for x in range(w):
        stack.append((x, 0))
        stack.append((x, h - 1))
    for y in range(h):
        stack.append((0, y))
        stack.append((w - 1, y))

    while stack:
        x, y = stack.pop()
        if is_bg[y][x]:
            continue
        r, g, b = rgb[y][x]
        if max(r, g, b) >= threshold:
            continue
        is_bg[y][x] = True
        if x + 1 < w:
            stack.append((x + 1, y))
        if x - 1 >= 0:
            stack.append((x - 1, y))
        if y + 1 < h:
            stack.append((x, y + 1))
        if y - 1 >= 0:
            stack.append((x, y - 1))

    out = QImage(w, h, QImage.Format_RGBA8888)
    out.fill(Qt.transparent)
    for y in range(h):
        for x in range(w):
            if not is_bg[y][x]:
                r, g, b = rgb[y][x]
                out.setPixelColor(x, y, QColor(r, g, b, 255))
    return out


def _opaque_bbox(image: QImage) -> tuple[int, int, int, int]:
    w, h = image.width(), image.height()
    min_x, min_y, max_x, max_y = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if image.pixelColor(x, y).alpha() > 0:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if max_x < min_x:
        return (0, 0, w, h)
    return (min_x, min_y, max_x + 1, max_y + 1)


def _render_square(image: QImage, size: int) -> QImage:
    min_x, min_y, max_x, max_y = _opaque_bbox(image)
    cropped = image.copy(min_x, min_y, max_x - min_x, max_y - min_y)
    out = QImage(size, size, QImage.Format_RGBA8888)
    out.fill(Qt.transparent)

    pad = max(1, int(size * 0.06))
    avail = size - 2 * pad
    cw, ch = cropped.width(), cropped.height()
    scale = min(avail / cw, avail / ch)
    tw = max(1, int(cw * scale))
    th = max(1, int(ch * scale))
    scaled = cropped.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    painter = QPainter(out)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.drawImage((size - tw) // 2, (size - th) // 2, scaled)
    painter.end()
    return out


def _png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def build_ico(image: QImage) -> bytes:
    blobs = [_png_bytes(_render_square(image, size)) for size in SIZES]
    entry_size = 16
    offset = 6 + entry_size * len(SIZES)

    parts = [struct.pack("<HHH", 0, 1, len(SIZES))]
    entries = bytearray()
    for size, blob in zip(SIZES, blobs):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    parts.append(bytes(entries))
    parts.extend(blobs)
    return b"".join(parts)


def main() -> int:
    frame = _load_first_frame()
    cutout = _remove_background(frame)
    OUT_ICO.write_bytes(build_ico(cutout))
    print(f"Wrote {OUT_ICO} ({OUT_ICO.stat().st_size} bytes, {len(SIZES)} sizes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
