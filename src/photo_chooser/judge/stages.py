"""Облачные ступени: первая — отбор по всем темам сразу, вторая — оценка кандидатов темы (SPEC FR-3 п.5, п.8).

Обе ступени: группы превью, структурированный ответ по pydantic-схеме, кэш каждой группы сразу после
ответа и учёт фактической стоимости по usage.
"""

import base64
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

import anthropic
import objc
import pydantic
from pydantic import BaseModel

from photo_chooser.cache import Cache
from photo_chooser.config import CloudCfg, ConfigError, Theme, themes_key
from photo_chooser.judge.cost import HAIKU_PRICE_LIMIT_TOKENS, group_input_tokens, price_for, usage_usd
from photo_chooser.judge.prompts import (
    JUDGE_PROMPT_VERSION,
    JUDGE_SYSTEM_PROMPT,
    SCREEN_PROMPT_VERSION,
    SCREEN_SYSTEM_PROMPT,
    STYLE_BLOCK,
    STYLE_REFERENCE_LABEL,
    STYLE_SCREENSHOTS_NOTE,
)
from photo_chooser.models import Asset, Judgment, Screening

if TYPE_CHECKING:
    from photo_chooser.style import StyleContext

MAX_TOKENS = 16000
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PARALLEL_REQUESTS = 3
RATE_LIMIT_SLEEP_S = 20
RATE_LIMIT_ATTEMPTS = 12
CLIENT_MAX_RETRIES = 4

T = TypeVar("T")


class ScreenMatch(BaseModel):
    theme: int
    relevance: int


class ScreenItem(BaseModel):
    id: str
    matches: list[ScreenMatch]
    feed_score: int
    stories_score: int


class ScreenOutput(BaseModel):
    items: list[ScreenItem]


class JudgeItem(BaseModel):
    id: str
    matches_theme: bool
    feed_score: int
    stories_score: int
    reason: str


class JudgeOutput(BaseModel):
    items: list[JudgeItem]


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
    """Ключ — из ANTHROPIC_API_KEY или профиля `ant auth login`."""
    return anthropic.Anthropic(max_retries=CLIENT_MAX_RETRIES)


def check_credentials(client) -> None:
    """SDK создаёт клиента и без ключа и падает только на первом запросе — проверяем заранее."""
    if isinstance(client, anthropic.Anthropic) and not (client.api_key or client.auth_token or client.credentials):
        raise ConfigError("Нет ключа Claude API: задайте ANTHROPIC_API_KEY или выполните `ant auth login`. "
                          "Без облака можно запустить с --no-cloud.")


def call_model(client, model: str, effort: str, system: str, content: list[dict],
               output_model: type[BaseModel] | None):
    """messages.create со схемой ответа в output_config.format (без схемы — свободный текст): usage и stop_reason
    доступны и тогда, когда текст ответа не разбирается (refusal с текстом, обрезка по max_tokens)."""
    output_config: dict = {"effort": effort}
    if output_model is not None:
        output_config["format"] = {"type": "json_schema", "schema": anthropic.transform_schema(output_model)}
    kwargs = dict(model=model, max_tokens=MAX_TOKENS, system=system,
                  messages=[{"role": "user", "content": content}], output_config=output_config)
    for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
        try:
            if model in FALLBACK_MODELS:
                return client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            # SDK уже повторил запрос; лимит организации — входные токены в минуту, ждём и пробуем снова.
            if attempt == RATE_LIMIT_ATTEMPTS:
                raise
            time.sleep(RATE_LIMIT_SLEEP_S)


def image_content(group: list[Asset], preview: Callable[[Asset], bytes]) -> list[dict]:
    content = []
    for i, a in enumerate(group, 1):
        with objc.autorelease_pool():  # превью кодируются через ImageIO в рабочих потоках
            data = base64.standard_b64encode(preview(a)).decode()
        content.append({"type": "text", "text": f"id: p{i:02d}"})
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
        a = ids.get(item.id)
        if a is None or a.uuid in seen:
            continue
        seen.add(a.uuid)
        items.append(convert(a, item))
    return GroupResult(items=items, unjudged=[a.uuid for a in group if a.uuid not in seen], refused=False, **tokens)


