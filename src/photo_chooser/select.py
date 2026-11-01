"""Чистая логика отбора: интервал темы, кандидаты после первой ступени, пулы ленты и stories (SPEC FR-3, FR-4)."""

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from photo_chooser.analysis.quality import percentile_ranks
from photo_chooser.config import Theme, Thresholds
from photo_chooser.models import Asset, Judgment, LocalFeatures, Screening


@dataclass(frozen=True)
class Scored:
    asset: Asset
    score: float
    reason: str
    crop: str = ""  # совет по кадрированию для этого списка: 4:5 в ленте, 9:16 в stories


@dataclass
class Pools:
    """Упорядоченные и не обрезанные пулы; один снимок может быть в обоих — обрезку делает export.assemble."""

    feed: list[Scored]
    stories: list[Scored]


def in_interval(asset: Asset, theme: Theme, tz: ZoneInfo | None) -> bool:
    """Время съёмки в поясе поездки сравнивается с границами темы включительно (FR-1.3)."""
    if not theme.has_interval:
        return True
    if tz is None:
        raise ValueError(f"для интервала темы «{theme.name}» нужен часовой пояс поездки")
    local = asset.taken_at.astimezone(tz).replace(tzinfo=None)
    return theme.start <= local <= theme.end


def screen_candidates(assets: list[Asset], screenings: dict[str, Screening], quality: dict[str, float],
                      theme: Theme, min_relevance: int, limit: int) -> list[Asset]:
    key = theme.screen_key()

    def relevance(a: Asset) -> int:
        s = screenings.get(a.uuid)
        return s.relevance.get(key, 0) if s else 0

    def rank(a: Asset):
        s = screenings.get(a.uuid)
        draft = max(s.feed_score, s.stories_score) if s else 0
        return relevance(a), draft, quality.get(a.uuid, 0.0)

    pool = assets if theme.interval_only else [a for a in assets if relevance(a) >= min_relevance]
    return sorted(pool, key=rank, reverse=True)[:limit]


def _verticals_first(items: list[Scored], key) -> list[Scored]:
    return (sorted((s for s in items if s.asset.is_vertical), key=key)
            + sorted((s for s in items if not s.asset.is_vertical), key=key))


def pools_from_judgments(cands: list[Asset], judgments: dict[str, Judgment], quality: dict[str, float],
                         th: Thresholds, interval_only: bool) -> Pools:
    fit = [(a, judgments[a.uuid]) for a in cands
           if a.uuid in judgments and (interval_only or judgments[a.uuid].matches_theme)]
    q = lambda s: quality.get(s.asset.uuid, 0.0)  # noqa: E731
    feed = sorted((Scored(a, j.feed_score, j.reason, j.crop_feed) for a, j in fit if j.feed_score >= th.min_feed_score),
                  key=lambda s: (-s.score, -q(s)))
    stories = [Scored(a, j.stories_score, j.reason, j.crop_stories) for a, j in fit
               if j.stories_score >= th.min_stories_score]
    return Pools(feed=feed, stories=_verticals_first(stories, key=lambda s: (-s.score, -q(s))))


def _local_reason(f: LocalFeatures | None, sharp_rank: float) -> str:
    parts = []
    if f is not None and f.aesthetic is not None:
        parts.append(f"эстетика {f.aesthetic:.2f}")
    parts.append(f"резкость {sharp_rank:.2f}")
    if f is not None and f.face_quality is not None:
        parts.append(f"лица {f.face_quality:.2f}")
    return " · ".join(parts)


def pools_local(cands: list[Asset], quality: dict[str, float], feats: dict[str, LocalFeatures]) -> Pools:
    """Режим без облака (FR-10.2): только локальная оценка; резкость в причине — ранг среди кандидатов."""
    ranks = dict(zip((a.uuid for a in cands),
                     percentile_ranks([feats[a.uuid].sharpness if a.uuid in feats else 0.0 for a in cands])))
    scored = [Scored(a, quality.get(a.uuid, 0.0), _local_reason(feats.get(a.uuid), ranks[a.uuid])) for a in cands]
    feed = sorted(scored, key=lambda s: -s.score)
    return Pools(feed=feed, stories=_verticals_first(scored, key=lambda s: -s.score))
