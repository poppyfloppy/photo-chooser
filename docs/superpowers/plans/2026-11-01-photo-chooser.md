# Photo Chooser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CLI для macOS, который по альбому Photos и темам собирает шорт-лист кадров для ленты и stories Instagram и копирует оригиналы в отдельную директорию.

**Architecture:**
- Python-пакет `photo_chooser`, конвейер:
  - чтение альбома через PhotoKit (только чтение);
  - локальные признаки (Apple Vision);
  - отсев мусора и дублей;
  - фильтр интервала;
  - **первая ступень** — дешёвая модель Claude отбирает подходящие к теме кадры с черновыми оценками;
  - **вторая ступень** — основная модель оценивает кандидатов;
  - раскладка на ленту и stories → экспорт оригиналов и отчёт.
- Внешние системы (Photos, Claude) спрятаны за интерфейсами, в тестах заменяются заглушками.
- Всё посчитанное и оплаченное кэшируется в SQLite.

**Tech Stack:**
- Python 3.12, `uv`, `pytest`;
- `pyobjc-framework-Photos`, `pyobjc-framework-Vision`, `pyobjc-framework-Quartz`, `pyobjc-framework-AVFoundation`;
- `anthropic` (официальный SDK);
- `pydantic` v2, `PyYAML`, `typer`, `numpy`.

**Spec:** [SPEC.md](../../../SPEC.md)

## Global Constraints

- macOS 26 (Tahoe), Apple Silicon. Python 3.12, зависимости и запуск через `uv`.
- Доступ к Photos **только через PhotoKit и только на чтение**. Никаких `performChanges` и других изменяющих вызовов. osxphotos не используется.
- Terminal нужен только доступ **Photos** (System Settings → Privacy & Security → Photos), Full Disk Access не нужен (NFR-5).
- Копируются только **оригиналы**: основной ресурс снимка (тип Photo) без правок Photos. RAW в MVP отдельно не поддерживается — вторая часть пары RAW+JPEG/HEIC не копируется.
- Каждый запуск пишет в новую директорию `<output_dir>/<альбом>/<YYYY-MM-DD_HHMM>/<тема>/{feed,stories}/`, ничего не перезаписывает. Выходная директория внутри `*.photoslibrary` запрещена.
- Шорт-лист по умолчанию: лента 20, stories 40.
- Модели по умолчанию: `screen_model = claude-haiku-5-5` (первая ступень), `model = claude-haiku-5-5` (вторая). Цены по умолчанию (за 1 млн токенов, вход / выход):
  - haiku 0.10 / 0.50;
  - sonnet-5-5 2.00 / 10.00;
  - opus-5-5 4.00 / 20.00.
- Первая ступень — **один проход на запуск** по всем снимкам после отсева, со всеми темами с `query` и названием альбома; релевантность 1–10, кандидаты темы — релевантность ≥ `min_theme_relevance` (5). Вторая ступень — по каждой теме (SPEC FR-3 п.5–8).
- Лимит скорости API: не больше 3 параллельных запросов; при `RateLimitError` после повторов SDK — ожидание и повтор (SPEC §9).
- Лимит накопленной стоимости обеих ступеней по альбому `max_cost_usd_per_album: 5.0`. Проверка — до первого обращения к Claude, по всем темам запуска. `--max-cost` заменяет лимит на один запуск.
- Часовой пояс — только `timezone` из trip.yaml или `--timezone` (IANA-имя). Обязателен, если у какой-либо темы есть интервал. Геотеги не используются.
- Ключ API в конфигах не хранится: берётся из `ANTHROPIC_API_KEY` или профиля `ant auth login`. В `--no-cloud` ключ не нужен.
- Кадр из Live Photo сохраняется в TIFF с суффиксом `_live-frame`; дата и GPS копируются из оригинала.
- Числовые пороги и константы стоимости берутся из `docs/spike-findings.md` (Task 1).
- Тексты CLI и `report.md` на русском. Причина выбора от Claude пишется на языке темы. Названия настроек macOS в сообщениях — английские.

## Review Focus

1. **Имя темы или альбома с `/`, `:`, эмодзи или пробелами по краям.** Ожидание: валидное имя директории, запуск не падает. Тест в Task 10.
2. **PhotoKit не отдал превью** (ошибка или таймаут загрузки из iCloud). Ожидание: снимок не анализируется, учитывается в отчёте как «нет превью», запуск продолжается. Тест в Task 11.
3. **Запуск прерван после того, как часть групп уже оплачена.** Ожидание: повторный запуск не платит за них снова, накопленная стоимость альбома равна реально потраченной. Тест в Task 7.
4. **Первая ступень не признала подходящим ни один кадр** (или интервал пуст). Ожидание: вторая ступень не вызывается, отчёт с пустыми списками и недобором. Тест в Task 11.
5. **Видеочасть Live Photo не скачалась.** Ожидание: копируется оригинал ключевого снимка, в отчёте пометка. Тест в Task 10.

---

## File Structure

```
pyproject.toml, .gitignore
src/photo_chooser/
  models.py            # Asset, LocalFeatures, Judgment, AlbumRef
  config.py            # AppConfig, TripConfig, Theme (pydantic) + загрузка YAML
  cache.py             # SQLite: признаки, оценки обеих ступеней, расходы
  library/
    base.py            # PhotosLibrary Protocol, resolve_album, исключения
    photokit_library.py
  images.py            # загрузка и масштабирование через ImageIO, JPEG-байты, TIFF-запись
  vision.py            # Vision: эстетика, utility, лица, feature print
  analysis/
    quality.py         # резкость, яркость, LocalFeatures, local_quality
    junk.py            # отсев мусора
    dedup.py           # кластеры дублей
    live.py            # выбор кадра Live Photo
  judge/
    cost.py            # оценка стоимости, бюджет
    prompts.py         # промпты и версии обеих ступеней
    stages.py          # группы, вызов API, кэширование, учёт расходов
  select.py            # интервал, кандидаты после первой ступени, пулы ленты и stories
  export.py            # имена, директории, сборка шорт-листа, экспорт
  report.py            # report.md
  pipeline.py          # оркестрация
  cli.py               # typer: albums, run
tests/ (unit) + tests/integration/ (реальная Photos, маркер integration)
spike/                 # одноразовые скрипты Task 1 (throwaway)
docs/spike-findings.md
```

---

### Task 1: Spike — проверка предпосылок на медиатеке пользователя (throwaway)

