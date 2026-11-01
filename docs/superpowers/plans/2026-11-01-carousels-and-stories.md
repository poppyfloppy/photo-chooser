# Версия 0.3: карусели и stories — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** вместо плоского шорт-листа «20 в ленту, 40 в stories» — посты на тему: 1–2 карусели ленты, до 9 кадров в каждой, с запасными ракурсами и до 9 stories. Посты собирает модель (FR-4, D11). Кадры оцениваются по потенциалу после обработки (D12). Модель видит 2 скриншота ленты и 2 скриншота stories (FR-11.4).

**Architecture:** вторая ступень остаётся и получает новую рубрику. Новый шаг сборки — один запрос на тему (`ComposeRunner`, кэш `compositions`) — возвращает карусели и stories. Чистая логика в `select.py` делает три вещи:
- выбирает кандидатов сборки;
- проверяет ответ модели;
- раскладывает кадры по оценкам при сбое или в `--no-cloud`.

`export.py` раскладывает посты по папкам, заменяя упавший слот альтернативой. `report.py` выводит таблицы каруселей и stories. Конвейер и конфиг переключаются на посты последней задачей кода. Старые `Pools`, `assemble` и `shortlist` удаляются там же.

**Tech Stack:** Python 3.12, uv, pytest, pyobjc (PhotoKit, ImageIO, Vision), anthropic SDK 2.3.0 (`messages.create` + `output_config.format`), pydantic v2, typer.

**Spec:** [SPEC.md](../../../SPEC.md):
- FR-3 п.8–10, FR-4, FR-5.5, FR-6.1–6.3, FR-7.1, FR-8, FR-9.1, FR-9.3, FR-10.2, FR-11.4, FR-11.6;
- §9, §10, NFR-3;
- D11, D12.

Процесс — [DEVELOPMENT.md](../../../DEVELOPMENT.md).

## Global Constraints

- **Ветки и PR.** Одна ветка `feat/carousels-and-stories` от `main`, один PR после Task 6. Мердж сквошем делает пользователь.
- **Тесты.** TDD на каждом шаге: тест → FAIL → код → PASS. Перед каждым коммитом `uv run pytest` зелёный. Только `FakeClient`, без платных вызовов.
- **Photos только на чтение** (NFR-1).
- **Лимиты:**
  - `posts.carousels` = 2, `posts.carousel_size` = 9, `posts.stories` = 9;
  - до 2 запасных на слот;
  - `cloud.compose_candidates` = 30;
  - `style.reference_feed` = 2, `style.reference_stories` = 2;
  - лимит альбома остаётся `max_cost_usd_per_album: 5.0`.
- **Версии промптов:** `judge-4`, новая `compose-1`. Первая ступень остаётся `screen-2`.
- **Тексты.** CLI, отчёт, названия сюжетов и подсказки — на русском (на языке темы). Промпты — на английском.

## Review Focus

1. **Модель вернула в сборке id не из списка, повторила кадр в двух местах или превысила лимиты.** Каждый кадр используется один раз, лишнее отбрасывается, запуск не падает. Тест в Task 3.
2. **Модель вернула пустой ответ сборки или карусель, все слайды которой отброшены.** Пустые карусели убираются. Без кандидатов сборка не вызывается, отчёт выходит с пустыми постами. Тесты в Task 3 и Task 6.
3. **Оригинал кадра карусели не скачался.** На его место встаёт первая альтернатива слота, отчёт помечает замену. Без альтернатив слот выпадает, позиции перенумеровываются. Тест в Task 5.
4. **Название сюжета с `/`, `:`, эмодзи или длиннее 60 знаков.** Получается валидное имя папки, две карусели с одинаковым названием не сливаются. Тест в Task 5.
5. **В `config.yaml` остались `shortlist.feed`, `shortlist.stories` или `style.reference_images`.** Ошибка конфигурации (код 2) с подсказкой, на что заменить. Тест в Task 6.

