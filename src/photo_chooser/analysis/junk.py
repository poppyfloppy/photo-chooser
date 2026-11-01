"""Отсев явного брака. Порог мягкий: остальное оценивают облачные ступени (SPEC FR-3 п.3).

Флаг Vision «утилитарный кадр» не используется: на Egypt.26 он помечал росписи гробниц, рельефы и экспонаты
музеев (197 из 2522), а не документы и чеки. Таблички и экраны отсекают облачные ступени.
"""

from photo_chooser.config import Thresholds
from photo_chooser.models import Asset, LocalFeatures


def junk_reason(asset: Asset, f: LocalFeatures, th: Thresholds) -> str | None:
    if asset.is_screenshot:
        return "скриншот"
    if f.sharpness < th.junk_blur:
        return "смаз"
    if f.mean_luma < th.junk_dark_luma:
        return "слишком тёмный"
    if f.mean_luma > th.junk_bright_luma:
        return "пересвечен"
    return None