Код этого задания одноразовый. Результат — только `docs/spike-findings.md`. Скрипт, обращающийся к Photos, запускает пользователь в своём Terminal: macOS не даёт процессу Claude доступ к Photos. Остальные шаги выполняются по файлам из `spike/out/`.

**Files:**
- Create: `spike/*.py` (одноразовые), `docs/spike-findings.md`

**Interfaces:**
- Produces: `docs/spike-findings.md` со всеми полями из Step 7. Задачи 2–11 берут оттуда значения.

- [ ] **Step 1:** Git и `.gitignore` уже настроены (репозиторий `poppyfloppy/photo-chooser`, ветка `feature/mvp`). Выполнить `uv init --package --name photo-chooser --python 3.12` (если ещё не сделано). Установить зависимости: `uv add pyobjc-framework-Photos pyobjc-framework-Vision pyobjc-framework-Quartz pyobjc-framework-AVFoundation anthropic numpy`. osxphotos удалить, если он стоит.
- [ ] **Step 2: Скрипт для пользователя** `spike/photokit_dump.py`:
  - **без аргумента** — запрос доступа (`PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_`, уровень ReadWrite: PhotoKit требует его и для чтения) и список альбомов с числом снимков;
  - **с именем альбома** — для каждого снимка `localIdentifier`, `creationDate`, `pixelWidth/pixelHeight`, `mediaType`, `mediaSubtypes` (Live, скриншот), ресурсы (`PHAssetResource`: тип, `originalFilename`, `uniformTypeIdentifier`) → `spike/out/album.json`;
  - **превью** с длинной стороной 1024 через `PHImageManager` (`PHImageRequestOptions`: `synchronous=True`, `networkAccessAllowed=True`, `deliveryMode=HighQualityFormat`) — до 400 снимков блоками по 50 подряд по всей поездке → `spike/out/previews/`; замерить время на превью;
  - **оригиналы** снимков (обычный, Live Photo) через `PHAssetResourceManager.writeDataForAssetResource_toFile_options_completionHandler_` с `networkAccessAllowed=True` → `spike/out/export/`; время;
  - сводка → `spike/out/summary.json`.

  Пользователь запускает его в Terminal. **Критерий провала:** не работают доступ и чтение альбома или не скачиваются из iCloud оригиналы и видео. Тогда остановиться и обсудить с пользователем Swift-хелпер (SPEC §11).
- [ ] **Step 3: Vision через pyobjc на превью.**
  - `VNCalculateImageAestheticsScoresRequest`: `overallScore`, `isUtility`;
  - `VNDetectFaceCaptureQualityRequest`;
  - `VNGenerateImageFeaturePrintRequest`.

  Сравнить `computeDistance:` с L2-нормой векторов из `featurePrint.data()`. Посчитать дисперсию лапласиана (оттенки серого, 1024) и среднюю яркость. Вместе с пользователем отметить явный брак и серии и подобрать `junk_blur`, `junk_dark_luma`, `junk_bright_luma`, `duplicate_distance`, `duplicate_window_s`.
- [ ] **Step 4: Первая ступень на Haiku.**
  - Пользователь даёт 5 запросов-тем.
  - По ~200 превью прогнать черновой промпт первой ступени на `claude-haiku-5-5`: `client.messages.parse`, схема `ScreenOutput` из Task 7, `output_config={"effort": "medium"}`, группы по 20.
  - Сформировать `spike/out/screen.html` — контактный лист по каждому запросу, кадры, отмеченные моделью, выделены.
  - Пользователь отмечает пропуски и лишние. Посчитать полноту и точность, время на тему.
- [ ] **Step 5: Стоимость.**
  - По `usage` из Step 4: токенов на картинку против формулы `ceil(w*h/750)`, накладные расходы промпта на группу, выходных токенов на снимок.
  - Один прогон группы второй ступени на `claude-haiku-5-5` со схемой `JudgeOutput`: выходных токенов на снимок с `reason`.
- [ ] **Step 6: Live Photo.** `AVAssetImageGenerator` с нулевыми допусками извлекает 15 равномерных кадров из скачанной видеочасти. Разрешение кадра и время.
- [ ] **Step 7: Записать `docs/spike-findings.md`.** Поля:
  - `orientation_fields` (отражают ли `pixelWidth/Height` итоговую ориентацию);
  - `live_video_resource_type`, `original_filename_source`;
  - `preview_seconds_local`, `preview_seconds_icloud`;
  - `featureprint_distance`;
  - `junk_blur`, `junk_dark_luma`, `junk_bright_luma`, `duplicate_distance`, `duplicate_window_s`;
  - `image_token_divisor`, `prompt_overhead_tokens`, `est_output_tokens_per_photo_screen`, `est_output_tokens_per_photo_judge`;
  - `screen_recall`, `screen_precision`, `screen_seconds_per_theme`;
  - `live_frames_per_video`, `live_frame_resolution`;
  - значения по умолчанию `candidates_per_theme`, `preview_long_side_px`, `group_size`.
- [ ] **Step 8: Commit.**

```bash
git add pyproject.toml uv.lock .python-version src/ docs/spike-findings.md spike/
git commit -m "spike: verify PhotoKit, Vision, Haiku screening and token costs on macOS 26"
```

---

### Task 2: Модели данных и конфигурация

**Files:**
- Create: `src/photo_chooser/models.py`, `src/photo_chooser/config.py`
- Test: `tests/test_config.py`, `tests/conftest.py`

