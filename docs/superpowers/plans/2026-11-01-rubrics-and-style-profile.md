# Роли ленты и stories, профиль стиля Instagram — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** версия 0.2. Новые рубрики ленты и stories (D8). Профиль стиля страницы Instagram: строится командой `photo-chooser style` и вместе с 1–2 скриншотами страницы используется на второй ступени (FR-11, D9).

**Architecture:** рубрики — только тексты промптов и их версии. Профиль стиля — новый модуль `style.py`:
- выбор образцов из Photos;
- запрос к Claude, запись `style.md`;
- загрузка профиля и скриншотов для второй ступени.

`JudgeRunner` получает необязательный `StyleContext` и строит из него системный промпт, образцы в запросе и версию промпта для ключа кэша. Конвейер, отчёт и CLI только передают его дальше.

**Tech Stack:** Python 3.12, uv, pytest, pyobjc (PhotoKit, ImageIO), anthropic SDK 2.3.0, typer, pydantic v2.

**Spec:** [SPEC.md](../../../SPEC.md) — FR-3 п.5 и п.8, FR-6.3, FR-7, FR-8, FR-9.3, FR-11, §9 Claude API, D8, D9. Процесс — [DEVELOPMENT.md](../../../DEVELOPMENT.md).

## Global Constraints

- **Ветки и PR** — по [DEVELOPMENT.md](../../../DEVELOPMENT.md):
  - Task 1 — ветка `feat/feed-stories-rubrics` от `main`, свой PR;
  - Tasks 2–5 — ветка `feat/style-profile` от `main` после мерджа PR Task 1, один PR;
  - мердж сквошем делает пользователь.
- **TDD на каждом шаге:** тест → FAIL → код → PASS. `uv run pytest` зелёный перед каждым коммитом. Платных вызовов Claude в тестах нет, только `FakeClient`.
- **Photos только на чтение** (NFR-1). Новые методы адаптера — без изменяющих вызовов.
- **Стиль используется только на второй ступени** (FR-11.4). В `--no-cloud` он не используется (FR-11.5).
- **Константы FR-11:**
  - `style.sample` = 40;
  - не больше 12 скриншотов в профиле;
  - минимум 10 постов;
  - `style.reference_images` = 2;
  - длинная сторона скриншотов-образцов — 1568 px, постов при построении профиля — 1024 px;
  - `style.model` = `claude-opus-5-5`;
  - профиль — около 300 слов, на русском, разделы «Лента» и «Stories»;
  - путь — `~/.config/photo-chooser/style.md`, прежний профиль — `style.prev.md`.
- **Тексты** CLI, отчёта и профиля — на русском, промпты — на английском. Названия настроек macOS — английские.
- **Решение плана:** значения `style` по умолчанию — пустой `albums` и `screenshots_album: None`. Без настройки команда `style` останавливается с ошибкой конфигурации. Имена альбомов пользователь пишет в `config.yaml` сам (Task 6).
- **Решение плана:** профиль пишется с `cloud.effort` (по умолчанию `medium`). Отдельной настройки effort для профиля нет.

## Review Focus

1. **В `style.albums` указан заголовок, под которым в Photos несколько альбомов** (`Snapseed` ×2). Берутся все, один снимок не попадает в выборку дважды. Тест в Task 3.
2. **Альбом скриншотов пуст, отсутствует или в нём меньше `reference_images` снимков.** Вторая ступень идёт с тем, что есть, в шапке отчёта предупреждение, запуск не падает. Тест в Task 4.
3. **`style.md` пустой или из одних пробелов после ручной правки.** Считается отсутствующим (FR-11.5), а не профилем из пустой строки. Тест в Task 4.
4. **Посты раньше `since` или с датой 1970** (у снимка без `creationDate`) в выборку не попадают. Если постов меньше 10, команда `style` выходит с кодом 2 и ничего не отправляет. Тесты в Task 3 и Task 5.
5. **Пользователь отвечает «n» на подтверждение `style`, или ввод не с терминала.** Ничего не отправлено, `style.md` не тронут, код выхода 1. Тест в Task 5.

---

### Task 1: Новые рубрики ленты и stories (PR `feat/feed-stories-rubrics`)

**Files:**
- Modify: `src/photo_chooser/judge/prompts.py`
- Test: `tests/test_stages.py`

