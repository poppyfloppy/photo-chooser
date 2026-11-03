"""Облачные ступени: первая — отбор по всем темам сразу, вторая — оценка кандидатов тем, сборка постов
(SPEC FR-3 п.5, п.8, FR-4).

Раннеры строят запросы и разбирают ответы; отправляет их транспорт (judge/transport.py) — синхронно или пакетом.
Функции этапов собирают задания для всех тем запуска сразу (FR-3, «Порядок этапов»): ответ каждого задания
пишется в кэш и расход его обработчиком, который строится только из meta задания — так же его восстановит
пакетный режим в следующем запуске (FR-12.4).
"""

import base64
import hashlib
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

import objc
import pydantic
from pydantic import BaseModel

from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig, CloudCfg, PostsCfg, Theme, themes_key
from photo_chooser.judge.cost import (
    COMPOSE_LABEL_TOKENS,
    HAIKU_PRICE_LIMIT_TOKENS,
    TokenUsage,
    group_input_tokens,
    price_for,
    usage_usd,
)
from photo_chooser.judge.prompts import (
    COMPOSE_PROMPT_VERSION,
    COMPOSE_SCREENSHOTS_NOTE,
    COMPOSE_STYLE_BLOCK,
    COMPOSE_SYSTEM_PROMPT,
    JUDGE_PROMPT_VERSION,
    JUDGE_SYSTEM_PROMPT,
    SCREEN_PROMPT_VERSION,
    SCREEN_SYSTEM_PROMPT,
    STYLE_BLOCK,
    STYLE_REFERENCE_LABELS,
    STYLE_SCREENSHOTS_NOTE,
)
from photo_chooser.judge.transport import Job, ModelRequest, Transport, _quiet
from photo_chooser.models import Asset, Judgment, Screening
from photo_chooser.select import Posts, ShotFacts, validate_composition

if TYPE_CHECKING:
    from photo_chooser.style import StyleContext

T = TypeVar("T")


class ScreenMatch(BaseModel):
    theme: int
    relevance: int


class ScreenItem(BaseModel):
    id: str
    matches: list[ScreenMatch]
    score: int


class ScreenOutput(BaseModel):
    items: list[ScreenItem]


class JudgeItem(BaseModel):
    id: str
    matches_theme: bool
    score: int
    interesting: bool
    reason: str
    crop_feed: str  # обязательные: у вертикальных кадров модель возвращает пустую строку
    crop_stories: str


class JudgeOutput(BaseModel):
    items: list[JudgeItem]


class ComposeSlide(BaseModel):
    id: str
    alternatives: list[str]


class ComposeCarousel(BaseModel):
    title: str
    slides: list[ComposeSlide]


class ComposeStory(BaseModel):
    id: str
    kind: Literal["best", "interesting"]
    hint: str


class ComposeOutput(BaseModel):
    carousels: list[ComposeCarousel]
    stories: list[ComposeStory]


@dataclass
class GroupResult(Generic[T]):
    items: list[T]
    unjudged: list[str]
    refused: bool
    usage: TokenUsage


@dataclass
class StageOutcome(Generic[T]):
    results: dict[str, T]  # из кэша и новые
    unjudged: list[str]  # отправлены, но без оценки: пропущенный id или refusal
    sent: int  # снимков отправлено в этом запуске
    usd: float  # фактическая стоимость этого запуска
    from_cache: int = 0
    refused_groups: int = 0
    error: Exception | None = None  # ошибка API: тема не обработана, остальные продолжаются
    usage: TokenUsage = TokenUsage()  # токены запросов этого запуска (кэш — для строки отчёта)


@dataclass
class JudgeTask:
    theme: Theme
    assets: list[Asset]  # кандидаты темы
    order: dict[str, float]  # порядок раздачи по группам: в каждой группе разброс черновых оценок


@dataclass
class ComposeTask:
    theme: Theme
    cands: list[Asset]
    facts: dict[str, ShotFacts]


def orientation(a: Asset) -> str:
    return "square" if a.width == a.height else ("vertical" if a.is_vertical else "horizontal")


