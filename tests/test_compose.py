import base64
from dataclasses import replace
from datetime import datetime

import pytest

from conftest import make_asset
from fakes import FakeClient, fake_response, output_model_of
from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig, Theme
from photo_chooser.judge.prompts import (
    COMPOSE_SYSTEM_PROMPT,
    STYLE_REFERENCE_LABELS,
    STYLE_SCREENSHOTS_NOTE,
)
from photo_chooser.judge.stages import (
    ComposeCarousel,
    ComposeOutput,
    ComposeRunner,
    ComposeSlide,
    ComposeStory,
    compose_input_fingerprint,
    run_compose,
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


def _facts(cands, feed=8):
    return {a.uuid: ShotFacts(feed, 6, f"Причина {a.uuid}", "", "") for a in cands}


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
    ComposeRunner(client, HAIKU, "medium", _preview).run(THEME, cands, _facts(cands), AppConfig().posts)
    call = client.calls[0]
    assert call["system"] == COMPOSE_SYSTEM_PROMPT.format(carousels=2, carousel_size=9, stories=9)
    assert output_model_of(call) is ComposeOutput and call["_beta"] is False
    content = call["messages"][0]["content"]
    labels = [b["text"] for b in content if b["type"] == "text" and b["text"].startswith("id: ")]
    assert labels[0] == "id: c01 (horizontal) · лента 8 · stories 6 · «Причина K0»"
    assert labels[1].startswith("id: c02 (vertical) · ")
    assert [base64.b64decode(b["source"]["data"]) for b in content if b["type"] == "image"] == [b"K0", b"K1", b"K2"]
    assert "«Пирамиды в Гиза: Пирамиды в Гизе, Сфинкс»" in content[-1]["text"]


def test_compose_request_with_style_and_opus():
    client = FakeClient(compose_handler())
    cands = _cands(1)
    ComposeRunner(client, OPUS, "medium", _preview, style=_style()).run(THEME, cands, _facts(cands), AppConfig().posts)
    call = client.calls[0]
    assert "Плёнка, тёплые тона." in call["system"] and STYLE_SCREENSHOTS_NOTE in call["system"]
    content = call["messages"][0]["content"]
    assert content[0]["text"] == STYLE_REFERENCE_LABELS["feed"] and content[2]["text"] == STYLE_REFERENCE_LABELS["stories"]
    assert content[4]["text"].startswith("id: c01 ")
    assert call["_beta"] is True and call["fallbacks"] == "default"


def test_compose_maps_ids_to_assets(tmp_path):
    cands = _cands()
    outcome = run_compose(ComposeRunner(FakeClient(compose_handler()), HAIKU, "medium", _preview),
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
        return run_compose(ComposeRunner(client, HAIKU, "medium", _preview, style=style), cache, THEME, cands, facts,
                           cfg, "album-1")

    run(_facts(cands))
    again = run(_facts(cands))
    assert len(client.calls) == 1 and again.from_cache and again.usd == 0
    assert again.posts.carousels[0].slides[0].asset.uuid == "K1"
    run(_facts(cands, feed=9))  # другие оценки — другой вход
    assert len(client.calls) == 2
    run(_facts(cands, feed=9), style=_style())
    assert len(client.calls) == 3


def test_compose_fingerprint_depends_on_limits_and_style():
    cands = _cands()
    facts = _facts(cands)
    base = compose_input_fingerprint(cands, facts, AppConfig().posts, None)
    assert base == compose_input_fingerprint(cands, facts, AppConfig().posts, None)
    assert base != compose_input_fingerprint(cands, facts, AppConfig().posts.model_copy(update={"stories": 5}), None)
    assert base != compose_input_fingerprint(cands, facts, AppConfig().posts, _style())
    assert base != compose_input_fingerprint(list(reversed(cands)), facts, AppConfig().posts, None)


def test_compose_records_spend_once(tmp_path):
    cache, cands = Cache(tmp_path / "c.sqlite"), _cands()
    runner = ComposeRunner(FakeClient(compose_handler()), HAIKU, "medium", _preview)
    run_compose(runner, cache, THEME, cands, _facts(cands), AppConfig(), "album-1")
    run_compose(runner, cache, THEME, cands, _facts(cands), AppConfig(), "album-1")
    assert cache.album_spend("album-1") == pytest.approx(USD)


def test_compose_refusal_returns_none(tmp_path):
    cache, cands = Cache(tmp_path / "c.sqlite"), _cands()
    client = FakeClient(lambda n, kw: fake_response(text="I can't", stop_reason="refusal", input_tokens=30_000,
                                                     output_tokens=10))
    outcome = run_compose(ComposeRunner(client, HAIKU, "medium", _preview), cache, THEME, cands, _facts(cands),
                          AppConfig(), "album-1")
    assert outcome.posts is None and outcome.refused
    assert cache.album_spend("album-1") == pytest.approx(30_000 * 0.10 / 1e6 + 10 * 0.50 / 1e6)
    run_compose(ComposeRunner(client, HAIKU, "medium", _preview), cache, THEME, cands, _facts(cands), AppConfig(),
                "album-1")
    assert len(client.calls) == 2  # отказ не кэшируется


def test_compose_unparsable_returns_none(tmp_path):
    client = FakeClient(lambda n, kw: fake_response(text='{"carousels": [', stop_reason="max_tokens"))
    cands = _cands()
    outcome = run_compose(ComposeRunner(client, HAIKU, "medium", _preview), Cache(tmp_path / "c.sqlite"), THEME, cands,
                          _facts(cands), AppConfig(), "album-1")
    assert outcome.posts is None and not outcome.refused


def test_compose_without_candidates_no_call(tmp_path):
    client = FakeClient(compose_handler())
    outcome = run_compose(ComposeRunner(client, HAIKU, "medium", _preview), Cache(tmp_path / "c.sqlite"), THEME, [],
                          {}, AppConfig(), "album-1")
    assert client.calls == [] and outcome.posts.carousels == [] and outcome.posts.stories == []


def test_compose_over_price_threshold_rejected(tmp_path):
    client = FakeClient(compose_handler())
    cands = [make_asset(uuid=f"K{i}") for i in range(30)]
    heavy = replace(_style(), extra_tokens=70_000)
    with pytest.raises(ValueError):
        run_compose(ComposeRunner(client, HAIKU, "medium", _preview, style=heavy), Cache(tmp_path / "c.sqlite"), THEME,
                    cands, _facts(cands), AppConfig(), "album-1")
    assert client.calls == []
