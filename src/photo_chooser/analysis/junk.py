"""Отсев явного брака. Порог мягкий: остальное оценивают облачные ступени (SPEC FR-3 п.3)."""

from photo_chooser.config import Thresholds
from photo_chooser.models import Asset, LocalFeatures


def junk_reason(asset: Asset, f: LocalFeatures, th: Thresholds) -> str | None:
    if asset.is_screenshot:
        return "скриншот"
    if f.is_utility:
        return "утилитарный кадр"
    if f.sharpness < th.junk_blur:
        return "смаз"
    if f.mean_luma < th.junk_dark_luma:
        return "слишком тёмный"
    if f.mean_luma > th.junk_bright_luma:
        return "пересвечен"
    return None
