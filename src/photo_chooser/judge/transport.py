"""Отправка запросов к модели (SPEC §9, D16). Раннеры этапов строят запросы, транспорт их отправляет:
SyncTransport — параллельные запросы с повторами и строками хода (FR-7.2); BatchTransport — один пакет
Message Batches на этап, ожидание и разбор результатов (FR-12).

Задание (Job) — запрос и обработчик ответа: обработчик сам разбирает ответ, пишет его в кэш и расход, поэтому
оплаченный ответ не теряется ни при ошибке другого задания, ни при Ctrl+C.
"""

import time
from collections.abc import Callable
from datetime import datetime
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

import anthropic
from pydantic import BaseModel

from photo_chooser import keychain
from photo_chooser.cache import Cache
from photo_chooser.config import ConfigError
from photo_chooser.judge.errors import InsufficientCredit, is_billing_error, is_fatal

MAX_TOKENS = 16000
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PARALLEL_REQUESTS = 3
RATE_LIMIT_SLEEP_S = 20
RATE_LIMIT_ATTEMPTS = 12
REQUEST_TIMEOUT_S = 300  # без ответа дольше — повтор (SPEC FR-7.2); у SDK по умолчанию 10 минут
TRANSIENT_RETRIES = 4  # таймаут, сбой сети, ошибка сервера 5xx
WAIT_NOTICE_S = 30  # строка ожидания, если ответа нет дольше
POLL_S = 60  # опрос пакета (FR-12.3)
BATCH_RESULTS_TTL_S = 29 * 24 * 3600  # результаты пакета API хранит 29 дней (FR-12.4)
BATCH_LABELS = {"judge": "второй ступени", "compose": "сборки постов"}

Stage = Literal["screen", "judge", "compose"]
# подпись этапа в строках хода и единица: именительный падеж и родительный множественного
_SYNC_LABELS = {"screen": ("первая ступень", "группа", "групп"),
                "judge": ("вторая ступень", "группа", "групп"),
                "compose": ("сборка постов", "тема", "тем")}


def _quiet(message: str) -> None:
    pass


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def make_client() -> anthropic.Anthropic:
    """Ключ — из Keychain; без записи SDK берёт ANTHROPIC_API_KEY, затем профиль `ant auth login` (FR-13.1).
    Повторы SDK выключены: их делает call_model, чтобы о каждом было видно в выводе."""
    return anthropic.Anthropic(api_key=keychain.read_api_key(), max_retries=0, timeout=REQUEST_TIMEOUT_S)


def has_credentials(client: anthropic.Anthropic) -> bool:
    return bool(client.api_key or client.auth_token or client.credentials)


def check_credentials(client) -> None:
    """SDK создаёт клиента и без ключа и падает только на первом запросе — проверяем заранее."""
    if isinstance(client, anthropic.Anthropic) and not has_credentials(client):
        raise ConfigError("Нет ключа Claude API: выполните `photo-chooser key set` или задайте ANTHROPIC_API_KEY / "
                          "`ant auth login`. Без облака можно запустить с --no-cloud.")


@dataclass(frozen=True)
class ModelRequest:
    custom_id: str  # ^[a-zA-Z0-9_-]{1,64}$ — id запроса в пакете (FR-12)
    model: str
    effort: str
    system: str | list[dict]
    content: list[dict]
    output_model: type[BaseModel] | None  # None — свободный текст (профиль стиля)
    cache_ttl: Literal["5m", "1h"] | None = None  # у запроса есть метка кэша мета-блока (D15)


def request_kwargs(req: ModelRequest) -> dict:
    """Параметры messages.create без server-side fallback: его добавляет call_model, в пакетах он запрещён."""
    output_config: dict = {"effort": req.effort}
    if req.output_model is not None:
        output_config["format"] = {"type": "json_schema", "schema": anthropic.transform_schema(req.output_model)}
    return dict(model=req.model, max_tokens=MAX_TOKENS, system=req.system,
                messages=[{"role": "user", "content": req.content}], output_config=output_config)


def _transient(e: Exception) -> str | None:
    """Почему запрос стоит повторить, или None: таймаут, сбой сети, ошибка сервера."""
    if isinstance(e, anthropic.APITimeoutError):
        return f"нет ответа {REQUEST_TIMEOUT_S // 60} мин"
    if isinstance(e, anthropic.APIConnectionError):
        return "сбой сети"
    if isinstance(e, anthropic.APIStatusError) and e.status_code >= 500:
        return f"ошибка сервера {e.status_code}"
    return None