---

### Task 1: Рубрика второй ступени — потенциал после обработки (D12)

**Files:** Modify `src/photo_chooser/judge/prompts.py`. Test `tests/test_stages.py`.

**Interfaces:**
- Produces: `JUDGE_PROMPT_VERSION = "judge-4"`. В `JUDGE_SYSTEM_PROMPT` после строки `stories_score` добавляются две строки, слово в слово:

  ```
  - Judge feed_score and stories_score by the photo's potential after editing and cropping in the page's style: problems that editing fixes (exposure, colour and white balance, a tilted horizon, the crop) are not drawbacks; problems it cannot fix (motion blur, missed focus, blown highlights, a cut-off subject) are.
  - stories_score also rewards interesting moments that need a short explanatory text and therefore do not fit the feed; stories get lighter editing.
  ```

- [ ] **Step 1: Тест.** `test_judge3_cache_not_reused_after_potential_rubric`: оценка под `"judge-3"` в кэше → `run_judge` делает 1 вызов.
- [ ] **Step 2:** `uv run pytest tests/test_stages.py -k potential_rubric -v` → FAIL (0 вызовов).
- [ ] **Step 3:** Обновить `prompts.py`.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** `git switch -c feat/carousels-and-stories` от свежего `main`. Commit `feat: judge photos by their potential after editing (D12)`.

---

### Task 2: Скриншоты ленты и stories (FR-11.4)

**Files:** Modify `src/photo_chooser/config.py`, `src/photo_chooser/style.py`, `src/photo_chooser/judge/stages.py`, `src/photo_chooser/judge/prompts.py`. Test `tests/test_config.py`, `tests/test_style.py`, `tests/test_stages.py`.

**Interfaces:**
- Produces:
  - `StyleCfg.reference_feed: int = 2` (≥ 0), `StyleCfg.reference_stories: int = 2` (≥ 0).
  - Ключ `reference_images` в `style` → `ConfigError` с текстом «style.reference_images заменён на reference_feed и reference_stories». Проверка — `model_validator(mode="before")`.
  - `style.StyleContext.references: list[tuple[str, bytes]]` — пары `("feed" | "stories", JPEG)`. Первые `reference_feed` скриншотов альбома — `feed`, следующие `reference_stories` — `stories`. Если скриншотов не хватило, сначала укорачиваются stories, о нехватке — предупреждение.
  - Отпечаток — sha256 от текста и пар `kind:uuid`. `extra_tokens` считается по всем скриншотам.
  - `prompts.STYLE_REFERENCE_LABELS = {"feed": "Reference: screenshot of the author's Instagram page — feed grid (do not score it)", "stories": "Reference: screenshot of the author's Instagram page — stories (do not score it)"}` вместо `STYLE_REFERENCE_LABEL`. В запросе перед каждой картинкой-образцом — подпись её вида.
  - `stages.reference_content(style) -> list[dict]` — блоки подписей и картинок-образцов. Используют `JudgeRunner` и `ComposeRunner` (Task 4).

- [ ] **Step 1: Тесты.**

```python
def test_style_reference_split_defaults(tmp_path)          # load_app_config(missing).style: reference_feed == 2, reference_stories == 2
def test_style_reference_images_key_is_config_error(tmp_path)   # "style:\n  reference_images: 2\n" → ConfigError, "reference_feed" in str(e)
def test_load_style_splits_feed_and_stories(tmp_path)      # 5 скриншотов → kinds == ["feed", "feed", "stories", "stories"]
def test_load_style_short_album_drops_stories_first(tmp_path)   # 3 скриншота → ["feed", "feed", "stories"] + warning
def test_load_style_fingerprint_depends_on_kinds(tmp_path)      # reference_feed=1/stories=3 против 2/2 при тех же 4 файлах → разные отпечатки
def test_judge_reference_labels_per_kind()                 # content: label feed, img, label feed, img, label stories, img, … потом "id: p01 (horizontal)"
```

