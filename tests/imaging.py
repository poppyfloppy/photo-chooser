"""Генерация тестовых изображений (Pillow) — независимо от кода photo_chooser.images."""

from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

EXIF_IFD, GPS_IFD = 0x8769, 0x8825
DATETIME_ORIGINAL = 0x9003
ORIENTATION = 0x0112


def noise_image(seed: int = 1, size=(640, 480)) -> Image.Image:
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 256, size=(size[1], size[0], 3), dtype=np.uint8)
    return Image.fromarray(pixels, "RGB")


def scene_image(kind: int, size=(640, 480)) -> Image.Image:
    """Простые различимые сцены: 0 — круги на синем, 1 — полосы на зелёном."""
    im = Image.new("RGB", size, (30, 60, 160) if kind == 0 else (40, 140, 60))
    d = ImageDraw.Draw(im)
    if kind == 0:
        for i in range(6):
            d.ellipse((40 + i * 90, 120, 120 + i * 90, 200), fill=(240, 220, 40))
    else:
        for i in range(0, size[0], 40):
            d.rectangle((i, 0, i + 15, size[1]), fill=(250, 250, 250))
    return im


def blurred(im: Image.Image, radius: float = 6) -> Image.Image:
    return im.filter(ImageFilter.GaussianBlur(radius))


def save_jpeg(im: Image.Image, path: Path, *, orientation: int | None = None, date: str | None = None,
              gps: tuple[float, float] | None = None) -> Path:
    exif = Image.Exif()
    if orientation is not None:
        exif[ORIENTATION] = orientation
    if date is not None:
        exif.get_ifd(EXIF_IFD)[DATETIME_ORIGINAL] = date
    if gps is not None:
        lat, lon = gps
        g = exif.get_ifd(GPS_IFD)
        g[1], g[2] = ("N" if lat >= 0 else "S"), _dms(abs(lat))
        g[3], g[4] = ("E" if lon >= 0 else "W"), _dms(abs(lon))
    im.save(path, "JPEG", quality=92, exif=exif)
    return path


def _dms(value: float):
    d = int(value)
    m = int((value - d) * 60)
    s = round(((value - d) * 60 - m) * 60, 2)
    return (Fraction(d), Fraction(m), Fraction(s).limit_denominator(100))
