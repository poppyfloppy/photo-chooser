"""Загрузка и масштабирование изображений через ImageIO, JPEG-байты для Claude, запись TIFF."""

from pathlib import Path

import numpy as np
from Foundation import NSMutableData, NSURL
from Quartz import (  # имена подгружаются при импорте: ленивая подгрузка pyobjc не потокобезопасна
    CGBitmapContextCreate,
    CGBitmapContextCreateImage,
    CGColorSpaceCreateDeviceGray,
    CGColorSpaceCreateDeviceRGB,
    CGContextDrawImage,
    CGContextSetInterpolationQuality,
    CGImageDestinationAddImage,
    CGImageDestinationCreateWithData,
    CGImageDestinationCreateWithURL,
    CGImageDestinationFinalize,
    CGImageGetHeight,
    CGImageGetWidth,
    CGImageSourceCopyPropertiesAtIndex,
    CGImageSourceCreateThumbnailAtIndex,
    CGImageSourceCreateWithURL,
    CGRectMake,
    kCGImageAlphaNone,
    kCGImageAlphaNoneSkipLast,
    kCGImageDestinationLossyCompressionQuality,
    kCGImagePropertyExifDictionary,
    kCGImagePropertyExifPixelXDimension,
    kCGImagePropertyExifPixelYDimension,
    kCGImagePropertyGPSDictionary,
    kCGImagePropertyOrientation,
    kCGImagePropertyTIFFDictionary,
    kCGImagePropertyTIFFOrientation,
    kCGImageSourceCreateThumbnailFromImageAlways,
    kCGImageSourceCreateThumbnailWithTransform,
    kCGImageSourceThumbnailMaxPixelSize,
    kCGInterpolationHigh,
)


def _url(path: Path):
    return NSURL.fileURLWithPath_(str(path))


def load_cgimage(path: Path, long_side: int):
    """Изображение с длинной стороной не больше long_side, с учётом ориентации EXIF."""
    source = CGImageSourceCreateWithURL(_url(path), None)
    if source is None:
        raise ValueError(f"не удалось открыть изображение {path}")
    options = {
        kCGImageSourceCreateThumbnailFromImageAlways: True,
        kCGImageSourceCreateThumbnailWithTransform: True,
        kCGImageSourceThumbnailMaxPixelSize: long_side,
    }
    img = CGImageSourceCreateThumbnailAtIndex(source, 0, options)
    if img is None:
        raise ValueError(f"не удалось декодировать изображение {path}")
    return img


def gray_array(img) -> np.ndarray:
    w, h = CGImageGetWidth(img), CGImageGetHeight(img)
    buf = bytearray(w * h)
    ctx = CGBitmapContextCreate(buf, w, h, 8, w, CGColorSpaceCreateDeviceGray(),
                                       kCGImageAlphaNone)
    CGContextDrawImage(ctx, CGRectMake(0, 0, w, h), img)
    del ctx
    return np.frombuffer(bytes(buf), dtype=np.uint8).reshape(h, w).astype(np.float32) / 255.0


def jpeg_bytes(path: Path, long_side: int, quality: float = 0.85) -> bytes:
    img = load_cgimage(path, long_side)
    data = NSMutableData.data()
    dest = CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    CGImageDestinationAddImage(dest, img, {kCGImageDestinationLossyCompressionQuality: quality})
    if not CGImageDestinationFinalize(dest):
        raise ValueError(f"не удалось закодировать JPEG из {path}")
    return bytes(data)


def write_tiff(img, dest: Path, metadata_from: Path) -> None:
    """TIFF без потерь; дата (EXIF/TIFF) и GPS копируются из metadata_from. Кадр уже повёрнут: ориентация 1."""
    props = {}
    source = CGImageSourceCreateWithURL(_url(metadata_from), None)
    original = CGImageSourceCopyPropertiesAtIndex(source, 0, None) if source is not None else None
    if original:
        exif = dict(original.get(kCGImagePropertyExifDictionary) or {})
        for key in (kCGImagePropertyExifPixelXDimension, kCGImagePropertyExifPixelYDimension):
            exif.pop(key, None)
        if exif:
            props[kCGImagePropertyExifDictionary] = exif
        tiff = dict(original.get(kCGImagePropertyTIFFDictionary) or {})
        tiff.pop(kCGImagePropertyTIFFOrientation, None)
        if tiff:
            props[kCGImagePropertyTIFFDictionary] = tiff
        gps = original.get(kCGImagePropertyGPSDictionary)
        if gps:
            props[kCGImagePropertyGPSDictionary] = dict(gps)
    props[kCGImagePropertyOrientation] = 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = CGImageDestinationCreateWithURL(_url(dest), "public.tiff", 1, None)
    if out is None:
        raise ValueError(f"не удалось создать файл {dest}")
    CGImageDestinationAddImage(out, img, props)
    if not CGImageDestinationFinalize(out):
        raise ValueError(f"не удалось записать TIFF {dest}")


def fit_long_side(img, long_side: int):
    """Уменьшает изображение до длинной стороны long_side (не увеличивает)."""
    w, h = CGImageGetWidth(img), CGImageGetHeight(img)
    if max(w, h) <= long_side:
        return img
    scale = long_side / max(w, h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    ctx = CGBitmapContextCreate(None, nw, nh, 8, 0, CGColorSpaceCreateDeviceRGB(),
                                       kCGImageAlphaNoneSkipLast)
    CGContextSetInterpolationQuality(ctx, kCGInterpolationHigh)
    CGContextDrawImage(ctx, CGRectMake(0, 0, nw, nh), img)
    return CGBitmapContextCreateImage(ctx)