- [ ] **Step 2:** `uv run pytest tests/test_config.py tests/test_style.py tests/test_stages.py -k "reference or splits or short_album or kinds" -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces. Обновить существующие тесты, которые строят `StyleContext(references=[b"…"])`: теперь там пары.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: feed and stories page screenshots in stage-2 requests (FR-11.4)`.

---

### Task 3: Посты — модель данных, кандидаты, проверка ответа, раскладка по оценкам

**Files:** Modify `src/photo_chooser/config.py`, `src/photo_chooser/select.py`, `src/photo_chooser/judge/stages.py` (только схема). Test `tests/test_posts.py`.

**Interfaces:**
- Produces:
  - **Конфиг:** `config.PostsCfg(carousels: int = 2, carousel_size: int = 9, stories: int = 9)`, все поля ≥ 0, `carousel_size` ≥ 1. `AppConfig.posts: PostsCfg`. `CloudCfg.compose_candidates: int = 30` (≥ 1). `shortlist` пока остаётся, его удаляет Task 6.
  - **Схема ответа сборки** (`stages`):
    - `ComposeSlide(id: str, alternatives: list[str])`;
    - `ComposeCarousel(title: str, slides: list[ComposeSlide])`;
    - `ComposeStory(id: str, kind: Literal["best", "interesting"], hint: str)`;
    - `ComposeOutput(carousels: list[ComposeCarousel], stories: list[ComposeStory])`.
  - **Модель данных постов** (`select`):
    - `Slide(asset: Asset, alternatives: list[Asset])`;
    - `Carousel(title: str, slides: list[Slide])`;
    - `StoryPick(asset: Asset, kind: str, hint: str)`;
    - `Posts(carousels: list[Carousel], stories: list[StoryPick])`;
    - `ShotFacts(feed_score: float, stories_score: float, reason: str, crop_feed: str, crop_stories: str)` — то, что видит отчёт.
  - `select.compose_candidates(cands, judgments, quality, th, interval_only, limit) -> list[Asset]` (FR-4.1):
    - подходящие к теме (или любые, если тема только с интервалом), с `feed ≥ min_feed_score` или `stories ≥ min_stories_score`;
    - сортировка по ключу `(max(feed, stories), quality)` по убыванию, первые `limit`.
  - `select.validate_composition(out: ComposeOutput, ids: dict[str, Asset], limits: PostsCfg) -> Posts` (FR-4.3):
    - id нормализуется по первому слову (`"c03 (horizontal)"` → `c03`), неизвестные отбрасываются;
    - каждый кадр используется один раз: слайды, затем их запасные, затем stories, в порядке ответа;
    - лимиты: `carousels`, `carousel_size`, 2 запасных на слот, `stories`;
    - карусель без слайдов отбрасывается; пустое название → `"Карусель N"`; `hint` у `best` обнуляется.
  - `select.fallback_posts(cands, judgments, quality, th, limits) -> Posts` (FR-4.4): одна карусель с названием `"Лучшее по оценкам"` из лучших по `feed_score ≥ min_feed_score` (до `carousel_size`) и stories — следующие по `stories_score ≥ min_stories_score`, тип `best`. Пустая карусель не добавляется.
  - `select.local_posts(cands, quality, feats, limits) -> Posts` (FR-10.2): карусель `"Лучшее по локальной оценке"` — первые `carousel_size` по quality, stories — следующие `stories`.
  - `select.shot_facts(judgments) -> dict[str, ShotFacts]` и `select.local_facts(cands, quality, feats) -> dict[str, ShotFacts]`. В `local_facts` обе оценки равны quality, причина — `_local_reason`, советов по кадрированию нет.

- [ ] **Step 1: Тесты `tests/test_posts.py`.**

