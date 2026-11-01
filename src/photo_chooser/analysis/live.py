"""Выбор кадра Live Photo: ключевой снимок или кадр видеочасти, если он явно лучше (SPEC FR-5)."""

from pathlib import Path

import AVFoundation
import CoreMedia
import Foundation
import objc
import Quartz

from photo_chooser import images, vision
from photo_chooser.analysis.quality import sharpness

LIVE_FRAMES_PER_VIDEO = 15  # docs/spike-findings.md
FRAME_WEIGHTS = {"aesthetic": 0.5, "sharpness": 0.3, "face": 0.2}


def sample_frames(video: Path, n: int = LIVE_FRAMES_PER_VIDEO) -> list:
    """n равномерных кадров с учётом поворота видео и без допуска по времени."""
    asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(Foundation.NSURL.fileURLWithPath_(str(video)), None)
    gen = AVFoundation.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
    gen.setAppliesPreferredTrackTransform_(True)
    gen.setRequestedTimeToleranceBefore_(CoreMedia.kCMTimeZero)
    gen.setRequestedTimeToleranceAfter_(CoreMedia.kCMTimeZero)
    duration = CoreMedia.CMTimeGetSeconds(asset.duration())
    frames = []
    for i in range(n):
        seconds = duration * i / (n - 1) * 0.999 if n > 1 else 0.0  # последний кадр — чуть раньше конца
        result = gen.copyCGImageAtTime_actualTime_error_(CoreMedia.CMTimeMakeWithSeconds(seconds, 600), None, None)
        image = result[0] if isinstance(result, tuple) else result
        if image is not None:
            frames.append(image)
    return frames


def frame_scores(imgs: list, long_side: int) -> list[float]:
    """Score = 0.5·эстетика + 0.3·резкость/max + 0.2·лица на общей длинной стороне; нет компонента — веса нормируются."""
    side = min([long_side] + [max(Quartz.CGImageGetWidth(i), Quartz.CGImageGetHeight(i)) for i in imgs])
    scaled = [images.fit_long_side(i, side) for i in imgs]
    sharp = [sharpness(images.gray_array(i)) for i in scaled]
    top = max(sharp, default=0.0)
    scores = []
    for img, s in zip(scaled, sharp):
        with objc.autorelease_pool():
            v = vision.analyze(img)
        parts = {"aesthetic": v.aesthetic, "sharpness": s / top if top > 0 else 0.0, "face": v.face_quality}
        present = {k: x for k, x in parts.items() if x is not None}
        total = sum(FRAME_WEIGHTS[k] for k in present)
        scores.append(sum(FRAME_WEIGHTS[k] * x for k, x in present.items()) / total)
    return scores


def choose(key_score: float, frame_scores: list[float], margin: float) -> int | None:
    """Индекс кадра видео, если он лучше ключевого снимка не меньше чем на margin; иначе None (ключевой)."""
    if not frame_scores:
        return None
    best = max(range(len(frame_scores)), key=lambda i: frame_scores[i])
    return best if frame_scores[best] >= key_score + margin else None