**Interfaces:**
- Produces:
  - `models.Asset` (frozen dataclass):
    - поля: `uuid: str` (`localIdentifier` PhotoKit), `filename: str`, `taken_at: datetime` (aware, UTC), `width: int`, `height: int`, `is_live: bool`, `is_screenshot: bool`;
    - свойство `is_vertical -> bool` (`height > width`; квадрат — не вертикальный).
  - `models.AlbumRef(uuid: str, title: str, count: int)`.
  - `models.LocalFeatures(uuid: str, aesthetic: float | None, is_utility: bool, sharpness: float, mean_luma: float, face_quality: float | None, feature_print: bytes)`. `feature_print` — float32-байты вектора.
  - `models.Judgment(uuid: str, matches_theme: bool, feed_score: int, stories_score: int, reason: str)` — результат второй ступени.
  - `models.Screening(uuid: str, relevance: dict[str, int], feed_score: int, stories_score: int)` — результат первой ступени; ключ `relevance` — `Theme.key()`.
  - `config.Theme` (pydantic):
    - поля: `name: str`, `query: str | None`, `start: datetime | None` (alias `from`), `end: datetime | None` (alias `to`);
    - метод `key() -> str` — первые 16 hex sha256 от `name|query|start|end`;
    - свойства `interval_only -> bool`, `has_interval -> bool`.
  - `config.TripConfig(album: str, timezone: str | None, themes: list[Theme])`. Валидация:
    - `timezone` — валидное IANA-имя (`zoneinfo.ZoneInfo`);
    - обязателен, если у какой-либо темы `has_interval`;
    - свойство `tz -> ZoneInfo | None`;
    - свойство `query_themes -> list[Theme]` — темы с `query` в порядке файла.
  - `config.themes_key(album_title: str, themes: list[Theme]) -> str` — первые 16 hex sha256 от названия альбома и упорядоченного списка `name|query` тем; ключ кэша первой ступени.
  - `config.Price(input: float, output: float)`; `config.ShortlistCfg(feed: int = 20, stories: int = 40)`.
  - `config.CloudCfg`: `screen_model: str = "claude-haiku-5-5"`, `model: str = "claude-haiku-5-5"`, `effort: Literal["low","medium","high","xhigh","max"] = "medium"`, `pricing: dict[str, Price]`, `max_cost_usd_per_album: float = 5.0`, `candidates_per_theme`, `preview_long_side_px`, `group_size` (по умолчанию из spike-findings).
  - `config.Thresholds`:
    - из spike-findings: `junk_blur`, `junk_dark_luma`, `junk_bright_luma`, `duplicate_distance`, `duplicate_window_s`;
    - заданные здесь: `min_theme_relevance: int = 5`, `min_feed_score: int = 6`, `min_stories_score: int = 4`, `live_frame_margin: float = 0.15`.
  - `config.AppConfig(output_dir: Path = ~/Pictures/photo-chooser, shortlist, cloud, thresholds)`.
  - `config.load_app_config(path: Path | None = None) -> AppConfig`: по умолчанию `~/.config/photo-chooser/config.yaml`; если файла нет — значения по умолчанию.
  - `config.load_trip(path: Path) -> TripConfig`.
  - `config.trip_from_args(album, name, query, start, end, timezone) -> TripConfig`.
  - `config.ConfigError(Exception)` — сообщения на русском.
- `tests/conftest.py`: фабрика `make_asset(**overrides) -> Asset`.

- [ ] **Step 1: Тесты.**

```python
def test_theme_requires_query_or_interval():          # Theme(name="X") -> ConfigError
def test_theme_interval_needs_both_bounds():          # только from -> ConfigError
def test_theme_interval_start_before_end():           # from >= to -> ConfigError
def test_theme_key_stable_and_sensitive()
def test_timezone_required_when_any_theme_has_interval()   # без timezone -> ConfigError
def test_timezone_not_required_for_query_only_themes()
def test_timezone_must_be_iana_name():                # "Cairo" -> ConfigError; "Africa/Cairo" -> ok
def test_load_trip_parses_aliases(tmp_path):          # from/to "2026-03-14 05:00" -> start == datetime(2026,3,14,5,0)
def test_app_config_defaults_when_file_missing(tmp_path):
    cfg = load_app_config(tmp_path / "nope.yaml")
    assert cfg.shortlist.feed == 20 and cfg.shortlist.stories == 40
    assert cfg.cloud.screen_model == "claude-haiku-5-5" and cfg.cloud.model == "claude-haiku-5-5"
    assert cfg.cloud.max_cost_usd_per_album == 5.0
    assert cfg.cloud.pricing["claude-opus-5-5"].output == 20.00
def test_app_config_partial_override(tmp_path)        # только shortlist.feed: 10 -> stories остаётся 40
def test_asset_is_vertical()                          # 3024x4032 -> True; 4032x4032 -> False
def test_themes_key_depends_on_album_and_theme_list() # другой порядок/запрос/альбом -> другой ключ; интервал не влияет
```

- [ ] **Step 2:** `uv run pytest tests/test_config.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces. Зависимости: `uv add pydantic pyyaml typer`, `uv add --dev pytest`.
- [ ] **Step 4:** `uv run pytest tests/test_config.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: config and domain models`.

---

### Task 3: Кэш (SQLite)

**Files:**
- Create: `src/photo_chooser/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `LocalFeatures`, `Judgment`, `Screening` (Task 2).
- Produces: `Cache(path: Path)`; по умолчанию путь `~/Library/Caches/photo-chooser/cache.sqlite`. Методы:
  - `get_features(uuid) -> LocalFeatures | None` / `put_features(f)`;
  - `get_screening(uuid, themes_key, model, prompt_version) -> Screening | None` / `put_screenings(items: list[Screening], themes_key, model, prompt_version)` — первая ступень;
  - `get_judgment(uuid, theme_key, model, prompt_version) -> Judgment | None` / `put_judgments(items: list[Judgment], theme_key, model, prompt_version)` — вторая ступень;
  - `add_spend(album_uuid, model, usd)` / `album_spend(album_uuid) -> float`.

- [ ] **Step 1: Тесты.**

```python
def test_features_roundtrip(tmp_path)
def test_judgment_keyed_by_model_and_prompt(tmp_path):
    c.put_judgments([j], "t1", "claude-haiku-5-5", "judge-1")
    assert c.get_judgment(j.uuid, "t1", "claude-haiku-5-5", "judge-1") == j
    assert c.get_judgment(j.uuid, "t1", "claude-opus-5-5", "judge-1") is None
    assert c.get_judgment(j.uuid, "t1", "claude-haiku-5-5", "judge-2") is None
def test_screening_roundtrip_keyed_by_themes_key(tmp_path)   # relevance dict сохраняется; другой themes_key -> None
def test_album_spend_accumulates(tmp_path)              # 0.10 + 0.25 -> 0.35 (approx); другой альбом -> 0.0
def test_cache_survives_reopen(tmp_path)
```

- [ ] **Step 2:** `uv run pytest tests/test_cache.py -v` → FAIL.
- [ ] **Step 3:** Реализовать на `sqlite3`. Таблицы: `features(uuid PK, json)`, `screenings(uuid, themes_key, model, prompt_version, json, PK(all four))`, `judgments(uuid, theme_key, model, prompt_version, json, PK(all four))`, `spend(album_uuid, model, usd, created_at)`. Каждая запись — сразу commit.
- [ ] **Step 4:** `uv run pytest tests/test_cache.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: sqlite cache for features, judgments and spend`.

---

### Task 4: Адаптер Photos на PhotoKit (только чтение)