**Interfaces:**
- Produces: `SCREEN_PROMPT_VERSION = "screen-2"`, `JUDGE_PROMPT_VERSION = "judge-2"`. Новые тексты `SCREEN_SYSTEM_PROMPT` и `JUDGE_SYSTEM_PROMPT`.

- [ ] **Step 1: Тесты.** Оценка, сохранённая под прежней версией промпта, не переиспользуется: смена рубрик пересчитывает обе ступени.

```python
def test_judge1_cache_not_reused_after_rubric_change(tmp_path):
    # put_judgments([...], THEMES[0].key(), HAIKU, "judge-1") → run_judge делает 1 вызов
def test_screen1_cache_not_reused_after_rubric_change(tmp_path):
    # put_screenings([...], themes_key(...), HAIKU, "screen-1") → run_screen делает 1 вызов
```

- [ ] **Step 2:** `uv run pytest tests/test_stages.py -k rubric_change -v` → FAIL. Пока версии `judge-1` и `screen-1`, оценки берутся из кэша, и вызовов 0.
- [ ] **Step 3: Обновить `prompts.py`.**
  - Версии: `screen-2` и `judge-2`.
  - В `JUDGE_SYSTEM_PROMPT` строки рубрик заменяются на эти, слово в слово:

    ```
    - feed_score (1-10): strength as an Instagram feed post — the trip's best shots: composition, light, visual impact, technical quality. Photos without people are often the strongest feed posts.
    - stories_score (1-10): suitability for Instagram stories — a lighter, less selective feed of the trip: good on-theme shots that tell the trip, including ones not strong enough for the feed. People are not required. Minor technical flaws are acceptable.
    - reason: one short sentence in the language of the theme on what makes the photo work or not. Do not treat the absence of people as a drawback.
    ```

  - В `SCREEN_SYSTEM_PROMPT` строки `feed_score` и `stories_score` заменяются короткими вариантами тех же определений, без фразы о `reason`.
- [ ] **Step 4:** `uv run pytest -q` → PASS, весь набор.
- [ ] **Step 5: Commit и PR.**

```bash
git switch main && git pull --ff-only && git switch -c feat/feed-stories-rubrics
git add src/photo_chooser/judge/prompts.py tests/test_stages.py
git commit -m "feat: feed and stories rubrics without people as a criterion (D8)"
git push -u origin feat/feed-stories-rubrics && gh pr create --base main --fill
```

Дальше — после мерджа этого PR пользователем.

---

### Task 2: Настройки `style` и порядок альбома в адаптере Photos

**Files:**
- Modify: `src/photo_chooser/config.py`, `src/photo_chooser/library/base.py`, `src/photo_chooser/library/photokit_library.py`, `tests/fakes.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `config.StyleCfg`. Поля:
    - `albums: list[str] = []`;
    - `since: date = date(2018, 1, 1)`;
    - `screenshots_album: str | None = None`;
    - `sample: int = 40` (≥ 1);
    - `model: str = "claude-opus-5-5"`;
    - `reference_images: int = 2` (≥ 0).
  - `AppConfig.style: StyleCfg`.
  - `PhotosLibrary.assets(album, *, album_order: bool = False) -> list[Asset]`. При `album_order=True` — порядок альбома в Photos, без сортировки по дате. В `PhotoKitLibrary` это `PHFetchOptions` без `sortDescriptors`, в `FakeLibrary` — порядок списка как задан.
  - Модуль `style` (Task 3) находит альбомы через `library.albums()` и `library.assets()`. Новых методов поиска в адаптере нет.

- [ ] **Step 1: Тесты.**

```python
def test_style_defaults():            # load_app_config(missing) → style.sample == 40, reference_images == 2,
                                      # model == "claude-opus-5-5", since == date(2018, 1, 1), albums == []
def test_style_from_yaml(tmp_path):   # "style:\n  albums: [Snapseed, Instasize]\n  screenshots_album: Instagram style\n"
                                      # → albums == ["Snapseed", "Instasize"], sample == 40
