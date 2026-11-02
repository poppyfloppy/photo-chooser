import base64
import random
from datetime import timedelta

import anthropic
import pytest

from conftest import make_asset
from fakes import FakeClient, api_error, fake_response, ids_in, last_text, output_model_of, rate_limit_error
from photo_chooser.cache import Cache
from photo_chooser.config import CloudCfg, Theme, themes_key
from photo_chooser.judge import stages
from photo_chooser.judge.prompts import (
    JUDGE_PROMPT_VERSION,
    JUDGE_SYSTEM_PROMPT,
    SCREEN_PROMPT_VERSION,
    SCREEN_SYSTEM_PROMPT,
)
from photo_chooser.judge.stages import (
    JudgeItem,
    JudgeOutput,
    JudgeRunner,
    ScreenItem,
    ScreenMatch,
    ScreenOutput,
    ScreenRunner,
    call_model,
    make_groups,
    run_judge,
    run_screen,
)

HAIKU = "claude-haiku-5-5"
THEMES = [Theme(name="Пирамиды в Гиза", query="Пирамиды в Гиза"), Theme(name="GEM", query="Большой Египетский музей"),
          Theme(name="Абу Симбел", query="храм Абу Симбел"), Theme(name="Нил", query="Нил"),
          Theme(name="Саккара", query="Саккара"), Theme(name="Луксор", query="Луксорский храм"),
          Theme(name="Карнак", query="Карнакский храм"), Theme(name="Храмы", query="оставшиеся храмы")]
GROUP_USD = 20_000 * 0.10 / 1e6 + 2_000 * 0.50 / 1e6  # usage fake_response по умолчанию на Haiku


def _preview(asset):
    return asset.uuid.encode()  # base64 картинки в запросе раскрывает, какой снимок отправлен


def _sent_uuids(call) -> list[str]:
    return [base64.b64decode(b["source"]["data"]).decode() for b in call["messages"][0]["content"]
            if b["type"] == "image"]


def _assets(n, start=None):
    t0 = (start or make_asset()).taken_at
    return [make_asset(uuid=f"U{i:03d}", taken_at=t0 + timedelta(minutes=i)) for i in range(n)]


def screen_handler(matches=lambda i: [ScreenMatch(theme=1, relevance=8)], score=6, **resp):
    def handler(n, kwargs):
        ids = ids_in(kwargs["messages"][0]["content"])
        return fake_response(ScreenOutput(items=[ScreenItem(id=i, matches=matches(i), score=score) for i in ids]),
                             **resp)
    return handler


def judge_handler(feed=7, stories=5, only=None, **resp):
    def handler(n, kwargs):
        ids = ids_in(kwargs["messages"][0]["content"])
        ids = [i for i in ids if only is None or i in only]
        return fake_response(JudgeOutput(items=[JudgeItem(id=i, matches_theme=True, feed_score=feed,
                                                          stories_score=stories, reason="Пирамиды на закате", crop_feed="", crop_stories="")
                                                for i in ids]), **resp)
    return handler


def test_make_groups_spreads_order():
    a = _assets(6)
    order = {x.uuid: float(i) for i, x in enumerate(a)}  # U005 — лучший
    groups = make_groups(a, order, 2)
    assert [[x.uuid for x in g] for g in groups] == [["U005", "U002"], ["U004", "U001"], ["U003", "U000"]]
    assert [len(g) for g in make_groups(_assets(5), {}, 2)] == [2, 2, 1]
    assert make_groups([], {}, 20) == []


def test_screen_maps_theme_numbers_to_keys():
    client = FakeClient(screen_handler(matches=lambda i: [ScreenMatch(theme=2, relevance=8)]))
    a = _assets(1)
    result = ScreenRunner(client, HAIKU, "medium", _preview).run_group("Egypt.26", THEMES, a)
    assert result.items[0].uuid == "U000"
    assert result.items[0].relevance == {THEMES[1].screen_key(): 8}
    assert result.items[0].score == 6