def image_content(group: list[Asset], preview: Callable[[Asset], bytes], with_orientation: bool = False) -> list[dict]:
    content = []
    for i, a in enumerate(group, 1):
        with objc.autorelease_pool():  # превью кодируются через ImageIO в рабочих потоках
            data = base64.standard_b64encode(preview(a)).decode()
        label = f"id: p{i:02d}" + (f" ({orientation(a)})" if with_orientation else "")
        content.append({"type": "text", "text": label})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
    return content


def make_groups(assets: list[Asset], order: dict[str, float], size: int) -> list[list[Asset]]:
    """ceil(n/size) групп; снимки раздаются по кругу в порядке убывания order — в каждой группе разброс оценок."""
    if not assets:
        return []
    ranked = sorted(assets, key=lambda a: -order.get(a.uuid, 0.0))
    groups: list[list[Asset]] = [[] for _ in range(math.ceil(len(assets) / size))]
    for i, a in enumerate(ranked):
        groups[i % len(groups)].append(a)
    return groups


def theme_text(theme: Theme) -> str:
    if theme.query is None or theme.query.strip() == theme.name.strip():
        return theme.name
    return f"{theme.name}: {theme.query}"


def _clamp(v: int) -> int:
    return max(1, min(10, int(v)))


def _parse_group(response, uuids: list[str], output_model: type[BaseModel], convert) -> GroupResult:
    ids = {f"p{i:02d}": u for i, u in enumerate(uuids, 1)}
    usage = TokenUsage.of(response.usage)
    if response.stop_reason == "refusal":
        return GroupResult(items=[], unjudged=list(uuids), refused=True, usage=usage)
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        parsed = output_model.model_validate_json(text) if text else None
    except pydantic.ValidationError:
        parsed = None  # обрезанный или неполный ответ: группа остаётся неоценённой, usage учтён
    items, seen = [], set()
    for item in (parsed.items if parsed else []):
        uuid = ids.get(item.id.strip().split(" ")[0])  # модель может повторить подпись «p01 (horizontal)»
        if uuid is None or uuid in seen:
            continue
        seen.add(uuid)
        items.append(convert(uuid, item))
    return GroupResult(items=items, unjudged=[u for u in uuids if u not in seen], refused=False, usage=usage)


def _check_group_price(groups: list[list[Asset]], cfg: CloudCfg, extra_tokens: int = 0) -> None:
    for g in groups:
        tokens = group_input_tokens([(a.width, a.height) for a in g], cfg.preview_long_side_px) + extra_tokens
        if tokens >= HAIKU_PRICE_LIMIT_TOKENS:
            raise ValueError(f"Группа из {len(g)} превью ≈ {tokens} входных токенов — больше ценового порога "
                             f"{HAIKU_PRICE_LIMIT_TOKENS}. Уменьшите cloud.group_size или preview_long_side_px.")


def _merge(outcome: StageOutcome, result: GroupResult, usd: float, lock: threading.Lock) -> None:
    """Вызывается из рабочих потоков транспорта."""
    with lock:
        outcome.usd += usd
        outcome.usage += result.usage
        outcome.results.update({x.uuid: x for x in result.items})
        outcome.unjudged.extend(result.unjudged)
        outcome.refused_groups += int(result.refused)


class ScreenRunner:
    def __init__(self, model: str, effort: str, preview: Callable[[Asset], bytes]):
        self.model, self.effort, self.preview = model, effort, preview

    def request(self, album_title: str, themes: list[Theme], group: list[Asset], custom_id: str) -> ModelRequest:
        listing = "\n".join(f"{n}. {theme_text(t)}" for n, t in enumerate(themes, 1))
        content = image_content(group, self.preview)
        content.append({"type": "text", "text": f"Trip album: «{album_title}»\nThemes:\n{listing}"})
        return ModelRequest(custom_id, self.model, self.effort, SCREEN_SYSTEM_PROMPT, content, ScreenOutput)

    def parse(self, response, themes: list[Theme], uuids: list[str]) -> GroupResult[Screening]:
        def convert(uuid: str, item: ScreenItem) -> Screening:
            relevance: dict[str, int] = {}
            for m in item.matches:
                if 1 <= m.theme <= len(themes):
                    key = themes[m.theme - 1].screen_key()
                    relevance[key] = max(relevance.get(key, 0), _clamp(m.relevance))
            return Screening(uuid=uuid, relevance=relevance, score=_clamp(item.score))

        return _parse_group(response, uuids, ScreenOutput, convert)


