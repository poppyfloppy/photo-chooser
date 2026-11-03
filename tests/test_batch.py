"""Пакетный режим Message Batches (SPEC FR-12): отправка, ожидание, результаты, продолжение с незабранного пакета."""

import pytest

from fakes import EXPIRED, FakeClient, api_error, billing_error, credit_error, fake_response
from photo_chooser.cache import Cache
from photo_chooser.judge import transport
from photo_chooser.judge.errors import InsufficientCredit
from photo_chooser.judge.transport import BatchRequestFailed, BatchTransport, Job, ModelRequest

HAIKU, OPUS = "claude-haiku-5-5", "claude-opus-5-5"
ALBUM = "album-1"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(transport.time, "sleep", lambda s: None)


def _jobs(tag: str, n: int, handled: list[str], model: str = HAIKU) -> list[Job]:
    return [Job(make_request=lambda i=i: ModelRequest(f"j-{tag}-{i:02d}", model, "low", "system",
                                                      [{"type": "text", "text": f"{tag}-{i}"}], None),
                tag=tag, meta={"n": i}, handle=lambda resp, i=i: handled.append(f"{tag}-{i}"))
            for i in range(1, n + 1)]


def _by_text(outcomes: dict):
    def handler(n, kw):
        return outcomes.get(kw["messages"][0]["content"][0]["text"], fake_response(text="ok"))
    return handler


def _strip_time(logs: list[str]) -> list[str]:
    return [line.rsplit(" (", 1)[0] if line.endswith(")") else line for line in logs]


def test_batch_requests_shape(tmp_path):
    client = FakeClient(_by_text({}))
    BatchTransport(client, Cache(tmp_path / "c.sqlite"), ALBUM).run("judge", _jobs("A", 2, [], model=OPUS))
    assert [c["model"] for c in client.calls] == [OPUS, OPUS]
    assert all(c["_batch"] and not {"betas", "fallbacks"} & set(c) for c in client.calls)


def test_batch_persisted_before_wait(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    seen = []

    def log(line):
        if "отправлен" in line:
            seen.extend(cache.pending_batches(ALBUM, "judge"))

    BatchTransport(FakeClient(_by_text({})), cache, ALBUM).run("judge", _jobs("A", 2, []), log=log)
    [pending] = seen
    assert pending.batch_id == "msgbatch_001" and pending.metas == {"j-A-01": {"n": 1}, "j-A-02": {"n": 2}}
    assert cache.pending_batches(ALBUM, "judge") == []  # после разбора пакет забран


def test_batch_progress_lines(tmp_path):
    logs: list[str] = []
    BatchTransport(FakeClient(_by_text({}), batch_polls=2), Cache(tmp_path / "c.sqlite"), ALBUM).run(
        "judge", _jobs("A", 3, []), log=logs.append)
    assert _strip_time(logs) == ["  пакет второй ступени msgbatch_001: отправлен, запросов 3",
                                 "  пакет второй ступени: готово 0 из 3 запросов",
                                 "  пакет второй ступени: готово 0 из 3 запросов",
                                 "  пакет второй ступени: готов"]


def test_batch_mixed_results(tmp_path):
    handled: list[str] = []
    cache = Cache(tmp_path / "c.sqlite")
    client = FakeClient(_by_text({"A-2": api_error(), "B-1": EXPIRED}))
    failures = BatchTransport(client, cache, ALBUM).run("judge", _jobs("A", 2, handled) + _jobs("B", 1, handled))
    assert handled == ["A-1"]
    assert [(f.tag, str(f.error)) for f in failures] == [
        ("A", "запрос пакета не выполнен (errored: api_error)"), ("B", "запрос пакета не выполнен (expired)")]
    assert all(isinstance(f.error, BatchRequestFailed) for f in failures)
    assert cache.pending_batches(ALBUM, "judge") == []


@pytest.mark.parametrize("error", [billing_error, credit_error])
def test_batch_billing_error_raises_after_collecting(tmp_path, error):
    handled: list[str] = []
    client = FakeClient(_by_text({"A-2": error()}))
    with pytest.raises(InsufficientCredit):
        BatchTransport(client, Cache(tmp_path / "c.sqlite"), ALBUM).run("judge", _jobs("A", 2, handled))
    assert handled == ["A-1"]


def test_batch_create_credit_error(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    client = FakeClient(_by_text({}), batch_create_error=credit_error())
    with pytest.raises(InsufficientCredit):
        BatchTransport(client, cache, ALBUM).run("judge", _jobs("A", 1, []))
    assert cache.pending_batches(ALBUM, "judge") == []


def test_batch_create_other_error_fails_every_theme(tmp_path):
    client = FakeClient(_by_text({}), batch_create_error=api_error())
    failures = BatchTransport(client, Cache(tmp_path / "c.sqlite"), ALBUM).run(
        "judge", _jobs("A", 2, []) + _jobs("B", 1, []))
    assert sorted(f.tag for f in failures) == ["A", "B"]


def test_batch_ctrl_c_keeps_pending(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    logs: list[str] = []

    def log(line):
        logs.append(line)
        if "готово" in line:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        BatchTransport(FakeClient(_by_text({})), cache, ALBUM).run("judge", _jobs("A", 1, []), log=log)
    assert logs[-1] == ("  пакет второй ступени msgbatch_001 досчитается на сервере; повторный запуск заберёт "
                        "результат без новой оплаты")
    assert [p.batch_id for p in cache.pending_batches(ALBUM, "judge")] == ["msgbatch_001"]


def test_empty_jobs_send_nothing(tmp_path):
    client = FakeClient(_by_text({}))
    assert BatchTransport(client, Cache(tmp_path / "c.sqlite"), ALBUM).run("judge", []) == []
    assert client.created_batches == []
