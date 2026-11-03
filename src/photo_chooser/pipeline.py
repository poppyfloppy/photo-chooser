"""Оркестрация запуска: альбом → отсев → первая ступень → по каждой теме вторая ступень, раскладка, экспорт, отчёт."""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import objc

from photo_chooser import images
from photo_chooser.analysis.dedup import cluster, representatives
from photo_chooser.analysis.junk import junk_reason
from photo_chooser.analysis.quality import compute_features, local_quality, percentile_ranks
from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig, Theme, TripConfig, themes_key
from photo_chooser.export import ExportedPosts, ensure_outside_library, export_posts, new_run_dir, safe_name
from photo_chooser.judge.cost import TokenUsage, check_budget, estimate_run_usd, price_for
from photo_chooser.judge.prompts import COMPOSE_PROMPT_VERSION, SCREEN_PROMPT_VERSION
from photo_chooser.judge.stages import (
    ComposeRunner,
    ComposeTask,
    JudgeRunner,
    JudgeTask,
    ScreenRunner,
    StageOutcome,
    compose_input_fingerprint,
    judge_prompt_version,
    run_compose,
    run_judge,
    run_screen,
)
from photo_chooser.judge.transport import BatchTransport, SyncTransport, check_credentials
from photo_chooser.library.base import PhotosLibrary
from photo_chooser.models import AlbumRef, Asset, LocalFeatures, Screening
from photo_chooser.report import MODE_BATCH, MODE_SYNC, ReportHeader, ThemeStats, write_report
from photo_chooser.select import (
    Posts,
    compose_candidates,
    compose_window,
    fallback_posts,
    in_interval,
    local_facts,
    local_posts,
    screen_candidates,
    shot_facts,
)
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
    batch: bool | None = None  # пакетный режим на этот запуск; None — как в cloud.batch (FR-12.1)


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
    posts: ExportedPosts | None
    error: str | None


@dataclass
class RunResult:
    run_dir: Path | None
    themes: list[ThemeOutcome]
    estimate_usd: float
    theme_counts: dict[str, int] = field(default_factory=dict)  # снимков темы после отсева и интервала
    album_spend_usd: float = 0.0
    batch: bool = False  # вторая ступень и сборка — пакетом (FR-12)


