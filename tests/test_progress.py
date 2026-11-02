"""Ход запуска (SPEC FR-7.2): таймаут и повторы запросов с сообщениями, строки групп, ожидание ответа, сборка."""

import threading
from datetime import timedelta

import anthropic
import pytest

from conftest import make_asset
from fakes import (
    FakeClient,
    api_error,
    connection_error,
    fake_response,
    ids_in,
    rate_limit_error,
    server_error,
    timeout_error,
)
from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig, CloudCfg, Theme
from photo_chooser.judge import stages
from photo_chooser.judge.stages import (
    ComposeCarousel,
    ComposeOutput,
    ComposeRunner,
    ComposeSlide,
    JudgeItem,
    JudgeOutput,
    JudgeRunner,
    ScreenItem,
    ScreenMatch,
    ScreenOutput,
    ScreenRunner,
    call_model,
    make_client,
    run_compose,
    run_judge,
    run_screen,
)
from photo_chooser.select import ShotFacts

HAIKU = "claude-haiku-5-5"
THEME = Theme(name="Пирамиды в Гиза", query="Пирамиды в Гиза")
TEXT = [{"type": "text", "text": "x"}]


def _preview(asset):
    return asset.uuid.encode()


def _assets(n):
    t0 = make_asset().taken_at
    return [make_asset(uuid=f"U{i:03d}", taken_at=t0 + timedelta(minutes=i)) for i in range(n)]


def _judge_reply(kw, pause=0.0):
    if pause:
        threading.Event().wait(pause)  # настоящая пауза: time.sleep в части тестов подменён
    return fake_response(JudgeOutput(items=[JudgeItem(id=i, matches_theme=True, feed_score=7, stories_score=5,
                                                      reason="r", crop_feed="", crop_stories="")
                                            for i in ids_in(kw["messages"][0]["content"])]))


def _screen_reply(kw):
    return fake_response(ScreenOutput(items=[ScreenItem(id=i, matches=[ScreenMatch(theme=1, relevance=8)],
                                                        score=6)
                                             for i in ids_in(kw["messages"][0]["content"])]))


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(stages.time, "sleep", calls.append)
    return calls


def test_make_client_has_request_timeout_and_no_sdk_retries():
    client = make_client()
    assert client.timeout == stages.REQUEST_TIMEOUT_S == 300
    assert client.max_retries == 0  # повторы делает call_model, чтобы их было видно


def test_transient_errors_are_retried_with_notices(sleeps):
    errors = [timeout_error(), connection_error(), server_error(529)]
    client = FakeClient(lambda n, kw: errors[n - 1] if n <= 3 else fake_response(text="ok"))
    notes: list[str] = []
    call_model(client, HAIKU, "low", "system", TEXT, None, notify=notes.append)
    assert len(client.calls) == 4
    assert notes == ["  нет ответа 5 мин — повторяю запрос (1/4)",
                     "  сбой сети — повторяю запрос (2/4)",
                     "  ошибка сервера 529 — повторяю запрос (3/4)"]
    assert sleeps == [2, 4, 8]


def test_transient_errors_give_up_after_retries(sleeps):
    client = FakeClient(lambda n, kw: timeout_error())
    with pytest.raises(anthropic.APITimeoutError):
        call_model(client, HAIKU, "low", "system", TEXT, None)
    assert len(client.calls) == 1 + stages.TRANSIENT_RETRIES


def test_client_errors_are_not_retried(sleeps):
    client = FakeClient(lambda n, kw: api_error())
    with pytest.raises(anthropic.BadRequestError):
        call_model(client, HAIKU, "low", "system", TEXT, None)
    assert len(client.calls) == 1 and sleeps == []


def test_rate_limit_pause_is_announced(sleeps):
    client = FakeClient(lambda n, kw: rate_limit_error() if n <= 2 else fake_response(text="ok"))
    notes: list[str] = []
    call_model(client, HAIKU, "low", "system", TEXT, None, notify=notes.append)
    assert notes == ["  лимит API, пауза 20 с (попытка 2/12)", "  лимит API, пауза 20 с (попытка 3/12)"]


