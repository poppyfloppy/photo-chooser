"""Облачные ступени: первая — отбор по всем темам сразу, вторая — оценка кандидатов темы (SPEC FR-3 п.5, п.8).

Обе ступени: группы превью, структурированный ответ по pydantic-схеме, кэш каждой группы сразу после
ответа и учёт фактической стоимости по usage.
"""

import base64
import hashlib
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

import anthropic
import objc
import pydantic
from pydantic import BaseModel

from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig, CloudCfg, ConfigError, PostsCfg, Theme, themes_key
from photo_chooser.judge.cost import (
    COMPOSE_LABEL_TOKENS,
    HAIKU_PRICE_LIMIT_TOKENS,
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
from photo_chooser.models import Asset, Judgment, Screening
from photo_chooser.select import Posts, ShotFacts, validate_composition

if TYPE_CHECKING:
    from photo_chooser.style import StyleContext

MAX_TOKENS = 16000
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PARALLEL_REQUESTS = 3
RATE_LIMIT_SLEEP_S = 20
RATE_LIMIT_ATTEMPTS = 12
REQUEST_TIMEOUT_S = 300  # без ответа дольше — повтор (SPEC FR-7.2); у SDK по умолчанию 10 минут
TRANSIENT_RETRIES = 4  # таймаут, сбой сети, ошибка сервера 5xx
WAIT_NOTICE_S = 30  # строка ожидания, если ответа нет дольше

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
    input_tokens: int
    output_tokens: int


@dataclass
class StageOutcome(Generic[T]):
    results: dict[str, T]  # из кэша и новые
    unjudged: list[str]  # отправлены, но без оценки: пропущенный id или refusal
    sent: int  # снимков отправлено в этом запуске
    usd: float  # фактическая стоимость этого запуска
    from_cache: int = 0
    refused_groups: int = 0


def make_client() -> anthropic.Anthropic:
    """Ключ — из ANTHROPIC_API_KEY или профиля `ant auth login`. Повторы SDK выключены: их делает call_model,
    чтобы о каждом было видно в выводе."""
    return anthropic.Anthropic(max_retries=0, timeout=REQUEST_TIMEOUT_S)


def check_credentials(client) -> None:
    """SDK создаёт клиента и без ключа и падает только на первом запросе — проверяем заранее."""
    if isinstance(client, anthropic.Anthropic) and not (client.api_key or client.auth_token or client.credentials):
        raise ConfigError("Нет ключа Claude API: задайте ANTHROPIC_API_KEY или выполните `ant auth login`. "
                          "Без облака можно запустить с --no-cloud.")


def _quiet(message: str) -> None:
    pass


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _transient(e: Exception) -> str | None:
    """Почему запрос стоит повторить, или None: таймаут, сбой сети, ошибка сервера."""
    if isinstance(e, anthropic.APITimeoutError):
        return f"нет ответа {REQUEST_TIMEOUT_S // 60} мин"
    if isinstance(e, anthropic.APIConnectionError):
        return "сбой сети"
    if isinstance(e, anthropic.APIStatusError) and e.status_code >= 500:
        return f"ошибка сервера {e.status_code}"
    return None


def call_model(client, model: str, effort: str, system: str, content: list[dict],
               output_model: type[BaseModel] | None, notify: Callable[[str], None] = _quiet):
    """messages.create со схемой ответа в output_config.format (без схемы — свободный текст): usage и stop_reason
    доступны и тогда, когда текст ответа не разбирается (refusal с текстом, обрезка по max_tokens).
    Пауза при 429 и повторы после таймаута, сбоя сети и 5xx сообщаются через notify (FR-7.2)."""
    output_config: dict = {"effort": effort}
    if output_model is not None:
        output_config["format"] = {"type": "json_schema", "schema": anthropic.transform_schema(output_model)}
    kwargs = dict(model=model, max_tokens=MAX_TOKENS, system=system,
                  messages=[{"role": "user", "content": content}], output_config=output_config)
    attempt, retries = 1, 0
    while True:
        try:
            if model in FALLBACK_MODELS:
                return client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            # лимит организации — входные токены в минуту: ждём и пробуем снова
            if attempt == RATE_LIMIT_ATTEMPTS:
                raise
            attempt += 1
            notify(f"  лимит API, пауза {RATE_LIMIT_SLEEP_S} с (попытка {attempt}/{RATE_LIMIT_ATTEMPTS})")
            time.sleep(RATE_LIMIT_SLEEP_S)
        except anthropic.APIError as e:
            why = _transient(e)
            if why is None or retries == TRANSIENT_RETRIES:
                raise
            retries += 1
            notify(f"  {why} — повторяю запрос ({retries}/{TRANSIENT_RETRIES})")
            time.sleep(min(2 ** retries, 30))


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


def _parse_group(response, group: list[Asset], output_model: type[BaseModel], convert) -> GroupResult:
    ids = {f"p{i:02d}": a for i, a in enumerate(group, 1)}
    usage = response.usage
    tokens = dict(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    if response.stop_reason == "refusal":
        return GroupResult(items=[], unjudged=[a.uuid for a in group], refused=True, **tokens)
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        parsed = output_model.model_validate_json(text) if text else None
    except pydantic.ValidationError:
        parsed = None  # обрезанный или неполный ответ: группа остаётся неоценённой, usage учтён
    items, seen = [], set()
    for item in (parsed.items if parsed else []):
        a = ids.get(item.id.strip().split(" ")[0])  # модель может повторить подпись «p01 (horizontal)»
        if a is None or a.uuid in seen:
            continue
        seen.add(a.uuid)
        items.append(convert(a, item))
    return GroupResult(items=items, unjudged=[a.uuid for a in group if a.uuid not in seen], refused=False, **tokens)


class ScreenRunner:
    def __init__(self, client, model: str, effort: str, preview: Callable[[Asset], bytes],
                 notify: Callable[[str], None] = _quiet):
        self.client, self.model, self.effort, self.preview, self.notify = client, model, effort, preview, notify

    def run_group(self, album_title: str, themes: list[Theme], group: list[Asset]) -> GroupResult[Screening]:
        listing = "\n".join(f"{n}. {theme_text(t)}" for n, t in enumerate(themes, 1))
        content = image_content(group, self.preview)
        content.append({"type": "text", "text": f"Trip album: «{album_title}»\nThemes:\n{listing}"})
        response = call_model(self.client, self.model, self.effort, SCREEN_SYSTEM_PROMPT, content, ScreenOutput,
                              self.notify)

        def convert(a: Asset, item: ScreenItem) -> Screening:
            relevance: dict[str, int] = {}
            for m in item.matches:
                if 1 <= m.theme <= len(themes):
                    key = themes[m.theme - 1].screen_key()
                    relevance[key] = max(relevance.get(key, 0), _clamp(m.relevance))
            return Screening(uuid=a.uuid, relevance=relevance, score=_clamp(item.score))

        return _parse_group(response, group, ScreenOutput, convert)


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
    def __init__(self, client, model: str, effort: str, preview: Callable[[Asset], bytes],
                 style: "StyleContext | None" = None, notify: Callable[[str], None] = _quiet):
        self.client, self.model, self.effort, self.preview = client, model, effort, preview
        self.style, self.notify = style, notify

    @property
    def prompt_version(self) -> str:
        return judge_prompt_version(self.style)

    @property
    def extra_input_tokens(self) -> int:
        return self.style.extra_tokens if self.style else 0

    def run_group(self, theme: Theme, group: list[Asset]) -> GroupResult[Judgment]:
        system, content = JUDGE_SYSTEM_PROMPT, []
        if self.style is not None:
            system += "\n\n" + STYLE_BLOCK.format(text=self.style.text)
            if self.style.references:  # оговорка о скриншотах — только когда они приложены
                system += "\n" + STYLE_SCREENSHOTS_NOTE
            content += reference_content(self.style)
        content += image_content(group, self.preview, with_orientation=True)
        content.append({"type": "text", "text": f"Theme: «{theme_text(theme)}»"})
        response = call_model(self.client, self.model, self.effort, system, content, JudgeOutput, self.notify)

        def convert(a: Asset, item: JudgeItem) -> Judgment:
            vertical = orientation(a) == "vertical"  # вертикальный кадр кадрировать не нужно
            return Judgment(uuid=a.uuid, matches_theme=bool(item.matches_theme), score=_clamp(item.score),
                            reason=item.reason.strip(), interesting=bool(item.interesting),
                            crop_feed="" if vertical else item.crop_feed.strip(),
                            crop_stories="" if vertical else item.crop_stories.strip())

        return _parse_group(response, group, JudgeOutput, convert)


def _run_groups(groups: list[list[Asset]], call: Callable[[list[Asset]], GroupResult],
                record: Callable[[GroupResult], None], cfg: CloudCfg, extra_tokens: int = 0,
                log: Callable[[str], None] = _quiet, label: str = "") -> None:
    """До MAX_PARALLEL_REQUESTS запросов сразу. Ответ записывается в кэш и расход прямо в рабочем потоке —
    оплаченная группа не теряется ни при ошибке другой группы, ни при Ctrl+C. После ошибки новые группы
    не отправляются, уже начатые доделываются; затем ошибка поднимается. Каждая готовая группа и ожидание
    ответа дольше WAIT_NOTICE_S пишутся в log (FR-7.2)."""
    for g in groups:
        tokens = group_input_tokens([(a.width, a.height) for a in g], cfg.preview_long_side_px) + extra_tokens
        if tokens >= HAIKU_PRICE_LIMIT_TOKENS:
            raise ValueError(f"Группа из {len(g)} превью ≈ {tokens} входных токенов — больше ценового порога "
                             f"{HAIKU_PRICE_LIMIT_TOKENS}. Уменьшите cloud.group_size или preview_long_side_px.")
    pending = iter(groups)
    error: BaseException | None = None
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as pool:
        running = set()

        def fill():
            while error is None and len(running) < MAX_PARALLEL_REQUESTS:
                g = next(pending, None)
                if g is None:
                    return
                running.add(pool.submit(lambda group: record(call(group)), g))

        fill()
        start = last = time.monotonic()
        finished = 0
        while running:
            done, running = wait(running, timeout=WAIT_NOTICE_S, return_when=FIRST_COMPLETED)
            if not done:
                log(f"  {label}: ждём ответ модели ({_mmss(time.monotonic() - last)}), "
                    f"готово групп {finished}/{len(groups)}")
                continue
            for f in done:
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001 — первая ошибка поднимается после уже начатых групп
                    error = error or e
                else:
                    finished += 1
                    log(f"  {label}: группа {finished}/{len(groups)} ({_mmss(time.monotonic() - start)})")
            last = time.monotonic()
            fill()
    if error is not None:
        raise error


def _run_stage(todo: list[Asset], groups: list[list[Asset]], cached: dict, call, put, model: str,
               cache: Cache, cfg: CloudCfg, album_uuid: str, extra_tokens: int = 0,
               log: Callable[[str], None] = _quiet, label: str = "") -> StageOutcome:
    price = price_for(cfg, model)
    outcome = StageOutcome(results=dict(cached), unjudged=[], sent=len(todo), usd=0.0, from_cache=len(cached))
    lock = threading.Lock()

    def record(result: GroupResult) -> None:  # вызывается из рабочих потоков
        put(result.items)
        usd = usage_usd(result.input_tokens, result.output_tokens, price)
        cache.add_spend(album_uuid, model, usd)
        with lock:
            outcome.usd += usd
            outcome.results.update({x.uuid: x for x in result.items})
            outcome.unjudged.extend(result.unjudged)
            outcome.refused_groups += int(result.refused)

    _run_groups(groups, call, record, cfg, extra_tokens, log, label)
    return outcome


def run_screen(runner: ScreenRunner, cache: Cache, album_title: str, album_uuid: str, themes: list[Theme],
               assets: list[Asset], cfg: CloudCfg, log: Callable[[str], None] = _quiet) -> StageOutcome[Screening]:
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
    return _run_stage(todo, groups, cached, lambda g: runner.run_group(album_title, themes, g),
                      lambda items: cache.put_screenings(items, key, runner.model, SCREEN_PROMPT_VERSION),
                      runner.model, cache, cfg, album_uuid, log=log, label="первая ступень")


def run_judge(runner: JudgeRunner, cache: Cache, theme: Theme, assets: list[Asset], order: dict[str, float],
              cfg: CloudCfg, album_uuid: str, log: Callable[[str], None] = _quiet) -> StageOutcome[Judgment]:
    key, version = theme.key(), runner.prompt_version
    cached, todo = {}, []
    for a in assets:
        j = cache.get_judgment(a.uuid, key, runner.model, version)
        if j is None:
            todo.append(a)
        else:
            cached[a.uuid] = j
    groups = make_groups(todo, order, cfg.group_size)
    log(f"  вторая ступень ({runner.model}): кандидатов {len(assets)}, из кэша {len(cached)}, групп {len(groups)}")
    return _run_stage(todo, groups, cached, lambda g: runner.run_group(theme, g),
                      lambda items: cache.put_judgments(items, key, runner.model, version),
                      runner.model, cache, cfg, album_uuid, runner.extra_input_tokens, log, "вторая ступень")



# --- сборка постов (SPEC FR-4.2–FR-4.4) -------------------------------------------------------


@dataclass
class ComposeResult:
    output: ComposeOutput | None  # None — отказ или неразборчивый ответ
    refused: bool
    input_tokens: int
    output_tokens: int


@dataclass
class ComposeOutcome:
    posts: Posts | None  # None — сборка не удалась, конвейер раскладывает по оценкам (FR-4.4)
    usd: float
    from_cache: bool
    refused: bool
    note: str = ""  # почему сборка не удалась, если не отказ


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}"


def _compose_style_system(system: str, style: "StyleContext | None") -> str:
    """Стиль для сборки — без языка оценок: сборка выбирает кадры, а не ставит им баллы."""
    if style is None:
        return system
    system += "\n\n" + COMPOSE_STYLE_BLOCK.format(text=style.text)
    return system + ("\n" + COMPOSE_SCREENSHOTS_NOTE if style.references else "")


class ComposeRunner:
    def __init__(self, client, model: str, effort: str, preview: Callable[[Asset], bytes],
                 style: "StyleContext | None" = None, notify: Callable[[str], None] = _quiet):
        self.client, self.model, self.effort, self.preview, self.style = client, model, effort, preview, style
        self.notify = notify

    def run(self, theme: Theme, cands: list[Asset], facts: dict[str, ShotFacts], limits: PostsCfg) -> ComposeResult:
        system = _compose_style_system(COMPOSE_SYSTEM_PROMPT.format(carousels=limits.carousels,
                                                            carousel_size=limits.carousel_size,
                                                            stories=limits.stories), self.style)
        content = reference_content(self.style)
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
        response = call_model(self.client, self.model, self.effort, system, content, ComposeOutput, self.notify)
        tokens = dict(input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)
        if response.stop_reason == "refusal":
            return ComposeResult(output=None, refused=True, **tokens)
        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            output = ComposeOutput.model_validate_json(text) if text else None
        except pydantic.ValidationError:
            output = None
        return ComposeResult(output=output, refused=False, **tokens)


def compose_input_fingerprint(cands: list[Asset], facts: dict[str, ShotFacts], limits: PostsCfg,
                              style: "StyleContext | None") -> str:
    """Отпечаток входа сборки: кандидаты по порядку с оценками, лимиты и профиль стиля (FR-9.3)."""
    parts = [f"{a.uuid}:{_num(facts[a.uuid].score)}:{int(facts[a.uuid].interesting)}" for a in cands]
    parts += [f"limits:{limits.carousels}/{limits.carousel_size}/{limits.stories}",
              f"style:{style.fingerprint if style else '-'}"]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _wait_with_notices(fn: Callable[[], T], log: Callable[[str], None], label: str) -> T:
    """fn в фоновом потоке, пока основной пишет строку ожидания каждые WAIT_NOTICE_S (FR-7.2). Поток —
    daemon: Ctrl+C прерывает запуск сразу, не дожидаясь ответа."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — исключение поднимается в основном потоке
            box["error"] = e

    worker = threading.Thread(target=target, daemon=True)
    start = time.monotonic()
    worker.start()
    while True:
        worker.join(WAIT_NOTICE_S)
        if not worker.is_alive():
            break
        log(f"  {label}: ждём ответ модели ({_mmss(time.monotonic() - start)})")
    if "error" in box:
        raise box["error"]
    return box["value"]


def run_compose(runner: ComposeRunner, cache: Cache, theme: Theme, cands: list[Asset], facts: dict[str, ShotFacts],
                cfg: AppConfig, album_uuid: str, log: Callable[[str], None] = _quiet) -> ComposeOutcome:
    """Один запрос сборки на тему; ответ кэшируется, расход пишется сразу. Ошибки API поднимаются (FR-4.4)."""
    if not cands:
        return ComposeOutcome(posts=Posts([], []), usd=0.0, from_cache=False, refused=False)
    ids = {f"c{i:02d}": a for i, a in enumerate(cands, 1)}
    fingerprint = compose_input_fingerprint(cands, facts, cfg.posts, runner.style)
    cache_key = (theme.key(), runner.model, COMPOSE_PROMPT_VERSION, fingerprint)
    cached = cache.get_composition(*cache_key)
    if cached is not None:
        log("  сборка постов: из кэша")
        posts = validate_composition(ComposeOutput.model_validate_json(cached), ids, cfg.posts)
        return ComposeOutcome(posts=posts, usd=0.0, from_cache=True, refused=False)
    tokens = (group_input_tokens([(a.width, a.height) for a in cands], cfg.cloud.preview_long_side_px)
              + COMPOSE_LABEL_TOKENS * len(cands) + (runner.style.extra_tokens if runner.style else 0))
    if tokens >= HAIKU_PRICE_LIMIT_TOKENS:
        raise ValueError(f"Запрос сборки из {len(cands)} превью ≈ {tokens} входных токенов — больше ценового порога "
                         f"{HAIKU_PRICE_LIMIT_TOKENS}. Уменьшите cloud.compose_candidates.")
    log(f"  сборка постов ({runner.model}): кандидатов {len(cands)}")
    start = time.monotonic()
    result = _wait_with_notices(lambda: runner.run(theme, cands, facts, cfg.posts), log, "сборка постов")
    log(f"  сборка постов: ответ за {_mmss(time.monotonic() - start)}")
    usd = usage_usd(result.input_tokens, result.output_tokens, price_for(cfg.cloud, runner.model))
    cache.add_spend(album_uuid, runner.model, usd)
    if result.output is None:
        return ComposeOutcome(posts=None, usd=usd, from_cache=False, refused=result.refused,
                              note="" if result.refused else "неразборчивый ответ")
    posts = validate_composition(result.output, ids, cfg.posts)
    answered = any(c.slides for c in result.output.carousels) or bool(result.output.stories)
    if answered and not posts.carousels and not posts.stories:  # модель ответила, но ни одного известного кадра
        return ComposeOutcome(posts=None, usd=usd, from_cache=False, refused=False, note="в ответе нет известных кадров")
    cache.put_composition(*cache_key, result.output.model_dump_json())
    return ComposeOutcome(posts=posts, usd=usd, from_cache=False, refused=False)
