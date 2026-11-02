"""Чистая логика отбора: интервал темы, кандидаты ступеней, проверка ответа сборки и раскладка постов (SPEC FR-3, FR-4)."""

import re
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
        return relevance(a), s.score if s else 0, quality.get(a.uuid, 0.0)

    pool = assets if theme.interval_only else [a for a in assets if relevance(a) >= min_relevance]
    return sorted(pool, key=rank, reverse=True)[:limit]


def _local_reason(f: LocalFeatures | None, sharp_rank: float) -> str:
    parts = []
    if f is not None and f.aesthetic is not None:
        parts.append(f"эстетика {f.aesthetic:.2f}")
    parts.append(f"резкость {sharp_rank:.2f}")
    if f is not None and f.face_quality is not None:
        parts.append(f"лица {f.face_quality:.2f}")
    return " · ".join(parts)


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
    """Что отчёт и сборка знают о кадре: сила, пометка «интересное», причина и советы по кадрированию."""

    score: float
    interesting: bool
    reason: str
    crop_feed: str
    crop_stories: str


def compose_candidates(cands: list[Asset], judgments: dict[str, Judgment], quality: dict[str, float],
                       th: Thresholds, interval_only: bool, limit: int) -> list[Asset]:
    """FR-4.1: подходящие к теме с силой не ниже min_score или с пометкой «интересное», по силе, затем по качеству."""
    fit = [a for a in cands if a.uuid in judgments and (interval_only or judgments[a.uuid].matches_theme)
           and (judgments[a.uuid].score >= th.min_score or judgments[a.uuid].interesting)]
    return sorted(fit, key=lambda a: (judgments[a.uuid].score, quality.get(a.uuid, 0.0)), reverse=True)[:limit]


def _norm_id(raw: str) -> str:
    """«id: C07 (horizontal)», «c7», «c007» → «c07»: модель может повторить подпись или изменить регистр."""
    token = raw.strip().lower().removeprefix("id:").strip().split(" ")[0] if raw.strip() else ""
    match = re.fullmatch(r"c0*(\d+)", token)
    return f"c{int(match.group(1)):02d}" if match else token


def validate_composition(out: "ComposeOutput", ids: dict[str, Asset], limits: PostsCfg) -> Posts:
    """FR-4.3: неизвестные id отбрасываются, каждый кадр — один раз, лимиты. Сначала занимаются слайды всех
    каруселей, затем их запасные, затем stories: запасной ракурс не отнимает кадр у слайда."""
    used: set[str] = set()

    def take(raw_id: str) -> Asset | None:
        a = ids.get(_norm_id(raw_id))
        if a is None or a.uuid in used:
            return None
        used.add(a.uuid)
        return a

    planned: list[tuple[str, list[tuple[Asset, list[str]]]]] = []
    for raw in out.carousels:
        if len(planned) >= limits.carousels:
            break
        slides: list[tuple[Asset, list[str]]] = []
        for raw_slide in raw.slides:
            if len(slides) >= limits.carousel_size:
                break
            a = take(raw_slide.id)
            if a is not None:
                slides.append((a, raw_slide.alternatives))
        if slides:
            planned.append((raw.title.strip() or f"Карусель {len(planned) + 1}", slides))
    carousels: list[Carousel] = []
    for title, slides in planned:
        built: list[Slide] = []
        for a, raw_alternatives in slides:
            alternatives: list[Asset] = []
            for raw_id in raw_alternatives:
                if len(alternatives) >= MAX_ALTERNATIVES:
                    break
                alt = take(raw_id)
                if alt is not None:
                    alternatives.append(alt)
            built.append(Slide(a, alternatives))
        carousels.append(Carousel(title, built))
    stories: list[StoryPick] = []
    for raw in out.stories:
        if len(stories) >= limits.stories:
            break
        a = take(raw.id)
        if a is not None:
            stories.append(StoryPick(a, raw.kind, raw.hint.strip() if raw.kind == "interesting" else ""))
    return Posts(carousels, stories)


def fallback_posts(cands: list[Asset], judgments: dict[str, Judgment], quality: dict[str, float],
                   th: Thresholds, limits: PostsCfg, interval_only: bool = False) -> Posts:
    """FR-4.4: сборка не удалась — одна карусель из сильнейших кадров, stories — следующие по силе; все не ниже
    min_score. Кадры, которые вторая ступень сочла не по теме, исключаются (FR-3 п.8)."""
    judged = [a for a in cands if a.uuid in judgments and (interval_only or judgments[a.uuid].matches_theme)]
    strong = sorted((a for a in judged if judgments[a.uuid].score >= th.min_score),
                    key=lambda a: (judgments[a.uuid].score, quality.get(a.uuid, 0.0)), reverse=True)
    feed = strong[:limits.carousel_size]
    stories = strong[limits.carousel_size:limits.carousel_size + limits.stories]
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
    return {u: ShotFacts(j.score, j.interesting, j.reason, j.crop_feed, j.crop_stories) for u, j in judgments.items()}


def local_facts(cands: list[Asset], quality: dict[str, float],
                feats: dict[str, LocalFeatures]) -> dict[str, ShotFacts]:
    ranks = dict(zip((a.uuid for a in cands),
                     percentile_ranks([feats[a.uuid].sharpness if a.uuid in feats else 0.0 for a in cands])))
    return {a.uuid: ShotFacts(quality.get(a.uuid, 0.0), False, _local_reason(feats.get(a.uuid), ranks[a.uuid]), "", "")
            for a in cands}
