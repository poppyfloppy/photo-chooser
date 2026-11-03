import base64
from dataclasses import replace
from datetime import datetime

import pytest

from conftest import make_asset
from fakes import FakeClient, compose_once, compose_one, fake_response, output_model_of
from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig, Theme
from photo_chooser.judge.prompts import (
    COMPOSE_PROMPT_VERSION,
    COMPOSE_SCREENSHOTS_NOTE,
    COMPOSE_STYLE_BLOCK,
    COMPOSE_SYSTEM_PROMPT,
    STYLE_REFERENCE_LABELS,
)
from photo_chooser.judge.stages import (
    ComposeCarousel,
    ComposeOutput,
    ComposeRunner,
    ComposeSlide,
    ComposeStory,
    compose_input_fingerprint,
)
from photo_chooser.select import ShotFacts
from photo_chooser.style import StyleContext

HAIKU, OPUS = "claude-haiku-5-5", "claude-opus-5-5"
THEME = Theme(name="Пирамиды в Гиза", query="Пирамиды в Гизе, Сфинкс")
USD = 20_000 * 0.10 / 1e6 + 2_000 * 0.50 / 1e6


def _preview(asset):
    return asset.uuid.encode()


def _cands(n=3):
    return [make_asset(uuid=f"K{i}", width=4032 if i % 2 == 0 else 3024, height=3024 if i % 2 == 0 else 4032)
            for i in range(n)]


def _facts(cands, score=8, interesting=False):
    return {a.uuid: ShotFacts(score, interesting, f"Причина {a.uuid}", "", "") for a in cands}


def _style(fp="fp000001"):
    return StyleContext(text="## Лента\nПлёнка, тёплые тона.", references=[("feed", b"F1"), ("stories", b"S1")],
                        fingerprint=fp, created_at=datetime(2026, 11, 1), extra_tokens=3500)


def compose_handler(out=None, **resp):
    out = out or ComposeOutput(carousels=[ComposeCarousel(title="Сфинкс", slides=[
        ComposeSlide(id="c02", alternatives=["c01"])])], stories=[ComposeStory(id="c03", kind="interesting",
                                                                               hint="рассказать о ладье")])
    return lambda n, kw: fake_response(out, **resp)


def test_compose_request_shape():
    client = FakeClient(compose_handler())
    cands = _cands()
    compose_once(client, ComposeRunner(HAIKU, "medium", _preview), THEME, cands, _facts(cands), AppConfig().posts)
    call = client.calls[0]
    assert call["system"] == COMPOSE_SYSTEM_PROMPT.format(carousels=1, carousel_size=5, stories=9)
    assert output_model_of(call) is ComposeOutput and call["_beta"] is False
    content = call["messages"][0]["content"]
    labels = [b["text"] for b in content if b["type"] == "text" and b["text"].startswith("id: ")]
    assert labels[0] == "id: c01 (horizontal) · сила 8 · «Причина K0»"
    assert labels[1].startswith("id: c02 (vertical) · ")
    assert [base64.b64decode(b["source"]["data"]) for b in content if b["type"] == "image"] == [b"K0", b"K1", b"K2"]
    assert "«Пирамиды в Гиза: Пирамиды в Гизе, Сфинкс»" in content[-1]["text"]


def test_compose_request_with_style_and_opus():
    client = FakeClient(compose_handler())
    cands = _cands(1)
    compose_once(client, ComposeRunner(OPUS, "medium", _preview, style=_style()), THEME, cands, _facts(cands), AppConfig().posts)
    call = client.calls[0]
    assert "Плёнка, тёплые тона." in call["system"] and COMPOSE_SCREENSHOTS_NOTE in call["system"]
    assert "feed_score" not in call["system"] and "stories_score" not in call["system"]  # сборка не ставит оценки
    content = call["messages"][0]["content"]
    assert content[0]["text"] == STYLE_REFERENCE_LABELS["feed"] and content[2]["text"] == STYLE_REFERENCE_LABELS["stories"]
    assert content[4]["text"].startswith("id: c01 ")
    assert call["_beta"] is True and call["fallbacks"] == "default"


def test_compose_maps_ids_to_assets(tmp_path):
    cands = _cands()
    outcome = compose_one(FakeClient(compose_handler()), ComposeRunner(HAIKU, "medium", _preview),
                          Cache(tmp_path / "c.sqlite"), THEME, cands, _facts(cands), AppConfig(), "album-1")
    [carousel] = outcome.posts.carousels
    assert carousel.title == "Сфинкс" and carousel.slides[0].asset.uuid == "K1"
    assert [a.uuid for a in carousel.slides[0].alternatives] == ["K0"]
    assert [(s.asset.uuid, s.kind, s.hint) for s in outcome.posts.stories] == [("K2", "interesting", "рассказать о ладье")]
    assert not outcome.from_cache and outcome.usd == pytest.approx(USD)


