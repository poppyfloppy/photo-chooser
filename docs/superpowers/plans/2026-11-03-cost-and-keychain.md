# Версия 0.5: кэш мета-блока, пакетный режим, ключ в Keychain — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** облако дешевле, ключ безопаснее, ответы модели те же.
- Вторая ступень и сборка читают общий мета-блок из кэша API (D15).
- По выбору пользователя они уходят пакетами Message Batches со скидкой 50 % (FR-12, D16).
- Ключ Claude API берётся из Keychain (FR-13, D17).
- Ошибка баланса останавливает запуск (§9).

**Architecture:** запросы к модели отделяются от их отправки.
- Раннеры строят `ModelRequest` и разбирают ответ.
- Транспорт (`judge/transport.py`) отправляет задания: `SyncTransport` — как сейчас, параллельно и с повторами; `BatchTransport` — одним пакетом на этап.
- `pipeline.run` идёт по этапам: первая ступень, вторая ступень всех тем, сборка всех тем, выгрузка.
- Ключ читается утилитой `/usr/bin/security` из нового модуля `keychain.py`.

**Tech Stack:** Python 3.12, uv, pytest, anthropic SDK (`messages.create`, `messages.batches`, `cache_control`), pydantic v2, typer, SQLite, `/usr/bin/security`.

