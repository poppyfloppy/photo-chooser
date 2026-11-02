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

TH = Thresholds(min_feed_score=6, min_stories_score=4)
LIMITS = PostsCfg()


def _j(a, feed, stories, matches=True, reason="r", crop_feed="", crop_stories=""):
    return Judgment(uuid=a.uuid, matches_theme=matches, feed_score=feed, stories_score=stories, reason=reason,
                    crop_feed=crop_feed, crop_stories=crop_stories)


def _ids(n):
    assets = [make_asset(uuid=f"A{i:02d}") for i in range(1, n + 1)]
    return assets, {f"c{i:02d}": a for i, a in enumerate(assets, 1)}


def _uuids(items):
    return [x.uuid for x in items]


def test_compose_candidates_thresholds_order_and_limit():
    a, b, c, d, e, f, g = (make_asset(uuid=u) for u in "abcdefg")
    judgments = {a.uuid: _j(a, 9, 3), b.uuid: _j(b, 5, 3), c.uuid: _j(c, 9, 9, matches=False),
                 d.uuid: _j(d, 5, 8), e.uuid: _j(e, 8, 2), f.uuid: _j(f, 8, 7)}
    quality = {d.uuid: 0.9, e.uuid: 0.1, f.uuid: 0.5}
    got = compose_candidates([a, b, c, d, e, f, g], judgments, quality, TH, interval_only=False, limit=3)
    assert _uuids(got) == ["a", "d", "f"]  # b ниже обоих порогов, c не по теме, g не оценён


def test_compose_candidates_interval_only_ignores_matches():
    c = make_asset(uuid="c")
    got = compose_candidates([c], {"c": _j(c, 9, 9, matches=False)}, {}, TH, interval_only=True, limit=30)
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


def test_fallback_posts_by_scores():
    a, b, c, d, e = (make_asset(uuid=u) for u in "abcde")
    judgments = {a.uuid: _j(a, 7, 5), b.uuid: _j(b, 9, 2), c.uuid: _j(c, 6, 9), d.uuid: _j(d, 3, 6), e.uuid: _j(e, 2, 2)}
    posts = fallback_posts([a, b, c, d, e], judgments, {}, TH, PostsCfg(carousel_size=2, stories=2))
    [carousel] = posts.carousels
    assert carousel.title == "Лучшее по оценкам"
    assert [s.asset.uuid for s in carousel.slides] == ["b", "a"]
    assert [(s.asset.uuid, s.kind) for s in posts.stories] == [("c", "best"), ("d", "best")]
    assert fallback_posts([e], {"e": judgments["e"]}, {}, TH, LIMITS).carousels == []


def test_local_posts_by_quality():
    a, b, c = (make_asset(uuid=u) for u in "abc")
    posts = local_posts([a, b, c], {"a": 0.2, "b": 0.9, "c": 0.5}, {}, PostsCfg(carousel_size=2, stories=5))
    assert posts.carousels[0].title == "Лучшее по локальной оценке"
    assert [s.asset.uuid for s in posts.carousels[0].slides] == ["b", "c"]
    assert [s.asset.uuid for s in posts.stories] == ["a"]


def test_shot_facts_and_local_facts():
    a = make_asset(uuid="a")
    facts = shot_facts({"a": _j(a, 8, 6, reason="Сфинкс", crop_feed="4:5", crop_stories="9:16")})
    assert (facts["a"].feed_score, facts["a"].stories_score, facts["a"].reason, facts["a"].crop_feed) == \
        (8, 6, "Сфинкс", "4:5")
    feats = {"a": LocalFeatures(uuid="a", aesthetic=0.7, is_utility=False, sharpness=5, mean_luma=0.5,
                                face_quality=None, feature_print=b"")}
    local = local_facts([a], {"a": 0.61}, feats)
    assert local["a"].feed_score == 0.61 and "эстетика 0.70" in local["a"].reason and local["a"].crop_feed == ""


def test_fallback_posts_excludes_off_theme():
    a, b = make_asset(uuid="a"), make_asset(uuid="b")
    judgments = {"a": _j(a, 7, 5), "b": _j(b, 9, 9, matches=False)}
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
