from datetime import UTC, datetime

from conftest import make_asset
from photo_chooser.config import PostsCfg, Thresholds
from photo_chooser.judge.stages import ComposeCarousel, ComposeOutput, ComposeSlide, ComposeStory
from photo_chooser.models import Judgment, LocalFeatures
from photo_chooser.select import (
    compose_candidates,
    fallback_posts,
    local_facts,
    local_posts,
    shot_facts,
    validate_composition,
)

TH = Thresholds()  # min_score = 5
LIMITS = PostsCfg(carousels=2, carousel_size=9, stories=9)  # прежние лимиты: проверки ниже о них


def _j(a, score, matches=True, reason="r", interesting=False, crop_feed="", crop_stories=""):
    return Judgment(uuid=a.uuid, matches_theme=matches, score=score, reason=reason, interesting=interesting,
                    crop_feed=crop_feed, crop_stories=crop_stories)


def _ids(n):
    assets = [make_asset(uuid=f"A{i:02d}") for i in range(1, n + 1)]
    return assets, {f"c{i:02d}": a for i, a in enumerate(assets, 1)}


def _uuids(items):
    return [x.uuid for x in items]


def test_compose_candidates_thresholds_order_and_limit():
    a, b, c, d, e, f, g = (make_asset(uuid=u) for u in "abcdefg")
    judgments = {a.uuid: _j(a, 9), b.uuid: _j(b, 4), c.uuid: _j(c, 9, matches=False),
                 d.uuid: _j(d, 6), e.uuid: _j(e, 8), f.uuid: _j(f, 8)}
    quality = {d.uuid: 0.9, e.uuid: 0.1, f.uuid: 0.5}
    got = compose_candidates([a, b, c, d, e, f, g], judgments, quality, TH, interval_only=False, limit=3)
    assert _uuids(got) == ["a", "f", "e"]  # b ниже порога, c не по теме, g не оценён; при равенстве — качество


def test_compose_candidates_single_score():
    a, b, c, d = (make_asset(uuid=u) for u in "abcd")
    judgments = {"a": _j(a, 9), "b": _j(b, 7), "c": _j(c, 4), "d": _j(d, 3, interesting=True)}
    got = compose_candidates([a, b, c, d], judgments, {}, TH, interval_only=False, limit=10)
    assert _uuids(got) == ["a", "b", "d"]  # «интересное» проходит и ниже порога, слабый без пометки — нет


def test_compose_candidates_interval_only_ignores_matches():
    c = make_asset(uuid="c")
    got = compose_candidates([c], {"c": _j(c, 9, matches=False)}, {}, TH, interval_only=True, limit=30)
    assert _uuids(got) == ["c"]


def _out(carousels=(), stories=()):
    return ComposeOutput(carousels=[ComposeCarousel(title=t, slides=[ComposeSlide(id=i, alternatives=list(alts))
                                                                     for i, alts in slides])
                                    for t, slides in carousels],
                         stories=[ComposeStory(id=i, kind=k, hint=h) for i, k, h in stories])


def test_validate_drops_unknown_and_duplicates():
    assets, ids = _ids(5)
    out = _out(carousels=[("Пирамиды", [("c01", ["c02", "c99"]), ("c03", ["c01"])])],
               stories=[("c01", "best", ""), ("c04", "interesting", "рассказать о ладье"), ("c99", "best", "")])
    posts = validate_composition(out, ids, LIMITS)
    [carousel] = posts.carousels
    assert carousel.title == "Пирамиды"
    assert [s.asset.uuid for s in carousel.slides] == ["A01", "A03"]
    assert _uuids(carousel.slides[0].alternatives) == ["A02"] and carousel.slides[1].alternatives == []
    assert [(s.asset.uuid, s.kind, s.hint) for s in posts.stories] == [("A04", "interesting", "рассказать о ладье")]


def test_validate_normalizes_id_suffix():
    assets, ids = _ids(2)
    posts = validate_composition(_out(carousels=[("X", [("c02 (vertical)", ["c01 (horizontal)"])])]), ids, LIMITS)
    assert posts.carousels[0].slides[0].asset.uuid == "A02"
    assert _uuids(posts.carousels[0].slides[0].alternatives) == ["A01"]