def test_screen_drops_unknown_theme_numbers_and_clamps():
    matches = [ScreenMatch(theme=9, relevance=7), ScreenMatch(theme=1, relevance=12), ScreenMatch(theme=0, relevance=6)]
    client = FakeClient(screen_handler(matches=lambda i: matches))
    result = ScreenRunner(client, HAIKU, "medium", _preview).run_group("Egypt.26", THEMES, _assets(1))
    assert result.items[0].relevance == {THEMES[0].screen_key(): 10}


@pytest.mark.parametrize("raw, clamped", [(0, 1), (11, 10)])
def test_screen_score_clamped(raw, clamped):
    client = FakeClient(screen_handler(score=raw))
    result = ScreenRunner(client, HAIKU, "medium", _preview).run_group("Egypt.26", THEMES, _assets(1))
    assert result.items[0].score == clamped


def test_screen_schema_single_score():
    assert set(ScreenItem.model_fields) == {"id", "matches", "score"}


def test_screen_prompt_portrait_without_people_bias():
    assert "posed portrait of the traveller" in SCREEN_SYSTEM_PROMPT
    assert "without people" not in SCREEN_SYSTEM_PROMPT
    assert "feed_score" not in SCREEN_SYSTEM_PROMPT


def test_screen_request_lists_album_and_themes():
    client = FakeClient(screen_handler())
    ScreenRunner(client, HAIKU, "medium", _preview).run_group("Egypt.26", THEMES, _assets(2))
    content = client.calls[0]["messages"][0]["content"]
    assert ids_in(content) == ["p01", "p02"]
    assert _sent_uuids(client.calls[0]) == ["U000", "U001"]
    assert content[1]["source"]["media_type"] == "image/jpeg"
    text = last_text(content)
    assert "Trip album: «Egypt.26»" in text
    assert "1. Пирамиды в Гиза\n" in text
    assert "2. GEM: Большой Египетский музей" in text
    assert "8. Храмы: оставшиеся храмы" in text


def test_screen_cache_keyed_by_theme_set(tmp_path):
    cache, cfg, assets = Cache(tmp_path / "c.sqlite"), CloudCfg(), _assets(3)
    client = FakeClient(screen_handler())
    runner = ScreenRunner(client, HAIKU, "medium", _preview)
    first = run_screen(runner, cache, "Egypt.26", "album-1", THEMES[:2], assets, cfg)
    assert len(client.calls) == 1 and first.sent == 3 and first.from_cache == 0
    assert first.usd == pytest.approx(GROUP_USD)
    again = run_screen(runner, cache, "Egypt.26", "album-1", THEMES[:2], assets, cfg)
    assert len(client.calls) == 1 and again.sent == 0 and again.from_cache == 3 and again.usd == 0
    assert again.results == first.results
    run_screen(runner, cache, "Egypt.26", "album-1", THEMES[:3], assets, cfg)
    assert len(client.calls) == 2
    key = themes_key("Egypt.26", THEMES[:2])
    assert cache.get_screening("U000", key, HAIKU, SCREEN_PROMPT_VERSION) == first.results["U000"]


def test_screen_groups_follow_capture_time(tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "MAX_PARALLEL_REQUESTS", 1)
    assets = _assets(25)
    shuffled = random.Random(3).sample(assets, len(assets))
    client = FakeClient(screen_handler())
    run_screen(ScreenRunner(client, HAIKU, "medium", _preview), Cache(tmp_path / "c.sqlite"), "Egypt.26",
               "album-1", THEMES, shuffled, CloudCfg())
    assert _sent_uuids(client.calls[0]) == [f"U{i:03d}" for i in range(20)]
    assert _sent_uuids(client.calls[1]) == [f"U{i:03d}" for i in range(20, 25)]


def test_judge_maps_short_ids_and_clamps():
    def handler(n, kwargs):
        return fake_response(JudgeOutput(items=[
            JudgeItem(id="p02", matches_theme=False, feed_score=11, stories_score=0, reason="Не Гиза", crop_feed="", crop_stories=""),
            JudgeItem(id="p01", matches_theme=True, feed_score=8, stories_score=4, reason="Сфинкс крупно", crop_feed="", crop_stories="")]))

    client = FakeClient(handler)
    result = JudgeRunner(client, HAIKU, "medium", _preview).run_group(THEMES[0], _assets(2))
    by_uuid = {j.uuid: j for j in result.items}
    assert (by_uuid["U001"].matches_theme, by_uuid["U001"].feed_score, by_uuid["U001"].stories_score) == (False, 10, 1)
    assert by_uuid["U000"].reason == "Сфинкс крупно"
    assert result.unjudged == [] and not result.refused
    assert "Theme: «Пирамиды в Гиза»" in last_text(client.calls[0]["messages"][0]["content"])