def _cache_marker(ttl: str) -> dict:
    return {"type": "ephemeral"} if ttl == "5m" else {"type": "ephemeral", "ttl": ttl}


def _mark_prefix(system: str, references: list[dict], ttl: str) -> tuple[str | list[dict], list[dict]]:
    """Метка кэша на последнем блоке общего префикса (D15): последний скриншот-образец, без них — system.
    Тема и превью кандидатов идут после метки и в кэш не попадают."""
    images = [i for i, b in enumerate(references) if b["type"] == "image"]
    if images:
        references[images[-1]] = {**references[images[-1]], "cache_control": _cache_marker(ttl)}
        return system, references
    return [{"type": "text", "text": system, "cache_control": _cache_marker(ttl)}], references


def reference_content(style: "StyleContext | None") -> list[dict]:
    """Скриншоты страницы перед кандидатами: у каждого подпись — грид ленты или stories (FR-11.4)."""
    content: list[dict] = []
    for kind, data in (style.references if style else []):
        content.append({"type": "text", "text": STYLE_REFERENCE_LABELS[kind]})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(data).decode()}})
    return content


def judge_prompt_version(style: "StyleContext | None") -> str:
    """Версия промпта второй ступени для ключа кэша: со стилем в неё входит отпечаток профиля (FR-9.3)."""
    return JUDGE_PROMPT_VERSION if style is None else f"{JUDGE_PROMPT_VERSION}/style-{style.fingerprint}"


class JudgeRunner:
    def __init__(self, model: str, effort: str, preview: Callable[[Asset], bytes],
                 style: "StyleContext | None" = None, cache_ttl: Literal["5m", "1h"] = "5m"):
        self.model, self.effort, self.preview, self.style = model, effort, preview, style
        self.cache_ttl = cache_ttl

    @property
    def prompt_version(self) -> str:
        return judge_prompt_version(self.style)

    @property
    def extra_input_tokens(self) -> int:
        return self.style.extra_tokens if self.style else 0

    def request(self, theme: Theme, group: list[Asset], custom_id: str) -> ModelRequest:
        system = JUDGE_SYSTEM_PROMPT
        if self.style is not None:
            system += "\n\n" + STYLE_BLOCK.format(text=self.style.text)
            if self.style.references:  # оговорка о скриншотах — только когда они приложены
                system += "\n" + STYLE_SCREENSHOTS_NOTE
        marked, content = _mark_prefix(system, reference_content(self.style), self.cache_ttl)
        content += image_content(group, self.preview, with_orientation=True)
        content.append({"type": "text", "text": f"Theme: «{theme_text(theme)}»"})
        return ModelRequest(custom_id, self.model, self.effort, marked, content, JudgeOutput, self.cache_ttl)

    def parse(self, response, shots: list[tuple[str, str]]) -> GroupResult[Judgment]:
        """shots — (uuid, ориентация) кадров группы по порядку p01…"""
        orientations = dict(shots)

        def convert(uuid: str, item: JudgeItem) -> Judgment:
            vertical = orientations[uuid] == "vertical"  # вертикальный кадр кадрировать не нужно
            return Judgment(uuid=uuid, matches_theme=bool(item.matches_theme), score=_clamp(item.score),
                            reason=item.reason.strip(), interesting=bool(item.interesting),
                            crop_feed="" if vertical else item.crop_feed.strip(),
                            crop_stories="" if vertical else item.crop_stories.strip())

        return _parse_group(response, [uuid for uuid, _ in shots], JudgeOutput, convert)