def test_compose_cache_reuse_and_invalidation(tmp_path):
    cache, cands, cfg = Cache(tmp_path / "c.sqlite"), _cands(), AppConfig()
    client = FakeClient(compose_handler())

    def run(facts, style=None):
        return compose_one(client, ComposeRunner(HAIKU, "medium", _preview, style=style), cache, THEME, cands, facts,
                           cfg, "album-1")

    run(_facts(cands))
    again = run(_facts(cands))
    assert len(client.calls) == 1 and again.from_cache and again.usd == 0
    assert again.posts.carousels[0].slides[0].asset.uuid == "K1"
    run(_facts(cands, score=9))  # другие оценки — другой вход
    assert len(client.calls) == 2
    run(_facts(cands, score=9), style=_style())
    assert len(client.calls) == 3


def test_compose_fingerprint_depends_on_limits_and_style():
    cands = _cands()
    facts = _facts(cands)
    base = compose_input_fingerprint(cands, facts, AppConfig().posts, None)
    assert base == compose_input_fingerprint(cands, facts, AppConfig().posts, None)
    assert base != compose_input_fingerprint(cands, facts, AppConfig().posts.model_copy(update={"stories": 5}), None)
    assert base != compose_input_fingerprint(cands, facts, AppConfig().posts, _style())
    assert base != compose_input_fingerprint(list(reversed(cands)), facts, AppConfig().posts, None)


def test_compose_fingerprint_depends_on_interesting():
    cands = _cands()
    facts = _facts(cands)
    marked = {**facts, "K1": ShotFacts(8, True, "Причина K1", "", "")}
    assert compose_input_fingerprint(cands, facts, AppConfig().posts, None) != \
        compose_input_fingerprint(cands, marked, AppConfig().posts, None)


def test_compose_label_strength_and_interesting():
    client = FakeClient(compose_handler())
    cands = [make_asset(uuid="H1"), make_asset(uuid="H2")]  # обе горизонтальные
    facts = {"H1": ShotFacts(8, True, "r", "", ""), "H2": ShotFacts(6, False, "r", "", "")}
    compose_once(client, ComposeRunner(HAIKU, "medium", _preview), THEME, cands, facts, AppConfig().posts)
    content = client.calls[0]["messages"][0]["content"]
    labels = [b["text"] for b in content if b["type"] == "text" and b["text"].startswith("id: ")]
    assert labels == ["id: c01 (horizontal) · сила 8 · интересное · «r»", "id: c02 (horizontal) · сила 6 · «r»"]


def test_compose_records_spend_once(tmp_path):
    cache, cands = Cache(tmp_path / "c.sqlite"), _cands()
    runner = ComposeRunner(HAIKU, "medium", _preview)
    compose_one(FakeClient(compose_handler()), runner, cache, THEME, cands, _facts(cands), AppConfig(), "album-1")
    compose_one(FakeClient(compose_handler()), runner, cache, THEME, cands, _facts(cands), AppConfig(), "album-1")
    assert cache.album_spend("album-1") == pytest.approx(USD)


def test_compose_refusal_returns_none(tmp_path):
    cache, cands = Cache(tmp_path / "c.sqlite"), _cands()
    client = FakeClient(lambda n, kw: fake_response(text="I can't", stop_reason="refusal", input_tokens=30_000,
                                                     output_tokens=10))
    outcome = compose_one(client, ComposeRunner(HAIKU, "medium", _preview), cache, THEME, cands, _facts(cands),
                          AppConfig(), "album-1")
    assert outcome.posts is None and outcome.refused
    assert cache.album_spend("album-1") == pytest.approx(30_000 * 0.10 / 1e6 + 10 * 0.50 / 1e6)
    compose_one(client, ComposeRunner(HAIKU, "medium", _preview), cache, THEME, cands, _facts(cands), AppConfig(),
                "album-1")
    assert len(client.calls) == 2  # отказ не кэшируется


def test_compose_unparsable_returns_none(tmp_path):
    client = FakeClient(lambda n, kw: fake_response(text='{"carousels": [', stop_reason="max_tokens"))
    cands = _cands()
    outcome = compose_one(client, ComposeRunner(HAIKU, "medium", _preview), Cache(tmp_path / "c.sqlite"), THEME, cands,
                          _facts(cands), AppConfig(), "album-1")
    assert outcome.posts is None and not outcome.refused


def test_compose_without_candidates_no_call(tmp_path):
    client = FakeClient(compose_handler())
    outcome = compose_one(client, ComposeRunner(HAIKU, "medium", _preview), Cache(tmp_path / "c.sqlite"), THEME, [],
                          {}, AppConfig(), "album-1")
    assert client.calls == [] and outcome.posts.carousels == [] and outcome.posts.stories == []