```python
def test_compose_candidates_thresholds_order_and_limit()   # matches_theme=False отброшен; feed 5/stories 3 отброшен; ключ (max, quality); limit
def test_compose_candidates_interval_only_ignores_matches()
def test_validate_drops_unknown_and_duplicates()           # "c99" отброшен; c01 в слайде и в stories → только слайд
def test_validate_normalizes_id_suffix()                   # "c02 (vertical)" → c02
def test_validate_enforces_limits()                        # 3 карусели → 2; 11 слайдов → 9; 3 запасных → 2; 12 stories → 9
def test_validate_drops_empty_carousel_and_names_untitled()  # все слайды неизвестны → карусель отброшена; title "" → "Карусель 1"
def test_validate_clears_hint_for_best()
def test_fallback_posts_by_scores()                        # лента — feed ≥ 6 по убыванию до 9; stories — следующие stories ≥ 4
def test_local_posts_by_quality()
```

- [ ] **Step 2:** `uv run pytest tests/test_posts.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: post model, composition candidates, validation and fallback layout (FR-4)`.

---

### Task 4: Шаг сборки — запрос, кэш, стоимость

**Files:** Modify `src/photo_chooser/judge/prompts.py`, `src/photo_chooser/judge/stages.py`, `src/photo_chooser/judge/cost.py`, `src/photo_chooser/cache.py`. Test `tests/test_compose.py`, `tests/test_cost.py`, `tests/test_cache.py`.

**Interfaces:**
- Consumes: Tasks 2–3.
- Produces:
  - `prompts.COMPOSE_PROMPT_VERSION = "compose-1"`. `prompts.COMPOSE_SYSTEM_PROMPT` — шаблон с `{carousels}`, `{carousel_size}`, `{stories}`, текст слово в слово:

    ```
    You are a photo editor assembling Instagram posts for one theme of a trip from the author's best candidate photos.
    Each candidate is preceded by its id, orientation, its feed and stories scores (1-10) and a short reason. Judge photos by their potential after editing and cropping in the page's style.
    Build:
    - carousels: up to {carousels} feed carousels of at most {carousel_size} photos each. Split by meaning: each carousel is one storyline of the theme with a short title in the language of the theme. The first photo is the cover — the strongest and most characteristic shot of the storyline; then order the photos so they tell the story. Make a second carousel only if there are enough strong, distinct photos for two. Include only photos the author would publish: fewer is better, quality first.
    - alternatives: for each carousel photo, up to 2 other candidates showing the same moment from another angle, so the author can swap them.
    - stories: up to {stories} photos not used above — the best of the rest (kind "best", empty hint), and interesting moments that need a short explanatory text and so do not fit the feed (kind "interesting", hint: what to explain, in the language of the theme).
    Use every id at most once across carousels, alternatives and stories.
    ```

  - `cache.get_composition(theme_key, model, prompt_version, input_fp) -> str | None` / `put_composition(..., json: str)`. Таблица `compositions(theme_key, model, prompt_version, input_fp, json, PK(all four))`. В ней хранится сырой `ComposeOutput.model_dump_json()`.
  - `stages.ComposeRunner(client, model, effort, preview, style: StyleContext | None = None)`:
    - `run(theme, cands: list[Asset], facts: dict[str, ShotFacts], limits: PostsCfg) -> ComposeResult(output: ComposeOutput | None, refused: bool, input_tokens, output_tokens)`;
    - `content`: `reference_content(style)`, затем по кандидату подпись `f"id: c{i:02d} ({orientation}) · лента {feed} · stories {stories} · «{reason}»"` и картинка, в конце `"Theme: «…»"`;
    - system — `COMPOSE_SYSTEM_PROMPT.format(...)` и `STYLE_BLOCK` при стиле;
    - разбор, как у `_parse_group`: refusal или неразборчивый ответ → `output=None`, usage учтён.
  - `stages.compose_input_fingerprint(cands, facts, limits, style) -> str` — 16 hex sha256 от uuid кандидатов с оценками, лимитов и отпечатка стиля.
  - `stages.run_compose(runner, cache, theme, cands, facts, cfg: AppConfig, album_uuid) -> ComposeOutcome(posts: Posts | None, usd: float, from_cache: bool, refused: bool)`:
    - кэш по `(theme.key(), runner.model, COMPOSE_PROMPT_VERSION, fingerprint)`; пустой `cands` → `Posts([], [])` без вызова;
    - расход записывается сразу; `posts = validate_composition(...)`;
    - ошибки API поднимаются — их обрабатывает конвейер (FR-4.4);
    - проверка ценового порога: `group_input_tokens(...)` + `COMPOSE_LABEL_TOKENS * n` + `style.extra_tokens`.
  - `cost.EST_OUTPUT_TOKENS_COMPOSE = 1500`, `cost.COMPOSE_LABEL_TOKENS = 60`. `estimate_run_usd(..., n_compose: list[int] | None = None)` добавляет запрос сборки на каждую тему с `n > 0`: `n * (per_image + COMPOSE_LABEL_TOKENS) + PROMPT_OVERHEAD_TOKENS + judge_extra_tokens` входа и `EST_OUTPUT_TOKENS_COMPOSE` выхода по цене `judge_model`.