def _with_retries(fn: Callable[[], Any], notify: Callable[[str], None]):
    """Пауза при 429 и повторы после таймаута, сбоя сети и 5xx сообщаются через notify (FR-7.2); ошибка
    баланса — InsufficientCredit, без повторов."""
    attempt, retries = 1, 0
    while True:
        try:
            return fn()
        except anthropic.RateLimitError:
            # лимит организации — входные токены в минуту: ждём и пробуем снова
            if attempt == RATE_LIMIT_ATTEMPTS:
                raise
            attempt += 1
            notify(f"  лимит API, пауза {RATE_LIMIT_SLEEP_S} с (попытка {attempt}/{RATE_LIMIT_ATTEMPTS})")
            time.sleep(RATE_LIMIT_SLEEP_S)
        except anthropic.APIError as e:
            if is_billing_error(e):
                raise InsufficientCredit() from e
            why = _transient(e)
            if why is None or retries == TRANSIENT_RETRIES:
                raise
            retries += 1
            notify(f"  {why} — повторяю запрос ({retries}/{TRANSIENT_RETRIES})")
            time.sleep(min(2 ** retries, 30))


def call_model(client, req: ModelRequest, notify: Callable[[str], None] = _quiet):
    """messages.create со схемой ответа в output_config.format: usage и stop_reason доступны и тогда, когда текст
    ответа не разбирается (refusal с текстом, обрезка по max_tokens)."""
    kwargs = request_kwargs(req)
    if req.model in FALLBACK_MODELS:
        return _with_retries(lambda: client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default"),
                             notify)
    return _with_retries(lambda: client.messages.create(**kwargs), notify)


@dataclass
class Job:
    make_request: Callable[[], ModelRequest]  # строится при отправке: превью кодируются по одной группе за раз
    tag: str  # ключ темы; у первой ступени — "screen"
    meta: dict  # JSON: чем записать ответ в кэш без текущего запуска (FR-9.3)
    handle: Callable[[Any], None]  # ответ API → разбор, кэш, расход


@dataclass
class JobFailure:
    tag: str
    error: Exception


class Transport(Protocol):
    batch: bool

    def resume(self, stage: Stage, handler_for: Callable[[dict], Callable[[Any], None]],
               log: Callable[[str], None] = _quiet) -> None: ...

    def run(self, stage: Stage, jobs: list[Job], log: Callable[[str], None] = _quiet) -> list[JobFailure]: ...


class SyncTransport:
    """До MAX_PARALLEL_REQUESTS запросов сразу. Ошибка задания выключает оставшиеся задания его тега, остальные
    идут; ошибка ключа, доступа или баланса останавливает отправку, начатые задания доделываются, ошибка
    поднимается. Каждое готовое задание и ожидание ответа дольше WAIT_NOTICE_S пишутся в log (FR-7.2).
    Если у запросов есть метка кэша, первый идёт один: запись кэша читается только после его ответа (§9)."""

    batch = False

    def __init__(self, client, notify: Callable[[str], None] = _quiet):
        self.client, self.notify = client, notify

    def resume(self, stage: Stage, handler_for, log: Callable[[str], None] = _quiet) -> None:
        pass  # незабранных пакетов у синхронной отправки не бывает

    def _send(self, job: Job) -> None:
        job.handle(call_model(self.client, job.make_request(), self.notify))

    def run(self, stage: Stage, jobs: list[Job], log: Callable[[str], None] = _quiet) -> list[JobFailure]:
        label, unit, units = _SYNC_LABELS[stage]
        gate = False  # первый запрос пишет кэш мета-блока — остальные ждут его ответа
        if jobs:
            first = jobs[0].make_request()
            gate = first.cache_ttl is not None
            jobs = [replace(jobs[0], make_request=lambda: first), *jobs[1:]]
        pending = iter(jobs)
        failures: list[JobFailure] = []
        failed_tags: set[str] = set()
        fatal: BaseException | None = None
        finished = 0
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as pool:
            running: dict[Future, Job] = {}

            def fill():
                while fatal is None and len(running) < (1 if gate else MAX_PARALLEL_REQUESTS):
                    job = next(pending, None)
                    if job is None:
                        return
                    if job.tag not in failed_tags:
                        running[pool.submit(self._send, job)] = job

            fill()
            start = last = time.monotonic()
            try:
                while running:
                    done, _ = wait(running, timeout=WAIT_NOTICE_S, return_when=FIRST_COMPLETED)
                    if not done:
                        log(f"  {label}: ждём ответ модели ({_mmss(time.monotonic() - last)}), "
                            f"готово {units} {finished}/{len(jobs)}")
                        continue
                    for f in done:
                        job = running.pop(f)
                        try:
                            f.result()
                        except Exception as e:  # noqa: BLE001 — разбирается ниже: тема или весь запуск
                            if is_fatal(e):
                                fatal = fatal or e
                            else:
                                failed_tags.add(job.tag)
                                failures.append(JobFailure(job.tag, e))
                        else:
                            finished += 1
                            log(f"  {label}: {unit} {finished}/{len(jobs)} ({_mmss(time.monotonic() - start)})")
                    last = time.monotonic()
                    gate = False
                    fill()
            except KeyboardInterrupt:
                log("  Ctrl+C: дожидаюсь ответов на уже отправленные запросы — они оплачены и попадут в кэш")
                raise
        if fatal is not None:
            raise fatal
        return failures


class BatchRequestFailed(Exception):
    def __init__(self, kind: str):
        super().__init__(f"запрос пакета не выполнен ({kind})")


