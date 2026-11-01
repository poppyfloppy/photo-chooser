from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from conftest import make_asset
from photo_chooser.config import Theme, Thresholds
from photo_chooser.models import Judgment, LocalFeatures, Screening
from photo_chooser.select import in_interval, pools_from_judgments, pools_local, screen_candidates

TH = Thresholds(min_feed_score=6, min_stories_score=4)
GIZA = Theme(name="Гиза", query="пирамиды")
DAY = Theme(name="День 3", start=datetime(2026, 3, 14, 5, 0), end=datetime(2026, 3, 14, 12, 0))


def _at(iso: str):
    return make_asset(taken_at=datetime.fromisoformat(iso).astimezone(UTC))


def _j(a, feed=7, stories=5, matches=True, reason="r"):
    return Judgment(uuid=a.uuid, matches_theme=matches, feed_score=feed, stories_score=stories, reason=reason)


def _s(a, rel=None, feed=5, stories=5):
    return Screening(uuid=a.uuid, relevance=rel or {}, feed_score=feed, stories_score=stories)


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
    screenings = {a.uuid: _s(a, {k: 6}, feed=9, stories=2),
                  b.uuid: _s(b, {k: 9}, feed=3, stories=3),
                  c.uuid: _s(c, {k: 6}, feed=4, stories=9),
                  d.uuid: _s(d, {k: 6}, feed=9, stories=1)}
    quality = {a.uuid: 0.2, b.uuid: 0.1, c.uuid: 0.9, d.uuid: 0.5}
    got = screen_candidates([a, b, c, d], screenings, quality, GIZA, min_relevance=5, limit=3)
    # b — самая высокая релевантность; c (9, q .9) > d (9, q .5) > a (9, q .2)
    assert got == [b, c, d]


def test_screen_candidates_relevance_ignores_theme_interval():
    # первая ступень оценивает тему по name+query; интервал темы на её ключ не влияет
    a = make_asset()
    with_interval = Theme(name="Гиза", query="пирамиды", start=datetime(2026, 3, 14, 5, 0),
                          end=datetime(2026, 3, 14, 12, 0))
    screenings = {a.uuid: _s(a, {GIZA.screen_key(): 8})}
    assert screen_candidates([a], screenings, {}, with_interval, min_relevance=5, limit=100) == [a]


def test_screen_candidates_interval_only_includes_unscreened():
    screened, unscreened = make_asset(), make_asset()
    screenings = {screened.uuid: _s(screened, {}, feed=8, stories=2)}
    quality = {screened.uuid: 0.1, unscreened.uuid: 0.9}
    got = screen_candidates([unscreened, screened], screenings, quality, DAY, min_relevance=5, limit=100)
    assert got == [screened, unscreened]  # черновая оценка первой ступени важнее локальной


def test_feed_threshold_and_order():
    a, b, c, d = (make_asset() for _ in range(4))
    judgments = {a.uuid: _j(a, feed=6), b.uuid: _j(b, feed=9), c.uuid: _j(c, feed=5), d.uuid: _j(d, feed=6)}
    quality = {a.uuid: 0.3, d.uuid: 0.8}
    pools = pools_from_judgments([a, b, c, d], judgments, quality, TH, interval_only=False)
    assert [s.asset for s in pools.feed] == [b, d, a]
    assert [s.score for s in pools.feed] == [9, 6, 6]


def test_feed_excludes_not_matching_theme():
    a, b = make_asset(), make_asset()
    judgments = {a.uuid: _j(a, feed=9, matches=False), b.uuid: _j(b, feed=7, reason="Сфинкс")}
    pools = pools_from_judgments([a, b], judgments, {}, TH, interval_only=False)
    assert [s.asset for s in pools.feed] == [b]
    assert pools.feed[0].reason == "Сфинкс"
    assert [s.asset for s in pools.stories] == [b]


def test_interval_only_ignores_matches_theme():
    a = make_asset()
    pools = pools_from_judgments([a], {a.uuid: _j(a, feed=9, matches=False)}, {}, TH, interval_only=True)
    assert [s.asset for s in pools.feed] == [a]


def test_stories_verticals_first():
    vertical = make_asset(width=3024, height=4032)
    horizontal = make_asset(width=4032, height=3024)
    judgments = {vertical.uuid: _j(vertical, stories=5), horizontal.uuid: _j(horizontal, stories=9)}
    pools = pools_from_judgments([horizontal, vertical], judgments, {}, TH, interval_only=False)
    assert [s.asset for s in pools.stories] == [vertical, horizontal]
    assert [s.score for s in pools.stories] == [5, 9]


def test_stories_lower_threshold():
    a, b = make_asset(), make_asset()
    judgments = {a.uuid: _j(a, feed=3, stories=4), b.uuid: _j(b, feed=3, stories=3)}
    pools = pools_from_judgments([a, b], judgments, {}, TH, interval_only=False)
    assert pools.feed == []
    assert [s.asset for s in pools.stories] == [a]


def test_square_counts_as_horizontal():
    square = make_asset(width=3000, height=3000)
    vertical = make_asset(width=3000, height=4000)
    judgments = {square.uuid: _j(square, stories=9), vertical.uuid: _j(vertical, stories=4)}
    pools = pools_from_judgments([square, vertical], judgments, {}, TH, interval_only=False)
    assert [s.asset for s in pools.stories] == [vertical, square]


def test_unjudged_candidates_excluded():
    judged, unjudged = make_asset(), make_asset()
    pools = pools_from_judgments([judged, unjudged], {judged.uuid: _j(judged, feed=8)}, {}, TH, interval_only=True)
    assert [s.asset for s in pools.feed] == [judged]
    assert [s.asset for s in pools.stories] == [judged]


def _f(a, aesthetic, sharp, face):
    return LocalFeatures(uuid=a.uuid, aesthetic=aesthetic, is_utility=False, sharpness=sharp, mean_luma=0.5,
                         face_quality=face, feature_print=b"")


def test_local_pools_order_and_verticals_first():
    h1 = make_asset(width=4032, height=3024)
    v1 = make_asset(width=3024, height=4032)
    h2 = make_asset(width=4032, height=3024)
    quality = {h1.uuid: 0.9, v1.uuid: 0.5, h2.uuid: 0.7}
    feats = {a.uuid: _f(a, 0.5, 10, None) for a in (h1, v1, h2)}
    pools = pools_local([h1, v1, h2], quality, feats)
    assert [s.asset for s in pools.feed] == [h1, h2, v1]
    assert [s.asset for s in pools.stories] == [v1, h1, h2]
    assert pools.feed[0].score == 0.9


def test_local_pools_reason_format():
    a, b = make_asset(), make_asset()
    feats = {a.uuid: _f(a, 0.72, 50.0, 0.65), b.uuid: _f(b, None, 10.0, None)}
    pools = pools_local([a, b], {a.uuid: 0.8, b.uuid: 0.2}, feats)
    by = {s.asset.uuid: s.reason for s in pools.feed}
    assert by[a.uuid] == "эстетика 0.72 · резкость 1.00 · лица 0.65"  # резкость — ранг среди кандидатов
    assert by[b.uuid] == "резкость 0.00"