def test_missing_ids_are_unjudged(tmp_path):
    cache, cfg, assets = Cache(tmp_path / "c.sqlite"), CloudCfg(), _assets(3)

    def handler(n, kwargs):
        return fake_response(JudgeOutput(items=[
            JudgeItem(id="p01", matches_theme=True, feed_score=8, stories_score=4, reason="r", crop_feed="", crop_stories=""),
            JudgeItem(id="p99", matches_theme=True, feed_score=8, stories_score=4, reason="r", crop_feed="", crop_stories="")]))

    client = FakeClient(handler)
    runner = JudgeRunner(client, HAIKU, "medium", _preview)
    order = {"U000": 3.0, "U001": 2.0, "U002": 1.0}
    outcome = run_judge(runner, cache, THEMES[0], assets, order, cfg, "album-1")
    assert set(outcome.results) == {"U000"}
    assert sorted(outcome.unjudged) == ["U001", "U002"]
    run_judge(runner, cache, THEMES[0], assets, order, cfg, "album-1")
    assert _sent_uuids(client.calls[1]) == ["U001", "U002"]  # неоценённые не кэшируются и уходят снова


def test_refusal_marks_group_unjudged(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    client = FakeClient(lambda n, kw: fake_response(None, stop_reason="refusal", input_tokens=30_000,
                                                     output_tokens=100))
    runner = JudgeRunner(client, HAIKU, "medium", _preview)
    group = _assets(2)
    result = runner.run_group(THEMES[0], group)
    assert result.refused and result.items == [] and result.unjudged == ["U000", "U001"]
    assert (result.input_tokens, result.output_tokens) == (30_000, 100)
    outcome = run_judge(runner, cache, THEMES[0], group, {}, CloudCfg(), "album-1")
    assert outcome.results == {} and sorted(outcome.unjudged) == ["U000", "U001"]
    assert cache.album_spend("album-1") == pytest.approx(30_000 * 0.10 / 1e6 + 100 * 0.50 / 1e6)


def test_interrupted_run_does_not_pay_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "MAX_PARALLEL_REQUESTS", 1)
    cache, cfg, assets = Cache(tmp_path / "c.sqlite"), CloudCfg(), _assets(50)
    order = {a.uuid: float(i) for i, a in enumerate(assets)}
    ok = judge_handler()
    failing = FakeClient(lambda n, kw: api_error() if n == 2 else ok(n, kw))
    with pytest.raises(anthropic.APIStatusError):
        run_judge(JudgeRunner(failing, HAIKU, "medium", _preview), cache, THEMES[0], assets, order, cfg, "album-1")
    assert len(failing.calls) == 2
    assert cache.album_spend("album-1") == pytest.approx(GROUP_USD)
    working = FakeClient(ok)
    outcome = run_judge(JudgeRunner(working, HAIKU, "medium", _preview), cache, THEMES[0], assets, order, cfg,
                        "album-1")
    assert len(working.calls) == 2
    assert len(outcome.results) == 50 and outcome.from_cache == 17
    assert cache.album_spend("album-1") == pytest.approx(3 * GROUP_USD)


def test_parallel_failure_keeps_completed_groups(tmp_path):
    cache, cfg, assets = Cache(tmp_path / "c.sqlite"), CloudCfg(), _assets(60)
    ok = judge_handler()
    client = FakeClient(lambda n, kw: api_error() if n == 1 else ok(n, kw))
    with pytest.raises(anthropic.APIStatusError):
        run_judge(JudgeRunner(client, HAIKU, "medium", _preview), cache, THEMES[0], assets, {}, cfg, "album-1")
    paid = len(client.calls) - 1
    assert cache.album_spend("album-1") == pytest.approx(paid * GROUP_USD)
    cached = [a for a in assets if cache.get_judgment(a.uuid, THEMES[0].key(), HAIKU, JUDGE_PROMPT_VERSION)]
    assert len(cached) == paid * 20