def test_style_rejects_negative_reference_images(tmp_path)   # reference_images: -1 → ConfigError
```

- [ ] **Step 2:** `uv run pytest tests/test_config.py -k style -v` → FAIL (нет `AppConfig.style`).
- [ ] **Step 3:** Реализовать `StyleCfg` и `album_order` по Interfaces. Перенести в `FakeLibrary.assets` аргумент `album_order`: в фейке порядок и так остаётся как задан.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** — `feat: style settings and album order in the Photos adapter`. Ветка `feat/style-profile` от `main` после мерджа PR Task 1.

---

### Task 3: Выбор образцов и построение профиля

**Files:**
- Create: `src/photo_chooser/style.py`
- Modify: `src/photo_chooser/judge/stages.py` (`call_model` без схемы ответа)
- Test: `tests/test_style.py`

**Interfaces:**
- Consumes: `StyleCfg` (Task 2), `library.albums/assets/preview`, `images.jpeg_bytes`, `stages.call_model`, `stages.image_content`, `cost.image_tokens`.
- Produces:
  - Константы:
    - `DEFAULT_STYLE_PATH = config.DEFAULT_CONFIG_PATH.parent / "style.md"`;
    - `POST_LONG_SIDE = 1024`, `SCREENSHOT_LONG_SIDE = 1568`;
    - `MAX_SCREENSHOTS = 12`, `MIN_POSTS = 10`.
  - `select_posts(assets: list[Asset], since: date, n: int) -> list[Asset]`:
    - убирает повторы по `uuid`, оставляет `taken_at.date() >= since`, сортирует от новых к старым;
    - если снимков `N > n`, берёт индексы `floor(N * (i / n) ** 2)` для `i = 0..n-1`, повтор индекса сдвигается на следующий свободный. Так ровно половина выборки приходится на самую новую четверть;
    - иначе — все.
  - `gather_samples(library, cfg: StyleCfg) -> tuple[list[Asset], list[Asset]]` — `(posts, screenshots)`:
    - посты — из всех альбомов, чей заголовок есть в `cfg.albums`. Заголовок без альбома → `AlbumNotFound` с похожими названиями;
    - скриншоты — первые `MAX_SCREENSHOTS` из `cfg.screenshots_album` в порядке альбома, или `[]`, если альбом не задан;
    - постов меньше `MIN_POSTS` → `ConfigError`.
  - `estimate_profile_usd(posts, screenshots, cfg: AppConfig) -> float` — по `image_tokens` (посты при 1024, скриншоты при 1568), `PROMPT_OVERHEAD_TOKENS` и 1500 выходных токенов, цена — `price_for(cfg.cloud, cfg.style.model)`.
  - `build_profile(client, model: str, effort: str, posts, screenshots, preview: Callable[[Asset, int], bytes]) -> ProfileResult(text: str, input_tokens: int, output_tokens: int)`:
    - один запрос; перед каждой картинкой подпись `"post, 2025-08"` или `"page screenshot"`;
    - системный промпт `STYLE_SYSTEM_PROMPT`: описать стиль страницы по-русски, около 300 слов, разделы «Лента» и «Stories» — обработка, свет и цвет, композиция, типичные сюжеты, чего на странице не бывает; свежие посты весомее старых;
    - `stop_reason == "refusal"` или пустой текст → `RuntimeError` с текстом на русском.
  - `save_profile(path: Path, text: str) -> None` — прежний файл переименовывается в `style.prev.md`, запись в UTF-8.
  - `stages.call_model(..., output_model: type[BaseModel] | None)` — при `None` без `output_config.format`, только `{"effort": effort}`.

- [ ] **Step 1: Тесты.**

```python
def test_select_posts_half_from_newest_quarter()      # 400 снимков по дням, n=40 → 20 из 100 самых новых, первый — самый новый
def test_select_posts_drops_old_and_duplicates()      # since 2018-01-01: снимок 2017 и снимок 1970 выкинуты; повтор uuid — один раз
def test_select_posts_takes_all_when_few()            # 12 снимков, n=40 → 12, от новых к старым
def test_gather_samples_merges_albums_with_same_title()   # два альбома "Snapseed" + "Instasize" → посты из всех трёх, без повторов
def test_gather_samples_screenshots_keep_album_order()    # альбом скриншотов не по дате → порядок альбома, не больше 12
def test_gather_samples_unknown_album_lists_similar()     # albums: ["Snapsed"] → AlbumNotFound, "Snapseed" in similar
def test_gather_samples_too_few_posts()                   # 9 постов → ConfigError
def test_build_profile_request_shape()   # FakeClient: model, system == STYLE_SYSTEM_PROMPT, нет output_config["format"],
                                         # подписи "post, 2026-10" и "page screenshot" перед картинками, opus → _beta и fallbacks
