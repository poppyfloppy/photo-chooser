"""Локальные признаки снимка и локальная оценка качества."""

from pathlib import Path

import numpy as np

from photo_chooser import images, vision
from photo_chooser.models import LocalFeatures

QUALITY_WEIGHTS = {"aesthetic": 0.5, "sharpness": 0.35, "face": 0.15}
SHARPNESS_SCALE = 1e4  # шкала spike: junk_blur и замеры резкости в docs/spike-findings.md


def sharpness(gray: np.ndarray) -> float:
    """Дисперсия лапласиана 3×3 (в шкале spike, ×1e4)."""
    lap = -4 * gray[1:-1, 1:-1] + gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:]
    return float(lap.var() * SHARPNESS_SCALE)


def mean_luma(gray: np.ndarray) -> float:
    return float(gray.mean())


def compute_features(uuid: str, preview: Path, long_side: int = 1024) -> LocalFeatures:
    img = images.load_cgimage(preview, long_side)
    gray = images.gray_array(img)
    v = vision.analyze(img)
    return LocalFeatures(uuid=uuid, aesthetic=v.aesthetic, is_utility=v.is_utility, sharpness=sharpness(gray),
                         mean_luma=mean_luma(gray), face_quality=v.face_quality,
                         feature_print=v.feature_print.astype(np.float32).tobytes())


def percentile_ranks(values: list[float]) -> list[float]:
    """Ранг каждого значения в 0..1 (минимум 0, максимум 1, равные — средний ранг; одно значение — 0.5)."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def local_quality(f: LocalFeatures, sharpness_rank: float) -> float:
    parts = {"aesthetic": f.aesthetic, "sharpness": sharpness_rank, "face": f.face_quality}
    present = {k: v for k, v in parts.items() if v is not None}
    total = sum(QUALITY_WEIGHTS[k] for k in present)
    return sum(QUALITY_WEIGHTS[k] * v for k, v in present.items()) / total if total else 0.0


def feature_distance(a: bytes, b: bytes) -> float:
    va, vb = np.frombuffer(a, dtype=np.float32), np.frombuffer(b, dtype=np.float32)
    if va.shape != vb.shape:
        raise ValueError("отпечатки разной длины")
    return float(np.linalg.norm(va - vb))