def test_rate_limit_waits_and_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr(stages.time, "sleep", lambda s: sleeps.append(s))
    ok = judge_handler()
    client = FakeClient(lambda n, kw: rate_limit_error() if n <= 2 else ok(n, kw))
    result = JudgeRunner(client, HAIKU, "medium", _preview).run_group(THEMES[0], _assets(2))
    assert len(result.items) == 2
    assert sleeps == [20, 20]


def test_rate_limit_gives_up_after_12_attempts(monkeypatch):
    monkeypatch.setattr(stages.time, "sleep", lambda s: None)
    client = FakeClient(lambda n, kw: rate_limit_error())
    with pytest.raises(anthropic.RateLimitError):
        call_model(client, HAIKU, "medium", "system", [{"type": "text", "text": "x"}], JudgeOutput)
    assert len(client.calls) == 12


def test_request_shape():
    client = FakeClient(screen_handler())
    ScreenRunner(client, HAIKU, "low", _preview).run_group("Egypt.26", THEMES, _assets(1))
    call = client.calls[0]
    assert call["_beta"] is False
    assert call["model"] == HAIKU and call["max_tokens"] == 16000
    assert call["system"] == SCREEN_SYSTEM_PROMPT
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert output_model_of(call) is ScreenOutput
    assert [m["role"] for m in call["messages"]] == ["user"]
    assert not {"thinking", "betas", "fallbacks", "output_format"} & set(call)
    judge_client = FakeClient(judge_handler())
    JudgeRunner(judge_client, HAIKU, "medium", _preview).run_group(THEMES[0], _assets(1))
    assert judge_client.calls[0]["system"] == JUDGE_SYSTEM_PROMPT
    assert output_model_of(judge_client.calls[0]) is JudgeOutput


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5"])
def test_opus_and_sonnet_use_server_side_fallbacks(model):
    client = FakeClient(judge_handler())
    JudgeRunner(client, model, "high", _preview).run_group(THEMES[0], _assets(1))
    call = client.calls[0]
    assert call["_beta"] is True
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["model"] == model


def test_group_over_haiku_price_threshold_rejected(tmp_path):
    client = FakeClient(judge_handler())
    cfg = CloudCfg(group_size=100)
    with pytest.raises(ValueError):
        run_judge(JudgeRunner(client, HAIKU, "medium", _preview), Cache(tmp_path / "c.sqlite"), THEMES[0],
                  _assets(100), {}, cfg, "album-1")
    assert client.calls == []


def test_truncated_response_records_actual_usage(tmp_path):
    # ответ обрезан по max_tokens: JSON неполный, но usage оплачен полностью
    client = FakeClient(lambda n, kw: fake_response(text='{"items": [{"id": "p01", "matc', stop_reason="max_tokens",
                                                     input_tokens=30_000, output_tokens=16_000))
    cache = Cache(tmp_path / "c.sqlite")
    outcome = run_judge(JudgeRunner(client, HAIKU, "medium", _preview), cache, THEMES[0], _assets(2), {}, CloudCfg(),
                        "album-1")
    assert outcome.results == {} and sorted(outcome.unjudged) == ["U000", "U001"]
    assert cache.album_spend("album-1") == pytest.approx(30_000 * 0.10 / 1e6 + 16_000 * 0.50 / 1e6)


def test_refusal_with_text_is_refused():
    client = FakeClient(lambda n, kw: fake_response(text="I can't help with that.", stop_reason="refusal"))
    result = JudgeRunner(client, HAIKU, "medium", _preview).run_group(THEMES[0], _assets(1))
    assert result.refused and result.unjudged == ["U000"]


