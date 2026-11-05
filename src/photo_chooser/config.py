"""Конфигурация: общий config.yaml, файл поездки trip.yaml и темы."""

import hashlib
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("~/.config/photo-chooser/config.yaml").expanduser()
ARG_TIME_FORMAT = "%Y-%m-%d %H:%M"


class ConfigError(Exception):
    pass


def _hash16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Theme(_Strict):
    name: str
    query: str | None = None
    start: datetime | None = Field(default=None, alias="from")
    end: datetime | None = Field(default=None, alias="to")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ConfigError("У темы пустое имя (name)")
        return v

    @field_validator("query")
    @classmethod
    def _blank_query_is_none(cls, v: str | None) -> str | None:
        return v if v and v.strip() else None

    @model_validator(mode="after")
    def _check(self) -> "Theme":
        if (self.start is None) != (self.end is None):
            raise ConfigError(f"Тема «{self.name}»: интервал задаётся обеими границами from и to")
        for bound in (self.start, self.end):
            if bound is not None and bound.tzinfo is not None:
                raise ConfigError(f"Тема «{self.name}»: укажите from/to без часового пояса — "
                                  "интервал задаётся в местном времени поездки (timezone)")
        if self.has_interval and self.start >= self.end:
            raise ConfigError(f"Тема «{self.name}»: начало интервала (from) должно быть раньше конца (to)")
        if self.query is None and not self.has_interval:
            raise ConfigError(f"Тема «{self.name}»: нужен query или интервал from/to")
        return self

    @property
    def has_interval(self) -> bool:
        return self.start is not None and self.end is not None

    @property
    def interval_only(self) -> bool:
        return self.has_interval and self.query is None

    def screen_key(self) -> str:
        """Ключ темы в оценках первой ступени: модель видит только name и query, интервал на них не влияет."""
        return _hash16(f"{self.name}|{self.query or ''}")

    def key(self) -> str:
        parts = [self.name, self.query or "",
                 self.start.isoformat() if self.start else "",
                 self.end.isoformat() if self.end else ""]
        return _hash16("|".join(parts))


class TripConfig(_Strict):
    album: str
    timezone: str | None = None
    themes: list[Theme]

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"Неизвестный часовой пояс «{v}»: нужно IANA-имя, например Africa/Cairo") from None
        return v

    @model_validator(mode="after")
    def _check(self) -> "TripConfig":
        if not self.themes:
            raise ConfigError("Не задано ни одной темы")
        seen: set[str] = set()
        for t in self.themes:
            folded = t.name.strip().casefold()
            if folded in seen:
                raise ConfigError(f"Тема «{t.name}» указана дважды: имена тем должны различаться")
            seen.add(folded)
        if self.timezone is None and any(t.has_interval for t in self.themes):
            raise ConfigError("У темы задан интервал from/to, поэтому нужен timezone поездки "
                              "(в trip.yaml — поле timezone, в CLI — флаг --timezone), например Africa/Cairo")
        return self

    @property
    def tz(self) -> ZoneInfo | None:
        return ZoneInfo(self.timezone) if self.timezone else None

    @property
    def query_themes(self) -> list[Theme]:
        return [t for t in self.themes if t.query is not None]


def themes_key(album_title: str, themes: list[Theme]) -> str:
    """Ключ кэша первой ступени: название альбома и упорядоченный список name|query."""
    lines = [album_title] + [f"{t.name}|{t.query or ''}" for t in themes]
    return _hash16("\n".join(lines))


class Price(_Strict):
    input: float
    output: float
    cache_read: float | None = None  # чтение кэша; без значения — 0.1 × input (FR-8)

    @property
    def cache_read_price(self) -> float:
        return self.cache_read if self.cache_read is not None else 0.1 * self.input


DEFAULT_PRICING = {
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.10),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
}


class PostsCfg(_Strict):
    """Посты на тему (SPEC FR-4): лимиты каруселей ленты и stories."""

    carousels: int = Field(default=1, ge=0)
    carousel_size: int = Field(default=5, ge=1)
    stories: int = Field(default=9, ge=0)


