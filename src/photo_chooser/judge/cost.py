"""Оценка стоимости облачных ступеней и защита бюджета (SPEC FR-9.1). Константы — docs/spike-findings.md."""

import math
from dataclasses import dataclass

from photo_chooser.config import CloudCfg, Price

IMAGE_TOKEN_DIVISOR = 750
PROMPT_OVERHEAD_TOKENS = 500  # на группу: системный промпт, id, список тем
EST_OUTPUT_TOKENS_SCREEN = 105  # на снимок
EST_OUTPUT_TOKENS_JUDGE = 80  # на снимок, с причиной
EST_OUTPUT_TOKENS_COMPOSE = 1500  # на запрос сборки постов темы
COMPOSE_LABEL_TOKENS = 60  # подпись кандидата сборки: id, ориентация, оценки, причина
HAIKU_PRICE_LIMIT_TOKENS = 100_000  # цена Haiku 5.5 действует для запросов до 100K токенов
CACHE_WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}  # запись кэша — доля цены входа по TTL (FR-9.2)
BATCH_DISCOUNT = 0.5  # Message Batches: скидка на все токены, включая запись и чтение кэша (FR-12)


@dataclass(frozen=True)
class TokenUsage:
    input: int = 0  # входные токены без кэша
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0

    @classmethod
    def of(cls, usage) -> "TokenUsage":
        """Из usage ответа API; поля кэша могут отсутствовать или быть None."""
        return cls(input=usage.input_tokens, output=usage.output_tokens,
                   cache_write=getattr(usage, "cache_creation_input_tokens", None) or 0,
                   cache_read=getattr(usage, "cache_read_input_tokens", None) or 0)

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(self.input + other.input, self.output + other.output, self.cache_write + other.cache_write,
                          self.cache_read + other.cache_read)


class UnknownModelPrice(Exception):
    def __init__(self, model: str):
        self.model = model
        super().__init__(f"Для модели {model} нет цены в cloud.pricing — без неё нельзя проверить бюджет. "
                         f"Добавьте в ~/.config/photo-chooser/config.yaml: cloud.pricing.{model}: "
                         "{input: …, output: …} ($ за 1 млн токенов).")


class BudgetExceeded(Exception):
    def __init__(self, spent: float, estimate: float, limit: float):
        self.spent, self.estimate, self.limit = spent, estimate, limit
        super().__init__(f"Бюджет альбома превышен: уже потрачено ${spent:.2f}, оценка запуска ${estimate:.2f}, "
                         f"лимит ${limit:.2f}. В облако ничего не отправлено. Разово поднять лимит: "
                         f"--max-cost {math.ceil(spent + estimate)}")


def image_tokens(width: int, height: int, long_side: int) -> int:
    scale = min(1.0, long_side / max(width, height))
    return math.ceil(round(width * scale) * round(height * scale) / IMAGE_TOKEN_DIVISOR)


def price_for(cfg: CloudCfg, model: str) -> Price:
    if model not in cfg.pricing:
        raise UnknownModelPrice(model)
    return cfg.pricing[model]


def usage_usd(u: TokenUsage, price: Price, *, cache_ttl: str = "5m", batch: bool = False) -> float:
    """Стоимость по usage (FR-9.2): вход, запись кэша по TTL, чтение кэша, выход; в пакете — со скидкой."""
    usd = (u.input * price.input + u.cache_write * price.input * CACHE_WRITE_MULTIPLIER[cache_ttl]
           + u.cache_read * price.cache_read_price + u.output * price.output) / 1e6
    return usd * BATCH_DISCOUNT if batch else usd


def group_input_tokens(sizes: list[tuple[int, int]], long_side: int) -> int:
    return sum(image_tokens(w, h, long_side) for w, h in sizes) + PROMPT_OVERHEAD_TOKENS


def _stage_usd(n: int, cfg: CloudCfg, output_per_photo: int, price: Price, extra_per_group: int = 0) -> float:
    if n <= 0:
        return 0.0
    side = cfg.preview_long_side_px
    per_image = image_tokens(side, side * 3 // 4, side)
    groups = math.ceil(n / cfg.group_size)
    return usage_usd(TokenUsage(input=n * per_image + groups * (PROMPT_OVERHEAD_TOKENS + extra_per_group),
                                output=n * output_per_photo), price)


def estimate_run_usd(n_screen: int, n_judge: list[int], cfg: CloudCfg, screen_model: str, judge_model: str,
                     judge_extra_tokens: int = 0, n_compose: list[int] | None = None) -> float:
    """Первая ступень — один проход по n_screen снимкам; вторая — n_judge[i] кандидатов темы i. Превью 4:3.
    judge_extra_tokens — профиль стиля и скриншоты-образцы в каждой группе второй ступени (FR-11.6)."""
    total = _stage_usd(n_screen, cfg, EST_OUTPUT_TOKENS_SCREEN, price_for(cfg, screen_model)) if n_screen else 0.0
    if any(n > 0 for n in n_judge):
        judge_price = price_for(cfg, judge_model)
        total += sum(_stage_usd(n, cfg, EST_OUTPUT_TOKENS_JUDGE, judge_price, judge_extra_tokens) for n in n_judge)
    if any(n > 0 for n in n_compose or []):
        judge_price = price_for(cfg, judge_model)
        side = cfg.preview_long_side_px
        per_image = image_tokens(side, side * 3 // 4, side) + COMPOSE_LABEL_TOKENS
        total += sum(usage_usd(TokenUsage(input=n * per_image + PROMPT_OVERHEAD_TOKENS + judge_extra_tokens,
                                          output=EST_OUTPUT_TOKENS_COMPOSE), judge_price) for n in n_compose if n > 0)
    return total


def check_budget(spent: float, estimate: float, limit: float) -> None:
    if spent + estimate > limit:
        raise BudgetExceeded(spent, estimate, limit)