def test_interrupt_keeps_completed_groups(tmp_path, monkeypatch):
    # Ctrl+C в главном потоке, пока группы в работе: оплаченные ответы всё равно записываются
    real_wait = stages.wait

    def interrupted_wait(fs, timeout=None, return_when=None):
        real_wait(fs)  # все запущенные группы завершились
        raise KeyboardInterrupt

    monkeypatch.setattr(stages, "wait", interrupted_wait)
    cache, assets = Cache(tmp_path / "c.sqlite"), _assets(60)
    client = FakeClient(judge_handler())
    with pytest.raises(KeyboardInterrupt):
        run_judge(JudgeRunner(client, HAIKU, "medium", _preview), cache, THEMES[0], assets, {}, CloudCfg(), "album-1")
    assert len(client.calls) == 3
    assert cache.album_spend("album-1") == pytest.approx(3 * GROUP_USD)
    assert all(cache.get_judgment(a.uuid, THEMES[0].key(), HAIKU, JUDGE_PROMPT_VERSION) for a in assets)


def test_judge1_cache_not_reused_after_rubric_change(tmp_path):
    # рубрики ленты и stories изменились (D8) — оценки по прежнему промпту не годятся
    from photo_chooser.models import Judgment

    cache, assets = Cache(tmp_path / "c.sqlite"), _assets(2)
    old = [Judgment(uuid=a.uuid, matches_theme=True, feed_score=5, stories_score=8, reason="есть люди") for a in assets]
    cache.put_judgments(old, THEMES[0].key(), HAIKU, "judge-1")
    client = FakeClient(judge_handler())
    run_judge(JudgeRunner(client, HAIKU, "medium", _preview), cache, THEMES[0], assets, {}, CloudCfg(), "album-1")
    assert len(client.calls) == 1


def test_screen1_cache_not_reused_after_rubric_change(tmp_path):
    from photo_chooser.models import Screening

    cache, assets = Cache(tmp_path / "c.sqlite"), _assets(2)
    old = [Screening(uuid=a.uuid, relevance={THEMES[0].screen_key(): 8}, score=5) for a in assets]
    cache.put_screenings(old, themes_key("Egypt.26", THEMES[:2]), HAIKU, "screen-1")
    client = FakeClient(screen_handler())
    run_screen(ScreenRunner(client, HAIKU, "medium", _preview), cache, "Egypt.26", "album-1", THEMES[:2], assets,
               CloudCfg())
    assert len(client.calls) == 1


def test_screen2_cache_not_reused(tmp_path):
    # две черновые оценки заменены одной силой кадра (D13) — оценки screen-2 не годятся
    from photo_chooser.models import Screening

    cache, assets = Cache(tmp_path / "c.sqlite"), _assets(2)
    old = [Screening(uuid=a.uuid, relevance={THEMES[0].screen_key(): 8}, score=5) for a in assets]
    cache.put_screenings(old, themes_key("Egypt.26", THEMES[:2]), HAIKU, "screen-2")
    client = FakeClient(screen_handler())
    run_screen(ScreenRunner(client, HAIKU, "medium", _preview), cache, "Egypt.26", "album-1", THEMES[:2], assets,
               CloudCfg())
    assert len(client.calls) == 1


def _style(fp="ab12cd34", refs=(("feed", b"REF1"), ("stories", b"REF2"))):
    from datetime import datetime

    from photo_chooser.style import StyleContext

    return StyleContext(text="## Лента\nТёплый свет, минимализм.", references=list(refs), fingerprint=fp,
                        created_at=datetime(2026, 11, 1, 18, 0), extra_tokens=3500, warnings=[])


def test_judge_with_style_request():
    client = FakeClient(judge_handler())
    runner = JudgeRunner(client, HAIKU, "medium", _preview, style=_style())
    runner.run_group(THEMES[0], _assets(2))
    call = client.calls[0]
    assert call["system"].startswith(JUDGE_SYSTEM_PROMPT) and "Тёплый свет, минимализм." in call["system"]
    assert "do not score them" in call["system"]  # скриншоты приложены — оговорка на месте
    content = call["messages"][0]["content"]
    assert "feed grid" in content[0]["text"] and "stories" in content[2]["text"]
    assert [base64.b64decode(content[i]["source"]["data"]) for i in (1, 3)] == [b"REF1", b"REF2"]
    assert content[4]["text"] == "id: p01 (horizontal)"
    assert ids_in(content) == ["p01", "p02"] and _sent_uuids(call)[2:] == ["U000", "U001"]
    assert runner.prompt_version == f"{JUDGE_PROMPT_VERSION}/style-ab12cd34" and runner.extra_input_tokens == 3500


