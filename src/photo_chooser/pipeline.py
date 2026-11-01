"""Оркестрация запуска: альбом → отсев → первая ступень → по каждой теме вторая ступень, раскладка, экспорт, отчёт."""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
import objc

from photo_chooser import images
from photo_chooser.analysis.dedup import cluster, representatives
from photo_chooser.analysis.junk import junk_reason
from photo_chooser.analysis.quality import compute_features, local_quality, percentile_ranks
from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig, Theme, TripConfig, themes_key
from photo_chooser.export import Assembled, assemble, ensure_outside_library, export_asset, new_run_dir, safe_name
from photo_chooser.judge.cost import check_budget, estimate_run_usd, price_for
from photo_chooser.judge.prompts import SCREEN_PROMPT_VERSION
from photo_chooser.judge.stages import (
    JudgeRunner,
    ScreenRunner,
    StageOutcome,
    check_credentials,
    judge_prompt_version,
    run_judge,
    run_screen,
)
from photo_chooser.library.base import PhotosLibrary
from photo_chooser.models import AlbumRef, Asset, LocalFeatures, Screening
from photo_chooser.report import ReportHeader, ThemeStats, write_report
from photo_chooser.select import Pools, in_interval, pools_from_judgments, pools_local, screen_candidates
from photo_chooser.style import StyleContext, load_style

PROGRESS_EVERY = 50
NO_STYLE_WARNING = "Профиль стиля не найден — выполните `photo-chooser style`"
NO_CLOUD_QUERY_WARNING = ("Режим без облака: запрос темы не учитывается — кандидаты: все снимки {scope} после "
                          "отсева, ранжирование только по локальной оценке")


@dataclass
class RunOptions:
    dry_run: bool = False
    no_cloud: bool = False
    model: str | None = None  # модель второй ступени на этот запуск
    max_cost: float | None = None  # лимит бюджета альбома на этот запуск


@dataclass
class AlbumState:
    album: AlbumRef
    assets: list[Asset]
    previews: dict[str, Path]
    features: dict[str, LocalFeatures]
    quality: dict[str, float]
    junk: dict[str, str]
    no_preview: list[str]
    duplicates_collapsed: int
    pool: list[Asset]  # после отсева мусора и дублей, по времени съёмки


@dataclass
class ThemeOutcome:
    theme: Theme
    report_path: Path | None
    assembled: Assembled | None
    error: str | None


@dataclass
class RunResult:
    run_dir: Path | None
    themes: list[ThemeOutcome]
    estimate_usd: float
    theme_counts: dict[str, int] = field(default_factory=dict)  # снимков темы после отсева и интервала
    album_spend_usd: float = 0.0


def _no_log(message: str) -> None:
    pass


def prepare_album(library: PhotosLibrary, cache: Cache, album: AlbumRef, cfg: AppConfig,
                  log: Callable[[str], None] = _no_log) -> AlbumState:
    long_side = cfg.cloud.preview_long_side_px
    assets = sorted(library.assets(album), key=lambda a: a.taken_at)
    previews: dict[str, Path] = {}
    features: dict[str, LocalFeatures] = {}
    no_preview: list[str] = []
    for i, a in enumerate(assets, 1):
        if i % PROGRESS_EVERY == 0 or i == len(assets):
            log(f"Превью и локальные признаки: {i}/{len(assets)}")
        with objc.autorelease_pool():  # без пула объекты PhotoKit/Vision/ImageIO копятся до конца цикла
            path = library.preview(a, long_side)
            f = cache.get_features(a.uuid) if path is not None else None
            if path is not None and f is None:
                try:
                    f = compute_features(a.uuid, path, long_side)
                    cache.put_features(f)
                except Exception:  # noqa: BLE001 — нечитаемое превью равносильно его отсутствию
                    f = None
        if f is None:
            no_preview.append(a.uuid)
            continue
        previews[a.uuid], features[a.uuid] = path, f

    analysed = [a for a in assets if a.uuid in features]
    junk = {a.uuid: r for a in analysed if (r := junk_reason(a, features[a.uuid], cfg.thresholds))}
    kept = [a for a in analysed if a.uuid not in junk]
    ranks = percentile_ranks([features[a.uuid].sharpness for a in kept])
    quality = {a.uuid: local_quality(features[a.uuid], r) for a, r in zip(kept, ranks)}
    clusters = cluster([(a, features[a.uuid]) for a in kept], cfg.thresholds)
    reps = set(representatives(clusters, quality))
    pool = [a for a in kept if a.uuid in reps]
    return AlbumState(album=album, assets=assets, previews=previews, features=features, quality=quality, junk=junk,
                      no_preview=no_preview, duplicates_collapsed=len(kept) - len(pool), pool=pool)