- [ ] **Step 1: Тесты `tests/test_compose.py`** и дополнения в `tests/test_cost.py`, `tests/test_cache.py`.

```python
def test_compose_request_shape()        # system = COMPOSE_SYSTEM_PROMPT с лимитами 2/9/9 (+ STYLE_BLOCK при стиле); подписи кандидатов
                                        # "id: c01 (horizontal) · лента 8 · stories 6 · «…»"; схема ComposeOutput; opus → fallbacks
def test_compose_maps_ids_to_assets()   # ответ c02 обложка, c01 запасной → Posts с правильными Asset
def test_compose_cache_reuse_and_invalidation(tmp_path)   # тот же вход → 0 вызовов; другая оценка или другой стиль → вызов
def test_compose_records_spend_once(tmp_path)
def test_compose_refusal_returns_none(tmp_path)           # posts is None, refused, usage учтён
def test_compose_without_candidates_no_call(tmp_path)
def test_estimate_includes_compose()                      # n_compose=[30] на opus: (30×(1049+60)+500)×4/1e6 + 1500×20/1e6
def test_composition_roundtrip(tmp_path)                  # cache put/get по четырём ключам
```

- [ ] **Step 2:** `uv run pytest tests/test_compose.py tests/test_cost.py tests/test_cache.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: composition step — one request per theme assembles carousels and stories`.

---

### Task 5: Экспорт постов и отчёт

**Files:** Modify `src/photo_chooser/export.py`, `src/photo_chooser/report.py`. Test `tests/test_export_report.py`.