def test_judge_cache_keyed_by_style_fingerprint(tmp_path):
    cache, assets = Cache(tmp_path / "c.sqlite"), _assets(2)
    client = FakeClient(judge_handler())

    def run(style):
        run_judge(JudgeRunner(client, HAIKU, "medium", _preview, style=style), cache, THEMES[0], assets, {},
                  CloudCfg(), "album-1")

    run(_style("aaaa1111"))
    run(_style("aaaa1111"))
    assert len(client.calls) == 1
    run(_style("bbbb2222"))
    assert len(client.calls) == 2


def test_judge_without_style_unchanged():
    client = FakeClient(judge_handler())
    runner = JudgeRunner(client, HAIKU, "medium", _preview)
    runner.run_group(THEMES[0], _assets(1))
    assert runner.prompt_version == JUDGE_PROMPT_VERSION and runner.extra_input_tokens == 0
    assert client.calls[0]["system"] == JUDGE_SYSTEM_PROMPT
    assert client.calls[0]["messages"][0]["content"][0]["text"] == "id: p01 (horizontal)"


def test_style_tokens_count_toward_price_threshold(tmp_path):
    # 80 превью ≈ 84 тыс. токенов — меньше порога; со стилем на 20 тыс. — уже больше
    client = FakeClient(judge_handler())
    cfg = CloudCfg(group_size=80)
    run_judge(JudgeRunner(client, HAIKU, "medium", _preview), Cache(tmp_path / "a.sqlite"), THEMES[0], _assets(80),
              {}, cfg, "album-1")
    from dataclasses import replace

    heavy = replace(_style(), extra_tokens=20_000)
    with pytest.raises(ValueError):
        run_judge(JudgeRunner(client, HAIKU, "medium", _preview, style=heavy), Cache(tmp_path / "b.sqlite"),
                  THEMES[0], _assets(80), {}, cfg, "album-1")



def test_judge_style_without_screenshots_omits_screenshot_note():
    # reference_images: 0 или альбома нет — модель не должна искать образцы среди кандидатов
    client = FakeClient(judge_handler())
    JudgeRunner(client, HAIKU, "medium", _preview, style=_style(refs=())).run_group(THEMES[0], _assets(2))
    call = client.calls[0]
    assert "Тёплый свет, минимализм." in call["system"]
    from photo_chooser.judge.prompts import STYLE_SCREENSHOTS_NOTE

    assert STYLE_SCREENSHOTS_NOTE not in call["system"] and "start of the message" not in call["system"]
    assert call["messages"][0]["content"][0]["text"] == "id: p01 (horizontal)"



def _labels(call) -> list[str]:
    return [b["text"] for b in call["messages"][0]["content"] if b["type"] == "text" and b["text"].startswith("id: ")]


def _oriented():
    return [make_asset(uuid="H", width=4032, height=3024), make_asset(uuid="V", width=3024, height=4032),
            make_asset(uuid="S", width=3000, height=3000)]


def test_judge_labels_orientation():
    client = FakeClient(judge_handler())
    JudgeRunner(client, HAIKU, "medium", _preview).run_group(THEMES[0], _oriented())
    assert _labels(client.calls[0]) == ["id: p01 (horizontal)", "id: p02 (vertical)", "id: p03 (square)"]


def test_screen_labels_stay_plain():
    client = FakeClient(screen_handler())
    ScreenRunner(client, HAIKU, "medium", _preview).run_group("Egypt.26", THEMES, _oriented())
    assert _labels(client.calls[0]) == ["id: p01", "id: p02", "id: p03"]