def _screen_scope(trip: TripConfig, theme_assets: dict[str, list[Asset]], pool: list[Asset]) -> list[Asset]:
    """Первая ступень идёт по всему пулу (FR-3 п.5) — черновые оценки нужны и темам-интервалам. Если ни у одной
    темы с query нет снимков в интервале, первая ступень не нужна."""
    return list(pool) if any(theme_assets[t.key()] for t in trip.query_themes) else []


def _estimate(trip: TripConfig, cfg: AppConfig, state: AlbumState, theme_assets: dict[str, list[Asset]],
              screen_scope: list[Asset], cache: Cache, screen_model: str, judge_model: str,
              style: StyleContext | None) -> float:
    price_for(cfg.cloud, screen_model)
    price_for(cfg.cloud, judge_model)
    key = themes_key(state.album.title, trip.query_themes)
    screenings: dict[str, Screening] = {}
    n_screen = 0
    for a in screen_scope:
        s = cache.get_screening(a.uuid, key, screen_model, SCREEN_PROMPT_VERSION)
        if s is None:
            n_screen += 1
        else:
            screenings[a.uuid] = s
    n_judge = []
    for t in trip.themes:
        assets = theme_assets[t.key()]
        if n_screen and not t.interval_only:
            n_judge.append(min(len(assets), cfg.cloud.candidates_per_theme))  # кандидаты ещё неизвестны
            continue
        cands = screen_candidates(assets, screenings, state.quality, t, cfg.thresholds.min_theme_relevance,
                                  cfg.cloud.candidates_per_theme)
        n_judge.append(sum(cache.get_judgment(a.uuid, t.key(), judge_model, judge_prompt_version(style)) is None
                           for a in cands))
    return estimate_run_usd(n_screen, n_judge, cfg.cloud, screen_model, judge_model,
                            judge_extra_tokens=style.extra_tokens if style else 0)


def _theme_dir(run_dir: Path, theme: Theme) -> Path:
    base = safe_name(theme.name)
    path, n = run_dir / base, 1
    while path.exists():
        n += 1
        path = run_dir / f"{base}_{n}"
    path.mkdir()
    for list_name in ("feed", "stories"):
        (path / list_name).mkdir()
    return path


