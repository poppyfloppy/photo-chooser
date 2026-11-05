# Версия 0.5.1: без пакетного режима, настройки ступеней, кадрирование в сборке — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** дешевле без роста времени.
- Пакетный режим удаляется (D16).
- Модель, `effort` и размер превью задаются по ступеням; вторая ступень и сборка по умолчанию — 768 px и `effort` high (D18).
- Советы по кадрированию пишет сборка, только для кадров постов (D10).
- Отчёт показывает токены и время ступеней (FR-6.3).

**Architecture:** изменения ложатся на устройство 0.5 — транспорт `judge/transport.py`, раннеры в `judge/stages.py`, конвейер по этапам.
- Транспорт становится только синхронным: уходят `BatchTransport`, `resume`, `meta` заданий и восстановление обработчиков.
- Секции `cloud.screen` и `cloud.judge` заменяют плоские ключи. `effort` и размер превью входят в версию промпта, то есть в ключ кэша своей ступени.

**Tech Stack:** Python 3.12, uv, pytest, anthropic SDK, pydantic v2, typer, SQLite.

**Spec:** [SPEC.md](../../../SPEC.md) версии 0.5.1 (PR #34): FR-3 п.2, п.5, п.8; FR-4.2, FR-4.4; FR-6.3; FR-7; FR-8; FR-9; FR-10.1; FR-11.2; FR-12 (удалён); §9; §10; D10, D15, D16, D18. Процесс — [DEVELOPMENT.md](../../../DEVELOPMENT.md).

## Global Constraints

- **Ветки и PR.** Ветка от свежего `main`. Пока предыдущий PR не слит, следующую ветку можно вести от неё, а после сквоша перенести: `git rebase --onto origin/main <старая база>`. Мердж сквошем делает пользователь.

  | PR | Ветка | Задачи |
  |---|---|---|
  | 1 | `refactor/remove-batch-mode` | 1 |
  | 2 | `feat/stage-settings` | 2–3 |
  | 3 | `feat/crop-in-compose` | 4 |
  | 4 | `feat/report-tokens-time` | 5 |

- **Тесты.** TDD: тест → FAIL → код → PASS. Перед коммитом `uv run pytest` зелёный и `uvx ruff check --select F,E9 src tests` чистый. Только `FakeClient`, без платных вызовов.
- **Значения по умолчанию** (FR-8, D18):

  | Секция | model | effort | preview_px |
  |---|---|---|---|
  | `cloud.screen` | `claude-haiku-5-5` | `medium` | 1024 |
  | `cloud.judge` | `claude-haiku-5-5` | `high` | 768 |

  `--model` разово меняет `cloud.judge.model`.
- **Версии промптов:** `screen-4` без изменений, `judge-8`, `compose-6`. В ключ кэша входит `<версия>/<effort>/<px>px`, у второй ступени — ещё `/style-<отпечаток>`.
- **Кэш мета-блока (D15):** TTL только 5 минут; первый запрос этапа идёт один.
- **Тексты** CLI, отчёта и ошибок — на русском, готовые строки ниже даны дословно. Промпты — на английском.

## Review Focus

1. **Частично заданная секция:** `cloud.judge: {model: claude-opus-5-5}` без `effort` и `preview_px`. Недостающие значения берутся по умолчанию **этой** ступени (high, 768), а не первой. Тест в Task 2.
2. **Старый конфиг** с `cloud.model` и `cloud.effort` (так сейчас на обоих Mac). Ошибка конфигурации с готовым фрагментом, где модели пользователя подставлены в новые секции, а не молчаливые значения по умолчанию. Тест в Task 2.
3. **Сборка вернула `crop` для вертикального кадра.** В отчёте «—», как раньше у второй ступени. Тест в Task 4.
4. **Смена только `effort` или только размера превью** пересчитывает свою ступень, а первая ступень остаётся в кэше. Тест в Task 3.
5. **Тема без облачных шагов** (без кандидатов, `--no-cloud`, всё из кэша): строки токенов и времени не падают и не печатают мусор — в `--no-cloud` их нет, при кэше нули. Тест в Task 5.

---

### Task 1: Удалить пакетный режим

**Files:**
- Modify:
  - `src/photo_chooser/judge/transport.py`, `src/photo_chooser/judge/stages.py`, `src/photo_chooser/judge/cost.py`;
  - `src/photo_chooser/cache.py`, `src/photo_chooser/config.py`, `src/photo_chooser/pipeline.py`, `src/photo_chooser/report.py`, `src/photo_chooser/cli.py`;
  - `tests/fakes.py`, `tests/test_transport.py`, `tests/test_stages.py`, `tests/test_compose.py`, `tests/test_pipeline.py`, `tests/test_cli.py`, `tests/test_cost.py`, `tests/test_export_report.py`, `tests/test_config.py`.
- Delete: `tests/test_batch.py`.

**Interfaces:**
- Produces (`transport.py`):
  - `ModelRequest(custom_id, model, effort, system, content, output_model, cached: bool = False)` — поле `cache_ttl` заменяется флагом `cached`;
  - `Job(make_request, tag, handle)` — без `meta`;
  - `Transport` — только `run(stage, jobs, log) -> list[JobFailure]`;
  - `SyncTransport(client, notify=_quiet)` — без `collector` и `resume`; первый запрос идёт один, если у него `cached`.
  - Удаляются `BatchTransport`, `BatchRequestFailed`, `_error_of`, `POLL_S`, `BATCH_RESULTS_TTL_S`, `BATCH_MAX_BYTES`, `BATCH_LABELS`.
- Produces (`stages.py`):
  - `JudgeRunner(model, effort, preview, style=None)` и `ComposeRunner(model, effort, preview, style=None)` — без `cache_ttl`;
  - метка — всегда `{"type": "ephemeral"}`;
  - `run_judge` и `run_compose` без `transport.resume` и `handler_for`: обработчик ответа — замыкание задания;
  - заголовки этапов без «, пакет».
- Produces (`cost.py`):
  - `CACHE_WRITE_MULTIPLIER = 1.25`;
  - `usage_usd(u: TokenUsage, price: Price) -> float`;
  - `estimate_run_usd(...)` без `batch`;
  - `BATCH_DISCOUNT` удаляется.
- Produces (прочее):
  - `cache.py` — без таблицы `batches` в схеме, без `PendingBatch`, `add_batch`, `pending_batches`, `mark_batch_collected`;
  - `pipeline.py` — `RunOptions` и `RunResult` без `batch`, один `SyncTransport`;
  - `report.py` — без `MODE_SYNC`, `MODE_BATCH` и `ReportHeader.mode`;
  - `cli.py` — без `--batch/--no-batch` и строки «Режим:».
- Produces (`config.py`): `CloudCfg` без `batch`. На ключ `cloud.batch` — `ConfigError` дословно: «cloud.batch удалён в версии 0.5.1: пакетного режима больше нет (SPEC D16) — уберите ключ из config.yaml».

- [ ] **Step 1: Тесты.**
  - `test_batch_flag_removed` (`test_cli.py`): `run --album … --theme … --batch` → код выхода 2, в выводе «No such option».
  - `test_cloud_batch_key_rejected` (`test_config.py`): `cloud: {batch: true}` → `ConfigError` с текстом выше.
  - `test_cached_request_goes_first_alone` (`test_transport.py`): перенос `test_cached_prefix_first_job_alone` на `ModelRequest(..., cached=True)`.
  - `test_usage_usd_with_cache_opus` — без аргумента TTL; `test_usage_usd_1h_write_and_batch` удалить.
  - Тесты пакетов, TTL 1 час, строк режима и `handler_for` из `meta` удалить. `tests/test_batch.py` удалить целиком; из `fakes.py` убрать `_Batches`, `EXPIRED`, `not_found_error`.
- [ ] **Step 2:** `uv run pytest -k "batch_flag_removed or cloud_batch_key or cached_request_goes_first" -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше.
- [ ] **Step 4:** `uv run pytest` → PASS. `grep -rni "batch" src` → пусто.
- [ ] **Step 5: Коммит и PR 1.** `git commit -m "refactor: удалить пакетный режим (D16)"`. PR `refactor/remove-batch-mode`.

### Task 2: Секции `cloud.screen` и `cloud.judge`

**Files:**
- Modify:
  - `src/photo_chooser/config.py`, `src/photo_chooser/pipeline.py`, `src/photo_chooser/export.py` (`_live_frame`);
  - `src/photo_chooser/judge/cost.py`, `src/photo_chooser/judge/stages.py` (пороги цены);
  - `src/photo_chooser/report.py`, `src/photo_chooser/cli.py` (команда `style`).
- Test: `tests/test_config.py`, `tests/test_pipeline.py`, `tests/test_cost.py`, `tests/test_export_report.py`, `tests/test_cli.py`

**Interfaces:**
- Produces (`config.py`):

  ```python
  Effort = Literal["low", "medium", "high", "xhigh", "max"]

  class ScreenCfg(_Strict):
      model: str = "claude-haiku-5-5"
      effort: Effort = "medium"
      preview_px: int = Field(default=1024, ge=64)

  class JudgeCfg(_Strict):
      model: str = "claude-haiku-5-5"
      effort: Effort = "high"
      preview_px: int = Field(default=768, ge=64)
  ```

  - `CloudCfg.screen: ScreenCfg`, `CloudCfg.judge: JudgeCfg`. Поля `screen_model`, `model`, `effort`, `preview_long_side_px` удаляются.
  - Валидатор `CloudCfg._renamed_keys`: если в данных есть любой из этих четырёх ключей — `ConfigError`. Модели пользователя подставляются, остальное — по умолчанию:

    ```
    cloud.screen_model, cloud.model, cloud.effort и cloud.preview_long_side_px заменены секциями cloud.screen и cloud.judge (версия 0.5.1). Замените их в config.yaml, например:
    cloud:
      screen: {model: <screen_model или claude-haiku-5-5>, effort: medium, preview_px: 1024}
      judge: {model: <model или claude-haiku-5-5>, effort: high, preview_px: 768}
    ```

- Produces (конвейер):
  - `pipeline.run`: `screen = cfg.cloud.screen`; `judge = cfg.cloud.judge.model_copy(update={"model": opts.model}) if opts.model else cfg.cloud.judge`.
  - Превью: из Photos — по `screen.preview_px` (`prepare_album`, локальные признаки, первая ступень). Вторая ступень и сборка получают `images.jpeg_bytes(path, judge.preview_px)`.
  - `export._live_frame` берёт `cfg.cloud.screen.preview_px`.
- Produces (`cost.py`): `estimate_run_usd(n_screen, n_judge, cfg: CloudCfg, screen: ScreenCfg, judge: JudgeCfg, judge_extra_tokens=0, n_compose=None)`. Картинки первой ступени — по `screen.preview_px`, второй ступени и сборки — по `judge.preview_px`.
- Produces (`stages.py`): пороги цены считают по `cfg.judge.preview_px` (вторая ступень и сборка) и по `cfg.screen.preview_px` (первая). Подсказка: «Уменьшите cloud.group_size или cloud.judge.preview_px».
- Produces (`report.py`):
  - `ReportHeader(screen_stage: str | None, judge_stage: str | None, prompt_versions, run_at, warnings, album, style)` — поля `screen_model`, `model`, `effort` уходят;
  - `stage_line(model: str, effort: str, preview_px: int) -> str` возвращает `"claude-opus-5-5, high, 768 px"`;
  - строка шапки дословно: `f"- Первая ступень: {screen_stage or '—'}; вторая ступень и сборка: {judge_stage or '—'}"`; строки `effort:` больше нет;
  - «без облака» — когда `judge_stage is None`.
- Produces (`cli.py`): команда `style` берёт `effort` из `cfg.cloud.judge.effort`.

- [ ] **Step 1: Тесты.**
  - `test_stage_defaults`: `CloudCfg().screen == ScreenCfg(model="claude-haiku-5-5", effort="medium", preview_px=1024)`; `CloudCfg().judge.effort == "high"` и `.preview_px == 768`.
  - `test_partial_judge_section_keeps_judge_defaults` (Review Focus 1): `cloud: {judge: {model: claude-opus-5-5}}` → `effort == "high"`, `preview_px == 768`.
  - `test_old_flat_keys_rejected_with_snippet` (Review Focus 2): `cloud: {model: claude-opus-5-5, effort: medium}` → `ConfigError`; в тексте есть «judge: {model: claude-opus-5-5, effort: high, preview_px: 768}» и «screen: {model: claude-haiku-5-5».
  - `test_judge_and_compose_get_judge_sized_previews` (`test_pipeline.py`): превью снимков 320×240, `judge.preview_px=160`, `screen.preview_px=320`. Картинки в вызовах второй ступени и сборки — длинная сторона 160; первой ступени — 320. Размер читается из base64 через `images`/Pillow в тесте.
  - `test_model_flag_overrides_judge_model`: `RunOptions(model="claude-sonnet-5-5")` → модель в вызовах второй ступени `claude-sonnet-5-5`, у первой — `claude-haiku-5-5`.
  - `test_estimate_uses_stage_preview_sizes` (`test_cost.py`): при `judge.preview_px=512` оценка второй ступени меньше, чем при 1024, а первой — та же.
  - `test_report_header_stage_lines` (`test_export_report.py`): строка «- Первая ступень: claude-haiku-5-5, medium, 1024 px; вторая ступень и сборка: claude-opus-5-5, high, 768 px»; строки «effort:» нет.
  - `test_style_command_uses_judge_effort` (`test_cli.py`): в вызове `style` `output_config.effort == "high"`.
- [ ] **Step 2:** `uv run pytest tests/test_config.py tests/test_pipeline.py tests/test_cost.py tests/test_export_report.py tests/test_cli.py -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше; остальные тесты перевести на новые поля.
- [ ] **Step 4:** `uv run pytest` → PASS; `grep -rn "preview_long_side_px\|screen_model\|cloud\.effort\|cloud\.model\b" src` → только текст ошибки в `config.py`.
- [ ] **Step 5: Коммит.** `git commit -m "feat: секции cloud.screen и cloud.judge — модель, effort, размер превью (D18)"`

### Task 3: `effort` и размер превью в ключах кэша

**Files:**
- Modify: `src/photo_chooser/judge/stages.py`, `src/photo_chooser/pipeline.py` (`_estimate`, шапка отчёта)
- Test: `tests/test_stages.py`, `tests/test_compose.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `ScreenCfg`, `JudgeCfg` (Task 2).
- Produces (`stages.py`):
  - `cache_version(base: str, effort: str, preview_px: int, style: "StyleContext | None" = None) -> str` → `f"{base}/{effort}/{preview_px}px"` плюс `f"/style-{style.fingerprint}"`, если стиль есть;
  - `ScreenRunner(model, effort, preview, preview_px=1024)`, свойство `.version`;
  - `JudgeRunner(model, effort, preview, style=None, preview_px=768)`, `.prompt_version` = `cache_version(JUDGE_PROMPT_VERSION, …, style)`;
  - `ComposeRunner(model, effort, preview, style=None, preview_px=768)`, свойство `.version`;
  - `run_screen`, `run_judge`, `run_compose` и `pipeline._estimate` берут версии только отсюда; `judge_prompt_version(style)` удаляется;
  - в строке «Версия промпта» отчёта — те же три версии.

- [ ] **Step 1: Тесты.**
  - `test_cache_version_format`: `cache_version("judge-8", "high", 768, _style())` == `"judge-8/high/768px/style-ab12cd34"` (`_style()` из `test_stages.py`, отпечаток `ab12cd34`); без стиля — `"judge-8/high/768px"`.
  - `test_effort_change_recomputes_only_its_stage` (Review Focus 4, `test_pipeline.py`):
    - прогон с `judge.effort="high"`, затем с `"medium"`;
    - во втором прогоне вызовов первой ступени 0, вызовов второй ступени и сборки больше 0.
  - `test_preview_change_recomputes_judge`: то же при смене `judge.preview_px`.
  - `test_screen_effort_change_recomputes_screen`: смена `screen.effort` → первая ступень вызвана снова.
- [ ] **Step 2:** `uv run pytest -k "cache_version or recomputes" -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит и PR 2.** `git commit -m "feat: effort и размер превью в ключах кэша ступеней (FR-9.3, D18)"`. PR `feat/stage-settings`. В описании:
  - пользователь правит `config.yaml` на обоих Mac по тексту ошибки;
  - первая ступень один раз пересчитается, около $0.14.

### Task 4: Советы по кадрированию — в сборке (judge-8, compose-6)

**Files:**
- Modify:
  - `src/photo_chooser/judge/prompts.py`, `src/photo_chooser/judge/stages.py`;
  - `src/photo_chooser/models.py` (`Judgment`), `src/photo_chooser/select.py`, `src/photo_chooser/export.py`, `src/photo_chooser/report.py`.
- Test: `tests/test_stages.py`, `tests/test_compose.py`, `tests/test_select.py`, `tests/test_export_report.py`, `tests/test_pipeline.py`

**Interfaces:**
- Produces:
  - `JUDGE_PROMPT_VERSION = "judge-8"`; из `JUDGE_SYSTEM_PROMPT` удаляется строка про `crop_feed, crop_stories`. `JudgeItem` без `crop_feed`, `crop_stories`; `Judgment` и `ShotFacts` — тоже без них.
  - `COMPOSE_PROMPT_VERSION = "compose-6"`. В `COMPOSE_SYSTEM_PROMPT` перед строкой «Each id may appear at most once…» — дословно:

    ```
    - crop: for each carousel photo and each story that is horizontal or square, one short phrase in the language of the theme on how to crop it to a vertical frame — 4:5 for a carousel photo, 9:16 for a story — and what to keep in the frame; an empty string for a vertical photo. Alternatives get no crop advice.
    ```

  - `ComposeSlide(id, alternatives, crop: str)`, `ComposeStory(id, kind, hint, crop: str)` — поля обязательные.
  - `select.Slide(asset, alternatives=[], crop: str = "")` и `select.StoryPick(asset, kind, hint, crop: str = "")`. `validate_composition` переносит `crop.strip()`, но для вертикального кадра (`orientation == "vertical"`) — `""`. `fallback_posts` и `local_posts` оставляют `""`.
  - `ExportedSlide.crop: str`, `ExportedStory.crop: str` копируются из `Slide` и `StoryPick`. Отчёт берёт колонки кадрирования оттуда; пустое — «—».

- [ ] **Step 1: Тесты.**
  - `test_judge_schema_without_crop`: `set(JudgeItem.model_fields) == {"id", "matches_theme", "score", "interesting", "reason"}`; `"crop_feed" not in JUDGE_SYSTEM_PROMPT`.
  - `test_compose_schema_has_crop`: `"crop" in ComposeSlide.model_fields and "crop" in ComposeStory.model_fields`; `"4:5 for a carousel photo, 9:16 for a story" in COMPOSE_SYSTEM_PROMPT`.
  - `test_validate_composition_keeps_crop_and_blanks_vertical` (Review Focus 3): у горизонтального слайда crop «оставить пирамиду справа» сохраняется; у вертикального — `""`; у story — так же.
  - `test_report_crop_from_compose`: в таблице карусели колонка «Кадрирование (4:5)» — совет из `ExportedSlide.crop`, в stories — из `ExportedStory.crop`; у пустого — «—».
  - `test_fallback_posts_have_no_crop`: раскладка по оценкам → все `crop == ""`.
  - `test_judge7_and_compose5_cache_not_reused`: оценки под `judge-7` и сборка под `compose-5` не берутся.
- [ ] **Step 2:** `uv run pytest -k "crop or judge7 or compose5 or schema" -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше; фикстуры `ShotFacts(...)` и `Judgment(...)` в тестах — без полей кадрирования.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит и PR 3.** `git commit -m "feat: советы по кадрированию пишет сборка, только для кадров постов (D10, judge-8, compose-6)"`. PR `feat/crop-in-compose`.

### Task 5: Токены и время ступеней в отчёте

**Files:**
- Modify: `src/photo_chooser/report.py` (`ThemeStats`, сводка), `src/photo_chooser/pipeline.py`
- Test: `tests/test_export_report.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `StageOutcome.usage`, `ComposeOutcome.usage` (`TokenUsage`, версия 0.5).
- Produces:
  - `ThemeStats` получает поля:
    - `tokens_screen: TokenUsage = TokenUsage()` — общие на запуск;
    - `tokens_judge: TokenUsage = TokenUsage()`, `tokens_compose: TokenUsage = TokenUsage()` — по теме;
    - `time_screen_s: float = 0.0`, `time_judge_s: float = 0.0`, `time_compose_s: float = 0.0` — за запуск.
  - Поля `cache_write_tokens` и `cache_read_tokens` уходят: строка кэша считается из `tokens_judge + tokens_compose`.
  - `pipeline.run` замеряет `time.monotonic()` вокруг `run_screen`, `run_judge` и `run_compose`.
  - Строки сводки, только в облачном режиме, дословно (числа — тысячи токенов `:.1f`, время — `м:сс`):

    ```
    - Токены, тыс.: первая ступень — вход {a}, выход {b} (общие на запуск); вторая ступень — вход {c}, выход {d}; сборка — вход {e}, выход {f}
    - Кэш мета-блока (вторая ступень и сборка): записано {w} тыс., прочитано {r} тыс. токенов
    - Время запуска: первая ступень {м:сс}, вторая ступень {м:сс}, сборка {м:сс} (общее на все темы)
    ```

- [ ] **Step 1: Тесты.**
  - `test_report_tokens_and_time_lines`: `ThemeStats` с заданными токенами и временем → три строки дословно, в этом порядке, сразу после строки «Стоимость».
  - `test_no_cloud_report_has_no_tokens_or_time` (Review Focus 5).
  - `test_cached_run_reports_zero_tokens` (Review Focus 5, `test_pipeline.py`): повторный прогон из кэша → «вход 0.0, выход 0.0», время — в формате `0:00`, без исключений.
  - `test_pipeline_fills_stage_tokens`: в отчёте темы токены второй ступени равны сумме `usage` её вызовов из `FakeClient`.
- [ ] **Step 2:** `uv run pytest tests/test_export_report.py tests/test_pipeline.py -k "tokens or time" -v` → FAIL.
- [ ] **Step 3: Код** по интерфейсам выше.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит и PR 4.** `git commit -m "feat: токены и время ступеней в отчёте (FR-6.3)"`. PR `feat/report-tokens-time`. В описании — проверка пользователем:
  - прогон шести тем на Opus в Terminal.app с исправленным `config.yaml`;
  - Claude сравнивает с `2026-11-03_0133`: стоимость, токены, время, доля совпавших кадров постов и список расхождений.

---

## После всех PR

- Пользователь правит `~/.config/photo-chooser/config.yaml` по тексту ошибки и делает прогон шести тем на Opus.
- Claude читает отчёты, считает совпадение кадров постов с `2026-11-03_0133` по именам файлов и показывает расхождения.
- Итог — в `docs/handoff.md` или `docs/acceptance.md` отдельным PR, по решению пользователя.
