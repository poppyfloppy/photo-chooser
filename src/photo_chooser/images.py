"""Загрузка и масштабирование изображений через ImageIO, JPEG-байты для Claude, запись TIFF."""

from pathlib import Path

import Foundation
import numpy as np
import Quartz


def _url(path: Path):
    return Foundation.NSURL.fileURLWithPath_(str(path))


def load_cgimage(path: Path, long_side: int):
    """Изображение с длинной стороной не больше long_side, с учётом ориентации EXIF."""
    source = Quartz.CGImageSourceCreateWithURL(_url(path), None)
    if source is None:
        raise ValueError(f"не удалось открыть изображение {path}")
    options = {
        Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
        Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
        Quartz.kCGImageSourceThumbnailMaxPixelSize: long_side,
    }
    img = Quartz.CGImageSourceCreateThumbnailAtIndex(source, 0, options)
    if img is None:
        raise ValueError(f"не удалось декодировать изображение {path}")
    return img


def gray_array(img) -> np.ndarray:
    w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    buf = bytearray(w * h)
    ctx = Quartz.CGBitmapContextCreate(buf, w, h, 8, w, Quartz.CGColorSpaceCreateDeviceGray(),
                                       Quartz.kCGImageAlphaNone)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), img)
    del ctx
    return np.frombuffer(bytes(buf), dtype=np.uint8).reshape(h, w).astype(np.float32) / 255.0


def jpeg_bytes(path: Path, long_side: int, quality: float = 0.85) -> bytes:
    img = load_cgimage(path, long_side)
    data = Foundation.NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    Quartz.CGImageDestinationAddImage(dest, img, {Quartz.kCGImageDestinationLossyCompressionQuality: quality})
    if not Quartz.CGImageDestinationFinalize(dest):
        raise ValueError(f"не удалось закодировать JPEG из {path}")
    return bytes(data)


def write_tiff(img, dest: Path, metadata_from: Path) -> None:
    """TIFF без потерь; дата (EXIF/TIFF) и GPS копируются из metadata_from. Кадр уже повёрнут: ориентация 1."""
    props = {}
    source = Quartz.CGImageSourceCreateWithURL(_url(metadata_from), None)
    original = Quartz.CGImageSourceCopyPropertiesAtIndex(source, 0, None) if source is not None else None
    if original:
        exif = dict(original.get(Quartz.kCGImagePropertyExifDictionary) or {})
        for key in (Quartz.kCGImagePropertyExifPixelXDimension, Quartz.kCGImagePropertyExifPixelYDimension):
            exif.pop(key, None)
        if exif:
            props[Quartz.kCGImagePropertyExifDictionary] = exif
        tiff = dict(original.get(Quartz.kCGImagePropertyTIFFDictionary) or {})
        tiff.pop(Quartz.kCGImagePropertyTIFFOrientation, None)
        if tiff:
            props[Quartz.kCGImagePropertyTIFFDictionary] = tiff
        gps = original.get(Quartz.kCGImagePropertyGPSDictionary)
        if gps:
            props[Quartz.kCGImagePropertyGPSDictionary] = dict(gps)
    props[Quartz.kCGImagePropertyOrientation] = 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = Quartz.CGImageDestinationCreateWithURL(_url(dest), "public.tiff", 1, None)
    if out is None:
        raise ValueError(f"не удалось создать файл {dest}")
    Quartz.CGImageDestinationAddImage(out, img, props)
    if not Quartz.CGImageDestinationFinalize(out):
        raise ValueError(f"не удалось записать TIFF {dest}")