def run(trip: TripConfig, cfg: AppConfig, opts: RunOptions, library: PhotosLibrary, cache: Cache,
        client_factory: Callable[[], object], now: datetime, log: Callable[[str], None] = _no_log,
        style_path: Path | None = None) -> RunResult:
    """style_path — профиль стиля (FR-11); None — стиль выключен."""
    ensure_outside_library(cfg.output_dir)
    client = None
    if not opts.no_cloud and not opts.dry_run:
        client = client_factory()
        check_credentials(client)  # до долгого чтения превью
    album = library.find_album(trip.album)
    log(f"Альбом «{album.title}»: {album.count} фото")
    state = prepare_album(library, cache, album, cfg, log)
    theme_assets = {t.key(): [a for a in state.pool if in_interval(a, t, trip.tz)] for t in trip.themes}
    counts = {t.name: len(theme_assets[t.key()]) for t in trip.themes}
    screen_model, judge_model = cfg.cloud.screen_model, opts.model or cfg.cloud.model
    screen_scope = _screen_scope(trip, theme_assets, state.pool)

    style: StyleContext | None = None
    style_warnings: list[str] = []
    if not opts.no_cloud and style_path is not None:
        style = load_style(style_path, library, cfg)
        style_warnings = style.warnings if style else [NO_STYLE_WARNING]

    estimate = 0.0
    if not opts.no_cloud:
        estimate = _estimate(trip, cfg, state, theme_assets, screen_scope, cache, screen_model, judge_model,
                             style)
        limit = opts.max_cost if opts.max_cost is not None else cfg.cloud.max_cost_usd_per_album
        check_budget(cache.album_spend(album.uuid), estimate, limit)
    if opts.dry_run:
        return RunResult(run_dir=None, themes=[ThemeOutcome(t, None, None, None) for t in trip.themes],
                         estimate_usd=estimate, theme_counts=counts, album_spend_usd=cache.album_spend(album.uuid))

    long_side = cfg.cloud.preview_long_side_px

    def preview(a: Asset) -> bytes:
        return images.jpeg_bytes(state.previews[a.uuid], long_side)

    screenings: dict[str, Screening] = {}
    screen: StageOutcome | None = None
    if not opts.no_cloud and trip.query_themes and screen_scope:
        log(f"Первая ступень ({screen_model}): {len(screen_scope)} снимков")
        runner = ScreenRunner(client, screen_model, cfg.cloud.effort, preview)
        # Ошибка здесь останавливает запуск: без первой ступени темы с query не обработать.
        screen = run_screen(runner, cache, album.title, album.uuid, trip.query_themes, screen_scope, cfg.cloud)
        screenings = screen.results

    run_dir = new_run_dir(cfg.output_dir, album.title, now)
    judge_runner = None
    outcomes: list[ThemeOutcome] = []
    with tempfile.TemporaryDirectory(prefix="photo-chooser-") as tmp:
        for t in trip.themes:
            log(f"Тема «{t.name}»")
            theme_dir = _theme_dir(run_dir, t)
            assets = theme_assets[t.key()]
            warnings: list[str] = list(style_warnings)
            judge: StageOutcome | None = None
            error = None
            th = cfg.thresholds
            # «признано подходящими» — только для тем с query и только когда первая ступень работала
            matched = None if opts.no_cloud or t.interval_only else sum(
                1 for a in assets if a.uuid in screenings and screenings[a.uuid].relevance.get(t.screen_key(), 0)
                >= th.min_theme_relevance)
            if opts.no_cloud:
                if t.query:
                    warnings.append(NO_CLOUD_QUERY_WARNING.format(scope="интервала" if t.has_interval else "альбома"))
                cands = assets
                pools = pools_local(cands, state.quality, state.features)
            else:
                cands = screen_candidates(assets, screenings, state.quality, t, th.min_theme_relevance,
                                          cfg.cloud.candidates_per_theme)
                pools = Pools(feed=[], stories=[])
                if cands:
                    try:
                        if judge_runner is None:
                            judge_runner = JudgeRunner(client, judge_model, cfg.cloud.effort, preview, style=style)
                        order = {a.uuid: _draft(screenings.get(a.uuid)) + state.quality.get(a.uuid, 0.0) / 10
                                 for a in cands}
                        judge = run_judge(judge_runner, cache, t, cands, order, cfg.cloud, album.uuid)
                        pools = pools_from_judgments(cands, judge.results, state.quality, th, t.interval_only)
                    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
                        raise  # ключ не подходит — другие темы упадут так же
                    except anthropic.APIError as e:
                        error = f"{type(e).__name__}: {e}"
                        warnings.append(f"Тема не обработана: ошибка Claude API ({error}). Повторный запуск "
                                        "продолжит с уже оплаченных оценок.")
            if judge and judge.refused_groups:
                warnings.append(f"Модель отказалась оценивать групп: {judge.refused_groups} — эти кадры не оценены")

            assembled = assemble(pools, cfg.shortlist.feed, cfg.shortlist.stories,
                                 lambda s, ln, r: export_asset(library, s, ln, r, theme_dir, Path(tmp), cfg))
            header = ReportHeader(
                screen_model=None if opts.no_cloud or screen is None else screen_model,
                model=None if opts.no_cloud else judge_model,
                effort=None if opts.no_cloud else cfg.cloud.effort,
                prompt_versions=(None if opts.no_cloud
                                 else f"{SCREEN_PROMPT_VERSION}, {judge_prompt_version(style)}"),
                run_at=now, warnings=warnings, album=album.title,
                style=None if style is None else _style_line(style))
            stats = ThemeStats(
                read=len(state.assets), no_preview=len(state.no_preview), junk=len(state.junk),
                duplicates_collapsed=state.duplicates_collapsed, in_interval=len(assets),
                sent_to_screen=len(screen_scope) if screen else 0, matched=matched,
                sent_to_judge=len(cands) if not opts.no_cloud else 0,
                from_cache=judge.from_cache if judge else 0, unjudged=len(judge.unjudged) if judge else 0,
                usd_screen=screen.usd if screen else 0.0, usd_judge=judge.usd if judge else 0.0,
                usd_album_total=cache.album_spend(album.uuid),
                skipped=[(a.filename, "нет превью") for a in state.assets if a.uuid in set(state.no_preview)])
            report_path = theme_dir / "report.md"
            write_report(report_path, t, header, assembled, stats)
            outcomes.append(ThemeOutcome(t, report_path, assembled, error))
    return RunResult(run_dir=run_dir, themes=outcomes, estimate_usd=estimate, theme_counts=counts,
                     album_spend_usd=cache.album_spend(album.uuid))


def _style_line(style: StyleContext) -> str:
    kinds = [kind for kind, _ in style.references]
    return (f"style.md от {style.created_at:%Y-%m-%d}, отпечаток {style.fingerprint}, скриншотов: лента "
            f"{kinds.count('feed')}, stories {kinds.count('stories')}")


def _draft(s: Screening | None) -> float:
    return float(max(s.feed_score, s.stories_score)) if s else 0.0