def run_screen(runner: ScreenRunner, transport: Transport, cache: Cache, album_title: str, album_uuid: str,
               themes: list[Theme], assets: list[Asset], cfg: CloudCfg,
               log: Callable[[str], None] = _quiet) -> StageOutcome[Screening]:
    """Первая ступень по всем темам с query. Ошибка поднимается: без неё темы с query не обработать."""
    key = themes_key(album_title, themes)
    cached, todo = {}, []
    for a in assets:
        s = cache.get_screening(a.uuid, key, runner.model, SCREEN_PROMPT_VERSION)
        if s is None:
            todo.append(a)
        else:
            cached[a.uuid] = s
    todo.sort(key=lambda a: a.taken_at)
    groups = [todo[i:i + cfg.group_size] for i in range(0, len(todo), cfg.group_size)]
    log(f"Первая ступень ({runner.model}): {len(assets)} снимков, из кэша {len(cached)}, групп {len(groups)}")
    price = price_for(cfg, runner.model)
    _check_group_price(groups, cfg)
    outcome = StageOutcome(results=dict(cached), unjudged=[], sent=len(todo), usd=0.0, from_cache=len(cached))
    lock = threading.Lock()

    def job(n: int, group: list[Asset]) -> Job:
        uuids = [a.uuid for a in group]

        def handle(response) -> None:
            result = runner.parse(response, themes, uuids)
            cache.put_screenings(result.items, key, runner.model, SCREEN_PROMPT_VERSION)
            usd = usage_usd(result.usage, price)
            cache.add_spend(album_uuid, runner.model, usd)
            _merge(outcome, result, usd, lock)

        return Job(lambda: runner.request(album_title, themes, group, f"s-{n:03d}"), "screen", {}, handle)

    failures = transport.run("screen", [job(n, g) for n, g in enumerate(groups, 1)], log)
    if failures:
        raise failures[0].error
    return outcome


def run_judge(runner: JudgeRunner, transport: Transport, cache: Cache, tasks: list[JudgeTask], cfg: CloudCfg,
              album_uuid: str, log: Callable[[str], None] = _quiet) -> dict[str, StageOutcome[Judgment]]:
    """Вторая ступень всех тем одним вызовом транспорта; исходы — по theme.key()."""
    version = runner.prompt_version
    lock = threading.Lock()

    def handler_for(meta: dict, outcome: StageOutcome | None = None) -> Callable[[object], None]:
        shots = [(uuid, orient) for uuid, orient in meta["shots"]]
        price = price_for(cfg, meta["model"])

        def handle(response) -> None:
            result = runner.parse(response, shots)
            cache.put_judgments(result.items, meta["theme_key"], meta["model"], meta["prompt_version"])
            usd = usage_usd(result.usage, price, cache_ttl=meta.get("cache_ttl") or "5m", batch=meta["batch"])
            cache.add_spend(album_uuid, meta["model"], usd)
            if outcome is not None:
                _merge(outcome, result, usd, lock)

        return handle

    transport.resume("judge", handler_for, log)
    price_for(cfg, runner.model)
    log(f"Вторая ступень ({runner.model}{', пакет' if transport.batch else ''}): тем {len(tasks)}")
    outcomes: dict[str, StageOutcome[Judgment]] = {}
    jobs: list[Job] = []
    for t in tasks:
        key = t.theme.key()
        cached, todo = {}, []
        for a in t.assets:
            j = cache.get_judgment(a.uuid, key, runner.model, version)
            if j is None:
                todo.append(a)
            else:
                cached[a.uuid] = j
        groups = make_groups(todo, t.order, cfg.group_size)
        log(f"  «{t.theme.name}»: кандидатов {len(t.assets)}, из кэша {len(cached)}, групп {len(groups)}")
        _check_group_price(groups, cfg, runner.extra_input_tokens)
        outcome = outcomes[key] = StageOutcome(results=dict(cached), unjudged=[], sent=len(todo), usd=0.0,
                                               from_cache=len(cached))
        for n, g in enumerate(groups, 1):
            meta = {"theme_key": key, "model": runner.model, "prompt_version": version, "batch": transport.batch,
                    "cache_ttl": runner.cache_ttl, "shots": [[a.uuid, orientation(a)] for a in g]}
            jobs.append(Job(lambda theme=t.theme, g=g, cid=f"j-{key}-{n:02d}": runner.request(theme, g, cid), key,
                            meta, handler_for(meta, outcome)))
    for failure in transport.run("judge", jobs, log):
        outcomes[failure.tag].error = outcomes[failure.tag].error or failure.error
    return outcomes


# --- сборка постов (SPEC FR-4.2–FR-4.4) -------------------------------------------------------


