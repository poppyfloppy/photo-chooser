"""Отправка запросов к модели (SPEC §9, D16, FR-7.2): синхронный транспорт, теги тем, фатальные ошибки, Ctrl+C."""

import threading

import pytest

from fakes import FakeClient, api_error, credit_error, fake_response
from photo_chooser.judge import transport
from photo_chooser.judge.errors import InsufficientCredit
from photo_chooser.judge.transport import Job, JobFailure, ModelRequest, SyncTransport, call_model, request_kwargs

HAIKU = "claude-haiku-5-5"


def _req(custom_id="j-a-01", model=HAIKU) -> ModelRequest:
    return ModelRequest(custom_id=custom_id, model=model, effort="low", system="system",
                        content=[{"type": "text", "text": custom_id}], output_model=None)


def _jobs(tag: str, n: int, handled: list[str]) -> list[Job]:
    return [Job(make_request=lambda i=i: _req(f"{tag}-{i}"), tag=tag,
                handle=lambda resp, i=i: handled.append(f"{tag}-{i}")) for i in range(1, n + 1)]


def _sent(client) -> list[str]:
    return [c["messages"][0]["content"][0]["text"] for c in client.calls]


def test_request_kwargs_has_no_fallbacks():
    kwargs = request_kwargs(_req(model="claude-opus-5-5"))
    assert not {"betas", "fallbacks"} & set(kwargs)
    assert kwargs["messages"] == [{"role": "user", "content": [{"type": "text", "text": "j-a-01"}]}]


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5"])
def test_call_model_uses_fallbacks_for_opus_and_sonnet(model):
    client = FakeClient(lambda n, kw: fake_response(text="ok"))
    call_model(client, _req(model=model))
    call = client.calls[0]
    assert call["_beta"] is True and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]


def test_failure_skips_rest_of_tag_only(monkeypatch):
    monkeypatch.setattr(transport, "MAX_PARALLEL_REQUESTS", 1)
    handled: list[str] = []
    client = FakeClient(lambda n, kw: api_error() if kw["messages"][0]["content"][0]["text"] == "A-2"
                        else fake_response(text="ok"))
    failures = SyncTransport(client).run("judge", _jobs("A", 3, handled) + _jobs("B", 3, handled))
    assert [(f.tag, type(f.error).__name__) for f in failures] == [("A", "BadRequestError")]
    assert "A-3" not in _sent(client)
    assert handled == ["A-1", "B-1", "B-2", "B-3"]


def test_fatal_error_raises_after_in_flight():
    handled: list[str] = []
    started = threading.Barrier(3)

    def h(n, kw):
        if n <= 3:
            started.wait()  # все три запроса в работе, когда первый падает
        if kw["messages"][0]["content"][0]["text"] == "A-1":
            return credit_error()
        threading.Event().wait(0.2)  # остальные отвечают позже ошибки
        return fake_response(text="ok")

    client = FakeClient(h)
    with pytest.raises(InsufficientCredit):
        SyncTransport(client).run("judge", _jobs("A", 3, handled) + _jobs("B", 2, handled))
    assert sorted(handled) == ["A-2", "A-3"]  # начатые доделаны и записаны
    assert len(client.calls) == 3  # после ошибки баланса новые запросы не отправляются


def test_job_lines_per_stage():
    logs: list[str] = []
    client = FakeClient(lambda n, kw: fake_response(text="ok"))
    SyncTransport(client).run("compose", _jobs("A", 1, []), log=logs.append)
    SyncTransport(client).run("judge", _jobs("A", 2, []), log=logs.append)
    assert [line.rsplit(" (", 1)[0] for line in logs] == ["  сборка постов: тема 1/1", "  вторая ступень: группа 1/2",
                                                          "  вторая ступень: группа 2/2"]


def test_wait_notice_uses_stage_units(monkeypatch):
    monkeypatch.setattr(transport, "WAIT_NOTICE_S", 0.02)
    logs: list[str] = []

    def slow(n, kw):
        threading.Event().wait(0.2)
        return fake_response(text="ok")

    SyncTransport(FakeClient(slow)).run("compose", _jobs("A", 1, []), log=logs.append)
    assert "  сборка постов: ждём ответ модели (0:00), готово тем 0/1" in logs


def test_ctrl_c_waits_in_flight_and_announces(monkeypatch):
    real_wait = transport.wait

    def interrupted_wait(fs, timeout=None, return_when=None):
        real_wait(fs)  # все запущенные задания завершились
        raise KeyboardInterrupt

    monkeypatch.setattr(transport, "wait", interrupted_wait)
    handled: list[str] = []
    logs: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        SyncTransport(FakeClient(lambda n, kw: fake_response(text="ok"))).run("judge", _jobs("A", 5, handled),
                                                                              log=logs.append)
    assert len(handled) == 3  # оплаченные ответы записаны, новые запросы не отправлены
    assert logs[-1] == "  Ctrl+C: дожидаюсь ответов на уже отправленные запросы — они оплачены и попадут в кэш"


def test_failure_carries_error():
    assert JobFailure("A", ValueError("x")).error.args == ("x",)


def test_cached_request_goes_first_alone(monkeypatch):
    monkeypatch.setattr(transport, "MAX_PARALLEL_REQUESTS", 3)
    first_done = threading.Event()
    seen: list[bool] = []

    def h(n, kw):
        if n == 1:
            threading.Event().wait(0.1)  # остальные успели бы стартовать, если бы шли параллельно
        else:
            seen.append(first_done.is_set())
        return fake_response(text="ok")

    jobs = [Job(make_request=lambda i=i: ModelRequest(f"j-{i}", HAIKU, "low", "s", [], None, cached=True),
                tag="A", handle=lambda resp, i=i: first_done.set() if i == 1 else None) for i in range(1, 6)]
    SyncTransport(FakeClient(h)).run("judge", jobs)
    assert seen == [True] * 4


def test_non_api_error_is_raised_not_marked_as_theme_failure():
    def broken(resp):
        raise ValueError("bug in parsing")

    jobs = [Job(make_request=lambda: _req("A-1"), tag="A", handle=broken)]
    with pytest.raises(ValueError, match="bug in parsing"):
        SyncTransport(FakeClient(lambda n, kw: fake_response(text="ok"))).run("judge", jobs)