def test_compose_over_price_threshold_rejected(tmp_path):
    client = FakeClient(compose_handler())
    cands = [make_asset(uuid=f"K{i}") for i in range(30)]
    heavy = replace(_style(), extra_tokens=70_000)
    with pytest.raises(ValueError):
        compose_one(client, ComposeRunner(HAIKU, "medium", _preview, style=heavy), Cache(tmp_path / "c.sqlite"), THEME,
                    cands, _facts(cands), AppConfig(), "album-1")
    assert client.calls == []


def test_compose_answer_without_known_ids_is_failure(tmp_path):
    out = ComposeOutput(carousels=[ComposeCarousel(title="X", slides=[ComposeSlide(id="z9", alternatives=[])])],
                        stories=[ComposeStory(id="q1", kind="best", hint="")])
    client = FakeClient(compose_handler(out))
    cache, cands = Cache(tmp_path / "c.sqlite"), _cands()
    runner = ComposeRunner(HAIKU, "medium", _preview)
    outcome = compose_one(client, runner, cache, THEME, cands, _facts(cands), AppConfig(), "album-1")
    assert outcome.posts is None and not outcome.refused and outcome.note
    compose_one(client, runner, cache, THEME, cands, _facts(cands), AppConfig(), "album-1")
    assert len(client.calls) == 2  # не кэшируется


def test_compose_empty_answer_is_valid_nothing(tmp_path):
    client = FakeClient(compose_handler(ComposeOutput(carousels=[], stories=[])))
    cands = _cands()
    outcome = compose_one(client, ComposeRunner(HAIKU, "medium", _preview), Cache(tmp_path / "c.sqlite"), THEME, cands,
                          _facts(cands), AppConfig(), "album-1")
    assert outcome.posts is not None and outcome.posts.carousels == [] and outcome.posts.stories == []


# --- правила сборки по приёмке 0.3 (FR-4.2) --------------------------------------------------


def test_compose_prompt_version_bumped():
    assert COMPOSE_PROMPT_VERSION == "compose-5"  # сборки по прежним правилам в кэше не годятся


def test_compose_prompt_allows_leaving_candidates_out():
    # модель раскладывала всех 30 кандидатов: «Use every id at most once» читалось как «используй каждый»
    assert "do not have to use every candidate" in COMPOSE_SYSTEM_PROMPT
    assert "Each id may appear at most once" in COMPOSE_SYSTEM_PROMPT and "Use every id" not in COMPOSE_SYSTEM_PROMPT


def test_compose_prompt_one_carousel_by_default():
    assert "the strongest storyline of the theme" in COMPOSE_SYSTEM_PROMPT
    assert "at least 4 strong photos" in COMPOSE_SYSTEM_PROMPT
    assert "otherwise the other storyline goes to stories" in COMPOSE_SYSTEM_PROMPT


def test_compose_prompt_requires_different_views_by_principle():
    # разные планы — принцип с примером, а не перечень, который модель станет заполнять
    assert "Each slide must add something new" in COMPOSE_SYSTEM_PROMPT
    assert "same framing from almost the same viewpoint" in COMPOSE_SYSTEM_PROMPT and "(e.g." in COMPOSE_SYSTEM_PROMPT


def test_compose_prompt_stories_quality_bar():
    assert "light editing will not fix" in COMPOSE_SYSTEM_PROMPT


def test_compose_prompt_puts_aesthetics_and_style_first():
    assert "first of all by aesthetics" in COMPOSE_SYSTEM_PROMPT
    assert "main criterion" in COMPOSE_STYLE_BLOCK


def test_compose_prompt_post_and_stories():
    # D13: пост — сильнейший сюжет до 5 кадров, stories — лучшие из оставшихся в порядке съёмки; D14 — портрет-обложка
    from photo_chooser.config import PostsCfg

    limits = PostsCfg()
    text = COMPOSE_SYSTEM_PROMPT.format(carousels=limits.carousels, carousel_size=limits.carousel_size,
                                        stories=limits.stories)
    for phrase in ("at most 1 carousel(s) of at most 5 photos", "usually 3-5", "portrait of the traveller",
                   "stories are arranged by shooting time"):
        assert phrase in text
    assert "presence or absence of people" not in COMPOSE_STYLE_BLOCK


def test_compose_prompt_harmonious_cover():
    # приёмка 0.4 (D14): обложка-портрет — только гармоничный, портрет ради портрета не добавляется
    assert "only when it is harmonious" in COMPOSE_SYSTEM_PROMPT
    assert "do not add a portrait just to have one" in COMPOSE_SYSTEM_PROMPT


def test_compose_prompt_storyline_by_set_and_coherence():
    # приёмка 0.4 (D13): сюжет поста — по свету и эстетике набора, свет — по месту; карусель в одном ключе
    for phrase in ("taken together", "not by the highest single scores", "depends on the place", "golden hour",
                   "night lights of a big city", "in one key", "one interior among outdoor views, goes to stories"):
        assert phrase in COMPOSE_SYSTEM_PROMPT