class ScreenRunner:
    def __init__(self, client, model: str, effort: str, preview: Callable[[Asset], bytes]):
        self.client, self.model, self.effort, self.preview = client, model, effort, preview

    def run_group(self, album_title: str, themes: list[Theme], group: list[Asset]) -> GroupResult[Screening]:
        listing = "\n".join(f"{n}. {theme_text(t)}" for n, t in enumerate(themes, 1))
        content = image_content(group, self.preview)
        content.append({"type": "text", "text": f"Trip album: «{album_title}»\nThemes:\n{listing}"})
        response = call_model(self.client, self.model, self.effort, SCREEN_SYSTEM_PROMPT, content, ScreenOutput)

        def convert(a: Asset, item: ScreenItem) -> Screening:
            relevance: dict[str, int] = {}
            for m in item.matches:
                if 1 <= m.theme <= len(themes):
                    key = themes[m.theme - 1].screen_key()
                    relevance[key] = max(relevance.get(key, 0), _clamp(m.relevance))
            return Screening(uuid=a.uuid, relevance=relevance, feed_score=_clamp(item.feed_score),
                             stories_score=_clamp(item.stories_score))

        return _parse_group(response, group, ScreenOutput, convert)


def judge_prompt_version(style: "StyleContext | None") -> str:
    """Версия промпта второй ступени для ключа кэша: со стилем в неё входит отпечаток профиля (FR-9.3)."""
    return JUDGE_PROMPT_VERSION if style is None else f"{JUDGE_PROMPT_VERSION}/style-{style.fingerprint}"


class JudgeRunner:
    def __init__(self, client, model: str, effort: str, preview: Callable[[Asset], bytes],
                 style: "StyleContext | None" = None):
        self.client, self.model, self.effort, self.preview = client, model, effort, preview
        self.style = style

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
                content.append({"type": "text", "text": STYLE_REFERENCE_LABEL})
                content += [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.standard_b64encode(ref).decode()}}
                            for ref in self.style.references]
        content += image_content(group, self.preview)
        content.append({"type": "text", "text": f"Theme: «{theme_text(theme)}»"})
        response = call_model(self.client, self.model, self.effort, system, content, JudgeOutput)

        def convert(a: Asset, item: JudgeItem) -> Judgment:
            return Judgment(uuid=a.uuid, matches_theme=bool(item.matches_theme), feed_score=_clamp(item.feed_score),
                            stories_score=_clamp(item.stories_score), reason=item.reason.strip())

        return _parse_group(response, group, JudgeOutput, convert)


def _run_groups(groups: list[list[Asset]], call: Callable[[list[Asset]], GroupResult],
                record: Callable[[GroupResult], None], cfg: CloudCfg, extra_tokens: int = 0) -> None:
    """До MAX_PARALLEL_REQUESTS запросов сразу. Ответ записывается в кэш и расход прямо в рабочем потоке —
    оплаченная группа не теряется ни при ошибке другой группы, ни при Ctrl+C. После ошибки новые группы
    не отправляются, уже начатые доделываются; затем ошибка поднимается."""
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
        while running:
            done, running = wait(running, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001 — первая ошибка поднимается после уже начатых групп
                    error = error or e
            fill()
    if error is not None:
        raise error


def _run_stage(todo: list[Asset], groups: list[list[Asset]], cached: dict, call, put, model: str,
               cache: Cache, cfg: CloudCfg, album_uuid: str, extra_tokens: int = 0) -> StageOutcome:
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

    _run_groups(groups, call, record, cfg, extra_tokens)
    return outcome


def run_screen(runner: ScreenRunner, cache: Cache, album_title: str, album_uuid: str, themes: list[Theme],
               assets: list[Asset], cfg: CloudCfg) -> StageOutcome[Screening]:
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
    return _run_stage(todo, groups, cached, lambda g: runner.run_group(album_title, themes, g),
                      lambda items: cache.put_screenings(items, key, runner.model, SCREEN_PROMPT_VERSION),
                      runner.model, cache, cfg, album_uuid)


def run_judge(runner: JudgeRunner, cache: Cache, theme: Theme, assets: list[Asset], order: dict[str, float],
              cfg: CloudCfg, album_uuid: str) -> StageOutcome[Judgment]:
    key, version = theme.key(), runner.prompt_version
    cached, todo = {}, []
    for a in assets:
        j = cache.get_judgment(a.uuid, key, runner.model, version)
        if j is None:
            todo.append(a)
        else:
            cached[a.uuid] = j
    groups = make_groups(todo, order, cfg.group_size)
    return _run_stage(todo, groups, cached, lambda g: runner.run_group(theme, g),
                      lambda items: cache.put_judgments(items, key, runner.model, version),
                      runner.model, cache, cfg, album_uuid, runner.extra_input_tokens)
