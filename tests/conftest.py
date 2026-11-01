import itertools
from dataclasses import replace
from datetime import UTC, datetime

from photo_chooser.models import Asset

_counter = itertools.count(1)


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
