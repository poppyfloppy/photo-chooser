"""report.md по теме: шапка для сравнения прогонов, таблицы каруселей и stories, сводка (SPEC FR-6.3)."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from photo_chooser.config import Theme
from photo_chooser.export import ExportedPosts, ExportedShot
from photo_chooser.models import Asset
from photo_chooser.select import ShotFacts



@dataclass
class ReportHeader:
    screen_model: str | None  # None — без облака
    model: str | None
    effort: str | None
    prompt_versions: str | None
    run_at: datetime
    warnings: list[str] = field(default_factory=list)
    album: str = ""
    style: str | None = None  # строка о профиле стиля (FR-6.3); None — без профиля


@dataclass
class ThemeStats:
    read: int
    no_preview: int
    junk: int
    duplicates_collapsed: int
    in_interval: int
    sent_to_screen: int
    matched: int | None  # None — первая ступень к теме не применялась
    sent_to_judge: int
    from_cache: int
    unjudged: int
    usd_screen: float
    usd_judge: float
    usd_album_total: float
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (файл, причина) — пропущенные до отбора
    sent_to_compose: int = 0  # кандидатов в запросе сборки постов (FR-4.1)
    usd_compose: float = 0.0


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _score(score) -> str:
    return f"{score:.2f}" if isinstance(score, float) else str(score)


def _theme_line(theme: Theme) -> str:
    parts = []
    if theme.query:
        parts.append(f"запрос «{theme.query}»")
    if theme.has_interval:
        parts.append(f"интервал {theme.start:%Y-%m-%d %H:%M} – {theme.end:%Y-%m-%d %H:%M} (время поездки)")
    return "; ".join(parts)


# --- отчёт о постах (v0.3, FR-6.3) -----------------------------------------------------------

STORY_KINDS = {"best": "лучшее", "interesting": "интересное"}
SCORES_NOTE = "Оценка — сила кадра по второй ступени, 1–10; «Причина» — её пояснение к оценке, а не решение сборки."
SCORES_NOTE_LOCAL = "Оценка — локальная оценка качества 0–1 (без облака); «Причина» — из чего она сложилась."


def _orientation_of(a: Asset) -> str:
    if a.width == a.height:
        return "квадратная"
    return "вертикальная" if a.is_vertical else "горизонтальная"


def _score_cell(f: ShotFacts | None) -> str:
    return _score(f.score) if f else "—"


def _files(shot: ExportedShot) -> str:
    return ", ".join(shot.files)


def _header_lines(theme: Theme, header: ReportHeader) -> list[str]:
    lines = [f"# {theme.name}", ""]
    if header.album:
        lines.append(f"- Альбом: «{header.album}»")
    lines.append(f"- Тема: {_theme_line(theme)}")
    lines.append(f"- Запуск: {header.run_at:%Y-%m-%d %H:%M}")
    if header.screen_model is None and header.model is None:
        lines.append("- Модели: без облака (только локальные оценки)")
    else:
        lines.append(f"- Первая ступень: {header.screen_model or '—'}; вторая ступень и сборка: {header.model or '—'}")
        lines.append(f"- effort: {header.effort or '—'}")
        lines.append(f"- Версия промпта: {header.prompt_versions or '—'}")
        if header.style:
            lines.append(f"- Стиль: {header.style}")
    lines.append("")
    lines += [f"> ⚠️ {w}" for w in header.warnings]
    return lines + ([""] if header.warnings else [])


def write_report(path: Path, theme: Theme, header: ReportHeader, posts: ExportedPosts,
                       facts: dict[str, ShotFacts], stats: ThemeStats) -> None:
    lines = _header_lines(theme, header)
    lines += [SCORES_NOTE_LOCAL if header.model is None else SCORES_NOTE, ""]
    lines += ["## Лента", ""]
    if not posts.carousels:
        lines += ["Каруселей нет — сильных кадров для ленты не нашлось.", ""]
    for n, carousel in enumerate(posts.carousels, 1):
        lines += [f"### Карусель {n} — {carousel.title}", "", f"Папка: `feed/{carousel.folder}/`", "",
                  "| Слайд | Файл | Оценка | Ориентация | Пометки | Причина | Кадрирование (4:5) | Запасные |",
                  "|---|---|---|---|---|---|---|---|"]
        for pos, slide in enumerate(carousel.slides, 1):
            f = facts.get(slide.shot.asset.uuid)
            alternatives = ", ".join(_files(alt) for alt in slide.alternatives) or "—"
            lines.append(f"| {pos} | {_files(slide.shot)} | {_score_cell(f)} | {_orientation_of(slide.shot.asset)} "
                         f"| {_cell('; '.join(slide.shot.notes))} | {_cell(f.reason) if f else '—'} "
                         f"| {(_cell(f.crop_feed) if f else '') or '—'} | {alternatives} |")
        lines.append("")
    lines += ["## Stories", ""]
    if not posts.stories:
        lines += ["Stories нет.", ""]
    else:
        lines += ["| № | Файл | Оценка | Тип | Что пояснить | Причина | Кадрирование (9:16) |",
                  "|---|---|---|---|---|---|---|"]
        for pos, story in enumerate(posts.stories, 1):
            f = facts.get(story.shot.asset.uuid)
            lines.append(f"| {pos} | {_files(story.shot)} | {_score_cell(f)} "
                         f"| {STORY_KINDS.get(story.kind, story.kind)} "
                         f"| {_cell(story.hint) or '—'} | {_cell(f.reason) if f else '—'} "
                         f"| {(_cell(f.crop_stories) if f else '') or '—'} |")
        lines.append("")

    sizes = " + ".join(str(len(c.slides)) for c in posts.carousels)
    summary = f"каруселей {len(posts.carousels)}" + (f" ({sizes})" if sizes else "") + f", stories {len(posts.stories)}"
    lines += ["## Сводка", "",
              f"- Прочитано снимков: {stats.read}",
              f"- Без превью: {stats.no_preview}",
              f"- Отсеяно как мусор: {stats.junk}",
              f"- Схлопнуто как дубли: {stats.duplicates_collapsed}",
              f"- Прошло интервал: {stats.in_interval}",
              f"- Отправлено на первую ступень (общая на запуск): {stats.sent_to_screen}",
              f"- Признано подходящими к теме на первой ступени: {'—' if stats.matched is None else stats.matched}",
              f"- Кандидатов на второй ступени: {stats.sent_to_judge} (из кэша: {stats.from_cache}, "
              f"без оценки: {stats.unjudged})",
              f"- Отправлено в сборку: {stats.sent_to_compose}",
              f"- Посты: {summary}",
              f"- Стоимость: первая ступень ${stats.usd_screen:.4f} (общая на запуск), "
              f"вторая ступень ${stats.usd_judge:.4f}, сборка ${stats.usd_compose:.4f} (по теме); "
              f"накоплено по альбому ${stats.usd_album_total:.2f}",
              ""]
    skipped = [(a.filename, reason) for a, reason in posts.failed] + list(stats.skipped)
    if skipped:
        lines += ["## Пропущенные снимки", ""]
        lines += [f"- {name} — {reason}" for name, reason in skipped]
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
