import itertools
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from photo_chooser import keychain
from photo_chooser.models import Asset

_counter = itertools.count(1)


def _no_security(*args, **kwargs):
    raise AssertionError("юнит-тесты не вызывают /usr/bin/security")


@pytest.fixture(autouse=True)
def _keychain_untouched(request, monkeypatch):
    """Реальный Keychain в юнит-тестах недоступен: незаглушённый вызов security роняет тест (NFR-6).
    Интеграционные тесты пользователь запускает сам в Terminal — им ключ из Keychain нужен."""
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setattr(keychain, "_run", _no_security)


def make_asset(**overrides) -> Asset:
    n = next(_counter)
    base = Asset(
        uuid=f"ASSET-{n:04d}/L0/001",
        filename=f"IMG_{n:04d}.HEIC",
        taken_at=datetime(2026, 3, 14, 10, 0, tzinfo=UTC),
        width=4032,
        height=3024,
        is_live=False,
        is_screenshot=False,
    )
    return replace(base, **overrides)