def test_validate_enforces_limits():
    assets, ids = _ids(30)
    c = [f"c{i:02d}" for i in range(1, 31)]
    first = [(c[0], [c[11], c[12], c[13]])] + [(x, []) for x in c[1:11]]
    out = _out(carousels=[("Один", first), ("Два", [(c[14], []), (c[15], [])]), ("Три", [(c[16], [])])],
               stories=[(x, "best", "") for x in c[17:29]])
    posts = validate_composition(out, ids, LIMITS)
    assert [cr.title for cr in posts.carousels] == ["Один", "Два"]
    assert [s.asset.uuid for s in posts.carousels[0].slides] == [f"A{i:02d}" for i in range(1, 10)]
    assert _uuids(posts.carousels[0].slides[0].alternatives) == ["A12", "A13"]
    assert _uuids([s.asset for s in posts.stories]) == [f"A{i:02d}" for i in range(18, 27)]


def test_validate_drops_empty_carousel_and_names_untitled():
    assets, ids = _ids(3)
    out = _out(carousels=[("Пусто", [("c99", [])]), ("  ", [("c01", [])])])
    posts = validate_composition(out, ids, LIMITS)
    assert [cr.title for cr in posts.carousels] == ["Карусель 1"]
    assert validate_composition(_out(), ids, LIMITS).carousels == []


def test_validate_clears_hint_for_best():
    assets, ids = _ids(1)
    posts = validate_composition(_out(stories=[("c01", "best", "лишняя подсказка")]), ids, LIMITS)
    assert posts.stories[0].hint == ""


def test_fallback_posts_single_score():
    a, b, c, d = (make_asset(uuid=u) for u in "abcd")
    judgments = {"a": _j(a, 8), "b": _j(b, 9), "c": _j(c, 7), "d": _j(d, 4)}
    posts = fallback_posts([a, b, c, d], judgments, {}, TH, PostsCfg(carousel_size=2, stories=5))
    [carousel] = posts.carousels
    assert carousel.title == "Лучшее по оценкам"
    assert [s.asset.uuid for s in carousel.slides] == ["b", "a"]
    assert [(s.asset.uuid, s.kind) for s in posts.stories] == [("c", "best")]  # d ниже min_score


def test_fallback_posts_nothing_strong():
    a, b = make_asset(uuid="a"), make_asset(uuid="b")
    posts = fallback_posts([a, b], {"a": _j(a, 4), "b": _j(b, 2)}, {}, TH, LIMITS)
    assert posts.carousels == [] and posts.stories == []


def test_local_posts_by_quality():
    a, b, c = (make_asset(uuid=u) for u in "abc")
    posts = local_posts([a, b, c], {"a": 0.2, "b": 0.9, "c": 0.5}, {}, PostsCfg(carousel_size=2, stories=5))
    assert posts.carousels[0].title == "Лучшее по локальной оценке"
    assert [s.asset.uuid for s in posts.carousels[0].slides] == ["b", "c"]
    assert [s.asset.uuid for s in posts.stories] == ["a"]


def test_shot_facts_and_local_facts():
    a = make_asset(uuid="a")
    facts = shot_facts({"a": _j(a, 8, reason="Сфинкс", interesting=True, crop_feed="4:5", crop_stories="9:16")})
    assert (facts["a"].score, facts["a"].interesting, facts["a"].reason, facts["a"].crop_feed) == \
        (8, True, "Сфинкс", "4:5")
    feats = {"a": LocalFeatures(uuid="a", aesthetic=0.7, is_utility=False, sharpness=5, mean_luma=0.5,
                                face_quality=None, feature_print=b"")}
    local = local_facts([a], {"a": 0.61}, feats)
    assert local["a"].score == 0.61 and local["a"].interesting is False
    assert "эстетика 0.70" in local["a"].reason and local["a"].crop_feed == ""


def test_fallback_posts_excludes_off_theme():
    a, b = make_asset(uuid="a"), make_asset(uuid="b")
    judgments = {"a": _j(a, 7), "b": _j(b, 9, matches=False)}
    posts = fallback_posts([a, b], judgments, {}, TH, LIMITS)
    assert [s.asset.uuid for s in posts.carousels[0].slides] == ["a"] and posts.stories == []
    interval = fallback_posts([a, b], judgments, {}, TH, LIMITS, interval_only=True)
    assert [s.asset.uuid for s in interval.carousels[0].slides] == ["b", "a"]


def test_validate_normalizes_prefix_and_case():
    assets, ids = _ids(3)
    posts = validate_composition(_out(carousels=[("X", [("id: C01", ["c2"]), (" c003 ", [])])]), ids, LIMITS)
    assert [s.asset.uuid for s in posts.carousels[0].slides] == ["A01", "A03"]
    assert _uuids(posts.carousels[0].slides[0].alternatives) == ["A02"]


