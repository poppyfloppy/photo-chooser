"""Чистая логика отбора: интервал темы, кандидаты после первой ступени, пулы ленты и stories (SPEC FR-3, FR-4)."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from photo_chooser.analysis.quality import percentile_ranks
from photo_chooser.config import PostsCfg, Theme, Thresholds
from photo_chooser.models import Asset, Judgment, LocalFeatures, Screening

if TYPE_CHECKING:
    from photo_chooser.judge.stages import ComposeOutput

MAX_ALTERNATIVES = 2
FALLBACK_TITLE = "Лучшее по оценкам"
LOCAL_TITLE = "Лучшее по локальной оценке"


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



# --- посты: карусели ленты и stories (SPEC FR-4) ----------------------------------------------


@dataclass
class Slide:
    asset: Asset
    alternatives: list[Asset] = field(default_factory=list)


@dataclass
class Carousel:
    title: str
    slides: list[Slide]


@dataclass(frozen=True)
class StoryPick:
    asset: Asset
    kind: str  # "best" | "interesting"
    hint: str  # что пояснить в тексте; у "best" пусто


@dataclass
class Posts:
    carousels: list[Carousel]
    stories: list[StoryPick]


@dataclass(frozen=True)
class ShotFacts:
    """Что отчёт пишет о кадре: обе оценки, причина и советы по кадрированию."""

    feed_score: float
    stories_score: float
    reason: str
    crop_feed: str
    crop_stories: str


def compose_candidates(cands: list[Asset], judgments: dict[str, Judgment], quality: dict[str, float],
                       th: Thresholds, interval_only: bool, limit: int) -> list[Asset]:
    """FR-4.1: подходящие к теме и прошедшие хотя бы один порог, по большей из двух оценок, затем по качеству."""
    fit = [a for a in cands if a.uuid in judgments and (interval_only or judgments[a.uuid].matches_theme)
           and (judgments[a.uuid].feed_score >= th.min_feed_score
                or judgments[a.uuid].stories_score >= th.min_stories_score)]
    key = lambda a: (max(judgments[a.uuid].feed_score, judgments[a.uuid].stories_score),  # noqa: E731
                     quality.get(a.uuid, 0.0))
    return sorted(fit, key=key, reverse=True)[:limit]


def validate_composition(out: "ComposeOutput", ids: dict[str, Asset], limits: PostsCfg) -> Posts:
    """FR-4.3: неизвестные id отбрасываются, каждый кадр — один раз (слайды, их запасные, затем stories), лимиты."""
    used: set[str] = set()

    def take(raw_id: str) -> Asset | None:
        a = ids.get(raw_id.strip().split(" ")[0])
        if a is None or a.uuid in used:
            return None
        used.add(a.uuid)
        return a

    carousels: list[Carousel] = []
    for raw in out.carousels:
        if len(carousels) >= limits.carousels:
            break
        slides: list[Slide] = []
        for raw_slide in raw.slides:
            if len(slides) >= limits.carousel_size:
                break
            a = take(raw_slide.id)
            if a is None:
                continue
            alternatives = [alt for alt in (take(x) for x in raw_slide.alternatives[:MAX_ALTERNATIVES]) if alt]
            slides.append(Slide(a, alternatives))
        if slides:
            carousels.append(Carousel(raw.title.strip() or f"Карусель {len(carousels) + 1}", slides))
    stories: list[StoryPick] = []
    for raw in out.stories:
        if len(stories) >= limits.stories:
            break
        a = take(raw.id)
        if a is not None:
            stories.append(StoryPick(a, raw.kind, raw.hint.strip() if raw.kind == "interesting" else ""))
    return Posts(carousels, stories)


def fallback_posts(cands: list[Asset], judgments: dict[str, Judgment], quality: dict[str, float],
                   th: Thresholds, limits: PostsCfg) -> Posts:
    """FR-4.4: сборка не удалась — одна карусель лучших по оценке ленты, stories — следующие по оценке stories."""
    judged = [a for a in cands if a.uuid in judgments]
    q = lambda a: quality.get(a.uuid, 0.0)  # noqa: E731
    feed = sorted((a for a in judged if judgments[a.uuid].feed_score >= th.min_feed_score),
                  key=lambda a: (judgments[a.uuid].feed_score, q(a)), reverse=True)[:limits.carousel_size]
    taken = {a.uuid for a in feed}
    rest = [a for a in judged if a.uuid not in taken and judgments[a.uuid].stories_score >= th.min_stories_score]
    stories = sorted(rest, key=lambda a: (judgments[a.uuid].stories_score, q(a)), reverse=True)[:limits.stories]
    carousels = [Carousel(FALLBACK_TITLE, [Slide(a) for a in feed])] if feed and limits.carousels else []
    return Posts(carousels, [StoryPick(a, "best", "") for a in stories])


def local_posts(cands: list[Asset], quality: dict[str, float], feats: dict[str, LocalFeatures],
                limits: PostsCfg) -> Posts:
    """FR-10.2: без облака — карусель из лучших по локальной оценке, stories — следующие."""
    ranked = sorted(cands, key=lambda a: quality.get(a.uuid, 0.0), reverse=True)
    feed, stories = ranked[:limits.carousel_size], ranked[limits.carousel_size:limits.carousel_size + limits.stories]
    carousels = [Carousel(LOCAL_TITLE, [Slide(a) for a in feed])] if feed and limits.carousels else []
    return Posts(carousels, [StoryPick(a, "best", "") for a in stories])


def shot_facts(judgments: dict[str, Judgment]) -> dict[str, ShotFacts]:
    return {u: ShotFacts(j.feed_score, j.stories_score, j.reason, j.crop_feed, j.crop_stories)
            for u, j in judgments.items()}


def local_facts(cands: list[Asset], quality: dict[str, float],
                feats: dict[str, LocalFeatures]) -> dict[str, ShotFacts]:
    ranks = dict(zip((a.uuid for a in cands),
                     percentile_ranks([feats[a.uuid].sharpness if a.uuid in feats else 0.0 for a in cands])))
    return {a.uuid: ShotFacts(quality.get(a.uuid, 0.0), quality.get(a.uuid, 0.0),
                              _local_reason(feats.get(a.uuid), ranks[a.uuid]), "", "") for a in cands}
