"""report.md по теме: шапка для сравнения прогонов, таблица шорт-листа и сводка (SPEC FR-6.3)."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from photo_chooser.config import Theme
from photo_chooser.export import Assembled, ExportedItem

LIST_TITLES = {"feed": "лента", "stories": "stories"}


@dataclass
class ReportHeader:
    screen_model: str | None  # None — без облака
    model: str | None
    effort: str | None
    prompt_versions: str | None
    run_at: datetime
    warnings: list[str] = field(default_factory=list)
    album: str = ""


@dataclass
class ThemeStats:
    read: int
    no_preview: int
    junk: int
    duplicates_collapsed: int
    in_interval: int
    sent_to_screen: int
    matched: int
    sent_to_judge: int
    from_cache: int
    unjudged: int
    usd_screen: float
    usd_judge: float
    usd_album_total: float
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (файл, причина) — пропущенные до отбора


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _score(score) -> str:
    return f"{score:.2f}" if isinstance(score, float) else str(score)


def _orientation(item: ExportedItem) -> str:
    a = item.asset
    if a.width == a.height:
        return "квадратная"
    return "вертикальная" if a.is_vertical else "горизонтальная"


def _theme_line(theme: Theme) -> str:
    parts = []
    if theme.query:
        parts.append(f"запрос «{theme.query}»")
    if theme.has_interval:
        parts.append(f"интервал {theme.start:%Y-%m-%d %H:%M} – {theme.end:%Y-%m-%d %H:%M} (время поездки)")
    return "; ".join(parts)


def write_report(path: Path, theme: Theme, header: ReportHeader, assembled: Assembled, stats: ThemeStats) -> None:
    lines = [f"# {theme.name}", ""]
    if header.album:
        lines.append(f"- Альбом: «{header.album}»")
    lines.append(f"- Тема: {_theme_line(theme)}")
    lines.append(f"- Запуск: {header.run_at:%Y-%m-%d %H:%M}")
    if header.screen_model is None and header.model is None:
        lines.append("- Модели: без облака (только локальные оценки)")
    else:
        lines.append(f"- Первая ступень: {header.screen_model or '—'}; вторая ступень: {header.model or '—'}")
        lines.append(f"- effort: {header.effort or '—'}")
        lines.append(f"- Версия промпта: {header.prompt_versions or '—'}")
    lines.append("")
    for w in header.warnings:
        lines.append(f"> ⚠️ {w}")
    if header.warnings:
        lines.append("")

    lines += ["## Шорт-лист", "",
              "| Ранг | Файл(ы) | Список | Оценка | Ориентация | Пометки | Причина |",
              "|---|---|---|---|---|---|---|"]
    for item in assembled.feed + assembled.stories:
        lines.append(f"| {item.rank} | {', '.join(item.files)} | {LIST_TITLES[item.list_name]} | {_score(item.score)} "
                     f"| {_orientation(item)} | {_cell('; '.join(item.notes))} | {_cell(item.reason)} |")
    if not assembled.feed and not assembled.stories:
        lines.append("| — | — | — | — | — | — | подходящих кадров нет |")
    lines.append("")

    shortfall = [f"лента {assembled.shortfall_feed}"] if assembled.shortfall_feed else []
    shortfall += [f"stories {assembled.shortfall_stories}"] if assembled.shortfall_stories else []
    lines += ["## Сводка", "",
              f"- Прочитано снимков: {stats.read}",
              f"- Без превью: {stats.no_preview}",
              f"- Отсеяно как мусор: {stats.junk}",
              f"- Схлопнуто как дубли: {stats.duplicates_collapsed}",
              f"- Прошло интервал: {stats.in_interval}",
              f"- Отправлено на первую ступень (общая на запуск): {stats.sent_to_screen}",
              f"- Признано подходящими к теме на первой ступени: {stats.matched}",
              f"- Кандидатов на второй ступени: {stats.sent_to_judge} (из кэша: {stats.from_cache}, "
              f"без оценки: {stats.unjudged})",
              f"- Стоимость: первая ступень ${stats.usd_screen:.4f} (общая на запуск), "
              f"вторая ступень ${stats.usd_judge:.4f} (по теме); накоплено по альбому ${stats.usd_album_total:.2f}",
              f"- Недобор: {', '.join(shortfall) if shortfall else 'нет'}",
              ""]

    skipped = [(a.filename, reason) for a, reason in assembled.failed] + list(stats.skipped)
    if skipped:
        lines += ["## Пропущенные снимки", ""]
        lines += [f"- {name} — {reason}" for name, reason in skipped]
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