**Files:**
- Create: `src/photo_chooser/library/base.py`, `src/photo_chooser/library/photokit_library.py`
- Test: `tests/test_library.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: `Asset`, `AlbumRef` (Task 2).
- Produces:
  - `PhotosLibrary` (Protocol):
    - `albums() -> list[AlbumRef]` — только обычные альбомы пользователя;
    - `find_album(title: str) -> AlbumRef`;
    - `assets(album: AlbumRef) -> list[Asset]` — только изображения;
    - `preview(asset, long_side: int) -> Path | None` — JPEG в `~/Library/Caches/photo-chooser/previews/<safe uuid>_<long_side>.jpg`; уже существующий файл повторно не запрашивается; ошибка или таймаут → `None`;
    - `export_original(asset, dest_dir: Path) -> Path` — основной ресурс снимка (тип Photo);
    - `export_live_video(asset, dest_dir: Path) -> Path`.
  - `base.resolve_album(title, albums) -> AlbumRef`. Исключения:
    - `AlbumNotFound(title, similar: list[str])` — `difflib.get_close_matches(n=5, cutoff=0.5)`;
    - `AlbumAmbiguous(title, matches: list[AlbumRef])`.
  - Прочие исключения:
    - `DownloadError(uuid, reason)` — после 3 попыток, таймаут `DOWNLOAD_TIMEOUT_S = 120`;
    - `PhotosAccessError` — доступ запрещён или ограничен; сообщение «System Settings → Privacy & Security → Photos → включите Terminal».
  - `photokit_library.asset_from_phasset(a, resources: list) -> Asset | None` — чистая функция:
    - не изображение → `None`;
    - `is_live` и `is_screenshot` — по битам `mediaSubtypes`;
    - `filename` — `originalFilename` основного ресурса;
    - `taken_at` — из `creationDate.timeIntervalSince1970()` в UTC;
    - `width/height` — по `orientation_fields`.
  - `PhotoKitLibrary`: асинхронные обработчики PhotoKit синхронизируются через `threading.Event` с таймаутом. Превью — `PHImageRequestOptions(synchronous=True, networkAccessAllowed=True, deliveryMode=HighQualityFormat)`, JPEG через `NSBitmapImageRep`. Никаких изменяющих вызовов.
  - `tests/fakes.py: FakeLibrary(albums, assets: dict[str, list[Asset]], previews: dict[str, Path], files: dict[str, Path], live: dict[str, Path], failing: set[str])`.

- [ ] **Step 1: Тесты** (`types.SimpleNamespace` вместо объектов PhotoKit).

```python
def test_mapping_skips_non_image()
def test_mapping_live_and_screenshot_bits()
def test_mapping_filename_from_primary_resource()
def test_mapping_taken_at_is_utc():                   # timeIntervalSince1970=1773466200 -> datetime(..., tzinfo=UTC)
def test_find_album_not_found_lists_similar():        # "Египт 2026" -> AlbumNotFound, "Египет 2026" in e.similar
def test_find_album_ambiguous()
```

- [ ] **Step 2:** `uv run pytest tests/test_library.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces.
- [ ] **Step 4:** `uv run pytest tests/test_library.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: read-only Photos adapter on PhotoKit`.

---

### Task 5: Изображения, Vision и локальная оценка качества

**Files:**
- Create: `src/photo_chooser/images.py`, `src/photo_chooser/vision.py`, `src/photo_chooser/analysis/quality.py`
- Test: `tests/test_quality.py` (изображения генерируются в фикстурах; Vision настоящий)

**Interfaces:**
- Produces:
  - `images.load_cgimage(path: Path, long_side: int) -> CGImage` — `CGImageSourceCreateThumbnailAtIndex` с учётом ориентации.
  - `images.gray_array(img: CGImage) -> np.ndarray` — float32, значения 0..1.
  - `images.jpeg_bytes(path: Path, long_side: int, quality: float = 0.85) -> bytes`.
  - `images.write_tiff(img: CGImage, dest: Path, metadata_from: Path) -> None` — EXIF/GPS/TIFF копируются из `CGImageSourceCopyPropertiesAtIndex(metadata_from)`.
  - `vision.VisionResult(aesthetic: float | None, is_utility: bool, face_quality: float | None, feature_print: np.ndarray)`; `aesthetic` = `(overallScore + 1) / 2`.
  - `vision.analyze(img: CGImage) -> VisionResult`.
  - `quality.sharpness(gray) -> float` — дисперсия лапласиана 3×3.
  - `quality.mean_luma(gray) -> float`.
  - `quality.compute_features(uuid: str, preview: Path, long_side: int = 1024) -> LocalFeatures`.
  - `quality.percentile_ranks(values: list[float]) -> list[float]`.
  - `quality.local_quality(f: LocalFeatures, sharpness_rank: float) -> float`. Веса: aesthetic 0.5, sharpness_rank 0.35, face_quality 0.15. Компоненты `None` исключаются, оставшиеся веса нормируются до суммы 1.
  - `quality.feature_distance(a: bytes, b: bytes) -> float` — L2.

- [ ] **Step 1: Тесты.**

```python
def test_blurred_image_less_sharp(tmp_path)
def test_black_image_low_luma(tmp_path)               # < 0.03
def test_feature_distance_self_zero(tmp_path)         # == 0; другое изображение -> > 0
def test_local_quality_renormalizes_missing():        # aesthetic=1.0, face=None, sharp_rank=1.0 -> 1.0
def test_local_quality_weights():                     # aesthetic=0, sharp=1, face=1 -> 0.5
def test_percentile_ranks_bounds():                   # [3,1,2] -> [1.0, 0.0, 0.5]
def test_write_tiff_copies_date_and_gps(tmp_path)
```

