"""Ошибки Claude API, которые останавливают весь запуск, а не одну тему (SPEC §9, «Ошибки»)."""

import anthropic


class InsufficientCredit(Exception):
    """Не хватает средств на балансе: остальные темы упали бы так же."""

    def __init__(self):
        super().__init__("На балансе Claude API не хватает средств — пополните баланс в Claude Console. Оплаченное "
                         "сохранено в кэше, повторный запуск продолжит")


def is_billing_error(e: BaseException) -> bool:
    """402 billing_error или 400 «credit balance is too low» — так API отвечал в прогоне 2026-11-03_0114."""
    if isinstance(e, anthropic.APIStatusError) and e.status_code == 402:
        return True
    if getattr(e, "type", None) == "billing_error":
        return True
    return isinstance(e, anthropic.BadRequestError) and "credit balance" in str(e).lower()


def is_fatal(e: BaseException) -> bool:
    return isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError, InsufficientCredit))
