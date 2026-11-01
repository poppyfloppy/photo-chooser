"""Генерация тестовых изображений (Pillow) — независимо от кода photo_chooser.images."""

import time
from fractions import Fraction
from pathlib import Path

import AVFoundation
import CoreMedia
import Foundation
import numpy as np
import Quartz
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


def write_mov(path: Path, frames: list[Image.Image], fps: int = 10) -> Path:
    """H.264 .mov из кадров Pillow через AVAssetWriter (как видеочасть Live Photo)."""
    w, h = frames[0].size
    url = Foundation.NSURL.fileURLWithPath_(str(path))
    writer, err = AVFoundation.AVAssetWriter.assetWriterWithURL_fileType_error_(
        url, AVFoundation.AVFileTypeQuickTimeMovie, None)
    assert writer is not None, err
    settings = {AVFoundation.AVVideoCodecKey: AVFoundation.AVVideoCodecTypeH264,
                AVFoundation.AVVideoWidthKey: w, AVFoundation.AVVideoHeightKey: h}
    video = AVFoundation.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
        AVFoundation.AVMediaTypeVideo, settings)
    attrs = {Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA,
             Quartz.kCVPixelBufferWidthKey: w, Quartz.kCVPixelBufferHeightKey: h}
    adaptor = AVFoundation.AVAssetWriterInputPixelBufferAdaptor\
        .assetWriterInputPixelBufferAdaptorWithAssetWriterInput_sourcePixelBufferAttributes_(video, attrs)
    writer.addInput_(video)
    writer.startWriting()
    writer.startSessionAtSourceTime_(CoreMedia.kCMTimeZero)
    for i, frame in enumerate(frames):
        while not video.isReadyForMoreMediaData():
            time.sleep(0.01)
        _, buf = Quartz.CVPixelBufferPoolCreatePixelBuffer(None, adaptor.pixelBufferPool(), None)
        Quartz.CVPixelBufferLockBaseAddress(buf, 0)
        stride = Quartz.CVPixelBufferGetBytesPerRow(buf)
        target = np.frombuffer(Quartz.CVPixelBufferGetBaseAddress(buf).as_buffer(stride * h), dtype=np.uint8)
        rgb = np.asarray(frame.convert("RGB"), dtype=np.uint8)
        bgra = np.concatenate([rgb[..., ::-1], np.full((h, w, 1), 255, np.uint8)], axis=2)
        target.reshape(h, stride)[:, : w * 4] = bgra.reshape(h, w * 4)
        Quartz.CVPixelBufferUnlockBaseAddress(buf, 0)
        assert adaptor.appendPixelBuffer_withPresentationTime_(buf, CoreMedia.CMTimeMake(i, fps)), writer.error()
    video.markAsFinished()
    done = []
    writer.finishWritingWithCompletionHandler_(lambda: done.append(True))
    deadline = time.monotonic() + 10
    while not done and time.monotonic() < deadline:
        time.sleep(0.02)
    assert writer.status() == AVFoundation.AVAssetWriterStatusCompleted, writer.error()
    return path