- [ ] **Step 2:** `uv run pytest tests/test_quality.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces.
- [ ] **Step 4:** `uv run pytest tests/test_quality.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: image IO, Vision wrappers and local quality score`.

---

### Task 6: Отсев мусора и дублей

**Files:**
- Create: `src/photo_chooser/analysis/junk.py`, `src/photo_chooser/analysis/dedup.py`
- Test: `tests/test_junk_dedup.py`

**Interfaces:**
- Consumes: `Asset`, `LocalFeatures`, `Thresholds`, `feature_distance`.
- Produces:
  - `junk.junk_reason(asset, f, th) -> str | None`. Порядок проверок и тексты причин: `"скриншот"`, `"утилитарный кадр"`, `"смаз"` (`sharpness < junk_blur`), `"слишком тёмный"`, `"пересвечен"`.
  - `dedup.cluster(items: list[tuple[Asset, LocalFeatures]], th) -> list[list[str]]` — сортировка по `taken_at`, окно `duplicate_window_s`, union-find по парам с `feature_distance ≤ duplicate_distance`.
  - `dedup.representatives(clusters, quality: dict[str, float]) -> list[str]`.

- [ ] **Step 1: Тесты.**

```python
def test_junk_reasons_order()                          # скриншот с плохой резкостью -> "скриншот"
def test_good_photo_not_junk()
def test_cluster_requires_both_time_and_similarity()   # A,B рядом и похожи -> вместе; C похож, но через 2 ч -> отдельно; D рядом, но не похож -> отдельно
def test_cluster_is_transitive()
def test_representative_is_best_quality()
```

- [ ] **Step 2:** `uv run pytest tests/test_junk_dedup.py -v` → FAIL.
- [ ] **Step 3:** Реализовать.
- [ ] **Step 4:** `uv run pytest tests/test_junk_dedup.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: junk filter and duplicate clustering`.

---

### Task 7: Облачные ступени, стоимость и бюджет

**Files:**
- Create: `src/photo_chooser/judge/cost.py`, `src/photo_chooser/judge/prompts.py`, `src/photo_chooser/judge/stages.py`
- Test: `tests/test_cost.py`, `tests/test_stages.py`; `FakeClient` в `tests/fakes.py`

**Interfaces:**
- Consumes: `Asset`, `Theme`, `themes_key`, `Judgment`, `Screening`, `Cache`, `CloudCfg`, `Price`.
- Produces:
  - `cost`:
    - `image_tokens(width, height, long_side) -> int` — масштаб до `long_side`, затем `ceil(w*h/IMAGE_TOKEN_DIVISOR)`;
    - константы из spike-findings: `IMAGE_TOKEN_DIVISOR = 750`, `PROMPT_OVERHEAD_TOKENS = 500`, `EST_OUTPUT_TOKENS_SCREEN = 105`, `EST_OUTPUT_TOKENS_JUDGE = 80`;
    - `price_for(cfg: CloudCfg, model) -> Price` — нет цены → `UnknownModelPrice(model)`;
    - `estimate_run_usd(n_screen: int, n_judge: list[int], cfg: CloudCfg, screen_model, judge_model) -> float` — `n_screen` снимков на первой ступени (один проход), `n_judge[i]` кандидатов темы i на второй; превью 4:3 при `preview_long_side_px`, группы по `group_size`;
    - `usage_usd(input_tokens, output_tokens, price) -> float`;
    - `check_budget(spent, estimate, limit) -> None` — превышение → `BudgetExceeded(spent, estimate, limit)`.
  - `prompts`:
    - `SCREEN_PROMPT_VERSION = "screen-1"`, `JUDGE_PROMPT_VERSION = "judge-1"`;
    - `SCREEN_SYSTEM_PROMPT` — промпт варианта B из `spike/screen_probe_v2.py` (`SYSTEM`) без изменений по смыслу: распределение по темам одной поездки; аббревиатуры и темы, определённые через другие; `matches` с релевантностью 1–10, ниже 5 не указывать, кадр может не подходить ни к одной теме; черновые `feed_score` и `stories_score`;
    - `JUDGE_SYSTEM_PROMPT` — промпт из `spike/judge_probe.py` (`SYSTEM`): роль фоторедактора для Instagram; `matches_theme`; рубрики `feed_score` и `stories_score` (изъяны допустимы); `reason` — одно предложение на языке темы.
  - `stages`:
    - pydantic-схемы ответов: `ScreenMatch(theme: int, relevance: int)`, `ScreenItem(id, matches: list[ScreenMatch], feed_score, stories_score)`, `ScreenOutput(items)`; `JudgeItem(id, matches_theme, feed_score, stories_score, reason)`, `JudgeOutput(items)`;
    - `call_model(client, model, effort, system, content, output_model) -> response`:
      - `client.messages.parse(model=…, max_tokens=16000, system=…, messages=[{"role": "user", "content": content}], output_format=output_model, output_config={"effort": effort})`;
      - для `FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}` — `client.beta.messages.parse(…, betas=["server-side-fallback-2026-07-01"], fallbacks="default")`;
      - `RateLimitError` после повторов SDK → пауза `RATE_LIMIT_SLEEP_S = 20` и повтор, до 12 попыток;
    - `image_content(group, preview) -> list[dict]` — для каждого снимка `{"type": "text", "text": "id: pNN"}` и картинка base64 `image/jpeg`;
    - `make_groups(assets, order: dict[str, float], size) -> list[list[Asset]]` — раздача по кругу в порядке убывания `order` в `ceil(n/size)` групп;
    - `ScreenRunner(client, model, effort, preview: Callable[[Asset], bytes])`, метод `run_group(album_title, themes: list[Theme], group) -> GroupResult[Screening]`. В конце запроса текст `Trip album: «…»` и пронумерованный список тем; номер темы `n` → `themes[n-1].key()`, неизвестные номера отбрасываются, оценки приводятся к 1..10;
    - `JudgeRunner(client, model, effort, preview)`, метод `run_group(theme, group) -> GroupResult[Judgment]`. В конце запроса текст `Theme: «…»`;
    - `GroupResult(items: list, unjudged: list[str], refused: bool, input_tokens: int, output_tokens: int)`;
    - `run_screen(runner, cache, album_title, album_uuid, themes, assets, cfg) -> StageOutcome[Screening]`:
      - кэш по `themes_key(album_title, themes)`, `runner.model`, `SCREEN_PROMPT_VERSION`;
      - группы по времени съёмки;
    - `run_judge(runner, cache, theme, assets, order, cfg, album_uuid) -> StageOutcome[Judgment]`:
      - кэш по `theme.key()`, `runner.model`, `JUDGE_PROMPT_VERSION`;
      - группы через `make_groups`;
    - общее для обеих ступеней:
      - `StageOutcome(results: dict[str, T], unjudged: list[str], sent: int, usd: float)`;
      - запросы только для некэшированных, не больше `MAX_PARALLEL_REQUESTS = 3` одновременно;
      - после **каждой** группы сразу запись в кэш и `cache.add_spend`.

- [ ] **Step 1: Тесты `tests/test_cost.py`.**

```python
def test_image_tokens_scales_to_long_side()
def test_estimate_run_counts_screen_once_and_judge_per_theme():  # n_screen=1000, n_judge=[100,100]: первая ступень по цене screen_model один раз
def test_usage_usd_haiku():                           # 1M in + 1M out -> 0.60
def test_price_for_unknown_model_raises()
def test_check_budget()                               # 4.0 + 1.5 > 5 -> BudgetExceeded; limit 6 -> ok
```

- [ ] **Step 2: Тесты `tests/test_stages.py`** (`FakeClient` возвращает заданный `parsed_output`/`stop_reason`/`usage`, может бросать `RateLimitError`, считает вызовы).

```python
def test_make_groups_spreads_order()
def test_screen_maps_theme_numbers_to_keys():         # matches [{theme:2, relevance:8}] -> relevance {themes[1].key(): 8}
def test_screen_drops_unknown_theme_numbers_and_clamps()   # theme 9 при 8 темах отбрасывается; relevance 12 -> 10
def test_screen_request_lists_album_and_themes()      # последний text-блок: «Trip album: «Egypt.26»» и «1. Пирамиды…»
def test_screen_cache_keyed_by_theme_set(tmp_path)    # тот же набор тем -> 0 вызовов; добавлена тема -> вызовы снова
def test_judge_maps_short_ids_and_clamps()
def test_missing_ids_are_unjudged()
def test_refusal_marks_group_unjudged()               # stop_reason="refusal" -> refused, все unjudged, usage учтён
def test_interrupted_run_does_not_pay_twice(tmp_path):
    # FakeClient падает на 2-й группе из 3 -> исключение; spend == стоимость 1-й группы
    # повторный run_judge с рабочим клиентом -> вызовов 2, album_spend == сумма трёх групп
