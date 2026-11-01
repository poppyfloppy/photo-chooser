"""Spike (одноразовый): кадры видеочасти Live Photo (Task 1, Step 6).

Извлекает N равномерных кадров из .MOV через AVAssetImageGenerator, сохраняет их
в spike/out/live_frames/ и печатает разрешение, время и резкость рядом с ключевым снимком.

Запуск: uv run python spike/live_probe.py spike/out/export/live/IMG_7507.MOV spike/out/export/live/IMG_7507.HEIC
"""

import sys
import time
from pathlib import Path

import AVFoundation
import CoreMedia
import Foundation
import numpy as np
import Quartz
from PIL import Image

N = 15
OUT = Path(__file__).parent / "out" / "live_frames"


def save_png(cgimage, path):
    url = Foundation.NSURL.fileURLWithPath_(str(path))
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)  # без потерь
    Quartz.CGImageDestinationAddImage(dest, cgimage, None)
    Quartz.CGImageDestinationFinalize(dest)


def sharpness(path, side):
    with Image.open(path) as im:
        im.thumbnail((side, side))
        g = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var() * 1e4)


def main(video, key):
    OUT.mkdir(parents=True, exist_ok=True)
    asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(Foundation.NSURL.fileURLWithPath_(video), None)
    gen = AVFoundation.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
    gen.setAppliesPreferredTrackTransform_(True)
    gen.setRequestedTimeToleranceBefore_(CoreMedia.kCMTimeZero)
    gen.setRequestedTimeToleranceAfter_(CoreMedia.kCMTimeZero)
    duration = CoreMedia.CMTimeGetSeconds(asset.duration())
    print(f"duration: {duration:.2f} s")

    started = time.monotonic()
    frames = []
    for i in range(N):
        t = CoreMedia.CMTimeMakeWithSeconds(duration * i / (N - 1) * 0.999, 600)
        result = gen.copyCGImageAtTime_actualTime_error_(t, None, None)
        image = result[0] if isinstance(result, tuple) else result
        if image is None:
            print(f"frame {i}: failed {result}")
            continue
        path = OUT / f"frame_{i:02d}.png"
        save_png(image, path)
        frames.append((path, Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)))
    print(f"extracted {len(frames)} frames in {time.monotonic() - started:.2f} s")

    source = Quartz.CGImageSourceCreateWithURL(Foundation.NSURL.fileURLWithPath_(key), None)
    key_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    key = OUT / "key.png"  # Pillow не читает HEIC, ImageIO читает
    save_png(key_image, key)
    with Image.open(key) as im:
        key_size = im.size
    side = max(frames[0][1], frames[0][2]) if frames else 1024
    print(f"key photo: {key_size}, frames: {frames[0][1]}x{frames[0][2]}" if frames else "no frames")
    print(f"sharpness at long side {side}: key={sharpness(key, side):.1f}")
    for path, w, h in frames:
        print(f"  {path.name}: {sharpness(path, side):.1f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