def test_judge_maps_crop_advice_and_clears_vertical():
    def handler(n, kwargs):
        return fake_response(JudgeOutput(items=[
            JudgeItem(id="p01", matches_theme=True, feed_score=8, stories_score=6, reason="r",
                      crop_feed="Оставить сфинкса справа", crop_stories="Сфинкс по центру, небо сверху"),
            JudgeItem(id="p02", matches_theme=True, feed_score=8, stories_score=6, reason="r",
                      crop_feed="лишний совет", crop_stories="лишний совет"),
            JudgeItem(id="p03", matches_theme=True, feed_score=8, stories_score=6, reason="r",
                      crop_feed=" Пирамида по центру ", crop_stories="")]))

    result = JudgeRunner(FakeClient(handler), HAIKU, "medium", _preview).run_group(THEMES[0], _oriented())
    by = {j.uuid: j for j in result.items}
    assert (by["H"].crop_feed, by["H"].crop_stories) == ("Оставить сфинкса справа", "Сфинкс по центру, небо сверху")
    assert (by["V"].crop_feed, by["V"].crop_stories) == ("", "")  # вертикальный кадр кадрировать не нужно
    assert by["S"].crop_feed == "Пирамида по центру"


def test_judge_id_with_orientation_suffix_still_maps():
    def handler(n, kwargs):
        return fake_response(JudgeOutput(items=[JudgeItem(id="p01 (horizontal)", matches_theme=True, feed_score=8,
                                                          stories_score=6, reason="r", crop_feed="", crop_stories="")]))

    result = JudgeRunner(FakeClient(handler), HAIKU, "medium", _preview).run_group(THEMES[0], _oriented()[:1])
    assert [j.uuid for j in result.items] == ["H"] and result.unjudged == []


def test_judge2_cache_not_reused_after_crop_advice(tmp_path):
    from photo_chooser.models import Judgment

    cache, assets = Cache(tmp_path / "c.sqlite"), _assets(2)
    old = [Judgment(uuid=a.uuid, matches_theme=True, feed_score=7, stories_score=5, reason="r") for a in assets]
    cache.put_judgments(old, THEMES[0].key(), HAIKU, "judge-2")
    client = FakeClient(judge_handler())
    run_judge(JudgeRunner(client, HAIKU, "medium", _preview), cache, THEMES[0], assets, {}, CloudCfg(), "album-1")
    assert len(client.calls) == 1


def test_judge3_cache_not_reused_after_potential_rubric(tmp_path):
    # D12: оценка по потенциалу после обработки — оценки judge-3 пересчитываются
    from photo_chooser.models import Judgment

    cache, assets = Cache(tmp_path / "c.sqlite"), _assets(2)
    old = [Judgment(uuid=a.uuid, matches_theme=True, feed_score=4, stories_score=4, reason="r") for a in assets]
    cache.put_judgments(old, THEMES[0].key(), HAIKU, "judge-3")
    client = FakeClient(judge_handler())
    run_judge(JudgeRunner(client, HAIKU, "medium", _preview), cache, THEMES[0], assets, {}, CloudCfg(), "album-1")
    assert len(client.calls) == 1


def test_judge_reference_labels_per_kind():
    from photo_chooser.judge.prompts import STYLE_REFERENCE_LABELS

    refs = (("feed", b"F1"), ("feed", b"F2"), ("stories", b"S1"))
    client = FakeClient(judge_handler())
    JudgeRunner(client, HAIKU, "medium", _preview, style=_style(refs=refs)).run_group(THEMES[0], _assets(1))
    content = client.calls[0]["messages"][0]["content"]
    labels = [content[i]["text"] for i in (0, 2, 4)]
    assert labels == [STYLE_REFERENCE_LABELS["feed"], STYLE_REFERENCE_LABELS["feed"], STYLE_REFERENCE_LABELS["stories"]]
    assert [base64.b64decode(content[i]["source"]["data"]) for i in (1, 3, 5)] == [b"F1", b"F2", b"S1"]
    assert content[6]["text"] == "id: p01 (horizontal)"


def test_judge_prompt_puts_aesthetics_and_style_first():
    # приёмка 0.3: вес эстетики и стиля поднят над техникой и познавательностью (FR-3 п.8, FR-11.4)
    from photo_chooser.judge.prompts import STYLE_BLOCK

    assert JUDGE_PROMPT_VERSION == "judge-5"
    assert JUDGE_SYSTEM_PROMPT.count("Aesthetics come first") == 2  # для ленты и для stories
    assert "main criterion" in STYLE_BLOCK and "as important as composition" not in STYLE_BLOCK