class CloudCfg(_Strict):
    screen_model: str = "claude-haiku-5-5"
    model: str = "claude-haiku-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    pricing: dict[str, Price] = Field(default_factory=lambda: dict(DEFAULT_PRICING))
    max_cost_usd_per_album: float = Field(default=5.0, ge=0)
    candidates_per_theme: int = Field(default=100, ge=1)
    preview_long_side_px: int = Field(default=1024, ge=64)
    group_size: int = Field(default=20, ge=1)
    compose_candidates: int = Field(default=30, ge=1)  # сильнейших кадров темы в запросе сборки (FR-4.1)

    @model_validator(mode="before")
    @classmethod
    def _removed_keys(cls, data):
        if isinstance(data, dict) and "batch" in data:
            raise ConfigError("cloud.batch удалён в версии 0.5.1: пакетного режима больше нет (SPEC D16) — уберите "
                              "ключ из config.yaml")
        return data

    @field_validator("pricing", mode="before")
    @classmethod
    def _merge_default_pricing(cls, v):
        # Цены из config.yaml дополняют встроенные, а не заменяют их целиком.
        if isinstance(v, dict):
            return {**{k: p.model_dump() for k, p in DEFAULT_PRICING.items()}, **v}
        return v


class Thresholds(_Strict):
    # Значения по умолчанию — docs/spike-findings.md.
    junk_blur: float = 1.0
    junk_dark_luma: float = 0.03
    junk_bright_luma: float = 0.97
    duplicate_distance: float = 0.45
    duplicate_window_s: float = 60
    min_theme_relevance: int = 5
    min_score: int = 5  # сила кадра для кандидатов сборки и раскладки по оценкам (FR-4.1, FR-4.4)
    live_frame_margin: float = 0.15

    @model_validator(mode="before")
    @classmethod
    def _renamed_keys(cls, data):
        if isinstance(data, dict) and {"min_feed_score", "min_stories_score"} & data.keys():
            raise ConfigError("thresholds.min_feed_score и min_stories_score заменены на thresholds.min_score "
                              "(версия 0.4): одна сила кадра для поста и stories")
        return data


class StyleCfg(_Strict):
    """Стиль страницы Instagram (SPEC FR-11). Альбомы задаёт пользователь в config.yaml."""

    albums: list[str] = Field(default_factory=list)
    since: date = date(2018, 1, 1)
    screenshots_album: str | None = None
    sample: int = Field(default=40, ge=1)
    model: str = "claude-opus-5-5"
    reference_feed: int = Field(default=2, ge=0)  # первые скриншоты альбома — гриды ленты
    reference_stories: int = Field(default=2, ge=0)  # следующие — stories

    @model_validator(mode="before")
    @classmethod
    def _renamed_keys(cls, data):
        if isinstance(data, dict) and "reference_images" in data:
            raise ConfigError("style.reference_images заменён на reference_feed и reference_stories (версия 0.3): "
                              "сколько первых скриншотов альбома — гриды ленты и сколько следующих — stories")
        return data


class AppConfig(_Strict):
    output_dir: Path = Field(default=Path("~/Pictures/photo-chooser"), validate_default=True)
    posts: PostsCfg = Field(default_factory=PostsCfg)
    cloud: CloudCfg = Field(default_factory=CloudCfg)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    style: StyleCfg = Field(default_factory=StyleCfg)

    @field_validator("output_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    @model_validator(mode="before")
    @classmethod
    def _renamed_keys(cls, data):
        if isinstance(data, dict) and "shortlist" in data:
            raise ConfigError("shortlist.feed/stories заменены на posts.carousels, posts.carousel_size и posts.stories "
                              "(версия 0.3): лента — до 2 каруселей по 9 кадров, stories — до 9")
        return data


def _describe(e: ValidationError) -> str:
    return "\n".join(f"  {'.'.join(str(p) for p in err['loc']) or '—'}: {err['msg']}" for err in e.errors())


def _read_yaml(path: Path, what: str) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{what} {path}: ошибка разбора YAML: {e}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{what} {path}: ожидается словарь ключей верхнего уровня")
    return data


def load_app_config(path: Path | None = None) -> AppConfig:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return AppConfig()
    data = _read_yaml(path, "Конфиг")
    try:
        return AppConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Ошибка в конфиге {path}:\n{_describe(e)}") from None


def load_trip(path: Path) -> TripConfig:
    if not path.exists():
        raise ConfigError(f"Файл поездки не найден: {path}")
    data = _read_yaml(path, "Файл поездки")
    try:
        return TripConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Ошибка в файле поездки {path}:\n{_describe(e)}") from None


def _parse_arg_time(value: str | datetime | None, flag: str) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value.strip(), ARG_TIME_FORMAT)
    except ValueError:
        raise ConfigError(f"{flag}: ожидается время в формате «ГГГГ-ММ-ДД ЧЧ:ММ», получено «{value}»") from None


def trip_from_args(album: str, name: str, query: str | None, start: str | datetime | None,
                   end: str | datetime | None, timezone: str | None) -> TripConfig:
    theme = Theme(name=name, query=query, start=_parse_arg_time(start, "--from"), end=_parse_arg_time(end, "--to"))
    return TripConfig(album=album, timezone=timezone, themes=[theme])
