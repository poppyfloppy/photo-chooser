from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from conftest import make_asset
from photo_chooser.config import Theme, Thresholds
from photo_chooser.models import Judgment, Screening
from photo_chooser.select import in_interval, screen_candidates

TH = Thresholds()
GIZA = Theme(name="Гиза", query="пирамиды")
DAY = Theme(name="День 3", start=datetime(2026, 3, 14, 5, 0), end=datetime(2026, 3, 14, 12, 0))


def _at(iso: str):
    return make_asset(taken_at=datetime.fromisoformat(iso).astimezone(UTC))


def _j(a, score=7, matches=True, reason="r"):
    return Judgment(uuid=a.uuid, matches_theme=matches, score=score, reason=reason)


def _s(a, rel=None, score=5):
    return Screening(uuid=a.uuid, relevance=rel or {}, score=score)


def test_in_interval_converts_to_trip_timezone():
    a = _at("2026-03-13T20:30:00+00:00")  # 05:30 в Токио 14 марта
    assert in_interval(a, DAY, ZoneInfo("Asia/Tokyo"))
    assert not in_interval(a, DAY, ZoneInfo("Africa/Cairo"))  # 22:30 13 марта


def test_in_interval_inclusive_bounds():
    tz = ZoneInfo("Africa/Cairo")  # UTC+2 в марте 2026
    assert in_interval(_at("2026-03-14T03:00:00+00:00"), DAY, tz)
    assert in_interval(_at("2026-03-14T10:00:00+00:00"), DAY, tz)
    assert not in_interval(_at("2026-03-14T02:59:59+00:00"), DAY, tz)
    assert not in_interval(_at("2026-03-14T10:00:01+00:00"), DAY, tz)


def test_in_interval_without_interval_true():
    assert in_interval(make_asset(), GIZA, None)


def test_screen_candidates_relevance_threshold():
    a4, a5, none = make_asset(), make_asset(), make_asset()
    screenings = {a4.uuid: _s(a4, {GIZA.screen_key(): 4}), a5.uuid: _s(a5, {GIZA.screen_key(): 5}),
                  none.uuid: _s(none, {"other-theme": 9})}
    got = screen_candidates([a4, a5, none], screenings, {}, GIZA, min_relevance=5, limit=100)
    assert got == [a5]


def test_screen_candidates_order_and_limit():
    a, b, c, d = (make_asset() for _ in range(4))
    k = GIZA.screen_key()
    screenings = {a.uuid: _s(a, {k: 6}, score=9),
                  b.uuid: _s(b, {k: 9}, score=3),
                  c.uuid: _s(c, {k: 6}, score=9),
                  d.uuid: _s(d, {k: 6}, score=9)}
    quality = {a.uuid: 0.2, b.uuid: 0.1, c.uuid: 0.9, d.uuid: 0.5}
    got = screen_candidates([a, b, c, d], screenings, quality, GIZA, min_relevance=5, limit=3)
    # b — самая высокая релевантность; c (9, q .9) > d (9, q .5) > a (9, q .2)
    assert got == [b, c, d]


def test_screen_candidates_rank_by_draft_score():
    weak, strong = make_asset(), make_asset()
    k = GIZA.screen_key()
    screenings = {weak.uuid: _s(weak, {k: 8}, score=5), strong.uuid: _s(strong, {k: 8}, score=9)}
    got = screen_candidates([weak, strong], screenings, {}, GIZA, min_relevance=5, limit=10)
    assert got == [strong, weak]


def test_screen_candidates_relevance_ignores_theme_interval():
    # первая ступень оценивает тему по name+query; интервал темы на её ключ не влияет
    a = make_asset()
    with_interval = Theme(name="Гиза", query="пирамиды", start=datetime(2026, 3, 14, 5, 0),
                          end=datetime(2026, 3, 14, 12, 0))
    screenings = {a.uuid: _s(a, {GIZA.screen_key(): 8})}
    assert screen_candidates([a], screenings, {}, with_interval, min_relevance=5, limit=100) == [a]


def test_screen_candidates_interval_only_includes_unscreened():
    screened, unscreened = make_asset(), make_asset()
    screenings = {screened.uuid: _s(screened, {}, score=8)}
    quality = {screened.uuid: 0.1, unscreened.uuid: 0.9}
    got = screen_candidates([unscreened, screened], screenings, quality, DAY, min_relevance=5, limit=100)
    assert got == [screened, unscreened]  # черновая оценка первой ступени важнее локальной