def test_rate_limit_waits_and_retries(monkeypatch)    # RateLimitError дважды, затем успех; time.sleep вызван 2 раза по 20
def test_request_shape()                              # messages.parse(model, max_tokens=16000, system, output_format, output_config={"effort": effort})
def test_opus_and_sonnet_use_server_side_fallbacks()  # beta.messages.parse(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default"); haiku — без них
```

- [ ] **Step 3:** `uv run pytest tests/test_cost.py tests/test_stages.py -v` → FAIL.
- [ ] **Step 4:** Реализовать по Interfaces. Клиент — `anthropic.Anthropic(max_retries=4)` из окружения; параметр `thinking` не передаётся. Если оценка входа группы ≥ 100 000 токенов — `ValueError` (ценовой порог Haiku).
- [ ] **Step 5:** `uv run pytest tests/test_cost.py tests/test_stages.py -v` → PASS.
- [ ] **Step 6: Commit** — `feat: two-stage Claude scoring with caching and budget guard`.

---

### Task 8: Интервал, кандидаты и раскладка

**Files:**
- Create: `src/photo_chooser/select.py`
- Test: `tests/test_select.py`

**Interfaces:**
- Consumes: `Asset`, `Judgment`, `Screening`, `LocalFeatures`, `Theme`, `Thresholds`.
- Produces:
  - `select.in_interval(asset, theme, tz: ZoneInfo | None) -> bool`. Местное время `asset.taken_at.astimezone(tz)` без tzinfo сравнивается с наивными `start/end` включительно. Тема без интервала → `True`.
  - `select.screen_candidates(assets, screenings: dict[str, Screening], quality: dict[str, float], theme: Theme, min_relevance: int, limit: int) -> list[Asset]`:
    - тема с `query` — снимки, у которых `screenings[uuid].relevance.get(theme.key(), 0) ≥ min_relevance`;
    - тема только с интервалом — все `assets`, включая снимки без результата первой ступени;
    - сортировка по ключу (релевантность, `max(feed_score, stories_score)`, quality) по убыванию; отсутствующие значения считаются 0;
    - первые `limit`.
  - `select.Scored(asset, score: float, reason: str)`, `select.Pools(feed: list[Scored], stories: list[Scored])`. Пулы упорядочены и не обрезаны: обрезку и замену выполняет Task 10.
  - `select.pools_from_judgments(cands, judgments, quality, th, interval_only) -> Pools`:
    - **feed** — оценённые, у которых `matches_theme` или `interval_only` и `feed_score ≥ min_feed_score`; сортировка по `feed_score` по убыванию, при равенстве — по quality;
    - **stories** — подходящие с `stories_score ≥ min_stories_score`; сначала вертикальные (`stories_score` по убыванию, затем quality), потом остальные;
    - один снимок может быть в обоих пулах.
  - `select.pools_local(cands, quality, feats) -> Pools` (FR-10.2):
    - feed — по quality;
    - stories — тот же порядок, вертикальные первыми;
    - reason — `"эстетика 0.72 · резкость 0.81 · лица 0.65"` (отсутствующие компоненты опускаются).

- [ ] **Step 1: Тесты.**

```python
def test_in_interval_converts_to_trip_timezone():     # taken_at 2026-03-13T20:30Z, Asia/Tokyo, [14.03 05:00, 14.03 12:00] -> True
def test_in_interval_inclusive_bounds()
def test_in_interval_without_interval_true()
def test_screen_candidates_relevance_threshold()      # 4 отсекается, 5 проходит (min_relevance=5)
def test_screen_candidates_order_and_limit()          # релевантность, затем черновая оценка, затем quality
def test_screen_candidates_interval_only_includes_unscreened()
def test_feed_threshold_and_order()
def test_interval_only_ignores_matches_theme()
def test_stories_verticals_first()                    # горизонтальный с 9 после вертикального с 5
def test_stories_lower_threshold()
def test_square_counts_as_horizontal()
def test_unjudged_candidates_excluded()
def test_local_pools_reason_format()
```

- [ ] **Step 2:** `uv run pytest tests/test_select.py -v` → FAIL.
- [ ] **Step 3:** Реализовать.
- [ ] **Step 4:** `uv run pytest tests/test_select.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: interval filter, stage-1 candidates and feed/stories pools`.

---

### Task 9: Выбор кадра Live Photo

**Files:**
- Create: `src/photo_chooser/analysis/live.py`
- Test: `tests/test_live.py` (короткое `.mov` генерируется в фикстуре через `AVAssetWriter`; ключевой снимок — PNG)

**Interfaces:**
- Consumes: `images.load_cgimage`, `images.gray_array`, `vision.analyze`, `quality.sharpness`.
- Produces:
  - `live.sample_frames(video: Path, n: int) -> list[CGImage]` — `n` из spike-findings.
  - `live.frame_scores(images: list[CGImage], long_side: int) -> list[float]`. Все изображения приводятся к общей длинной стороне. Score = 0.5·aesthetic + 0.3·sharpness/max(sharpness) + 0.2·face_quality; отсутствующие компоненты нормируются.
  - `live.choose(key_score, frame_scores, margin) -> int | None` — индекс кадра, если `max(frame) ≥ key + margin`, иначе `None`.
  - Использование (Task 10): `s = frame_scores([key] + frames, long_side)`, затем `choose(s[0], s[1:], margin)`.

- [ ] **Step 1: Тесты.**

```python
def test_choose_key_when_within_margin():             # key .70, лучший кадр .80, margin .15 -> None
def test_choose_frame_when_clearly_better():          # key .50, кадры [.40, .70] -> 1
def test_sample_frames_count(tmp_path)                # n=5 -> 5
def test_frame_scores_sharp_beats_blurred(tmp_path)
```

- [ ] **Step 2:** `uv run pytest tests/test_live.py -v` → FAIL.
- [ ] **Step 3:** Реализовать через `AVAssetImageGenerator` с `appliesPreferredTrackTransform=True` и нулевыми допусками.
- [ ] **Step 4:** `uv run pytest tests/test_live.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: best frame selection for Live Photos`.

---

### Task 10: Экспорт и отчёт

**Files:**
- Create: `src/photo_chooser/export.py`, `src/photo_chooser/report.py`
- Test: `tests/test_export_report.py`

**Interfaces:**
- Consumes: `PhotosLibrary`, `Pools`/`Scored` (Task 8), `live.*` (Task 9), `images.load_cgimage`, `images.write_tiff`, `Theme`.
- Produces:
  - `export.safe_name(s) -> str`:
    - `/ \ : * ? " < > |` и управляющие символы заменяются на `_`;
    - пробелы по краям обрезаются;
    - результат не длиннее 100 символов;
    - пустая строка → `"_"`.
  - `export.ensure_outside_library(path) -> None` — путь внутри `*.photoslibrary` → `ConfigError`.
  - `export.new_run_dir(output_dir, album_title, now) -> Path` — `<output>/<safe album>/<YYYY-MM-DD_HHMM>`, при совпадении суффиксы `_2`, `_3`…
  - `export.ExportedItem(list_name, rank, asset, files: list[str], score, reason, notes: list[str])`. Заметки:
    - `"кадр из Live Photo, разрешение ниже оригинала"`;
    - `"видео Live Photo не скачалось — взят ключевой снимок"`.
  - `export.assemble(pools, n_feed, n_stories, try_export: Callable[[Scored, str, int], ExportedItem | None]) -> Assembled(feed, stories, failed: list[tuple[Asset, str]], shortfall_feed, shortfall_stories)`. Алгоритм:
    - идти по `pools.feed`, пока не экспортировано `n_feed`;
    - затем идти по `pools.stories`, пропуская экспортированные и упавшие;
    - ранг — порядковый номер успешного экспорта в списке.
  - `export.export_asset(library, scored, list_name, rank, theme_dir, tmp, cfg) -> ExportedItem | None`:
    - имена `f"{rank:02d}_{original_name}"`;
    - Live Photo: видео → кадры → `live.choose` → оригинал или `…_live-frame.tiff`;
    - `DownloadError` видео → оригинал ключевого снимка и заметка;
    - `DownloadError` оригинала → `None`.
  - `report.write_report(path, theme, header: ReportHeader, assembled, stats: ThemeStats) -> None`:
    - `ReportHeader(screen_model: str | None, model: str | None, effort: str | None, prompt_versions: str | None, run_at: datetime, warnings: list[str])`; модели `None` → «без облака»;
    - `ThemeStats(read, no_preview, junk, duplicates_collapsed, in_interval, sent_to_screen, matched, sent_to_judge, from_cache, unjudged, usd_screen, usd_judge, usd_album_total)`;
    - таблица: «Ранг | Файл(ы) | Список | Оценка | Ориентация | Пометки | Причина»;
    - сводка, недобор, пропущенные снимки с причиной.