@dataclass
class ComposeResult:
    output: ComposeOutput | None  # None — отказ или неразборчивый ответ
    refused: bool
    usage: TokenUsage


@dataclass
class ComposeOutcome:
    posts: Posts | None  # None — сборка не удалась, конвейер раскладывает по оценкам (FR-4.4)
    usd: float
    from_cache: bool
    refused: bool
    note: str = ""  # почему сборка не удалась, если не отказ
    usage: TokenUsage = TokenUsage()


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}"


def _compose_style_system(system: str, style: "StyleContext | None") -> str:
    """Стиль для сборки — без языка оценок: сборка выбирает кадры, а не ставит им баллы."""
    if style is None:
        return system
    system += "\n\n" + COMPOSE_STYLE_BLOCK.format(text=style.text)
    return system + ("\n" + COMPOSE_SCREENSHOTS_NOTE if style.references else "")


class ComposeRunner:
    def __init__(self, model: str, effort: str, preview: Callable[[Asset], bytes],
                 style: "StyleContext | None" = None, cache_ttl: Literal["5m", "1h"] = "5m"):
        self.model, self.effort, self.preview, self.style = model, effort, preview, style
        self.cache_ttl = cache_ttl

    def request(self, theme: Theme, cands: list[Asset], facts: dict[str, ShotFacts], limits: PostsCfg,
                custom_id: str) -> ModelRequest:
        system = _compose_style_system(COMPOSE_SYSTEM_PROMPT.format(carousels=limits.carousels,
                                                                    carousel_size=limits.carousel_size,
                                                                    stories=limits.stories), self.style)
        system, content = _mark_prefix(system, reference_content(self.style), self.cache_ttl)
        for i, a in enumerate(cands, 1):
            f = facts[a.uuid]
            with objc.autorelease_pool():
                data = base64.standard_b64encode(self.preview(a)).decode()
            reason = " ".join(f.reason.split())
            mark = " · интересное" if f.interesting else ""
            content.append({"type": "text", "text": f"id: c{i:02d} ({orientation(a)}) · сила {_num(f.score)}{mark} · "
                                                    f"«{reason}»"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
        content.append({"type": "text", "text": f"Theme: «{theme_text(theme)}»"})
        return ModelRequest(custom_id, self.model, self.effort, system, content, ComposeOutput, self.cache_ttl)

    def parse(self, response) -> ComposeResult:
        usage = TokenUsage.of(response.usage)
        if response.stop_reason == "refusal":
            return ComposeResult(output=None, refused=True, usage=usage)
        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            output = ComposeOutput.model_validate_json(text) if text else None
        except pydantic.ValidationError:
            output = None
        return ComposeResult(output=output, refused=False, usage=usage)


def compose_input_fingerprint(cands: list[Asset], facts: dict[str, ShotFacts], limits: PostsCfg,
                              style: "StyleContext | None") -> str:
    """Отпечаток входа сборки: кандидаты по порядку с оценками, лимиты и профиль стиля (FR-9.3)."""
    parts = [f"{a.uuid}:{_num(facts[a.uuid].score)}:{int(facts[a.uuid].interesting)}" for a in cands]
    parts += [f"limits:{limits.carousels}/{limits.carousel_size}/{limits.stories}",
              f"style:{style.fingerprint if style else '-'}"]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _answer_known(output: ComposeOutput, known: dict[str, str]) -> bool:
    """Пустой ответ — валидное «ничего»; ответ только с неизвестными id — сбой, его не кэшируем."""
    ids = [s.id for c in output.carousels for s in c.slides] + [s.id for s in output.stories]
    return not ids or any(i in known for i in ids)


def _compose_outcome(result: ComposeResult, usd: float, ids: dict[str, Asset], limits: PostsCfg) -> ComposeOutcome:
    paid = dict(usd=usd, from_cache=False, usage=result.usage)
    if result.output is None:
        return ComposeOutcome(posts=None, refused=result.refused, note="" if result.refused else "неразборчивый ответ",
                              **paid)
    posts = validate_composition(result.output, ids, limits)
    answered = any(c.slides for c in result.output.carousels) or bool(result.output.stories)
    if answered and not posts.carousels and not posts.stories:  # модель ответила, но ни одного известного кадра
        return ComposeOutcome(posts=None, refused=False, note="в ответе нет известных кадров", **paid)
    return ComposeOutcome(posts=posts, refused=False, **paid)


def run_compose(runner: ComposeRunner, transport: Transport, cache: Cache, tasks: list[ComposeTask], cfg: AppConfig,
                album_uuid: str, log: Callable[[str], None] = _quiet) -> dict[str, ComposeOutcome]:
    """Один запрос сборки на тему, все темы одним вызовом транспорта; исходы — по theme.key(). Ошибка API
    в запросе темы — сбой сборки этой темы (FR-4.4); ошибки ключа и баланса поднимаются."""
    answers: dict[str, tuple[ComposeResult, float]] = {}
    lock = threading.Lock()

    def handler_for(meta: dict, key: str | None = None) -> Callable[[object], None]:
        price = price_for(cfg.cloud, meta["model"])

        def handle(response) -> None:
            result = runner.parse(response)
            usd = usage_usd(result.usage, price, cache_ttl=meta.get("cache_ttl") or "5m", batch=meta["batch"])
            cache.add_spend(album_uuid, meta["model"], usd)
            if result.output is not None and _answer_known(result.output, meta["ids"]):
                cache.put_composition(meta["theme_key"], meta["model"], meta["prompt_version"], meta["input_fp"],
                                      result.output.model_dump_json())
            if key is not None:
                with lock:
                    answers[key] = (result, usd)

        return handle

    transport.resume("compose", handler_for, log)
    if tasks:
        log(f"Сборка постов ({runner.model}{', пакет' if transport.batch else ''}): тем {len(tasks)}")
    outcomes: dict[str, ComposeOutcome] = {}
    sent: dict[str, dict[str, Asset]] = {}
    jobs: list[Job] = []
    for t in tasks:
        key = t.theme.key()
        if not t.cands:
            outcomes[key] = ComposeOutcome(posts=Posts([], []), usd=0.0, from_cache=False, refused=False)
            continue
        ids = {f"c{i:02d}": a for i, a in enumerate(t.cands, 1)}
        fingerprint = compose_input_fingerprint(t.cands, t.facts, cfg.posts, runner.style)
        cached = cache.get_composition(key, runner.model, COMPOSE_PROMPT_VERSION, fingerprint)
        if cached is not None:
            log(f"  «{t.theme.name}»: сборка из кэша")
            posts = validate_composition(ComposeOutput.model_validate_json(cached), ids, cfg.posts)
            outcomes[key] = ComposeOutcome(posts=posts, usd=0.0, from_cache=True, refused=False)
            continue
        tokens = (group_input_tokens([(a.width, a.height) for a in t.cands], cfg.cloud.preview_long_side_px)
                  + COMPOSE_LABEL_TOKENS * len(t.cands) + (runner.style.extra_tokens if runner.style else 0))
        if tokens >= HAIKU_PRICE_LIMIT_TOKENS:
            raise ValueError(f"Запрос сборки из {len(t.cands)} превью ≈ {tokens} входных токенов — больше ценового "
                             f"порога {HAIKU_PRICE_LIMIT_TOKENS}. Уменьшите cloud.compose_candidates.")
        log(f"  «{t.theme.name}»: кандидатов {len(t.cands)}")
        meta = {"theme_key": key, "model": runner.model, "prompt_version": COMPOSE_PROMPT_VERSION,
                "batch": transport.batch, "cache_ttl": runner.cache_ttl, "input_fp": fingerprint,
                "ids": {cid: a.uuid for cid, a in ids.items()}}
        sent[key] = ids
        jobs.append(Job(lambda t=t, cid=f"c-{key}": runner.request(t.theme, t.cands, t.facts, cfg.posts, cid), key,
                        meta, handler_for(meta, key)))
    failures = {f.tag: f.error for f in transport.run("compose", jobs, log)}
    for key, ids in sent.items():
        if key in failures:
            e = failures[key]
            outcomes[key] = ComposeOutcome(posts=None, usd=0.0, from_cache=False, refused=False,
                                           note=f"{type(e).__name__}: {e}")
        else:
            result, usd = answers[key]
            outcomes[key] = _compose_outcome(result, usd, ids, cfg.posts)
    return outcomes