def test_validate_slides_win_over_alternatives():
    assets, ids = _ids(3)
    posts = validate_composition(_out(carousels=[("X", [("c01", ["c02", "c03"]), ("c02", [])])]), ids, LIMITS)
    assert [s.asset.uuid for s in posts.carousels[0].slides] == ["A01", "A02"]
    assert _uuids(posts.carousels[0].slides[0].alternatives) == ["A03"]


def test_validate_alternatives_filtered_before_limit():
    assets, ids = _ids(4)
    posts = validate_composition(_out(carousels=[("X", [("c01", ["c99", "c02", "c03", "c04"])])]), ids, LIMITS)
    assert _uuids(posts.carousels[0].slides[0].alternatives) == ["A02", "A03"]


# --- версия 0.4: «интересные» сверх окна и stories по времени съёмки (FR-4.1, FR-4.3, D13) ------


def test_compose_candidates_adds_interesting_beyond_window():
    s9, s8, s6, i5, i4, i7 = (make_asset(uuid=u) for u in ("s9", "s8", "s6", "i5", "i4", "i7"))
    judgments = {"s9": _j(s9, 9), "s8": _j(s8, 8), "s6": _j(s6, 6), "i5": _j(i5, 5, interesting=True),
                 "i4": _j(i4, 4, interesting=True), "i7": _j(i7, 7, interesting=True)}
    cands = [s9, s8, s6, i5, i4, i7]
    got = compose_candidates(cands, judgments, {}, TH, interval_only=False, limit=2, extra_interesting=1)
    assert _uuids(got) == ["s9", "s8", "i7"]
    assert _uuids(compose_candidates(cands, judgments, {}, TH, interval_only=False, limit=2)) == ["s9", "s8"]


def test_compose_candidates_interesting_flood_capped():
    many = [make_asset(uuid=f"i{n:02d}") for n in range(20)]
    judgments = {a.uuid: _j(a, 6, interesting=True) for a in many}
    got = compose_candidates(many, judgments, {}, TH, interval_only=False, limit=2, extra_interesting=3)
    assert len(got) == 5


def test_compose_candidates_only_interesting_below_threshold():
    weak, i1, i2 = make_asset(uuid="w"), make_asset(uuid="i1"), make_asset(uuid="i2")
    judgments = {"w": _j(weak, 3), "i1": _j(i1, 4, interesting=True), "i2": _j(i2, 2, interesting=True)}
    got = compose_candidates([weak, i1, i2], judgments, {}, TH, interval_only=False, limit=30, extra_interesting=9)
    assert _uuids(got) == ["i1", "i2"]


def _timed(uuid, hour):
    return make_asset(uuid=uuid, taken_at=datetime(2026, 10, 26, hour, 0, tzinfo=UTC))


def test_validate_stories_by_shooting_time():
    a10, a11, a12 = _timed("A10", 10), _timed("A11", 11), _timed("A12", 12)
    ids = {"c01": a10, "c02": a11, "c03": a12}
    out = _out(stories=[("c03", "best", ""), ("c01", "best", ""), ("c02", "interesting", "что это")])
    assert _uuids([s.asset for s in validate_composition(out, ids, LIMITS).stories]) == ["A10", "A11", "A12"]
    two = PostsCfg(carousels=1, carousel_size=5, stories=2)
    assert _uuids([s.asset for s in validate_composition(out, ids, two).stories]) == ["A10", "A12"]


def test_validate_stories_same_time_keep_model_order():
    assets, ids = _ids(3)  # make_asset: одинаковое время съёмки
    out = _out(stories=[("c03", "best", ""), ("c01", "best", ""), ("c02", "best", "")])
    assert _uuids([s.asset for s in validate_composition(out, ids, LIMITS).stories]) == ["A03", "A01", "A02"]


def test_fallback_and_local_stories_by_shooting_time():
    top, late, early = _timed("top", 9), _timed("late", 15), _timed("early", 8)
    judgments = {"top": _j(top, 9), "late": _j(late, 8), "early": _j(early, 7)}
    limits = PostsCfg(carousels=1, carousel_size=1, stories=5)
    posts = fallback_posts([top, late, early], judgments, {}, TH, limits)
    assert _uuids([s.asset for s in posts.stories]) == ["early", "late"]
    local = local_posts([top, late, early], {"top": 0.9, "late": 0.8, "early": 0.7}, {}, limits)
    assert _uuids([s.asset for s in local.stories]) == ["early", "late"]


def test_compose_window_from_config():
    # FR-4.1: окно сборки и предел «интересных» сверх него берутся из конфига в одном месте
    from photo_chooser.config import CloudCfg
    from photo_chooser.select import compose_window

    assert compose_window(CloudCfg(compose_candidates=30), PostsCfg(stories=9)) == (30, 9)