def _error_of(result) -> tuple[str, str]:
    """Тип и текст ошибки результата errored: result.error — ErrorResponse с вложенной ошибкой."""
    error = getattr(result.error, "error", None)
    return getattr(error, "type", None) or "unknown", getattr(error, "message", "") or ""


class BatchTransport:
    """Один пакет Message Batches на вызов run (FR-12.2): пакет записывается в кэш сразу после отправки,
    затем ожидание с опросом раз в POLL_S (FR-12.3) и разбор результатов (FR-12.5)."""

    batch = True

    def __init__(self, client, cache: Cache, album_uuid: str, notify: Callable[[str], None] = _quiet):
        self.client, self.cache, self.album_uuid, self.notify = client, cache, album_uuid, notify

    def resume(self, stage: Stage, handler_for: Callable[[dict], Callable[[Any], None]],
               log: Callable[[str], None] = _quiet) -> None:
        """Сначала незабранные пакеты этого альбома и этапа (FR-12.4): ответы — в кэш, расход — в журнал, а
        недостающее функция этапа отправит новым пакетом. Сбои старого пакета не возвращаются."""
        label = BATCH_LABELS[stage]
        for pending in self.cache.pending_batches(self.album_uuid, stage):
            if time.time() - pending.created_at > BATCH_RESULTS_TTL_S:
                self.cache.mark_batch_collected(pending.batch_id)
                log(f"  пакет {label} {pending.batch_id} старше 29 дней — результаты в API удалены, пропускаю")
                continue
            log(f"  незабранный пакет {label} {pending.batch_id} от "
                f"{datetime.fromtimestamp(pending.created_at):%Y-%m-%d %H:%M} — забираю результат")
            self._collect(stage, pending.batch_id, {cid: handler_for(meta) for cid, meta in pending.metas.items()},
                          {}, log)

    def run(self, stage: Stage, jobs: list[Job], log: Callable[[str], None] = _quiet) -> list[JobFailure]:
        if not jobs:
            return []
        by_id = {}
        requests = []
        for job in jobs:
            req = job.make_request()
            by_id[req.custom_id] = job
            requests.append({"custom_id": req.custom_id, "params": request_kwargs(req)})
        try:
            batch = _with_retries(lambda: self.client.messages.batches.create(requests=requests), self.notify)
        except anthropic.APIError as e:
            if is_fatal(e):
                raise
            return [JobFailure(tag, e) for tag in dict.fromkeys(job.tag for job in jobs)]
        self.cache.add_batch(batch.id, self.album_uuid, stage, {cid: job.meta for cid, job in by_id.items()})
        log(f"  пакет {BATCH_LABELS[stage]} {batch.id}: отправлен, запросов {len(requests)}")
        return self._collect(stage, batch.id, {cid: job.handle for cid, job in by_id.items()},
                             {cid: job.tag for cid, job in by_id.items()}, log)

    def _wait(self, stage: Stage, batch_id: str, total: int, log: Callable[[str], None]) -> None:
        label = BATCH_LABELS[stage]
        start = time.monotonic()
        try:
            while True:
                batch = _with_retries(lambda: self.client.messages.batches.retrieve(batch_id), self.notify)
                if batch.processing_status == "ended":
                    log(f"  пакет {label}: готов ({_mmss(time.monotonic() - start)})")
                    return
                c = batch.request_counts
                done = c.succeeded + c.errored + c.canceled + c.expired
                log(f"  пакет {label}: готово {done} из {total} запросов ({_mmss(time.monotonic() - start)})")
                time.sleep(POLL_S)
        except KeyboardInterrupt:
            log(f"  пакет {label} {batch_id} досчитается на сервере; повторный запуск заберёт результат без новой "
                "оплаты")
            raise

    def _collect(self, stage: Stage, batch_id: str, handlers: dict[str, Callable[[Any], None]], tags: dict[str, str],
                 log: Callable[[str], None]) -> list[JobFailure]:
        """Ждёт пакет, отдаёт успешные ответы обработчикам; сбои — по тегам (у незабранного пакета тегов нет).
        Ошибка баланса поднимается после разбора: успешные ответы уже в кэше."""
        self._wait(stage, batch_id, len(handlers), log)
        results = _with_retries(lambda: list(self.client.messages.batches.results(batch_id)), self.notify)
        failures: list[JobFailure] = []
        billing = False
        for r in results:
            kind = r.result.type
            if kind == "succeeded":
                handlers[r.custom_id](r.result.message)
                continue
            error_type, message = _error_of(r.result) if kind == "errored" else ("", "")
            if error_type == "billing_error" or "credit balance" in message.lower():
                billing = True
            elif r.custom_id in tags:
                failures.append(JobFailure(tags[r.custom_id],
                                           BatchRequestFailed(f"errored: {error_type}" if kind == "errored" else kind)))
        self.cache.mark_batch_collected(batch_id)
        if billing:
            raise InsufficientCredit()
        return failures