@dataclass
class _ThemeWork:
    """Тема на пути через этапы запуска: кандидаты, оценки, посты, предупреждения отчёта и строки выгрузки."""

    theme: Theme
    assets: list[Asset]  # снимки темы после отсева и интервала
    warnings: list[str]
    matched: int | None = None
    cands: list[Asset] = field(default_factory=list)
    judge: StageOutcome | None = None
    facts: dict = field(default_factory=dict)
    comp: list[Asset] = field(default_factory=list)  # кандидаты сборки
    posts: Posts = field(default_factory=lambda: Posts([], []))
    compose_usd: float = 0.0
    compose_usage: TokenUsage = TokenUsage()
    error: str | None = None
    notes: list[str] = field(default_factory=list)  # строки хода, которые выводятся при выгрузке темы


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
              style: StyleContext | None, batch: bool) -> float:
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
    n_judge, n_compose = [], []
    limit, extra = compose_window(cfg.cloud, cfg.posts)
    for t in trip.themes:
        assets = theme_assets[t.key()]
        if n_screen and not t.interval_only:
            n_judge.append(min(len(assets), cfg.cloud.candidates_per_theme))  # кандидаты ещё неизвестны
            n_compose.append(min(len(assets), limit + extra))
            continue
        cands = screen_candidates(assets, screenings, state.quality, t, cfg.thresholds.min_theme_relevance,
                                  cfg.cloud.candidates_per_theme)
        judged = {a.uuid: j for a in cands
                  if (j := cache.get_judgment(a.uuid, t.key(), judge_model, judge_prompt_version(style))) is not None}
        n_judge.append(len(cands) - len(judged))
        if len(judged) < len(cands):  # оценки ещё неизвестны — вход сборки тоже
            n_compose.append(min(len(cands), limit + extra))
            continue
        comp = compose_candidates(cands, judged, state.quality, cfg.thresholds, t.interval_only, limit, extra)
        fingerprint = compose_input_fingerprint(comp, shot_facts(judged), cfg.posts, style)
        cached = comp and cache.get_composition(t.key(), judge_model, COMPOSE_PROMPT_VERSION, fingerprint) is not None
        n_compose.append(0 if cached else len(comp))
    return estimate_run_usd(n_screen, n_judge, cfg.cloud, screen_model, judge_model,
                            judge_extra_tokens=style.extra_tokens if style else 0, n_compose=n_compose, batch=batch)


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

    batch = cfg.cloud.batch if opts.batch is None else opts.batch
    estimate = 0.0
    if not opts.no_cloud:
        estimate = _estimate(trip, cfg, state, theme_assets, screen_scope, cache, screen_model, judge_model,
                             style, batch)
        limit = opts.max_cost if opts.max_cost is not None else cfg.cloud.max_cost_usd_per_album
        check_budget(cache.album_spend(album.uuid), estimate, limit)
    if opts.dry_run:
        return RunResult(run_dir=None, themes=[ThemeOutcome(t, None, None, None) for t in trip.themes],
                         estimate_usd=estimate, theme_counts=counts, album_spend_usd=cache.album_spend(album.uuid),
                         batch=batch)

    long_side = cfg.cloud.preview_long_side_px

    def preview(a: Asset) -> bytes:
        return images.jpeg_bytes(state.previews[a.uuid], long_side)

    screenings: dict[str, Screening] = {}
    screen: StageOutcome | None = None
    transport = SyncTransport(client, notify=log) if client is not None else None
    # Вторая ступень и сборка: пакетом — с кэшем на час, пакет считается дольше 5 минут (D15, D16)
    judge_transport = BatchTransport(client, cache, album.uuid, notify=log) if batch and client else transport
    cache_ttl = "1h" if batch else "5m"
    if not opts.no_cloud and trip.query_themes and screen_scope:
        runner = ScreenRunner(screen_model, cfg.cloud.effort, preview)
        # Ошибка здесь останавливает запуск: без первой ступени темы с query не обработать.
        screen = run_screen(runner, transport, cache, album.title, album.uuid, trip.query_themes, screen_scope,
                            cfg.cloud, log=log)
        screenings = screen.results

    th = cfg.thresholds
    works = [_ThemeWork(t, theme_assets[t.key()], list(style_warnings)) for t in trip.themes]
    for w in works:
        t = w.theme
        # «признано подходящими» — только для тем с query и только когда первая ступень работала
        w.matched = None if opts.no_cloud or t.interval_only else sum(
            1 for a in w.assets if a.uuid in screenings and screenings[a.uuid].relevance.get(t.screen_key(), 0)
            >= th.min_theme_relevance)
        if opts.no_cloud:
            if t.query:
                w.warnings.append(NO_CLOUD_QUERY_WARNING.format(scope="интервала" if t.has_interval else "альбома"))
            w.cands = w.assets
            w.posts = local_posts(w.cands, state.quality, state.features, cfg.posts)
            w.facts = local_facts(w.cands, state.quality, state.features)
        else:
            w.cands = screen_candidates(w.assets, screenings, state.quality, t, th.min_theme_relevance,
                                        cfg.cloud.candidates_per_theme)

    # Вторая ступень всех тем, затем сборка всех тем (FR-3, «Порядок этапов»)
    cloud = [w for w in works if not opts.no_cloud and w.cands]
    if cloud:
        judge_runner = JudgeRunner(judge_model, cfg.cloud.effort, preview, style=style, cache_ttl=cache_ttl)
        tasks = [JudgeTask(w.theme, w.cands, {a.uuid: _draft(screenings.get(a.uuid)) + state.quality.get(a.uuid, 0.0)
                                              / 10 for a in w.cands}) for w in cloud]
        judged = run_judge(judge_runner, judge_transport, cache, tasks, cfg.cloud, album.uuid, log=log)
        to_compose: list[_ThemeWork] = []
        for w in cloud:
            w.judge = judged[w.theme.key()]
            if w.judge.error is not None:
                w.error = f"{type(w.judge.error).__name__}: {w.judge.error}"
                w.warnings.append(f"Тема не обработана: ошибка Claude API ({w.error}). Повторный запуск "
                                  "продолжит с уже оплаченных оценок.")
                w.notes.append(f"тема не обработана: ошибка Claude API ({w.error})")
                continue
            w.facts = shot_facts(w.judge.results)
            w.comp = compose_candidates(w.cands, w.judge.results, state.quality, th, w.theme.interval_only,
                                        *compose_window(cfg.cloud, cfg.posts))
            if w.comp:
                to_compose.append(w)
        if to_compose:
            compose_runner = ComposeRunner(judge_model, cfg.cloud.effort, preview, style=style, cache_ttl=cache_ttl)
            composed = run_compose(compose_runner, judge_transport, cache,
                                   [ComposeTask(w.theme, w.comp, w.facts) for w in to_compose], cfg, album.uuid, log)
            for w in to_compose:
                c = composed[w.theme.key()]
                w.posts, w.compose_usd, w.compose_usage = c.posts, c.usd, c.usage
                if w.posts is None:
                    failure = "модель отказалась" if c.refused else c.note
                    w.warnings.append(f"Сборка не удалась — разложено по оценкам ({failure})")
                    w.notes.append(f"сборка не удалась — разложено по оценкам ({failure})")
                    w.posts = fallback_posts(w.cands, w.judge.results, state.quality, th, cfg.posts,
                                             w.theme.interval_only)

    log("Выгрузка")
    run_dir = new_run_dir(cfg.output_dir, album.title, now)
    outcomes: list[ThemeOutcome] = []
    no_preview = set(state.no_preview)
    skipped = [(a.filename, "нет превью") for a in state.assets if a.uuid in no_preview]
    with tempfile.TemporaryDirectory(prefix="photo-chooser-") as tmp:
        for w in works:
            t, judge = w.theme, w.judge
            theme_dir = _theme_dir(run_dir, t)
            for note in w.notes:
                log(f"  «{t.name}»: {note}")
            if judge and judge.refused_groups:
                w.warnings.append(f"Модель отказалась оценивать групп: {judge.refused_groups} — эти кадры не оценены")
            if w.error is None:
                log(f"  «{t.name}»: {_posts_line(w.posts)}")
            exported = export_posts(library, w.posts, theme_dir, Path(tmp), cfg)
            cached = (judge.usage if judge else TokenUsage()) + w.compose_usage
            header = ReportHeader(
                screen_model=None if opts.no_cloud or screen is None else screen_model,
                model=None if opts.no_cloud else judge_model,
                effort=None if opts.no_cloud else cfg.cloud.effort,
                prompt_versions=(None if opts.no_cloud else
                                 f"{SCREEN_PROMPT_VERSION}, {judge_prompt_version(style)}, {COMPOSE_PROMPT_VERSION}"),
                run_at=now, warnings=w.warnings, album=album.title,
                style=None if style is None else _style_line(style),
                mode=None if opts.no_cloud else (MODE_BATCH if batch else MODE_SYNC))
            stats = ThemeStats(
                read=len(state.assets), no_preview=len(state.no_preview), junk=len(state.junk),
                duplicates_collapsed=state.duplicates_collapsed, in_interval=len(w.assets),
                sent_to_screen=len(screen_scope) if screen else 0, matched=w.matched,
                sent_to_judge=len(w.cands) if not opts.no_cloud else 0,
                from_cache=judge.from_cache if judge else 0, unjudged=len(judge.unjudged) if judge else 0,
                usd_screen=screen.usd if screen else 0.0, usd_judge=judge.usd if judge else 0.0,
                usd_album_total=cache.album_spend(album.uuid), skipped=skipped,
                sent_to_compose=len(w.comp), usd_compose=w.compose_usd,
                cache_write_tokens=cached.cache_write, cache_read_tokens=cached.cache_read)
            report_path = theme_dir / "report.md"
            write_report(report_path, t, header, exported, w.facts, stats)
            outcomes.append(ThemeOutcome(t, report_path, exported, w.error))
    return RunResult(run_dir=run_dir, themes=outcomes, estimate_usd=estimate, theme_counts=counts, batch=batch,
                     album_spend_usd=cache.album_spend(album.uuid))


def _posts_line(posts: Posts) -> str:
    sizes = " + ".join(str(len(c.slides)) for c in posts.carousels)
    return f"каруселей {len(posts.carousels)}" + (f" ({sizes})" if sizes else "") + f", stories {len(posts.stories)}"


def _style_line(style: StyleContext) -> str:
    kinds = [kind for kind, _ in style.references]
    return (f"style.md от {style.created_at:%Y-%m-%d}, отпечаток {style.fingerprint}, скриншотов: лента "
            f"{kinds.count('feed')}, stories {kinds.count('stories')}")


def _draft(s: Screening | None) -> float:
    return float(s.score) if s else 0.0