**Interfaces:**
- Consumes: Task 3 (`Posts`, `ShotFacts`).
- Produces:
  - `export.export_shot(library, asset, dest_dir: Path, prefix: str, tmp: Path, cfg) -> ExportedShot(asset, files: list[str], notes: list[str])`. Это прежняя логика `export_asset`: оригинал, Live Photo, кадр в TIFF. Файл называется `f"{prefix}_{name}"`. `DownloadError` оригинала поднимается.
  - `export.export_posts(library, posts: Posts, theme_dir, tmp, cfg) -> ExportedPosts`:
    - `ExportedPosts(carousels: list[ExportedCarousel], stories: list[ExportedStory], failed: list[tuple[Asset, str]])`;
    - `ExportedCarousel(title, folder: str, slides: list[ExportedSlide])`, `ExportedSlide(shot: ExportedShot, alternatives: list[ExportedShot], replaced: bool)`;
    - `ExportedStory(shot: ExportedShot, kind: str, hint: str)`;
    - папка карусели — `feed/` + `safe_name(f"carousel-{n} {title}")`, обрезанное до 60 знаков. Совпадение имени → суффикс `_2`;
    - слайды нумеруются по итоговой позиции: `01`, `02`…; запасные — `alternatives/` с префиксом `f"{pos:02d}{letter}"` (`a`, `b`);
    - если оригинал слайда не скачался, слайдом становится первая альтернатива, скачавшаяся успешно (`replaced=True`, в `notes` — «замена: оригинал не скачался»). Остальные альтернативы остаются запасными. Без альтернатив слот выпадает в `failed`;
    - stories — в `stories/`, `01_…`; упавшие уходят в `failed`, без замены;
    - папки `feed/` и `stories/` создаются всегда.
  - `report.ThemeStats`:
    - убираются поля недобора, добавляются `sent_to_compose: int` и `usd_compose: float`;
    - `ReportHeader.warnings` принимает предупреждение FR-4.4.
  - `report.write_report(path, theme, header, posts: ExportedPosts, facts: dict[str, ShotFacts], stats)`:
    - **По карусели** — `### Карусель N — <название>` и таблица `| Слайд | Файл | Лента / stories | Ориентация | Пометки | Причина | Кадрирование (4:5) | Запасные |`. Оценки через ` / `, запасные — имена файлов через запятую.
    - **Stories** — `### Stories` и таблица `| № | Файл | Лента / stories | Тип | Что пояснить | Причина | Кадрирование (9:16) |`. Тип — «лучшее» или «интересное».
    - **Сводка** — строки «Отправлено в сборку: N», «Посты: каруселей 2 (9 + 6), stories 7» и стоимость «… сборка $X (по теме)».
  - `ExportedItem`, `Assembled` и `assemble` не трогаются, их удаляет Task 6.

- [ ] **Step 1: Тесты.**

```python
def test_export_posts_layout(tmp_path)               # 2 карусели: папки "carousel-1 Пирамиды", "carousel-2 Закат"; 01_/02_; alternatives/01a_, 01b_; stories/01_
def test_export_posts_replaces_failed_slide_with_alternative(tmp_path)  # слайд 2 failing, альтернатива ок → 02_<alt>, replaced, пометка
def test_export_posts_drops_slot_without_alternatives(tmp_path)         # позиции перенумерованы 01, 02; failed содержит кадр
def test_export_posts_unsafe_and_duplicate_titles(tmp_path)  # "Гиза/Каир: 🐪 …" длиной 80 → валидная папка ≤ 60; два одинаковых названия → "_2"
def test_report_posts_tables(tmp_path)               # заголовок карусели, строка слайда с "8 / 6", "Запасные"; stories с «интересное» и подсказкой;
                                                     # «Посты: каруселей 2 (2 + 1), stories 2»; «сборка $0.19»
def test_report_fallback_warning(tmp_path)           # предупреждение «разложено по оценкам» в шапке
```

- [ ] **Step 2:** `uv run pytest tests/test_export_report.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces. Старые тесты `assemble` и плоского отчёта не меняются до Task 6.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: export posts to carousel folders and report carousels and stories`.

---

### Task 6: Конвейер, конфиг и CLI на посты

