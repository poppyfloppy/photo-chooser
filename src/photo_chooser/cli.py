"""CLI: photo-chooser albums | run (SPEC FR-7)."""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import anthropic
import typer

from photo_chooser import pipeline
from photo_chooser.cache import Cache
from photo_chooser.config import ConfigError, load_app_config, load_trip, trip_from_args
from photo_chooser.judge.cost import BudgetExceeded, UnknownModelPrice
from photo_chooser.judge.stages import make_client
from photo_chooser.library.base import AlbumAmbiguous, AlbumNotFound, PhotosAccessError

EXIT_ERROR, EXIT_CONFIG, EXIT_BUDGET, EXIT_ALBUM, EXIT_ACCESS = 1, 2, 3, 4, 5

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Шорт-лист кадров для Instagram из альбома Photos (лента и stories).")


def make_library():
    from photo_chooser.library.photokit_library import PhotoKitLibrary

    return PhotoKitLibrary()


def make_cache() -> Cache:
    return Cache()


def _err(message: str) -> None:
    typer.echo(message, err=True)


@contextmanager
def _exit_codes():
    try:
        yield
    except (ConfigError, UnknownModelPrice) as e:
        _err(f"Ошибка конфигурации: {e}")
        raise typer.Exit(EXIT_CONFIG) from None
    except BudgetExceeded as e:
        _err(str(e))
        raise typer.Exit(EXIT_BUDGET) from None
    except (AlbumNotFound, AlbumAmbiguous) as e:
        _err(str(e))
        raise typer.Exit(EXIT_ALBUM) from None
    except PhotosAccessError as e:
        _err(str(e))
        raise typer.Exit(EXIT_ACCESS) from None
    except anthropic.AuthenticationError:
        _err("Claude API не принял ключ: задайте ANTHROPIC_API_KEY или выполните `ant auth login`. "
             "Без облака можно запустить с --no-cloud.")
        raise typer.Exit(EXIT_CONFIG) from None
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001 — последняя линия: сообщение вместо трассировки
        _err(f"Ошибка: {type(e).__name__}: {e}")
        raise typer.Exit(EXIT_ERROR) from None


@app.command()
def albums() -> None:
    """Список обычных альбомов Photos с числом фото."""
    with _exit_codes():
        for a in sorted(make_library().albums(), key=lambda a: a.title.casefold()):
            typer.echo(f"«{a.title}» — {a.count} фото")


@app.command("run")
def run_command(
    trip_file: Path | None = typer.Argument(None, metavar="[TRIP]", help="Файл поездки trip.yaml"),
    album: str | None = typer.Option(None, "--album", help="Альбом Photos (без файла поездки)"),
    theme: str | None = typer.Option(None, "--theme", help="Имя темы"),
    query: str | None = typer.Option(None, "--query", help="Что искать в кадре"),
    start: str | None = typer.Option(None, "--from", help="Начало интервала «ГГГГ-ММ-ДД ЧЧ:ММ», время поездки"),
    end: str | None = typer.Option(None, "--to", help="Конец интервала «ГГГГ-ММ-ДД ЧЧ:ММ», время поездки"),
    timezone: str | None = typer.Option(None, "--timezone", help="Часовой пояс поездки, например Africa/Cairo"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Только локальные этапы и оценка стоимости"),
    no_cloud: bool = typer.Option(False, "--no-cloud", help="Без Claude: только локальные оценки, бесплатно"),
    model: str | None = typer.Option(None, "--model", help="Модель второй ступени на этот запуск"),
    max_cost: float | None = typer.Option(None, "--max-cost", help="Лимит стоимости альбома на этот запуск, $"),
) -> None:
    """Собрать шорт-лист по темам поездки."""
    with _exit_codes():
        flags = [album, theme, query, start, end, timezone]
        if trip_file is not None:
            if any(v is not None for v in flags):
                raise ConfigError("Флаги --album/--theme/--query/--from/--to/--timezone задаются без файла поездки")
            trip = load_trip(trip_file)
        else:
            if not album or not theme:
                raise ConfigError("Укажите файл поездки (photo-chooser run trip.yaml) или --album и --theme")
            trip = trip_from_args(album, theme, query, start, end, timezone)
        cfg = load_app_config()
        opts = pipeline.RunOptions(dry_run=dry_run, no_cloud=no_cloud, model=model, max_cost=max_cost)
        result = pipeline.run(trip, cfg, opts, make_library(), make_cache(), make_client, datetime.now(), log=_err)
        failed = _print_result(result, opts, cfg.cloud.max_cost_usd_per_album if max_cost is None else max_cost)
    if failed:
        raise typer.Exit(EXIT_ERROR)


def _print_result(result: pipeline.RunResult, opts: pipeline.RunOptions, limit: float) -> bool:
    if opts.dry_run:
        typer.echo("Снимков по темам (после отсева мусора, дублей и интервала):")
        for name, n in result.theme_counts.items():
            typer.echo(f"  «{name}»: {n}")
        if opts.no_cloud:
            typer.echo("Режим без облака: стоимость $0")
        else:
            typer.echo(f"Оценка стоимости запуска (обе ступени): ${result.estimate_usd:.2f}; уже потрачено по альбому "
                       f"${result.album_spend_usd:.2f}, лимит ${limit:.2f}")
        return False
    typer.echo(f"Готово: {result.run_dir}")
    failed = False
    for o in result.themes:
        if o.error:
            failed = True
            typer.echo(f"  «{o.theme.name}»: тема не обработана — {o.error}")
        else:
            a = o.assembled
            typer.echo(f"  «{o.theme.name}»: лента {len(a.feed)}, stories {len(a.stories)} — {o.report_path}")
    if not opts.no_cloud:
        typer.echo(f"Накоплено по альбому: ${result.album_spend_usd:.2f}")
    return failed