- [ ] **Step 1: Тесты.**

```python
def test_safe_name_cases():                           # "Гиза/Каир: день 1 " -> "Гиза_Каир_ день 1"; "" -> "_"
def test_ensure_outside_library(tmp_path)
def test_new_run_dir_never_overwrites(tmp_path)
def test_assemble_disjoint_and_ranked()
def test_assemble_replaces_failed_export()
def test_assemble_shortfall()
def test_export_original_ranked_name(tmp_path)       # "01_IMG_1.HEIC"
def test_export_live_video_failure_falls_back_to_key(tmp_path)
def test_export_live_frame_writes_tiff(tmp_path)
def test_report_contains_header_table_and_summary(tmp_path)   # «без облака», предупреждения, «Недобор: лента 3», стоимость по ступеням
```

- [ ] **Step 2:** `uv run pytest tests/test_export_report.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces.
- [ ] **Step 4:** `uv run pytest tests/test_export_report.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: shortlist export and markdown report`.

---

### Task 11: Конвейер и CLI

**Files:**
- Create: `src/photo_chooser/pipeline.py`, `src/photo_chooser/cli.py`
- Modify: `pyproject.toml` — `[project.scripts] photo-chooser = "photo_chooser.cli:app"`
- Test: `tests/test_pipeline.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: всё из Task 2–10.
- Produces:
  - `pipeline.RunOptions(dry_run: bool, no_cloud: bool, model: str | None, max_cost: float | None)`.
  - `pipeline.prepare_album(library, cache, album, cfg) -> AlbumState(assets, previews: dict[str, Path], features, quality, junk: dict[str, str], no_preview: list[str], duplicates_collapsed: int, pool: list[Asset])`. `library.preview() is None` → снимок в `no_preview`, дальше не анализируется.
  - `pipeline.run(trip, cfg, opts, library, cache, screen_factory: Callable[[str, str], ScreenRunner], judge_factory: Callable[[str, str], JudgeRunner], now) -> RunResult(run_dir: Path | None, themes: list[ThemeOutcome], estimate_usd: float)`:
    - фабрики `(model, effort)` создают раннеры с превью `images.jpeg_bytes(previews[a.uuid], cfg.cloud.preview_long_side_px)`;
    - `ThemeOutcome(theme, report_path: Path | None, assembled: Assembled | None, error: str | None)`.

    Порядок:
    1. `ensure_outside_library(cfg.output_dir)`.
    2. `find_album`, `prepare_album`.
    3. Для каждой темы — снимки `pool`, прошедшие `in_interval` с `trip.tz`.
    4. Если не `no_cloud`:
       - проверка цен обеих моделей;
       - `estimate_run_usd`: `n_screen` — некэшированные снимки `pool` для набора `trip.query_themes` (0, если тем с `query` нет); `n_judge[i] = min(снимков темы i после интервала, candidates_per_theme)`;
       - `check_budget(cache.album_spend, estimate, opts.max_cost or limit)` — **до** первого вызова.
    5. `dry_run` → вернуть оценку без вызовов и экспорта.
    6. Облачный режим: если `trip.query_themes` не пуст — **один** `run_screen` по всему `pool` со всеми `query_themes`. Ошибка API здесь останавливает запуск: без первой ступени темы с `query` не обработать.
    7. По каждой теме:
       - облачный режим: `screen_candidates` → если кандидатов нет, вторая ступень не вызывается → `run_judge` → `pools_from_judgments`;
       - `no_cloud`: `query` игнорируется (предупреждение в шапке отчёта) → `pools_local`.
    8. Затем `assemble` + `export_asset` → `write_report`. Исключение API во второй ступени темы → тема помечается упавшей, остальные продолжаются.
  - `cli.app` (typer):
    - `albums` — строки `«<title> — <count> фото»`;
    - `run [TRIP] --album --theme --query --from --to --timezone --dry-run --no-cloud --model --max-cost`. Нужен либо `TRIP`, либо `--album` + `--theme`; `--from/--to` в формате `YYYY-MM-DD HH:MM`, вместе с ними обязателен `--timezone`.
  - Коды выхода: 0 — успех, 2 — `ConfigError`/`UnknownModelPrice`, 3 — `BudgetExceeded`, 4 — `AlbumNotFound`/`AlbumAmbiguous`, 5 — `PhotosAccessError`, 1 — прочее. Сообщения на русском.

