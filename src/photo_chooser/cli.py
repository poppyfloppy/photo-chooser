"""CLI: photo-chooser albums | run | style | key (SPEC FR-7)."""

import os
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import anthropic
import typer

from photo_chooser import images, keychain, pipeline, style
from photo_chooser.cache import Cache
from photo_chooser.config import ConfigError, load_app_config, load_trip, trip_from_args
from photo_chooser.judge.cost import BudgetExceeded, UnknownModelPrice, price_for, usage_usd
from photo_chooser.judge.errors import InsufficientCredit
from photo_chooser.judge.transport import check_credentials, has_credentials, make_client
from photo_chooser.library.base import AlbumAmbiguous, AlbumNotFound, PhotosAccessError

EXIT_ERROR, EXIT_CONFIG, EXIT_BUDGET, EXIT_ALBUM, EXIT_ACCESS, EXIT_BILLING = 1, 2, 3, 4, 5, 6
LOG_DIR = Path("~/Library/Logs/photo-chooser").expanduser()
STYLE_PATH = style.DEFAULT_STYLE_PATH
STYLE_SPEND_KEY = "style"  # расход команды style — отдельно от бюджетов альбомов (FR-11.6)

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Посты для Instagram из альбома Photos: карусели ленты и stories по темам поездки.")
key_app = typer.Typer(no_args_is_help=True, help="Ключ Claude API в Keychain (запись anthropic-api-key).")
app.add_typer(key_app, name="key")


def make_library():
    from photo_chooser.library.photokit_library import PhotoKitLibrary

    return PhotoKitLibrary()


def make_cache() -> Cache:
    return Cache()


def _err(message: str) -> None:
    typer.echo(message, err=True)


def _write_traceback() -> Path | None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"error-{datetime.now():%Y%m%d-%H%M%S}.log"
        path.write_text(traceback.format_exc(), encoding="utf-8")
        return path
    except OSError:
        return None


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
    except InsufficientCredit as e:
        _err(str(e))
        raise typer.Exit(EXIT_BILLING) from None
    except (AlbumNotFound, AlbumAmbiguous) as e:
        _err(str(e))
        raise typer.Exit(EXIT_ALBUM) from None
    except PhotosAccessError as e:
        _err(str(e))
        raise typer.Exit(EXIT_ACCESS) from None
    except anthropic.AuthenticationError:
        _err("Claude API не принял ключ: замените его командой `photo-chooser key set` (или ANTHROPIC_API_KEY / "
             "`ant auth login`). Без облака можно запустить с --no-cloud.")
        raise typer.Exit(EXIT_CONFIG) from None
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001 — последняя линия: сообщение, трассировка — в лог-файл
        log = _write_traceback()
        _err(f"Ошибка: {type(e).__name__}: {e}" + (f"\nПодробности: {log}" if log else ""))
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
    """Собрать посты по темам поездки."""
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
        result = pipeline.run(trip, cfg, opts, make_library(), make_cache(), make_client, datetime.now(), log=_err,
                              style_path=STYLE_PATH)
        failed = _print_result(result, opts, cfg.cloud.max_cost_usd_per_album if max_cost is None else max_cost)
    if failed:
        raise typer.Exit(EXIT_ERROR)


