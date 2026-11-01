"""Оценка стоимости облачных ступеней и защита бюджета (SPEC FR-9.1). Константы — docs/spike-findings.md."""

import math

from photo_chooser.config import CloudCfg, Price

IMAGE_TOKEN_DIVISOR = 750
PROMPT_OVERHEAD_TOKENS = 500  # на группу: системный промпт, id, список тем
EST_OUTPUT_TOKENS_SCREEN = 105  # на снимок
EST_OUTPUT_TOKENS_JUDGE = 80  # на снимок, с причиной
HAIKU_PRICE_LIMIT_TOKENS = 100_000  # цена Haiku 5.5 действует для запросов до 100K токенов


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


def usage_usd(input_tokens: int, output_tokens: int, price: Price) -> float:
    return input_tokens * price.input / 1e6 + output_tokens * price.output / 1e6


def group_input_tokens(sizes: list[tuple[int, int]], long_side: int) -> int:
    return sum(image_tokens(w, h, long_side) for w, h in sizes) + PROMPT_OVERHEAD_TOKENS


def _stage_usd(n: int, cfg: CloudCfg, output_per_photo: int, price: Price, extra_per_group: int = 0) -> float:
    if n <= 0:
        return 0.0
    side = cfg.preview_long_side_px
    per_image = image_tokens(side, side * 3 // 4, side)
    groups = math.ceil(n / cfg.group_size)
    return usage_usd(n * per_image + groups * (PROMPT_OVERHEAD_TOKENS + extra_per_group), n * output_per_photo, price)


def estimate_run_usd(n_screen: int, n_judge: list[int], cfg: CloudCfg, screen_model: str, judge_model: str,
                     judge_extra_tokens: int = 0) -> float:
    """Первая ступень — один проход по n_screen снимкам; вторая — n_judge[i] кандидатов темы i. Превью 4:3.
    judge_extra_tokens — профиль стиля и скриншоты-образцы в каждой группе второй ступени (FR-11.6)."""
    total = _stage_usd(n_screen, cfg, EST_OUTPUT_TOKENS_SCREEN, price_for(cfg, screen_model)) if n_screen else 0.0
    if any(n > 0 for n in n_judge):
        judge_price = price_for(cfg, judge_model)
        total += sum(_stage_usd(n, cfg, EST_OUTPUT_TOKENS_JUDGE, judge_price, judge_extra_tokens) for n in n_judge)
    return total


def check_budget(spent: float, estimate: float, limit: float) -> None:
    if spent + estimate > limit:
        raise BudgetExceeded(spent, estimate, limit)