**Spec:** [SPEC.md](../../../SPEC.md) версии 0.5 (PR #25): FR-3 «Порядок этапов», FR-4.4, FR-6.3, FR-7, FR-7.1, FR-7.2, FR-8, FR-9.1–9.3, FR-12, FR-13, NFR-4, NFR-6, §7, §9 («Кэш запросов», «Пакеты», «Ошибки»), §10, D15–D17. Процесс — [DEVELOPMENT.md](../../../DEVELOPMENT.md).

## Global Constraints

- **Пять PR, по одному на группу задач:**

  | PR | Ветка | Задачи |
  |---|---|---|
  | 1 | `feat/keychain` | 1–2 |
  | 2 | `fix/billing-error-stops-run` | 3 |
  | 3 | `refactor/transport-and-phases` | 4–5 |
  | 4 | `feat/prompt-cache` | 6–7 |
  | 5 | `feat/batch-mode` | 8–10 |

  Мердж сквошем делает пользователь, своим словом на каждый PR. Ветка — от свежего `main`. Пока предыдущий PR не слит, ветку можно вести от предыдущей и после сквоша перенести: `git rebase --onto origin/main <старая база>`.
- **Тесты.** TDD на каждом шаге: тест → FAIL → код → PASS. Перед каждым коммитом `uv run pytest` зелёный.
  - Claude заменён `FakeClient`, платных вызовов нет.
  - Реальный Keychain тесты не трогают: autouse-фикстура из Task 1 роняет любой незаглушённый вызов `security`.
- **Ответы модели не меняются.** Версии промптов остаются `screen-4`, `judge-7`, `compose-5`, ключи локального кэша — прежние.
- **Константы:**

  | Значение | Где |
  |---|---|
  | запись Keychain `anthropic-api-key`, утилита `/usr/bin/security` | — |
  | код выхода баланса `EXIT_BILLING = 6` | — |
  | TTL кэша `"5m"` (синхронно) и `"1h"` (пакет); запись 1.25 × и 2 × цены входа | — |
  | скидка пакета 0.5; опрос пакета раз в 60 с (`POLL_S`); результаты хранятся 29 дней | — |
  | `cache_read`: Haiku 5.5 — 0.01, Sonnet 5.5 — 0.10, Opus 5.5 — 0.20; без значения — 0.1 × `input` | `DEFAULT_PRICING` |
  | `custom_id`: `s-NNN` у первой ступени, `j-<theme.key()>-NN` у групп второй, `c-<theme.key()>` у сборки | — |
  | `cloud.batch: false` по умолчанию | — |

- **Тексты** CLI, отчёта и ошибок — на русском. Готовые строки ниже даны дословно.
- **Ключ не выводится, не логируется и не передаётся в аргументах процессов** (NFR-6).

## Review Focus

1. **Keychain заблокирован или пользователь отклонил окно доступа** — `security` вернула не 0. `read_api_key()` возвращает `None`, запуск идёт по прежней цепочке (`ANTHROPIC_API_KEY`, `ant`), ничего не падает. Тест в Task 1.
2. **На первом Mac запись `anthropic-api-key` уже есть с другим аккаунтом.** `key set` обновляет её (`-U` с тем же `-a`), а не создаёт вторую. Тест в Task 1.
3. **Ошибка баланса в одной из параллельных групп.** Уже отправленные группы доделываются и попадают в кэш, запуск останавливается с кодом 6. Тест в Task 3.
4. **Ctrl+C во время пакета, повторный запуск с изменённым списком тем.** Незабранный пакет сначала забирается в кэш, новым пакетом идёт только недостающее, двойной оплаты нет. Тест в Task 9.
5. **В одной теме часть запросов пакета `succeeded`, часть `errored` или `expired`.** Успешные группы в кэше и оплачены, тема помечена «не обработана», повторный запуск отправит только упавшие группы. Тест в Task 8.

---

### Task 1: Модуль `keychain`

**Files:**
- Create: `src/photo_chooser/keychain.py`, `tests/test_keychain.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces (`photo_chooser.keychain`):
  - `SERVICE = "anthropic-api-key"`, `SECURITY = "/usr/bin/security"`, `NOT_FOUND = 44` (код `security`, когда записи нет);
  - `_run = subprocess.run` — по умолчанию у всех функций ниже; параметр `run` имеет ту же сигнатуру, что `subprocess.run`;
  - `read_api_key(run=None) -> str | None`: `[SECURITY, "find-generic-password", "-s", SERVICE, "-w"]`, `capture_output=True, text=True`. Код 0 — `stdout.strip()` или `None`, если пусто. Любой другой код — `None`;
  - `has_api_key(run=None) -> bool`: то же без `-w`, ключ в память не читается;
  - `key_account(run=None) -> str | None`: аккаунт существующей записи из вывода `find-generic-password -s SERVICE` (строка `"acct"<blob>="…"`) или `None`;
  - `store_api_key(run=None) -> int`: `[SECURITY, "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w"]`, где `account = key_account(run) or getpass.getuser()`. `-w` — последний аргумент, ключ спрашивает сама утилита. Без `capture_output`, ввод с терминала. Возвращает код выхода;
  - `delete_api_key(run=None) -> bool`: `[SECURITY, "delete-generic-password", "-s", SERVICE]`. Код 0 — `True`, `NOT_FOUND` — `False`, иначе `KeychainError(code)`;
  - `class KeychainError(Exception)` с сообщением `f"security завершилась с кодом {code}"`.
- `tests/conftest.py`: autouse-фикстура подменяет `keychain._run` функцией, которая бросает `AssertionError("юнит-тесты не вызывают /usr/bin/security")`.

- [ ] **Step 1: Тесты** в `tests/test_keychain.py`. Заглушка `FakeSecurity(responses)` записывает аргументы и kwargs и отдаёт `subprocess.CompletedProcess` с заданными `returncode` и `stdout`.
  - `test_read_returns_key_from_stdout`: код 0, stdout `"sk-test\n"` → `"sk-test"`; аргументы ровно `[SECURITY, "find-generic-password", "-s", SERVICE, "-w"]`.
  - `test_read_missing_or_denied_is_none` (Review Focus 1): коды 44, 51, 128 → `None`.
  - `test_has_key_does_not_read_secret`: в аргументах нет `"-w"`.
  - `test_store_reuses_existing_account` (Review Focus 2): stdout поиска содержит `"acct"<blob>="valerii"` → вызов записи с `"-a", "valerii"`; последний аргумент — `"-w"`; `capture_output` не передан.
  - `test_store_new_account_is_login_name`: поиск — код 44 → `"-a", getpass.getuser()`.
  - `test_store_never_passes_key_in_args`: ни один аргумент ни одного вызова не начинается с `"sk-"`.
  - `test_delete_found_missing_failed`: 0 → `True`, 44 → `False`, 1 → `KeychainError`.
  - `test_unit_tests_cannot_call_security`: `keychain.read_api_key()` без `run` → `AssertionError`.
- [ ] **Step 2:** `uv run pytest tests/test_keychain.py -v` → FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Код** — функции выше в `keychain.py` и фикстура в `conftest.py`.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит.** `git commit -m "feat: ключ Claude API в Keychain — модуль keychain (FR-13)"`

### Task 2: Клиент из Keychain и команды `key`

**Files:**
- Modify: `src/photo_chooser/judge/stages.py` (`make_client`, `check_credentials`), `src/photo_chooser/cli.py`
- Test: `tests/test_cli.py`, `tests/test_progress.py` (`test_make_client_has_request_timeout_and_no_sdk_retries`)

**Interfaces:**
- Consumes: `keychain.read_api_key`, `has_api_key`, `store_api_key`, `delete_api_key`, `KeychainError`, `SERVICE`.
- Produces:
  - `make_client()`: `anthropic.Anthropic(api_key=keychain.read_api_key(), max_retries=0, timeout=REQUEST_TIMEOUT_S)`. При `api_key=None` SDK сам берёт `ANTHROPIC_API_KEY`, затем профиль `ant`;
  - `has_credentials(client) -> bool` — нынешняя проверка `api_key or auth_token or credentials`, вынесенная из `check_credentials`;
  - текст `check_credentials`: «Нет ключа Claude API: выполните `photo-chooser key set` или задайте ANTHROPIC_API_KEY / `ant auth login`. Без облака можно запустить с --no-cloud.»;
  - текст `AuthenticationError` в `cli._exit_codes`: «Claude API не принял ключ: замените его командой `photo-chooser key set` (или ANTHROPIC_API_KEY / `ant auth login`). Без облака можно запустить с --no-cloud.»;
  - группа `key` в CLI (`key_app = typer.Typer(...)`, `app.add_typer(key_app, name="key")`):

    | Команда | Вызов | Вывод | Код |
    |---|---|---|---|
    | `key set` | `keychain.store_api_key()` | 0 → «Ключ сохранён в Keychain (запись anthropic-api-key)» | 0 |
    | | | иначе → «Ключ не сохранён: security завершилась с кодом N» | 1 |
    | `key delete [--yes]` | `typer.confirm("Удалить ключ Claude API из Keychain?", default=False)`, затем `delete_api_key()` | отказ → «Отменено: ключ не удалён» | 1 |
    | | | `True` → «Ключ удалён из Keychain» | 0 |
    | | | `False` → «В Keychain нет записи anthropic-api-key» | 0 |
    | `key status` | — | одна строка, см. ниже | 0 |

  - `key status` выводит первое подходящее:
    - «Ключ Claude API: Keychain (запись anthropic-api-key)» — если `has_api_key()`;
    - «Ключ Claude API: переменная ANTHROPIC_API_KEY»;
    - «Ключ Claude API: профиль ant auth login» — если `has_credentials(anthropic.Anthropic())`;
    - «Ключ Claude API не найден — выполните `photo-chooser key set`».

    Сам ключ не выводится.

- [ ] **Step 1: Тесты.** `keychain` подменяется через `monkeypatch.setattr(cli.keychain, …)` и `stages.keychain`.
  - `test_make_client_uses_keychain_key`: `read_api_key` → `"sk-test"` → `make_client().api_key == "sk-test"`.
  - `test_make_client_falls_back_to_env`: `read_api_key` → `None`, `ANTHROPIC_API_KEY=sk-env` → `api_key == "sk-env"`.
  - `test_make_client_has_request_timeout_and_no_sdk_retries`: подменить `read_api_key` на `lambda: None`, остальное как есть.
  - `test_missing_api_key_exit_2_with_hint`: в выводе есть «photo-chooser key set».
  - `test_key_set_success_and_failure`: заглушка возвращает 0 и 1 → строки и коды выхода из таблицы.
  - `test_key_delete_confirms`: без `--yes` и ввод `n` → «Отменено: ключ не удалён», код 1, `delete_api_key` не вызван; `--yes` → «Ключ удалён из Keychain».
  - `test_key_status_order` (параметризованный): keychain / env / ant / ничего → четыре строки. Ни в одном выводе нет `"sk-"`.
- [ ] **Step 2:** `uv run pytest tests/test_cli.py tests/test_progress.py -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит и PR 1.** `git commit -m "feat: ключ Claude API из Keychain и команды key (FR-13, D17)"`. PR `feat/keychain`. В описании — ручная проверка после мерджа:
  - на втором Mac: `uv run photo-chooser key set`, затем `key status`;
  - на первом: `key status` без `set`.

### Task 3: Ошибка баланса останавливает запуск

**Files:**
- Create: `src/photo_chooser/judge/errors.py`
- Modify: `src/photo_chooser/judge/stages.py` (`call_model`), `src/photo_chooser/cli.py` (`_exit_codes`, `EXIT_BILLING`), `README.md` — коды выхода уже описаны, сверить
- Test: `tests/fakes.py`, `tests/test_progress.py`, `tests/test_pipeline.py`, `tests/test_cli.py`

**Interfaces:**
- Produces (`photo_chooser.judge.errors`):
  - `class InsufficientCredit(Exception)`; `str(e)` — дословно «На балансе Claude API не хватает средств — пополните баланс в Claude Console. Оплаченное сохранено в кэше, повторный запуск продолжит»;
  - `is_billing_error(e: BaseException) -> bool` — `True`, если выполняется любое из:
    - `APIStatusError` со `status_code == 402`;
    - `getattr(e, "type", None) == "billing_error"`;
    - `BadRequestError`, в `str(e).lower()` есть `"credit balance"`;
  - `is_fatal(e: BaseException) -> bool` — `AuthenticationError`, `PermissionDeniedError` или `InsufficientCredit`. Нужен транспорту в Task 4.
- `call_model`: в ветке `except anthropic.APIError as e` первым делом `if is_billing_error(e): raise InsufficientCredit() from e`, без повторов.
- `cli`: `EXIT_BILLING = 6`; в `_exit_codes` — `except InsufficientCredit as e:` вывести `str(e)`, `typer.Exit(EXIT_BILLING)`.
- `pipeline`: `InsufficientCredit` не наследует `APIError`, поэтому ветки «тема не обработана» и «разложено по оценкам» его не ловят. Проверить тестом, код не менять.
- `tests/fakes.py`:
  - `credit_error()` — `BadRequestError` 400 с сообщением «Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.»;
  - `billing_error()` — `APIStatusError` 402 с телом `{"type": "error", "error": {"type": "billing_error", "message": "billing"}}`.

- [ ] **Step 1: Тесты.**
  - `test_billing_errors_raise_insufficient_credit_without_retry` (`test_progress.py`, параметризованный): `credit_error()` и `billing_error()` → `InsufficientCredit`, ровно 1 вызов, `sleeps == []`.
  - `test_other_bad_request_is_not_billing`: `api_error()` → `BadRequestError`, не `InsufficientCredit`.
  - `test_credit_error_in_judge_stops_run` (`test_pipeline.py`, Review Focus 3): две темы, вторая ступень первой темы → `credit_error()` → `run` бросает `InsufficientCredit`; сборка не вызывалась; оплаченные до ошибки группы есть в кэше.
  - `test_credit_error_in_compose_stops_run`: сборка → `credit_error()` → `InsufficientCredit`, а не раскладка по оценкам.
  - `test_credit_error_exit_6` (`test_cli.py`): код 6, текст сообщения дословно.
- [ ] **Step 2:** `uv run pytest -k "credit or billing" -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит и PR 2.** `git commit -m "fix: ошибка баланса Claude API останавливает запуск (код 6)"`. PR `fix/billing-error-stops-run`. В описании — ссылка на прогон `2026-11-03_0114` из `docs/handoff.md`.

### Task 4: Транспорт и раннеры, которые строят запросы

Перестройка без смены ответов. `_run_groups`, `_run_stage` и `_wait_with_notices` заменяет `SyncTransport`. Раннеры больше не держат клиента.

**Files:**
- Create: `src/photo_chooser/judge/transport.py`, `tests/test_transport.py`
- Modify:
  - `src/photo_chooser/judge/stages.py` — раннеры, стадии, вынос `call_model`, `make_client`, `check_credentials` и констант отправки в `transport.py`;
  - `src/photo_chooser/style.py` — вызов `call_model`;
  - `src/photo_chooser/cli.py` — импорты;
  - `tests/fakes.py`, `tests/test_stages.py`, `tests/test_progress.py`, `tests/test_compose.py`, `tests/test_style.py`.

**Interfaces:**
- Consumes: `errors.is_fatal`, `errors.is_billing_error`, `InsufficientCredit` (Task 3).
- Produces (`photo_chooser.judge.transport`):
  - Переезжают без изменений поведения:
    - константы `MAX_TOKENS`, `FALLBACK_MODELS`, `FALLBACK_BETA`, `MAX_PARALLEL_REQUESTS`, `RATE_LIMIT_SLEEP_S`, `RATE_LIMIT_ATTEMPTS`, `REQUEST_TIMEOUT_S`, `TRANSIENT_RETRIES`, `WAIT_NOTICE_S`;
    - функции `make_client()`, `check_credentials(client)`, `has_credentials(client)`.
  - `Stage = Literal["screen", "judge", "compose"]`.
  - Запрос:

    ```python
    @dataclass(frozen=True)
    class ModelRequest:
        custom_id: str                     # ^[a-zA-Z0-9_-]{1,64}$
        model: str
        effort: str
        system: str | list[dict]
        content: list[dict]
        output_model: type[BaseModel] | None
        cache_ttl: Literal["5m", "1h"] | None = None   # заполняет Task 7
    ```

  - `request_kwargs(req) -> dict` — `model`, `max_tokens`, `system`, `messages=[{"role": "user", "content": …}]`, `output_config` (effort и схема, если `output_model`). Без `betas` и `fallbacks`.
  - `call_model(client, req: ModelRequest, notify=_quiet)` — нынешнее поведение: `fallbacks="default"` на beta-эндпоинте для `FALLBACK_MODELS`, повторы и паузы с прежними строками, `InsufficientCredit`.
  - Задание и сбой:

    ```python
    @dataclass
    class Job:
        request: ModelRequest
        tag: str                        # ключ темы; у первой ступени — "screen"
        meta: dict                      # JSON: чем записать ответ в кэш без текущего запуска (FR-9.3)
        handle: Callable[[Any], None]   # ответ API → разбор, кэш, расход; зовётся в рабочем потоке

    @dataclass
    class JobFailure:
        tag: str
        error: Exception
    ```

  - Протокол `Transport`:
    - атрибут `batch: bool`;
    - `resume(stage, handler_for: Callable[[dict], Callable[[Any], None]], log) -> None`;
    - `run(stage, jobs: list[Job], log) -> list[JobFailure]`.
  - `SyncTransport(client, notify=_quiet)`:
    - `batch = False`; `resume` ничего не делает;
    - `run` — логика `_run_groups`: до `MAX_PARALLEL_REQUESTS` запросов сразу, ответ обрабатывается в рабочем потоке;
    - ошибка задания: если `is_fatal` — новые задания не отправляются, начатые доделываются, ошибка поднимается. Иначе `JobFailure(tag, e)`, оставшиеся задания того же тега не отправляются, остальные идут;
    - строки по `stage`:

      | `stage` | подпись | единица (им. п. / род. мн.) |
      |---|---|---|
      | `screen` | первая ступень | группа / групп |
      | `judge` | вторая ступень | группа / групп |
      | `compose` | сборка постов | тема / тем |

      Готово: `f"  {подпись}: {ед} {k}/{N} ({мм:сс})"`. Ожидание: `f"  {подпись}: ждём ответ модели ({мм:сс}), готово {ед_мн} {k}/{N}"`;
    - Ctrl+C во время ожидания — вывести «  Ctrl+C: дожидаюсь ответов на уже отправленные запросы — они оплачены и попадут в кэш», доделать начатые и поднять `KeyboardInterrupt`. Сборка теперь тоже дожидается начатых запросов, как вторая ступень: так оплаченный ответ попадает в кэш.
- Produces (`photo_chooser.judge.stages`):
  - `ScreenRunner(model, effort, preview)`:
    - `.request(album_title, themes, group, custom_id) -> ModelRequest`;
    - `.parse(response, themes, uuids: list[str]) -> GroupResult[Screening]`.
  - `JudgeRunner(model, effort, preview, style=None)`:
    - `.request(theme, group, custom_id) -> ModelRequest`;
    - `.parse(response, shots: list[tuple[str, str]]) -> GroupResult[Judgment]`, где `shots` — пары `(uuid, orientation)` по порядку `p01…`;
    - `.prompt_version`, `.extra_input_tokens` — как сейчас.
  - `ComposeRunner(model, effort, preview, style=None)`:
    - `.request(theme, cands, facts, limits, custom_id) -> ModelRequest`;
    - `.parse(response) -> ComposeResult`.
  - Задачи этапов:

    ```python
    @dataclass
    class JudgeTask:
        theme: Theme
        assets: list[Asset]
        order: dict[str, float]

    @dataclass
    class ComposeTask:
        theme: Theme
        cands: list[Asset]
        facts: dict[str, ShotFacts]
    ```

  - `StageOutcome` получает поле `error: Exception | None = None`.
  - Функции этапов:
    - `run_screen(runner, transport, cache, album_title, album_uuid, themes, assets, cfg, log=_quiet) -> StageOutcome[Screening]` — ошибка задания поднимается: без первой ступени запуск не продолжить;
    - `run_judge(runner, transport, cache, tasks: list[JudgeTask], cfg: CloudCfg, album_uuid, log=_quiet) -> dict[str, StageOutcome[Judgment]]` — по `theme.key()`;
    - `run_compose(runner, transport, cache, tasks: list[ComposeTask], cfg: AppConfig, album_uuid, log=_quiet) -> dict[str, ComposeOutcome]`. Ошибка API → `ComposeOutcome(posts=None, …, note=f"{type(e).__name__}: {e}")`.
  - Каждая функция этапа второй ступени и сборки (у первой ступени `resume` нет, `meta = {}`):
    1. вызывает `transport.resume(stage, handler_for, log)`;
    2. берёт из кэша готовое и строит `Job` только для недостающего;
    3. вызывает `transport.run`;
    4. собирает исходы из кэша и обработчиков.
  - `handler_for(meta)` строит обработчик ответа только из `meta`. В `meta` обоих этапов есть `"batch": transport.batch`:
    - вторая ступень: `{"theme_key", "model", "prompt_version", "batch", "shots": [[uuid, orientation], …]}` → разбор, `put_judgments`, `add_spend`;
    - сборка: `{"theme_key", "model", "prompt_version", "batch", "input_fp", "ids": {"c01": uuid, …}}` → `put_composition`, если в ответе есть известный id; `add_spend` — всегда.

    Живой обработчик — тот же `handler_for(meta)` плюс запись результата в исход текущего запуска.
  - Порог цены группы и сборки (`ValueError`) проверяется до `transport.run`, как сейчас.
- `tests/fakes.py`: `send(client, req, notify=None)` — `transport.call_model(client, req, notify or (lambda m: None))`. Тесты раннеров переходят на `runner.parse(send(client, runner.request(...)), ...)`, стадий — на новые сигнатуры. Утверждения тестов не меняются, кроме строк хода сборки.

- [ ] **Step 1: Тесты** в `tests/test_transport.py`. Сюда переносятся проверки `_run_groups` из `test_stages.py` и `test_progress.py`, плюс новые:
  - `test_failure_skips_rest_of_tag_only`: теги A (3 задания) и B (3 задания), `MAX_PARALLEL_REQUESTS=1`, второе задание A → `api_error()` → `[JobFailure("A", …)]`; третье задание A не отправлено, все три задания B обработаны.
  - `test_fatal_error_raises_after_in_flight` (Review Focus 3 для этого слоя): `credit_error()` в задании 1 из 3 параллельных → `InsufficientCredit`; обработчики завершившихся заданий вызваны.
  - `test_compose_lines_use_theme_unit`: `stage="compose"` → строки `«  сборка постов: тема 1/1 (0:00)»` и `«  сборка постов: ждём ответ модели (0:00), готово тем 0/1»`.
  - `test_ctrl_c_waits_in_flight_and_announces`: перенос `test_interrupt_keeps_completed_groups` и проверка строки Ctrl+C.
  - `test_handler_for_judge_meta_writes_cache_and_spend`: `handler_for(meta)` на готовом ответе пишет оценки под ключом из `meta` и расход.
  - `test_request_kwargs_has_no_fallbacks`; `test_call_model_uses_fallbacks_for_opus` — перенос `test_opus_and_sonnet_use_server_side_fallbacks`.
- [ ] **Step 2:** `uv run pytest tests/test_transport.py -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше; старые тесты перевести на новые сигнатуры.
- [ ] **Step 4:** `uv run pytest` → PASS. `grep -n "_run_groups\|_run_stage\|_wait_with_notices\|run_group(" -r src tests` → пусто.
- [ ] **Step 5: Коммит.** `git commit -m "refactor: транспорт запросов к модели и раннеры, которые строят запросы (D16)"`

### Task 5: Конвейер по этапам

**Files:**
- Modify: `src/photo_chooser/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `SyncTransport`, `run_screen`, `run_judge`, `run_compose`, `JudgeTask`, `ComposeTask` (Task 4).
- Produces: в `pipeline.run` — `transport = SyncTransport(client, notify=log)` и этапы по FR-3 «Порядок этапов»:
  1. первая ступень;
  2. кандидаты всех тем;
  3. `run_judge` по всем темам с кандидатами;
  4. окно сборки и `run_compose` по темам без ошибки;
  5. раскладка по оценкам при сбое сборки;
  6. выгрузка и `report.md` по каждой теме.

  Папка темы создаётся на этапе выгрузки. Предупреждения и `ThemeOutcome.error` — как сейчас.
- Строки хода, дословно (`мм:сс` — прошедшее время):

  ```
  Вторая ступень (<model>): тем <N>
    «<тема>»: кандидатов <n>, из кэша <m>, групп <g>
  Сборка постов (<model>): тем <N>
    «<тема>»: кандидатов <n>                 # или: «<тема>»: сборка из кэша
  Выгрузка
    «<тема>»: каруселей 1 (3), stories 1
    «<тема>»: тема не обработана: ошибка Claude API (<Тип>: <текст>)
    «<тема>»: сборка не удалась — разложено по оценкам (<причина>)
  ```

  Прежняя строка «Тема «…»» больше не выводится.

- [ ] **Step 1: Тесты.**
  - `test_run_logs_each_stage`: ожидаемый список заменить на новые строки, порядок проверяется.
  - `test_judge_of_all_themes_before_any_compose`: в `client.calls` все вызовы со схемой `JudgeOutput` раньше первого `ComposeOutput`.
  - `test_theme_failure_does_not_stop_other_themes`: прежние утверждения.
  - `test_run_logs_compose_fallback`: строка начинается с «  «Гиза»: сборка не удалась — разложено по оценкам (BadRequestError».
- [ ] **Step 2:** `uv run pytest tests/test_pipeline.py -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит и PR 3.** `git commit -m "refactor: конвейер по этапам — вторая ступень всех тем, затем сборка всех тем (FR-3, D16)"`. PR `refactor/transport-and-phases`. В описании:
  - поведение и ответы те же;
  - меняются порядок вывода и ожидание начатых запросов сборки при Ctrl+C.

### Task 6: Стоимость с кэшем и строка кэша в отчёте

**Files:**
- Modify:
  - `src/photo_chooser/config.py` (`Price`, `DEFAULT_PRICING`);
  - `src/photo_chooser/judge/cost.py`;
  - `src/photo_chooser/judge/stages.py` (`GroupResult`, `ComposeResult`, `StageOutcome`, `ComposeOutcome`, обработчики);
  - `src/photo_chooser/report.py` (`ThemeStats`, сводка);
  - `src/photo_chooser/pipeline.py`, `src/photo_chooser/cli.py` (команда `style`), `src/photo_chooser/style.py`.
- Test: `tests/test_cost.py`, `tests/test_config.py`, `tests/test_export_report.py`, `tests/test_stages.py`, `tests/fakes.py`

**Interfaces:**
- Produces:
  - `Price(input, output, cache_read: float | None = None)`, свойство `cache_read_price -> float` — `cache_read`, если задан, иначе `0.1 * input`. `DEFAULT_PRICING` — значения из Global Constraints.
  - В `cost.py`:

    ```python
    @dataclass(frozen=True)
    class TokenUsage:
        input: int = 0
        output: int = 0
        cache_write: int = 0
        cache_read: int = 0

        @classmethod
        def of(cls, usage) -> "TokenUsage": ...   # None и отсутствующие поля кэша → 0
        def __add__(self, other: "TokenUsage") -> "TokenUsage": ...
    ```

  - `CACHE_WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}`, `BATCH_DISCOUNT = 0.5`.
  - `usage_usd(u: TokenUsage, price: Price, *, cache_ttl: str = "5m", batch: bool = False) -> float` — по FR-9.2.
  - `GroupResult` и `ComposeResult` вместо `input_tokens`/`output_tokens` несут `usage: TokenUsage`. `StageOutcome` и `ComposeOutcome` получают `usage: TokenUsage` — сумму по запросам этого запуска.
  - Обработчики из `handler_for(meta)` считают расход как `usage_usd(u, price, cache_ttl=meta.get("cache_ttl") or "5m", batch=meta["batch"])`.
  - `ThemeStats`: `cache_write_tokens: int = 0`, `cache_read_tokens: int = 0` — вторая ступень плюс сборка темы.
  - В сводке `report.md` сразу после строки «Стоимость», только в облачном режиме, дословно: `f"- Кэш мета-блока (вторая ступень и сборка): записано {w / 1000:.1f} тыс., прочитано {r / 1000:.1f} тыс. токенов"`.
  - `fake_response(..., cache_write=0, cache_read=0)` кладёт в `usage` поля `cache_creation_input_tokens` и `cache_read_input_tokens`.

- [ ] **Step 1: Тесты.**
  - `test_usage_usd_with_cache_opus`: Opus, `TokenUsage(input=1000, output=100, cache_write=4000, cache_read=40000)` → `(1000*4 + 100*20 + 4000*4*1.25 + 40000*0.20) / 1e6`.
  - `test_usage_usd_1h_write_and_batch`: та же сумма с `cache_ttl="1h"` (запись × 2) и `batch=True` → итог × 0.5.
  - `test_usage_of_missing_cache_fields` (Review Focus): `TokenUsage.of(SimpleNamespace(input_tokens=5, output_tokens=1))` и поля, равные `None` → `cache_write == cache_read == 0`.
  - `test_cache_read_price_default`: `Price(input=3, output=15).cache_read_price == 0.3`; конфиг с `pricing: {claude-opus-5-5: {input: 4, output: 20}}` без `cache_read` → `0.4`.
  - `test_default_pricing_cache_read`: `0.01 / 0.10 / 0.20`.
  - `test_judge_spend_includes_cache_tokens`: ответ с `cache_read=4000` → расход в журнале равен `usage_usd` с кэшем.
  - `test_report_cache_line`: `ThemeStats(cache_write_tokens=4500, cache_read_tokens=36000)` → строка «- Кэш мета-блока (вторая ступень и сборка): записано 4.5 тыс., прочитано 36.0 тыс. токенов»; без облака строки нет.
- [ ] **Step 2:** `uv run pytest tests/test_cost.py tests/test_config.py tests/test_export_report.py -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше. Все вызовы `usage_usd` перевести на `TokenUsage`, включая `style.estimate_profile_usd` и `cli.style_command`.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит.** `git commit -m "feat: стоимость по usage с записью и чтением кэша, строка кэша в отчёте (FR-9.2, FR-6.3)"`

### Task 7: Метка кэша на мета-блоке и первый запрос этапа

**Files:**
- Modify: `src/photo_chooser/judge/stages.py` (`JudgeRunner`, `ComposeRunner`, `reference_content`), `src/photo_chooser/judge/transport.py` (`SyncTransport.run`), `src/photo_chooser/pipeline.py`
- Test: `tests/test_stages.py`, `tests/test_compose.py`, `tests/test_transport.py`

**Interfaces:**
- Consumes: `ModelRequest.cache_ttl` (Task 4), `usage_usd(..., cache_ttl=...)` (Task 6).
- Produces:
  - `JudgeRunner(..., cache_ttl: Literal["5m", "1h"] = "5m")`, `ComposeRunner(..., cache_ttl=…)`. `request()` ставит метку и заполняет `ModelRequest.cache_ttl`:
    - со скриншотами-образцами — `cache_control` на последнем блоке `image` из `reference_content`;
    - без них — `system` становится `[{"type": "text", "text": <прежний system>, "cache_control": …}]`;
    - значение метки — `{"type": "ephemeral"}` при `"5m"` и `{"type": "ephemeral", "ttl": "1h"}` при `"1h"`;
    - текст темы и превью кандидатов — после метки; других меток в запросе нет.
  - Первая ступень метку не ставит, её `cache_ttl` — `None`.
  - `SyncTransport.run`: если у первого задания `request.cache_ttl` не `None`, оно отправляется одно; остальные — после его ответа, дальше параллельно, как раньше.
  - В `meta` второй ступени и сборки добавляется `"cache_ttl"` запроса, поэтому расход пишется с ним.

- [ ] **Step 1: Тесты.**
  - `test_judge_request_marks_last_reference`: стиль с двумя скриншотами → `cache_control` ровно у одного блока — последнего `image` образцов; превью кандидатов без метки; `system` — строка.
  - `test_judge_request_without_references_marks_system`: без скриншотов → `system[0]["cache_control"] == {"type": "ephemeral"}`, текст равен прежнему `system`.
  - `test_judge_request_1h_ttl`: `cache_ttl="1h"` → `{"type": "ephemeral", "ttl": "1h"}`.
  - `test_compose_request_marks_prefix`: то же для `ComposeRunner`.
  - `test_screen_request_has_no_cache_control`: в запросе первой ступени нет ключа `cache_control` ни в одном блоке.
  - `test_cached_prefix_first_job_alone` (`test_transport.py`): 5 заданий с `cache_ttl="5m"`, `MAX_PARALLEL_REQUESTS=3`. Второй вызов начинается только после завершения первого: обработчик первого ставит `Event`, заглушка второго проверяет, что он установлен.
  - `test_cache_does_not_change_cache_keys`: `judge_prompt_version(None) == "judge-7"`, `COMPOSE_PROMPT_VERSION == "compose-5"`.
- [ ] **Step 2:** `uv run pytest -k "cache_control or marks or first_job_alone or cache_keys" -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше; в `pipeline.run` раннеры получают `cache_ttl="5m"`.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит и PR 4.** `git commit -m "feat: кэш мета-блока второй ступени и сборки (D15)"`. PR `feat/prompt-cache`. В описании — проверка пользователем: синхронный прогон шести тем, строка кэша и стоимость в `report.md` против $5.75 прогона `2026-11-03_0133`.

### Task 8: `BatchTransport` — отправка, ожидание, результаты

**Files:**
- Modify: `src/photo_chooser/judge/transport.py`, `src/photo_chooser/cache.py`, `tests/fakes.py`
- Create: `tests/test_batch.py`

**Interfaces:**
- Consumes: `request_kwargs`, `Job`, `JobFailure`, `is_fatal`, `is_billing_error`, `InsufficientCredit`.
- Produces:
  - Таблица в `cache.py`:

    ```sql
    CREATE TABLE IF NOT EXISTS batches (batch_id TEXT PRIMARY KEY, album_uuid TEXT NOT NULL, stage TEXT NOT NULL,
        created_at REAL NOT NULL, collected INTEGER NOT NULL DEFAULT 0, jobs_json TEXT NOT NULL);
    ```

  - Методы `Cache`:
    - `add_batch(batch_id, album_uuid, stage, metas: dict[str, dict]) -> None`;
    - `pending_batches(album_uuid, stage) -> list[PendingBatch]`;
    - `mark_batch_collected(batch_id) -> None`.

    `PendingBatch(batch_id: str, created_at: float, metas: dict[str, dict])` — по `custom_id`.
  - `POLL_S = 60`, `BATCH_LABELS = {"judge": "второй ступени", "compose": "сборки постов"}`.
  - `class BatchRequestFailed(Exception)` — `str` вида «запрос пакета не выполнен (errored: overloaded_error)» или «(expired)».
  - `BatchTransport(client, cache, album_uuid, notify=_quiet)`, `batch = True`, метод `run(stage, jobs, log) -> list[JobFailure]`:
    1. Пустой список → `[]`.
    2. `client.messages.batches.create(requests=[{"custom_id": j.request.custom_id, "params": request_kwargs(j.request)} …])` — без `fallbacks`. Повторы при сбое сети и 5xx — как в `call_model`; `is_billing_error` → `InsufficientCredit`; `is_fatal` → поднять.
    3. Сразу `cache.add_batch(batch.id, album_uuid, stage, {cid: job.meta})`, вывести `f"  пакет {BATCH_LABELS[stage]} {batch.id}: отправлен, запросов {n}"`.
    4. Ожидание: `retrieve` раз в `POLL_S` секунд, пока `processing_status != "ended"`. Строка `f"  пакет {BATCH_LABELS[stage]}: готово {done} из {n} запросов ({мм:сс})"`, где `done` — сумма `succeeded`, `errored`, `canceled`, `expired` из `request_counts`. В конце — `f"  пакет {BATCH_LABELS[stage]}: готов ({мм:сс})"`.
    5. Ctrl+C во время ожидания — вывести `f"  пакет {BATCH_LABELS[stage]} {batch.id} досчитается на сервере; повторный запуск заберёт результат без новой оплаты"`, поднять `KeyboardInterrupt`; пакет остаётся незабранным.
    6. `results(batch.id)`:
       - `succeeded` → `job.handle(r.result.message)`;
       - `errored` с ошибкой баланса (`r.result.error.error.type == "billing_error"` или текст «credit balance») → запомнить;
       - прочие `errored`, `expired`, `canceled` → `JobFailure(tag, BatchRequestFailed(...))`.
    7. `cache.mark_batch_collected(batch.id)`; если была ошибка баланса — поднять `InsufficientCredit`, иначе вернуть сбои.
  - `tests/fakes.py`: `FakeClient(handler, batch_polls=1)` получает `messages.batches` с `create`, `retrieve`, `results`.
    - `create` прогоняет каждый `params` через `handler(n, params)` и записывает вызов в `calls` с `"_batch": True`.
    - Ответ становится `succeeded`; исключение — `errored` с `error.error.type` из `e.type` или `"api_error"`; новый маркер `fakes.EXPIRED` — `expired`.
    - `retrieve` возвращает `processing_status="in_progress"` первые `batch_polls` раз, затем `"ended"`, с `request_counts`.
    - `created_batches: list[str]`.

- [ ] **Step 1: Тесты** в `tests/test_batch.py`. `time.sleep` транспорта подменён.
  - `test_batch_requests_shape`: `custom_id` из задания, в `params` нет `betas` и `fallbacks`, модель Opus тоже без них.
  - `test_batch_persisted_before_wait`: после `create` и до первого `retrieve` в `cache.pending_batches(album, "judge")` есть пакет с `metas`.
  - `test_batch_progress_lines`: `batch_polls=2` → строки «отправлен, запросов 3», дважды «готово 0 из 3 запросов (…)», «готов (…)».
  - `test_batch_mixed_results` (Review Focus 5): тег A — одно задание `succeeded` и одно `errored`, тег B — `expired` → обработчик вызван один раз; сбои `[A, B]` с `BatchRequestFailed`; пакет помечен забранным.
  - `test_batch_billing_error_raises_after_collecting`: одно задание `succeeded`, одно `errored` с `billing_error` → обработчик успешного вызван, затем `InsufficientCredit`.
  - `test_batch_create_credit_error`: `create` бросает `credit_error()` → `InsufficientCredit`, в `batches` ничего не записано.
  - `test_batch_ctrl_c_keeps_pending`: `log`, который на строке «готово» бросает `KeyboardInterrupt` → исключение поднято, строка «досчитается на сервере» выведена, пакет не забран.
- [ ] **Step 2:** `uv run pytest tests/test_batch.py -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит.** `git commit -m "feat: отправка заданий пакетом Message Batches (FR-12.2–12.5)"`

### Task 9: Продолжение с незабранного пакета

**Files:**
- Modify: `src/photo_chooser/judge/transport.py` (`BatchTransport.resume`)
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: `Cache.pending_batches`, `mark_batch_collected`; `handler_for(meta)` этапов (Task 4).
- Produces: `BatchTransport.resume(stage, handler_for, log)`. Для каждого незабранного пакета альбома и этапа, от старых к новым:
  - пакет старше 29 дней по `created_at` → `mark_batch_collected`, строка `f"  пакет {BATCH_LABELS[stage]} {id} старше 29 дней — результаты в API удалены, пропускаю"`, к API не обращаться;
  - иначе строка `f"  незабранный пакет {BATCH_LABELS[stage]} {id} от {дата:%Y-%m-%d %H:%M} — забираю результат"`, затем те же ожидание и разбор, что в шагах 4–7 `run`. Обработчик успешного результата — `handler_for(metas[custom_id])`; сбои не возвращаются (функция этапа отправит недостающее заново); ошибка баланса — `InsufficientCredit` после разбора.

- [ ] **Step 1: Тесты.**
  - `test_resume_collects_before_new_batch` (Review Focus 4): первый `run_judge` с `BatchTransport` прерван Ctrl+C на ожидании. Второй `run_judge` на том же `FakeClient` и кэше, но с другим списком тем (одна прежняя, одна новая):
    - прежний пакет забран, оценки в кэше, расход записан один раз;
    - новый пакет содержит только группы новой темы;
    - `len(client.created_batches) == 2`.
  - `test_resume_skips_batches_older_than_29_days`: `created_at` 30 дней назад → строка «старше 29 дней», `retrieve` и `results` не вызывались, пакет помечен.
  - `test_resume_handles_compose_meta`: незабранный пакет сборки → `put_composition` под ключом из `meta`; следующий `run_compose` берёт тему из кэша без нового пакета.
- [ ] **Step 2:** `uv run pytest tests/test_batch.py -k resume -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит.** `git commit -m "feat: повторный запуск забирает незабранный пакет (FR-12.4)"`

### Task 10: Пакетный режим в конфиге, CLI, оценке и отчёте

**Files:**
- Modify:
  - `src/photo_chooser/config.py` (`CloudCfg.batch`);
  - `src/photo_chooser/pipeline.py` (`RunOptions.batch`, `RunResult.batch`, выбор транспорта и TTL, `_estimate`);
  - `src/photo_chooser/judge/cost.py` (`estimate_run_usd`);
  - `src/photo_chooser/report.py` (`ReportHeader.mode`);
  - `src/photo_chooser/cli.py` (флаг, вывод `--dry-run`).
- Test: `tests/test_config.py`, `tests/test_cost.py`, `tests/test_pipeline.py`, `tests/test_cli.py`, `tests/test_export_report.py`

**Interfaces:**
- Consumes: `BatchTransport` (Tasks 8–9), `BATCH_DISCOUNT` (Task 6).
- Produces:
  - `CloudCfg.batch: bool = False`.
  - `RunOptions.batch: bool | None = None` — `None` значит «как в конфиге»; итог `batch = opts.batch if opts.batch is not None else cfg.cloud.batch`.
  - CLI: `batch: bool | None = typer.Option(None, "--batch/--no-batch", help="Вторая ступень и сборка пакетом: вдвое дешевле, ответ до часа и дольше")`.
  - Выбор транспорта в `pipeline.run`:
    - первая ступень — всегда `SyncTransport`;
    - вторая ступень и сборка — `BatchTransport(client, cache, album.uuid, notify=log)` при `batch`, иначе тот же `SyncTransport`;
    - `cache_ttl` раннеров — `"1h"` при `batch`, иначе `"5m"`;
    - расход пишется с `batch=True` (× 0.5);
    - заголовки этапов: `f"Вторая ступень ({model}, пакет): тем {N}"` и `f"Сборка постов ({model}, пакет): тем {N}"`.
  - `estimate_run_usd(..., batch: bool = False)`: вторая ступень и сборка × `BATCH_DISCOUNT`, первая ступень без скидки, кэш не учитывается (FR-9.1).
  - Режим в отчёте:
    - `report.MODE_SYNC = "синхронный"`, `report.MODE_BATCH = "пакетный (Message Batches, −50%)"`;
    - `ReportHeader.mode: str | None = None`; в шапке после строки `effort` — `f"- Режим: {header.mode}"`; без облака строки нет.
  - `RunResult.batch: bool`; `--dry-run` после числа снимков выводит `f"Режим: {MODE_BATCH if result.batch else MODE_SYNC}"`.

- [ ] **Step 1: Тесты.**
  - `test_batch_flag_overrides_config` (`test_cli.py`): `cloud.batch: true` + `--no-batch` → `RunOptions.batch is False`; без флагов → `None`, режим из конфига.
  - `test_estimate_batch_halves_judge_and_compose` (`test_cost.py`): разница с `batch=False` равна половине стоимости второй ступени и сборки; первая ступень без скидки.
  - `test_batch_run_end_to_end` (`test_pipeline.py`, `FakeClient(batch_polls=1)`, сон подменён):
    - вызовы первой ступени без `_batch`;
    - все вызовы второй ступени — одним пакетом раньше пакета сборки;
    - у раннеров TTL `"1h"` в метке;
    - расход по альбому = сумма `usage_usd(..., batch=True)` (половина синхронного);
    - в `report.md` строка «- Режим: пакетный (Message Batches, −50%)».
  - `test_sync_report_mode_line`: синхронный прогон → «- Режим: синхронный».
  - `test_dry_run_prints_mode` (`test_cli.py`): `--dry-run --batch` → строка «Режим: пакетный (Message Batches, −50%)», вызовов к API нет.
  - `test_batch_theme_failure_marks_theme`: запрос пакета второй ступени одной темы `expired` → тема «не обработана» (ошибка `BatchRequestFailed`), вторая тема собрана.
  - `test_batch_compose_expired_falls_back`: запрос пакета сборки `expired` → тема разложена по оценкам с предупреждением «Сборка не удалась — разложено по оценкам (BatchRequestFailed: …)» (FR-4.4, FR-12.5).
- [ ] **Step 2:** `uv run pytest -k "batch or mode" -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше. Сверить README (раздел «Пакетный режим», коды выхода) с итоговыми строками.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит и PR 5.** `git commit -m "feat: пакетный режим второй ступени и сборки (FR-12, D16)"`. PR `feat/batch-mode`. В описании — проверка пользователем: прогон шести тем с `--batch` в Terminal.app, стоимость и строка кэша в `report.md` против синхронного прогона.

---

## После всех PR

- Пользователь делает два прогона шести тем в Terminal.app: синхронный и `--batch`.
- По `report.md` сверяются строка «Режим», строка кэша и стоимость. Итоги записываются в `docs/handoff.md` или `docs/acceptance.md` отдельным PR, по решению пользователя.
- Дальше — версия 0.6: ревью и техдолг, см. `docs/handoff.md`, раздел «Дальше».