def test_runner_passes_notices_to_requests(sleeps):
    client = FakeClient(lambda n, kw: rate_limit_error() if n == 1 else _judge_reply(kw))
    notes: list[str] = []
    JudgeRunner(client, HAIKU, "low", _preview, notify=notes.append).run_group(THEME, _assets(2))
    assert notes == ["  лимит API, пауза 20 с (попытка 2/12)"]


def test_judge_logs_header_and_each_group(tmp_path):
    logs: list[str] = []
    client = FakeClient(lambda n, kw: _judge_reply(kw))
    run_judge(JudgeRunner(client, HAIKU, "low", _preview), Cache(tmp_path / "c.sqlite"), THEME, _assets(40), {},
              CloudCfg(), "album-1", log=logs.append)
    assert logs[0] == "  вторая ступень (claude-haiku-5-5): кандидатов 40, из кэша 0, групп 2"
    assert sorted(line.rsplit(" (", 1)[0] for line in logs[1:]) == ["  вторая ступень: группа 1/2",
                                                                    "  вторая ступень: группа 2/2"]


def test_groups_announce_waiting_for_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "WAIT_NOTICE_S", 0.02)
    logs: list[str] = []
    client = FakeClient(lambda n, kw: _judge_reply(kw, pause=0.2))
    run_judge(JudgeRunner(client, HAIKU, "low", _preview), Cache(tmp_path / "c.sqlite"), THEME, _assets(5), {},
              CloudCfg(), "album-1", log=logs.append)
    assert "  вторая ступень: ждём ответ модели (0:00), готово групп 0/1" in logs
    assert logs[-1].startswith("  вторая ступень: группа 1/1 (")


def test_screen_logs_cached_count(tmp_path):
    cache, assets = Cache(tmp_path / "c.sqlite"), _assets(50)
    runner = ScreenRunner(FakeClient(lambda n, kw: _screen_reply(kw)), HAIKU, "low", _preview)
    run_screen(runner, cache, "Egypt.26", "album-1", [THEME], assets[:20], CloudCfg())
    logs: list[str] = []
    run_screen(runner, cache, "Egypt.26", "album-1", [THEME], assets, CloudCfg(), log=logs.append)
    assert logs[0] == "Первая ступень (claude-haiku-5-5): 50 снимков, из кэша 20, групп 2"


def _compose_reply(pause=0.0):
    def handler(n, kw):
        if pause:
            threading.Event().wait(pause)
        return fake_response(ComposeOutput(carousels=[ComposeCarousel(title="Сфинкс", slides=[
            ComposeSlide(id="c01", alternatives=[])])], stories=[]))
    return handler


def test_compose_logs_request_wait_and_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "WAIT_NOTICE_S", 0.02)
    cache, cands = Cache(tmp_path / "c.sqlite"), _assets(3)
    facts = {a.uuid: ShotFacts(8, 6, "r", "", "") for a in cands}
    runner = ComposeRunner(FakeClient(_compose_reply(pause=0.2)), HAIKU, "low", _preview)
    logs: list[str] = []
    run_compose(runner, cache, THEME, cands, facts, AppConfig(), "album-1", log=logs.append)
    assert logs[0] == "  сборка постов (claude-haiku-5-5): кандидатов 3"
    assert "  сборка постов: ждём ответ модели (0:00)" in logs
    assert logs[-1] == "  сборка постов: ответ за 0:00"
    cached: list[str] = []
    run_compose(runner, cache, THEME, cands, facts, AppConfig(), "album-1", log=cached.append)
    assert cached == ["  сборка постов: из кэша"]


def test_compose_request_error_propagates_from_wait(tmp_path):
    cands = _assets(2)
    facts = {a.uuid: ShotFacts(8, 6, "r", "", "") for a in cands}
    runner = ComposeRunner(FakeClient(lambda n, kw: api_error()), HAIKU, "low", _preview)
    with pytest.raises(anthropic.BadRequestError):
        run_compose(runner, Cache(tmp_path / "c.sqlite"), THEME, cands, facts, AppConfig(), "album-1")