- [ ] **Step 1: Тесты `tests/test_pipeline.py`** (`FakeLibrary`, `FakeClient`, `tmp_path`).

```python
def test_no_cloud_end_to_end(tmp_path)                # скриншот и дубль не в выдаче; feed/ и stories/; report.md; FakeClient не вызван
def test_no_cloud_ignores_query_with_warning(tmp_path)
def test_assets_without_preview_skipped_and_counted(tmp_path)
def test_screen_runs_once_for_all_themes(tmp_path)    # 3 темы с query, pool 45 -> 3 запроса первой ступени (группы по 20), не 9
def test_no_matches_in_screen_skips_judge(tmp_path)   # первая ступень: ни одного совпадения с темой -> 0 запросов второй ступени, недобор 20/40
def test_interval_only_trip_skips_screen(tmp_path)    # нет тем с query -> 0 запросов первой ступени
def test_empty_interval_no_calls(tmp_path)
def test_budget_checked_before_any_call(tmp_path)     # album_spend 4.9 + оценка > 5 -> BudgetExceeded, 0 вызовов, run_dir не создан
def test_max_cost_overrides_limit(tmp_path)
def test_dry_run_no_calls_no_files(tmp_path)
def test_unknown_model_price_stops_before_call(tmp_path)
def test_theme_failure_does_not_stop_other_themes(tmp_path)
def test_model_override_keys_cache_separately(tmp_path)   # haiku, затем --model opus -> вторая ступень вызывается заново, первая — из кэша
```

- [ ] **Step 2: Тесты `tests/test_cli.py`** (`typer.testing.CliRunner`, фабрики подменяются monkeypatch).

```python
def test_run_requires_trip_or_album_and_theme()       # exit 2
def test_interval_without_timezone_exit_2()
def test_album_not_found_exit_4_lists_similar()
def test_budget_exceeded_exit_3()
def test_photos_access_error_exit_5_mentions_settings()   # «System Settings → Privacy & Security → Photos»
```

- [ ] **Step 3:** `uv run pytest tests/test_pipeline.py tests/test_cli.py -v` → FAIL.
- [ ] **Step 4:** Реализовать по Interfaces.
- [ ] **Step 5:** `uv run pytest -v` → PASS, весь набор.
- [ ] **Step 6: Commit** — `feat: pipeline orchestration and CLI`.

---

### Task 12: Интеграционные тесты на реальной Photos

**Files:**
- Create: `tests/integration/test_album.py`, `tests/integration/README.md`
- Modify: `pyproject.toml` — маркеры `integration`, `cloud`; по умолчанию `-m "not integration"`

**Interfaces:**
- Consumes: CLI и `PhotoKitLibrary`.
- `tests/integration/README.md` описывает, как подготовить запуск:
  - **тестовый альбом** (~30 фото, собирается вручную): серия из 5 дублей, скриншот, смазанный кадр, Live Photo, кадры двух тем (например, «кофе» и «улица»);
  - **проверку NFR-5**: у Terminal в System Settings → Privacy & Security включён только Photos, а в Full Disk Access он выключен.
- Тесты запускает пользователь в Terminal, имя альбома передаётся через `PHOTO_CHOOSER_IT_ALBUM`.

- [ ] **Step 1: Тесты.**

```python
@pytest.mark.integration
def test_library_unchanged_after_run(tmp_path):        # NFR-1: множество localIdentifier, count и modificationDate до == после
@pytest.mark.integration
def test_no_cloud_run_on_test_album(tmp_path):         # нет скриншота; из серии максимум 1; Live — 1 файл
@pytest.mark.integration
@pytest.mark.cloud
def test_haiku_run_on_test_album(tmp_path):            # одна тема, report.md с моделями и стоимостью > 0
```

- [ ] **Step 2:** Пользователь в Terminal: `PHOTO_CHOOSER_IT_ALBUM="photo-chooser-test" uv run pytest -m integration -v` → PASS. Тест `cloud` — только при наличии ключа.
- [ ] **Step 3: Commit** — `test: integration tests on a real Photos album`.

---

### Task 13: Приёмка на прошлой поездке и выбор прод-модели (вместе с пользователем, без кода)

- [ ] **Step 1:** Альбом прошлой поездки, `trip.yaml` с 2–3 темами разного типа (SPEC §12). Последовательно выполнить: `--no-cloud`; затем с первой ступенью на Haiku — вторая на `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`. Лимит поднимается через `--max-cost` с согласия пользователя.
- [ ] **Step 2:** Записать в `docs/acceptance.md` по каждому прогону:
  - время первого прогона и повтора той же темы (NFR-4);
  - стоимость по ступеням (NFR-2);
  - оценку пользователя: пригодные кадры из 20 ленты и 40 stories (NFR-3);
  - замечания о пропусках первой ступени.
- [ ] **Step 3:** Пользователь выбирает модель второй ступени. Зафиксировать `cloud.model` и `cloud.effort` в `~/.config/photo-chooser/config.yaml` и результат в `docs/acceptance.md`.
- [ ] **Step 4: Commit** — `docs: acceptance results and production model choice`.
