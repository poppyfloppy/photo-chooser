"""Обёртки над Apple Vision: эстетика и флаг «утилитарный», качество лиц, визуальный отпечаток."""

from dataclasses import dataclass

import numpy as np
import Vision

FEATURE_ELEMENT_FLOAT = 1  # VNElementTypeFloat


@dataclass(frozen=True)
class VisionResult:
    aesthetic: float | None  # (overallScore + 1) / 2, шкала 0..1
    is_utility: bool
    face_quality: float | None  # лучшее лицо в кадре, None — лиц нет
    feature_print: np.ndarray  # float32


def analyze(img) -> VisionResult:
    aesthetics = Vision.VNCalculateImageAestheticsScoresRequest.alloc().init()
    faces = Vision.VNDetectFaceCaptureQualityRequest.alloc().init()
    fprint = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, {})
    ok, err = handler.performRequests_error_([aesthetics, faces, fprint], None)
    if not ok:
        raise RuntimeError(f"Vision: {err}")
    a = aesthetics.results()[0] if aesthetics.results() else None
    face_q = [float(f.faceCaptureQuality()) for f in (faces.results() or []) if f.faceCaptureQuality() is not None]
    fp = fprint.results()[0]
    # computeDistance через pyobjc не возвращает расстояние (spike) — сравниваем векторы data() по L2.
    dtype = np.float32 if fp.elementType() == FEATURE_ELEMENT_FLOAT else np.float64
    vec = np.frombuffer(bytes(fp.data()), dtype=dtype, count=fp.elementCount()).astype(np.float32)
    return VisionResult(
        aesthetic=(float(a.overallScore()) + 1) / 2 if a else None,
        is_utility=bool(a.isUtility()) if a else False,
        face_quality=max(face_q) if face_q else None,
        feature_print=vec,
    )
