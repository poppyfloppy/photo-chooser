"""Доменные модели: снимок альбома, локальные признаки и оценки обеих облачных ступеней."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Asset:
    uuid: str  # localIdentifier PhotoKit
    filename: str
    taken_at: datetime  # aware, UTC
    width: int
    height: int
    is_live: bool
    is_screenshot: bool

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width


@dataclass(frozen=True)
class AlbumRef:
    uuid: str
    title: str
    count: int


@dataclass(frozen=True)
class LocalFeatures:
    uuid: str
    aesthetic: float | None
    is_utility: bool
    sharpness: float
    mean_luma: float
    face_quality: float | None
    feature_print: bytes  # float32-байты вектора отпечатка Vision


@dataclass(frozen=True)
class Screening:
    """Результат первой ступени: релевантность по ключам тем (Theme.key()) и черновые оценки."""

    uuid: str
    relevance: dict[str, int] = field(hash=False)
    feed_score: int
    stories_score: int


@dataclass(frozen=True)
class Judgment:
    """Результат второй ступени по одной теме."""

    uuid: str
    matches_theme: bool
    feed_score: int
    stories_score: int
    reason: str