def test_build_profile_refusal_raises()
def test_save_profile_rotates_previous(tmp_path)          # второй save → style.prev.md с первым текстом
def test_estimate_profile_usd()          # 40 постов 4032×3024 и 2 скриншота 1179×2556 на opus:
                                         # (40×1049 + 2×1512 + 500)×4/1e6 + 1500×20/1e6
```

- [ ] **Step 2:** `uv run pytest tests/test_style.py -v` → FAIL (нет модуля `style`).
- [ ] **Step 3:** Реализовать по Interfaces. `FakeClient` возвращает текст через `fake_response(text=...)`.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** — `feat: style sample selection and profile building`.

---

### Task 4: Профиль на второй ступени

**Files:**
- Modify: `src/photo_chooser/style.py`, `src/photo_chooser/judge/stages.py`, `src/photo_chooser/judge/cost.py`, `src/photo_chooser/judge/prompts.py`
- Test: `tests/test_style.py`, `tests/test_stages.py`, `tests/test_cost.py`

**Interfaces:**
- Consumes: Task 3.
- Produces:
  - `style.StyleContext`. Поля:
    - `text: str`;
    - `references: list[bytes]` — JPEG при 1568;
    - `fingerprint: str` — 8 hex от sha256 текста и uuid выбранных скриншотов через `|`;
    - `created_at: datetime` — mtime файла;
    - `extra_tokens: int` — `image_tokens` скриншотов + `len(text) // 2` + 100;
    - `warnings: list[str]`.
  - `style.load_style(path: Path, library, cfg: AppConfig) -> StyleContext | None`:
    - файла нет или в нём одни пробелы → `None`;
    - альбом скриншотов не найден или в нём меньше снимков, чем нужно → берёт что есть, предупреждение в `warnings`.
  - `prompts.STYLE_BLOCK` — шаблон с `{text}`. Говорит модели, что ниже стиль страницы автора, скриншоты страницы приложены и не оцениваются, а соответствие стилю ленты или stories считается наравне с композицией и светом.
  - `JudgeRunner(client, model, effort, preview, style: StyleContext | None = None)`. Свойства:
    - `prompt_version` — `"judge-2"` или `f"judge-2/style-{fingerprint}"`;
    - `extra_input_tokens` — `0` или `style.extra_tokens`;
    - при стиле `system = JUDGE_SYSTEM_PROMPT + "\n\n" + STYLE_BLOCK.format(text=...)`, а в начале `content` — текст `"Reference: screenshots of the author's Instagram page (do not score them)"` и картинки-образцы.
  - `run_judge` кэширует по `runner.prompt_version`. `_run_groups(..., extra_tokens: int = 0)` прибавляет эти токены к каждой группе при проверке ценового порога.
  - `estimate_run_usd(..., judge_extra_tokens: int = 0)` прибавляет их к каждой группе второй ступени.

- [ ] **Step 1: Тесты.**

```python
def test_load_style_missing_or_blank_is_none(tmp_path)        # нет файла; файл "  \n" → None
def test_load_style_fingerprint_changes_with_text_and_screens(tmp_path)
def test_load_style_short_screenshot_album_warns(tmp_path)    # reference_images=2, в альбоме 1 → 1 образец + warning
def test_load_style_missing_screenshot_album_warns(tmp_path)  # альбома нет → references == [], warning, без исключения
def test_judge_with_style_request()       # system содержит текст профиля; первые блоки — подпись Reference и 2 картинки,
                                          # потом "id: p01"; ids_in() по-прежнему ["p01", ...]
def test_judge_cache_keyed_by_style_fingerprint(tmp_path)     # тот же стиль → 0 вызовов; другой fingerprint → вызовы снова
def test_judge_without_style_unchanged()  # prompt_version == "judge-2", нет Reference в content
def test_estimate_adds_judge_extra_tokens()   # n_judge=[100], extra 4000 на группу → +5×4000×price.input
```