**Files:** Modify `src/photo_chooser/pipeline.py`, `src/photo_chooser/cli.py`, `src/photo_chooser/config.py`, `src/photo_chooser/select.py`, `src/photo_chooser/export.py`, `src/photo_chooser/report.py`, `README.md`, `tests/integration/test_album.py`. Test `tests/test_pipeline.py`, `tests/test_cli.py`, `tests/test_config.py`.

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces:
  - **Облачный режим.** `pipeline.run` по каждой теме: `screen_candidates` → `run_judge` → `compose_candidates` → `run_compose`. Если `posts is None` или поднялась ошибка `anthropic.APIError`, кроме ошибок авторизации, — `fallback_posts` и предупреждение «Сборка не удалась — разложено по оценкам (…)».
  - **`--no-cloud`.** `local_posts` и `local_facts`.
  - **Дальше по теме:** `export_posts` → `write_report`.
  - **Оценка стоимости.** `_estimate` считает `n_compose` так: при полном кэше второй ступени — `0`, если сборка в кэше, иначе `min(кандидатов, compose_candidates)`; при неизвестных кандидатах — `min(len(theme_assets), compose_candidates)`. Передаёт `n_compose` в `estimate_run_usd`.
  - **`ThemeOutcome`.** `posts: ExportedPosts | None` вместо `assembled`.
  - **CLI.** По теме выводится `«<тема>»: каруселей N (9 + 6), stories M — <report.md>`.
  - **Конфиг.** `AppConfig` без `shortlist`. Ключ `shortlist` → `ConfigError` «shortlist.feed/stories заменены на posts.carousels, posts.carousel_size, posts.stories (версия 0.3)».
  - **Удаляются:** `select.Scored`, `Pools`, `pools_from_judgments`, `pools_local`, `_verticals_first`; `export.ExportedItem`, `Assembled`, `assemble`, `export_asset`; плоская таблица отчёта. Вместе с ними — их тесты.
  - **Интеграционный тест** `test_no_cloud_run_on_test_album` обходит файлы рекурсивно: `rglob` по `feed` и `stories`, без `alternatives`.
  - **README** — раздел «Результат»: папки каруселей, запасные, stories, отчёт; строка о четырёх скриншотах в разделе «Стиль страницы».

- [ ] **Step 1: Тесты.**

```python
def test_no_cloud_end_to_end(tmp_path)               # переписать: папка "carousel-1 Лучшее по локальной оценке" с 9 кадрами, stories, нет SHOT/BLUR
def test_cloud_run_builds_posts(tmp_path)            # FakeClient: сборка возвращает 2 карусели и stories → папки, отчёт, ComposeOutput 1 вызов на тему
def test_compose_failure_falls_back(tmp_path)        # сборка api_error → «разложено по оценкам», другие темы целы
def test_compose_refusal_falls_back(tmp_path)
def test_no_candidates_skips_compose(tmp_path)       # первая ступень без совпадений → 0 вызовов сборки, пустые посты
def test_compose_cached_on_rerun(tmp_path)           # второй запуск → 0 вызовов второй ступени и сборки
def test_dry_run_estimate_includes_compose(tmp_path)
def test_shortlist_key_is_config_error(tmp_path)     # "shortlist:\n  feed: 10\n" → ConfigError, "posts.carousels" in str(e)
def test_cli_prints_posts_summary(...)               # «каруселей 1 (9), stories 9»
```

- [ ] **Step 2:** `uv run pytest tests/test_pipeline.py tests/test_cli.py tests/test_config.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces, удалить мёртвый код и его тесты.
- [ ] **Step 4:** `uv run pytest -q` → PASS. `uvx ruff check --select F,E9 src tests` → чисто.
- [ ] **Step 5:** Commit `feat: pipeline, config and CLI produce posts instead of flat shortlists`. Push, `gh pr create --base main`.

---

### Task 7: Проверка на Egypt.26 (пользователь, без кода)

- [ ] **Step 1:** Пользователь ставит в альбоме «Instagram style» первыми два скриншота ленты, за ними два скриншота stories.
- [ ] **Step 2:** `uv run photo-chooser run docs/acceptance/egypt26.yaml --dry-run` — оценка с учётом сборки, на Opus около $3 за три темы. По альбому уже потрачено $3.21, поэтому в лимит $5 прогон не поместится. Нужен `--max-cost 7` — по решению пользователя.
- [ ] **Step 3:** `time uv run photo-chooser run docs/acceptance/egypt26.yaml --max-cost 7`. Первая ступень берётся из кэша, вторая и сборка считаются заново.
- [ ] **Step 4:** Пользователь оценивает карусели и stories по NFR-3: не меньше 2/3 кадров готов опубликовать без замены. Результаты — в `docs/acceptance.md`, PR `docs/acceptance-0-3`.