@app.command("style")
def style_command(
    yes: bool = typer.Option(False, "--yes", help="Отправить без подтверждения"),
) -> None:
    """Построить профиль стиля страницы Instagram по образцам из Photos (style.md)."""
    with _exit_codes():
        cfg = load_app_config()
        library = make_library()
        posts, shots = style.gather_samples(library, cfg.style)

        def preview(asset, side: int) -> bytes | None:
            path = library.preview(asset, side)
            return images.jpeg_bytes(path, side) if path else None

        samples = style.load_samples(posts, shots, preview)  # до подтверждения: считаем то, что уйдёт
        sent_posts = [s.asset for s in samples if s.kind == "post"]
        sent_shots = [s.asset for s in samples if s.kind == "screenshot"]
        estimate = style.estimate_profile_usd(sent_posts, sent_shots, cfg)
        typer.echo(f"Отправить {len(samples)} изображений (постов {len(sent_posts)}, скриншотов {len(sent_shots)}) "
                   f"модели {cfg.style.model}, ≈ ${estimate:.2f}?")
        try:
            confirmed = yes or typer.confirm("Продолжить?", default=False)
        except typer.Abort:  # ввод не с терминала
            confirmed = False
        if not confirmed:
            _err("Отменено: в облако ничего не отправлено")
            raise typer.Exit(EXIT_ERROR)
        client = make_client()
        check_credentials(client)
        profile = style.build_profile(client, cfg.style.model, cfg.cloud.effort, samples)
        style.save_profile(STYLE_PATH, profile.text)
        usd = usage_usd(profile.input_tokens, profile.output_tokens, price_for(cfg.cloud, cfg.style.model))
        make_cache().add_spend(STYLE_SPEND_KEY, cfg.style.model, usd)
        typer.echo(f"Профиль стиля: {STYLE_PATH} (прежний — style.prev.md)\nСтоимость: ${usd:.4f}\n"
                   "Прочитайте профиль и поправьте его, если нужно: он уйдёт в каждый запрос второй ступени.")


@key_app.command("set")
def key_set() -> None:
    """Сохранить или заменить ключ: его спросит утилита security, ввод скрыт (FR-13.2)."""
    code = keychain.store_api_key()
    if code != 0:
        _err(f"Ключ не сохранён: security завершилась с кодом {code}")
        raise typer.Exit(EXIT_ERROR)
    typer.echo(f"Ключ сохранён в Keychain (запись {keychain.SERVICE})")


@key_app.command("delete")
def key_delete(yes: bool = typer.Option(False, "--yes", help="Удалить без подтверждения")) -> None:
    """Удалить ключ из Keychain."""
    with _exit_codes():
        if not yes and not typer.confirm("Удалить ключ Claude API из Keychain?", default=False):
            _err("Отменено: ключ не удалён")
            raise typer.Exit(EXIT_ERROR)
        if keychain.delete_api_key():
            typer.echo("Ключ удалён из Keychain")
        else:
            typer.echo(f"В Keychain нет записи {keychain.SERVICE}")


@key_app.command("status")
def key_status() -> None:
    """Откуда будет взят ключ; сам ключ не выводится и в API не проверяется (FR-13.3)."""
    if keychain.has_api_key():
        typer.echo(f"Ключ Claude API: Keychain (запись {keychain.SERVICE})")
    elif os.environ.get("ANTHROPIC_API_KEY"):
        typer.echo("Ключ Claude API: переменная ANTHROPIC_API_KEY")
    elif has_credentials(anthropic.Anthropic()):
        typer.echo("Ключ Claude API: профиль ant auth login")
    else:
        typer.echo("Ключ Claude API не найден — выполните `photo-chooser key set`")


def _print_result(result: pipeline.RunResult, opts: pipeline.RunOptions, limit: float) -> bool:
    if opts.dry_run:
        typer.echo("Снимков по темам (после отсева мусора, дублей и интервала):")
        for name, n in result.theme_counts.items():
            typer.echo(f"  «{name}»: {n}")
        if opts.no_cloud:
            typer.echo("Режим без облака: стоимость $0")
        else:
            typer.echo(f"Оценка стоимости запуска (обе ступени и сборка): ${result.estimate_usd:.2f}; уже потрачено по альбому "
                       f"${result.album_spend_usd:.2f}, лимит ${limit:.2f}")
        return False
    typer.echo(f"Готово: {result.run_dir}")
    failed = False
    for o in result.themes:
        if o.error:
            failed = True
            typer.echo(f"  «{o.theme.name}»: тема не обработана — {o.error}")
        else:
            p = o.posts
            sizes = " + ".join(str(len(c.slides)) for c in p.carousels)
            typer.echo(f"  «{o.theme.name}»: каруселей {len(p.carousels)}" + (f" ({sizes})" if sizes else "")
                       + f", stories {len(p.stories)} — {o.report_path}")
    if not opts.no_cloud:
        typer.echo(f"Накоплено по альбому: ${result.album_spend_usd:.2f}")
    return failed