- [ ] **Step 2:** `uv run pytest tests/test_style.py tests/test_stages.py tests/test_cost.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** — `feat: style profile and page screenshots in stage-2 requests`.

---

### Task 5: Конвейер, отчёт и команда `style`

**Files:**
- Modify: `src/photo_chooser/pipeline.py`, `src/photo_chooser/report.py`, `src/photo_chooser/cli.py`, `README.md`
- Test: `tests/test_pipeline.py`, `tests/test_cli.py`, `tests/test_export_report.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:
  - `pipeline.run(..., style_path: Path | None = None)`:
    - `None` — стиль выключен, без предупреждения;
    - путь задан и не `no_cloud` — `load_style` до оценки стоимости. `judge_extra_tokens` передаётся в оценку, `StyleContext` — в `JudgeRunner`;
    - путь задан, а профиля нет — предупреждение FR-11.5 в шапке каждого отчёта, плюс `warnings` из `StyleContext`.
  - `ReportHeader.style: str | None` — строка `"style.md от 2026-11-01, отпечаток ab12cd34, скриншотов в запросе: 2"`. В шапке — строка «Стиль: …». Поле `prompt_versions` показывает `runner.prompt_version`.
  - `cli run` передаёт `style_path=style.DEFAULT_STYLE_PATH`.
  - `cli style [--yes]`:
    - `gather_samples` → печать «Отправить N изображений (постов P, скриншотов S) модели M, ≈ $X?» → `typer.confirm` (без `--yes`);
    - отказ или ввод не с терминала → «Отменено», код 1, ничего не отправлено;
    - иначе `check_credentials` → `build_profile` → `save_profile` → `cache.add_spend("style", model, usd)` → печать пути и фактической стоимости;
    - коды выхода как у `run`: `ConfigError` 2, `AlbumNotFound` 4, `PhotosAccessError` 5.
  - README: раздел «Стиль страницы» — что положить в `config.yaml`, как запустить `photo-chooser style`, что `style.md` можно править.

- [ ] **Step 1: Тесты.**

```python
def test_run_with_style_sends_profile_and_marks_header(tmp_path)   # style.md + альбом скриншотов в FakeLibrary →
                                                                   # system второй ступени содержит профиль, в отчёте «Стиль:»
def test_run_without_profile_warns(tmp_path)        # style_path указывает на отсутствующий файл → предупреждение FR-11.5
def test_run_no_cloud_ignores_style(tmp_path)       # --no-cloud → нет строки «Стиль:» и нет предупреждения
def test_style_command_yes_writes_profile(tmp_path) # cli style --yes → style.md записан, add_spend("style") > 0
def test_style_command_declined_sends_nothing(tmp_path)   # input="n\n" → exit 1, FakeClient.calls == [], style.md нет
def test_style_command_too_few_posts_exit_2(tmp_path)
def test_report_shows_style_line(tmp_path)
```

- [ ] **Step 2:** `uv run pytest tests/test_pipeline.py tests/test_cli.py tests/test_export_report.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit и PR** — `feat: photo-chooser style command and style in reports`. `git push -u origin feat/style-profile && gh pr create --base main --fill`.

---

### Task 6: Проверка на реальной медиатеке (пользователь, без кода)

- [ ] **Step 1: Настройки.** Пользователь добавляет в `~/.config/photo-chooser/config.yaml`:

```yaml
style:
  albums: [Snapseed, Instasize]
  screenshots_album: Instagram style
```

- [ ] **Step 2: Профиль.** Пользователь в Terminal запускает `uv run photo-chooser style` и подтверждает. Ожидаемо: около $0.25, файл `~/.config/photo-chooser/style.md` с разделами «Лента» и «Stories». Пользователь читает его и правит, если нужно.
- [ ] **Step 3: Прогон.** `uv run photo-chooser run docs/acceptance/egypt26.yaml` на Haiku. В шапке отчёта строка «Стиль: …». Пользователь сравнивает шорт-лист с прогоном 2026-11-01_1805.
- [ ] **Step 4: Приёмка.** Продолжение `docs/acceptance.md`: прогоны Sonnet и Opus, оценка пользователя, выбор модели и калибровка веса стиля (§12). Результаты — PR `docs/acceptance-0-2`.
