# Photo Chooser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CLI для macOS, который по альбому Photos и темам собирает шорт-лист кадров для ленты и stories Instagram и копирует оригиналы в отдельную директорию.

**Architecture:**
- Python-пакет `photo_chooser`, конвейер: чтение альбома → локальные признаки (Apple Vision) → отсев мусора и дублей → фильтр темы (интервал + мультиязычная модель «изображение–текст») → оценка Claude группами → раскладка → экспорт и отчёт.
- Внешние системы (Photos, Claude, модель эмбеддингов) спрятаны за интерфейсами, в тестах заменяются заглушками.
- Всё посчитанное и оплаченное кэшируется в SQLite.

**Tech Stack:**
- Python 3.12, `uv`, `pytest`;
- `osxphotos`;
- `pyobjc-framework-Vision`, `pyobjc-framework-Quartz`, `pyobjc-framework-AVFoundation`;
- `anthropic` (официальный SDK);
- `pydantic` v2, `PyYAML`, `typer`, `numpy`;
- модель эмбеддингов выбирается в Task 1.

**Spec:** [SPEC.md](../../../SPEC.md)

## Global Constraints

- macOS 26 (Tahoe), Apple Silicon. Python 3.12, зависимости и запуск через `uv`.
- Photos **только на чтение**:
  - никаких вызовов, меняющих медиатеку;
  - `osxphotos.ExportOptions` только с `edited=False` и без опций записи (альбомы, ключевые слова, метаданные, sidecar).
- Копируются только **оригиналы**. Пара RAW+JPEG/HEIC — один снимок, копируются оба файла с одним префиксом ранга.
- Каждый запуск пишет в новую директорию `<output_dir>/<альбом>/<YYYY-MM-DD_HHMM>/<тема>/{feed,stories}/`, ничего не перезаписывает. Выходная директория внутри `*.photoslibrary` запрещена.
- Шорт-лист по умолчанию: лента 20, stories 40.
- Модель по умолчанию — `claude-haiku-5-5`. Цены по умолчанию (за 1 млн токенов, вход / выход):
  - haiku 0.10 / 0.50;
  - sonnet-5-5 2.00 / 10.00;
  - opus-5-5 4.00 / 20.00.
- Лимит накопленной стоимости по альбому `max_cost_usd_per_album: 5.0`. Проверка выполняется до первого обращения к Claude. `--max-cost` заменяет лимит на один запуск.
- Ключ API в конфигах не хранится: берётся из `ANTHROPIC_API_KEY` или профиля `ant auth login`. В `--no-cloud` ключ не нужен.
- Кадр из Live Photo сохраняется в TIFF с суффиксом `_live-frame`; дата и GPS копируются из оригинала.
- Числовые пороги и константы стоимости берутся из `docs/spike-findings.md` (Task 1). Там же фиксируется выбранная модель эмбеддингов.
- Тексты CLI и `report.md` на русском. Причина выбора от Claude пишется на языке темы.

## Review Focus

1. **Имя темы или альбома с `/`, `:`, эмодзи или пробелами по краям.** Ожидание: валидное имя директории, запуск не падает. Тест в Task 11.
2. **У снимка нет локального превью** (только в iCloud). Ожидание: снимок не анализируется, учитывается в отчёте как «нет превью», запуск продолжается. Тесты в Task 4 и Task 12.
3. **Запуск прерван после того, как часть групп уже оплачена.** Ожидание: повторный запуск не платит за них снова, накопленная стоимость альбома равна реально потраченной. Тест в Task 8.
4. **В теме ноль кандидатов** (запрос ничего не нашёл или интервал пуст). Ожидание: Claude не вызывается, отчёт с пустыми списками и недобором. Тест в Task 12.
5. **Видеочасть Live Photo не скачалась.** Ожидание: копируется оригинал ключевого снимка, в отчёте пометка. Тест в Task 11.

---

## File Structure

```
pyproject.toml, .gitignore
src/photo_chooser/
  models.py            # Asset, LocalFeatures, Judgment, AlbumRef — общие dataclass'ы
  config.py            # AppConfig, TripConfig, Theme (pydantic) + загрузка YAML
  cache.py             # SQLite: признаки, эмбеддинги, оценки, расходы
  library/
    base.py            # PhotosLibrary Protocol + исключения
    osxphotos_library.py
  images.py            # загрузка и масштабирование через ImageIO, JPEG-байты, TIFF-запись
  vision.py            # Vision: эстетика, utility, лица, feature print
  analysis/
    quality.py         # резкость, яркость, LocalFeatures, local_quality
    junk.py            # отсев мусора
    dedup.py           # кластеры дублей
    encoder.py         # TextImageEncoder + реализация из Task 1
    theme_filter.py    # интервал, релевантность, кандидаты
    live.py            # выбор кадра Live Photo
  judge/
    cost.py            # оценка стоимости, бюджет
    prompt.py          # PROMPT_VERSION, системный промпт
    claude_judge.py    # группы, вызов API, кэширование оценок
  select.py            # пулы ленты и stories
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

Код этого задания одноразовый. Результат — только `docs/spike-findings.md`. Выполняется вместе с пользователем: нужен доступ Терминала к Photos (его даёт пользователь) и альбом поездки с «Оптимизацией хранилища».

**Files:**
- Create: `spike/*.py` (одноразовые), `docs/spike-findings.md`, `.gitignore`

**Interfaces:**
- Produces: `docs/spike-findings.md` со всеми полями из Step 9. Задачи 2–12 берут оттуда значения.

- [ ] **Step 1:** Git и `.gitignore` уже настроены (репозиторий `poppyfloppy/photo-chooser`, ветка `main`). Выполнить `uv init --package --name photo-chooser --python 3.12`. Добавить зависимости: `uv add osxphotos pyobjc-framework-Vision pyobjc-framework-Quartz pyobjc-framework-AVFoundation anthropic numpy`.
- [ ] **Step 2: Чтение альбома.** `spike/read_album.py <album>` через `osxphotos.PhotosDB()` печатает для 20 снимков:
  - `uuid`, `original_filename`, `date`, `tzoffset`;
  - `width/height` и `original_width/original_height`, `orientation`;
  - `live_photo`, `has_raw`, `israw`, `ismovie`, `screenshot`, `shared`;
  - `iscloudasset`, `ismissing`, `path`, `path_derivatives`, `score.overall`.

  Зафиксировать:
  - как выглядит снимок без часового пояса;
  - какие размеры отражают итоговую ориентацию;
  - есть ли локальное превью у снимков с `ismissing=True`;
  - диапазон и наличие `score.overall`.
- [ ] **Step 3: Экспорт.** `spike/export.py` выгружает в `spike/out/` три снимка, которых нет локально: обычный, пару RAW и Live Photo. Используется `osxphotos.PhotoExporter(photo).export(dest, options=ExportOptions(download_missing=True, use_photokit=True, edited=False, raw_photo=True, live_photo=True))`. Зафиксировать список полученных файлов и время скачивания. **Критерий провала:** альбом не читается (Step 2) или оригиналы и видео не скачиваются (Step 3). Тогда остановиться и обсудить с пользователем переход на Swift-хелпер (SPEC §11).
- [ ] **Step 4: Vision через pyobjc на 50 превью.**
  - `VNCalculateImageAestheticsScoresRequest`: `overallScore`, `isUtility`;
  - `VNDetectFaceCaptureQualityRequest`;
  - `VNGenerateImageFeaturePrintRequest`.

  Сравнить `computeDistance:` с L2-нормой векторов из `featurePrint.data()`. Зафиксировать, совпадают ли они, и типичные расстояния: внутри серии и между разными сценами.
- [ ] **Step 5: Пороги отсева.** На тех же превью посчитать дисперсию лапласиана (оттенки серого, длинная сторона 1024) и среднюю яркость. Вместе с пользователем отметить явный брак и подобрать `junk_blur`, `duplicate_distance`, `duplicate_window_s`.
- [ ] **Step 6: Модель «изображение–текст».** Зависимости ставятся в отдельную группу: `uv add --group spike …`. Сравнить кандидатов:
  - open_clip `xlm-roberta-base-ViT-B-32` / `laion5b_s13b_b90k`;
  - Hugging Face `google/siglip2-base-patch16-256`.

  Если id модели не найден, взять ближайший мультиязычный вариант той же библиотеки и записать фактический id. Проверка: 5 запросов пользователя на русском и английском по 200 превью. Критерий выбора — доля релевантных кадров в топ-30. Зафиксировать победителя, пакет, время эмбеддинга 1000 превью и порог `theme_relevance`.
- [ ] **Step 7: Стоимость.** Один вызов `claude-haiku-5-5` с группой из 20 JPEG-превью (длинная сторона 1024, quality 0.85) через `client.messages.parse` со схемой из Task 8 и `output_config={"effort": "medium"}`. Записать `usage.input_tokens`, `usage.output_tokens` и сравнить с формулой `ceil(w*h/750)` на картинку.
- [ ] **Step 8: Live Photo.** `AVAssetImageGenerator` с нулевыми допусками извлекает 15 равномерных кадров из скачанного `.mov`. Зафиксировать разрешение кадра и время извлечения.
- [ ] **Step 9: Записать `docs/spike-findings.md`.** Поля:
  - `tz_missing_signal`, `orientation_fields`, `preview_source`, `photos_score_available`, `photos_score_range`;
  - `featureprint_distance` (совпадает ли с `computeDistance`);
  - `junk_blur`, `junk_dark_luma`, `junk_bright_luma`, `duplicate_distance`, `duplicate_window_s`;
  - `encoder` (пакет + id модели), `theme_relevance`;
  - `image_token_divisor`, `prompt_overhead_tokens`, `est_output_tokens_per_photo`;
  - `live_frames_per_video`, `live_frame_resolution`.
- [ ] **Step 10: Commit.**

```bash
git add .gitignore pyproject.toml uv.lock SPEC.md docs/ spike/
git commit -m "spike: verify osxphotos, Vision, encoder and token costs on macOS 26"
```

---

### Task 2: Модели данных и конфигурация

**Files:**
- Create: `src/photo_chooser/models.py`, `src/photo_chooser/config.py`
- Test: `tests/test_config.py`, `tests/conftest.py`

**Interfaces:**
- Produces:
  - `models.Asset` (frozen dataclass): `uuid: str`, `filename: str`, `local_time: datetime` (naive, местное время съёмки), `tz_known: bool`, `width: int`, `height: int`, `is_live: bool`, `has_raw_pair: bool`, `is_screenshot: bool`, `preview_path: Path | None`, `photos_score: float | None`; свойство `is_vertical -> bool` (`height > width`; квадрат — не вертикальный).
  - `models.AlbumRef(uuid: str, title: str, folder: str | None, count: int)`.
  - `models.LocalFeatures(uuid: str, aesthetic: float | None, is_utility: bool, sharpness: float, mean_luma: float, face_quality: float | None, feature_print: bytes)`. `feature_print` — float32-байты вектора.
  - `models.Judgment(uuid: str, matches_theme: bool, feed_score: int, stories_score: int, reason: str)`.
  - `config.Theme` (pydantic): `name: str`, `query: str | None`, `start: datetime | None` (alias `from`), `end: datetime | None` (alias `to`); методы `key() -> str` (первые 16 hex sha256 от `name|query|start|end`) и `interval_only -> bool`.
  - `config.TripConfig(album: str, themes: list[Theme])`.
  - `config.Price(input: float, output: float)`.
  - `config.ShortlistCfg(feed: int = 20, stories: int = 40)`.
  - `config.CloudCfg(model: str, effort: Literal["low","medium","high","xhigh","max"] = "medium", pricing: dict[str, Price], max_cost_usd_per_album: float = 5.0, candidates_per_theme: int = 100, preview_long_side_px: int = 1024, group_size: int = 20)`.
  - `config.Thresholds`:
    - из `docs/spike-findings.md`: `junk_blur`, `junk_dark_luma`, `junk_bright_luma`, `duplicate_distance`, `duplicate_window_s`, `theme_relevance`;
    - заданные здесь: `min_feed_score: int = 6`, `min_stories_score: int = 4`, `live_frame_margin: float = 0.15`.
  - `config.AppConfig(output_dir: Path = ~/Pictures/photo-chooser, shortlist: ShortlistCfg, cloud: CloudCfg, thresholds: Thresholds)`.
  - `config.load_app_config(path: Path | None = None) -> AppConfig`: по умолчанию `~/.config/photo-chooser/config.yaml`; если файла нет — значения по умолчанию.
  - `config.load_trip(path: Path) -> TripConfig`.
  - `config.theme_from_args(name, query, start, end) -> Theme`.
  - `config.ConfigError(Exception)`: сообщения на русском.
- `tests/conftest.py`: фабрика `make_asset(**overrides) -> Asset` с разумными значениями по умолчанию.

- [ ] **Step 1: Тесты.**

```python
def test_theme_requires_query_or_interval():      # Theme(name="X") -> ConfigError
def test_theme_interval_needs_both_bounds():      # только from -> ConfigError
def test_theme_interval_start_before_end():       # from >= to -> ConfigError
def test_theme_key_stable_and_sensitive():        # одинаковые поля -> одинаковый key; другой query -> другой
def test_load_trip_parses_aliases(tmp_path):      # YAML с from/to "2026-03-14 05:00" -> start == datetime(2026,3,14,5,0)
def test_app_config_defaults_when_file_missing(tmp_path):
    cfg = load_app_config(tmp_path / "nope.yaml")
    assert cfg.shortlist.feed == 20 and cfg.shortlist.stories == 40
    assert cfg.cloud.model == "claude-haiku-5-5" and cfg.cloud.max_cost_usd_per_album == 5.0
    assert cfg.cloud.pricing["claude-opus-5-5"].output == 20.00
def test_app_config_partial_override(tmp_path):   # YAML только с shortlist.feed: 10 -> stories остаётся 40
def test_asset_is_vertical():                     # 3024x4032 -> True; 4032x4032 -> False
```

- [ ] **Step 2:** `uv run pytest tests/test_config.py -v` → FAIL (модули не существуют).
- [ ] **Step 3:** Реализовать `models.py` и `config.py` по Interfaces. Добавить зависимости: `uv add pydantic pyyaml typer` и `uv add --dev pytest`.
- [ ] **Step 4:** `uv run pytest tests/test_config.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: config and domain models`.

---

### Task 3: Кэш (SQLite)

**Files:**
- Create: `src/photo_chooser/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `LocalFeatures`, `Judgment` (Task 2).
- Produces: `Cache(path: Path)`; по умолчанию путь `~/Library/Caches/photo-chooser/cache.sqlite`. Методы:
  - `get_features(uuid) -> LocalFeatures | None` / `put_features(f)`;
  - `get_embedding(uuid, encoder: str) -> np.ndarray | None` / `put_embedding(uuid, encoder, vec)`;
  - `get_judgment(uuid, theme_key, model, prompt_version) -> Judgment | None` / `put_judgments(items: list[Judgment], theme_key, model, prompt_version)`;
  - `add_spend(album_uuid, model, usd)` / `album_spend(album_uuid) -> float`.

- [ ] **Step 1: Тесты.**

```python
def test_features_roundtrip(tmp_path)
def test_embedding_roundtrip_float32(tmp_path)          # np.allclose после чтения
def test_judgment_keyed_by_model_and_prompt(tmp_path):
    c.put_judgments([j], "t1", "claude-haiku-5-5", "1")
    assert c.get_judgment(j.uuid, "t1", "claude-haiku-5-5", "1") == j
    assert c.get_judgment(j.uuid, "t1", "claude-opus-5-5", "1") is None
    assert c.get_judgment(j.uuid, "t1", "claude-haiku-5-5", "2") is None
def test_album_spend_accumulates(tmp_path):              # 0.10 + 0.25 -> 0.35 (approx); другой альбом -> 0.0
def test_cache_survives_reopen(tmp_path)
```

- [ ] **Step 2:** `uv run pytest tests/test_cache.py -v` → FAIL.
- [ ] **Step 3:** Реализовать на `sqlite3`. Таблицы: `features(uuid PK, json)`, `embeddings(uuid, encoder, vec BLOB, PK(uuid, encoder))`, `judgments(uuid, theme_key, model, prompt_version, json, PK(all four))`, `spend(album_uuid, model, usd, created_at)`. Каждая запись — сразу commit.
- [ ] **Step 4:** `uv run pytest tests/test_cache.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: sqlite cache for features, judgments and spend`.

---

### Task 4: Адаптер Photos (только чтение)

**Files:**
- Create: `src/photo_chooser/library/base.py`, `src/photo_chooser/library/osxphotos_library.py`
- Test: `tests/test_library.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: `Asset`, `AlbumRef` (Task 2).
- Produces:
  - `PhotosLibrary` (Protocol):
    - `albums() -> list[AlbumRef]`;
    - `find_album(title: str) -> AlbumRef`;
    - `assets(album: AlbumRef) -> list[Asset]` — только фото, видео исключены, общие альбомы не поддерживаются;
    - `export_originals(asset, dest_dir: Path) -> list[Path]` — 1 файл или 2 для пары RAW;
    - `export_live_video(asset, dest_dir: Path) -> Path`.
  - Исключения:
    - `AlbumNotFound(title, similar: list[str])` — `difflib.get_close_matches(n=5, cutoff=0.5)`;
    - `AlbumAmbiguous(title, matches: list[AlbumRef])` — несколько альбомов с одним именем;
    - `DownloadError(uuid, reason)` — после 3 попыток;
    - `PhotosAccessError` — с инструкцией, какой доступ выдать Терминалу.
  - `osxphotos_library.asset_from_photoinfo(p) -> Asset | None`: чистая функция маппинга. Возвращает `None` для видео. `preview_path` — наибольший существующий файл из `p.path_derivatives`, иначе `p.path`, если файл существует, иначе `None`. `tz_known` и ориентация — по полям из `docs/spike-findings.md`. `photos_score` приводится к 0..1 по диапазону `photos_score_range`; если оценка недоступна — `None`.
  - `tests/fakes.py: FakeLibrary(albums: list[AlbumRef], assets: dict[str, list[Asset]], files: dict[str, list[Path]], live: dict[str, Path], failing: set[str])`.

- [ ] **Step 1: Тесты** (stub-объекты `types.SimpleNamespace` вместо `PhotoInfo`).

```python
def test_mapping_skips_video():                       # ismovie=True -> None
def test_mapping_raw_pair_and_live()                  # has_raw=True -> has_raw_pair; live_photo=True -> is_live
def test_mapping_local_time_uses_photo_timezone():    # date=2026-03-14T07:00+02:00 -> local_time 07:00, tz_known True
def test_mapping_missing_timezone_uses_system_tz()    # сигнал из spike-findings -> tz_known False, local_time в системной tz
def test_mapping_no_preview_gives_none(tmp_path):     # derivatives не существуют, path None -> preview_path None
def test_find_album_not_found_lists_similar():        # "Египт 2026" -> AlbumNotFound, "Египет 2026" in e.similar
def test_find_album_ambiguous():                      # два AlbumRef с одним title -> AlbumAmbiguous
```

- [ ] **Step 2:** `uv run pytest tests/test_library.py -v` → FAIL.
- [ ] **Step 3:** Реализовать `OsxPhotosLibrary`:
  - `PhotosDB()` создаётся лениво. `PermissionError` или ошибка открытия базы → `PhotosAccessError`.
  - Экспорт через `PhotoExporter` с опциями из Task 1 Step 3, 3 попытки, затем `DownloadError`.
  - `find_album` / `AlbumNotFound` / `AlbumAmbiguous` реализовать один раз в `base.py` как функцию `resolve_album(title, albums)`. Её используют и `OsxPhotosLibrary`, и `FakeLibrary`.
- [ ] **Step 4:** `uv run pytest tests/test_library.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: read-only Photos adapter on osxphotos`.

---

### Task 5: Изображения, Vision и локальная оценка качества

**Files:**
- Create: `src/photo_chooser/images.py`, `src/photo_chooser/vision.py`, `src/photo_chooser/analysis/quality.py`
- Test: `tests/test_quality.py` (тестовые изображения генерируются в фикстурах через ImageIO/numpy; Vision настоящий)

**Interfaces:**
- Produces:
  - `images.load_cgimage(path: Path, long_side: int) -> CGImage` — `CGImageSourceCreateThumbnailAtIndex` с учётом ориентации.
  - `images.gray_array(img: CGImage) -> np.ndarray` — float32, значения 0..1.
  - `images.jpeg_bytes(path: Path, long_side: int, quality: float = 0.85) -> bytes`.
  - `images.write_tiff(img: CGImage, dest: Path, metadata_from: Path) -> None` — свойства EXIF/GPS/TIFF копируются из `CGImageSourceCopyPropertiesAtIndex(metadata_from)`.
  - `vision.VisionResult(aesthetic: float | None, is_utility: bool, face_quality: float | None, feature_print: np.ndarray)`; `aesthetic` = `(overallScore + 1) / 2`.
  - `vision.analyze(img: CGImage) -> VisionResult`.
  - `quality.sharpness(gray) -> float` — дисперсия лапласиана 3×3.
  - `quality.mean_luma(gray) -> float`.
  - `quality.compute_features(asset: Asset, long_side: int = 1024) -> LocalFeatures`.
  - `quality.percentile_ranks(values: list[float]) -> list[float]` — значения 0..1.
  - `quality.local_quality(f: LocalFeatures, photos_score: float | None, sharpness_rank: float) -> float`. Веса: aesthetic 0.4, sharpness_rank 0.3, photos_score 0.2, face_quality 0.1. Отсутствующие компоненты (`None`) исключаются, оставшиеся веса нормируются до суммы 1.
  - `quality.feature_distance(a: bytes, b: bytes) -> float` — L2 (если spike подтвердил совпадение с `computeDistance`).

- [ ] **Step 1: Тесты.**

```python
def test_blurred_image_less_sharp(tmp_path):          # шум vs тот же шум после гаусс-размытия
def test_black_image_low_luma(tmp_path):              # mean_luma < 0.03
def test_feature_distance_self_zero(tmp_path):        # == 0; другое изображение -> > 0
def test_local_quality_renormalizes_missing():
    f = make_features(aesthetic=1.0, face_quality=None)
    assert local_quality(f, photos_score=None, sharpness_rank=1.0) == pytest.approx(1.0)
def test_local_quality_weights():                     # aesthetic=0, sharp=1, photos=1, face=1 -> 0.6
def test_percentile_ranks_bounds():                   # [3,1,2] -> [1.0, 0.0, 0.5]
def test_write_tiff_copies_date_and_gps(tmp_path):    # JPEG с EXIF DateTimeOriginal + GPS -> TIFF содержит те же значения
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
- Consumes: `Asset`, `LocalFeatures`, `Thresholds`, `feature_distance`, `local_quality`.
- Produces:
  - `junk.junk_reason(asset: Asset, f: LocalFeatures, th: Thresholds) -> str | None`. Порядок проверок и тексты причин: `"скриншот"` (`asset.is_screenshot`), `"утилитарный кадр"` (`f.is_utility`), `"смаз"` (`sharpness < junk_blur`), `"слишком тёмный"` (`mean_luma < junk_dark_luma`), `"пересвечен"` (`mean_luma > junk_bright_luma`).
  - `dedup.cluster(items: list[tuple[Asset, LocalFeatures]], th: Thresholds) -> list[list[str]]` — кластеры uuid.
  - `dedup.representatives(clusters: list[list[str]], quality: dict[str, float]) -> list[str]` — лучший из каждого кластера.

- [ ] **Step 1: Тесты.**

```python
def test_junk_reasons_order()                          # скриншот с плохой резкостью -> "скриншот"
def test_good_photo_not_junk()
def test_cluster_requires_both_time_and_similarity():
    # A,B: 10 с, distance < th -> один кластер; C: похож на A, но через 2 часа -> отдельно; D: рядом по времени, но не похож -> отдельно
def test_cluster_is_transitive()                       # A~B, B~C (в окне) -> {A,B,C}
def test_representative_is_best_quality()
```

- [ ] **Step 2:** `uv run pytest tests/test_junk_dedup.py -v` → FAIL.
- [ ] **Step 3:** Реализовать. `cluster`: сортировка по `local_time`, скользящее окно `duplicate_window_s`, union-find по парам с `feature_distance ≤ duplicate_distance`.
- [ ] **Step 4:** `uv run pytest tests/test_junk_dedup.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: junk filter and duplicate clustering`.

---

### Task 7: Фильтр темы и кандидаты

**Files:**
- Create: `src/photo_chooser/analysis/encoder.py`, `src/photo_chooser/analysis/theme_filter.py`
- Test: `tests/test_theme_filter.py`; `FakeEncoder` в `tests/fakes.py`

**Interfaces:**
- Consumes: `Asset`, `Theme`, `Cache`, `Thresholds`.
- Produces:
  - `encoder.TextImageEncoder` (Protocol): `name: str`, `embed_images(paths: list[Path]) -> np.ndarray` (n×d, L2-нормированные), `embed_text(text: str) -> np.ndarray` (d,).
  - `encoder.make_encoder() -> TextImageEncoder` — модель из `docs/spike-findings.md`. Тяжёлые зависимости ставятся через `uv add` в этом задании.
  - `theme_filter.in_interval(asset: Asset, theme: Theme) -> bool` — границы включительно, сравнение `asset.local_time` с наивными `start/end`; тема без интервала → `True`.
  - `theme_filter.relevance(enc, cache: Cache, assets: list[Asset], query: str) -> dict[str, float]` — косинус; эмбеддинги изображений берутся из кэша или считаются батчами и кладутся в кэш.
  - `theme_filter.candidates(assets: list[Asset], quality: dict[str, float], theme: Theme, rel: dict[str, float] | None, th: Thresholds, limit: int) -> list[Asset]` — интервал → релевантность (≥ `theme_relevance`; при `rel=None` фильтр не применяется) → сортировка по `quality` по убыванию → первые `limit`.

- [ ] **Step 1: Тесты.**

```python
def test_in_interval_inclusive_bounds()                # ровно from и ровно to -> True
def test_in_interval_uses_local_wall_clock()           # local_time 23:30 14 марта входит в [14.03 00:00, 14.03 23:59]
def test_relevance_uses_cache(tmp_path):               # второй вызов не вызывает FakeEncoder.embed_images
def test_candidates_interval_only_ranks_by_quality()   # rel=None, limit=2 -> два лучших по quality внутри интервала
def test_candidates_query_threshold()                  # rel ниже порога отсекается
def test_candidates_query_and_interval_both_required()
```

- [ ] **Step 2:** `uv run pytest tests/test_theme_filter.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces.
- [ ] **Step 4:** `uv run pytest tests/test_theme_filter.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: theme filter by interval and text-image relevance`.

---

### Task 8: Оценка Claude, стоимость и бюджет

**Files:**
- Create: `src/photo_chooser/judge/cost.py`, `src/photo_chooser/judge/prompt.py`, `src/photo_chooser/judge/claude_judge.py`
- Test: `tests/test_cost.py`, `tests/test_judge.py`; `FakeClient` в `tests/fakes.py`

**Interfaces:**
- Consumes: `Asset`, `Theme`, `Judgment`, `Cache`, `AppConfig.cloud`, `images.jpeg_bytes`.
- Produces:
  - `cost.image_tokens(width: int, height: int, long_side: int) -> int` — масштаб до `long_side`, затем `ceil(w*h/IMAGE_TOKEN_DIVISOR)`. Константы `IMAGE_TOKEN_DIVISOR`, `PROMPT_OVERHEAD_TOKENS`, `EST_OUTPUT_TOKENS_PER_PHOTO` берутся из spike-findings.
  - `cost.estimate_usd(assets: list[Asset], cfg: CloudCfg, model: str) -> float`.
  - `cost.usage_usd(input_tokens: int, output_tokens: int, price: Price) -> float`.
  - `cost.price_for(cfg: CloudCfg, model: str) -> Price` — нет цены → `UnknownModelPrice(model)`.
  - `cost.check_budget(spent: float, estimate: float, limit: float) -> None` — превышение → `BudgetExceeded(spent, estimate, limit)`.
  - `prompt.PROMPT_VERSION = "1"`; `prompt.SYSTEM_PROMPT` содержит:
    - роль — фоторедактор для Instagram;
    - рубрику `feed_score` 1–10: композиция, свет, визуальная сила;
    - рубрику `stories_score` 1–10: люди и эмоции, интересное в контексте темы, о чём стоит рассказать; изъяны допустимы;
    - `matches_theme`; для темы только с интервалом всегда `true`;
    - указание писать `reason` одним предложением на языке темы;
    - указание оценивать каждый кадр с `id` из подписи.
  - `claude_judge.GroupItem(BaseModel)`: `id: str`, `matches_theme: bool`, `feed_score: int`, `stories_score: int`, `reason: str`; `GroupJudgment(BaseModel)`: `items: list[GroupItem]`.
  - `claude_judge.make_groups(cands: list[Asset], quality: dict[str, float], size: int) -> list[list[Asset]]` — раздача по кругу в порядке убывания quality в `ceil(n/size)` групп.
  - `claude_judge.ClaudeJudge(client, model: str, effort: str, preview: Callable[[Asset], bytes])`, метод `judge_group(theme: Theme, group: list[Asset]) -> GroupResult(judgments: list[Judgment], unjudged: list[str], refused: bool, input_tokens: int, output_tokens: int)`.
  - `claude_judge.judge_theme(judge, cache, theme, cands, quality, cfg: AppConfig, album_uuid: str, model: str) -> JudgeOutcome(judgments: dict[str, Judgment], unjudged: list[str], usd: float)`. Сначала берёт оценки из кэша; запросы — только для некэшированных. После **каждой** группы сразу вызывает `cache.put_judgments` и `cache.add_spend`.

- [ ] **Step 1: Тесты `tests/test_cost.py`.**

```python
def test_image_tokens_scales_to_long_side():           # 4032x3024 при long_side=1024 -> ceil(1024*768/divisor)
def test_estimate_includes_overhead_and_output()
def test_usage_usd_haiku():                            # 1_000_000 in + 1_000_000 out -> 0.60
def test_price_for_unknown_model_raises()
def test_check_budget():                               # spent 4.0 + est 1.5, limit 5 -> BudgetExceeded; limit 6 -> ok
```

- [ ] **Step 2: Тесты `tests/test_judge.py`** (`FakeClient` возвращает заданный `parsed_output`/`stop_reason`/`usage` и считает вызовы).

```python
def test_make_groups_spreads_quality()                 # 40 кандидатов, size 20 -> в каждой группе есть и топ, и хвост
def test_judge_group_maps_short_ids_and_clamps():      # id "p01".. -> uuid; feed_score 12 -> 10, 0 -> 1
def test_judge_group_missing_ids_are_unjudged()
def test_judge_group_refusal():                        # stop_reason="refusal" -> refused True, все unjudged, usage учтён
def test_judge_theme_uses_cache_and_skips_api(tmp_path)   # все в кэше -> 0 вызовов, usd 0
def test_interrupted_run_does_not_pay_twice(tmp_path):
    # FakeClient падает на 2-й группе из 3 -> исключение; spend == стоимость 1-й группы
    # повторный judge_theme с рабочим клиентом -> вызовов 2 (группы 2 и 3), album_spend == сумма трёх групп
def test_request_shape(tmp_path):
    # client.messages.parse вызван с model, output_format=GroupJudgment, output_config={"effort": effort},
    # system=SYSTEM_PROMPT, в content для каждой картинки: text "id: pNN" + image base64 image/jpeg
def test_opus_and_sonnet_use_server_side_fallbacks()   # для claude-opus-5-5 / claude-sonnet-5-5:
    # client.beta.messages.parse(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default"); для haiku — без них
```

- [ ] **Step 3:** `uv run pytest tests/test_cost.py tests/test_judge.py -v` → FAIL.
- [ ] **Step 4:** Реализовать:
  - Клиент — `anthropic.Anthropic(max_retries=4)` из окружения.
  - Вызов: `messages.parse(model=…, max_tokens=16000, system=SYSTEM_PROMPT, messages=[…], output_format=GroupJudgment, output_config={"effort": effort})`. Параметр `thinking` не передаётся: на этих моделях он адаптивный по умолчанию.
  - Для моделей из `FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}` — `client.beta.messages.parse` с параметрами из теста.
  - Проверка: если вход группы ≥ 100 000 токенов, бросать `ValueError`: группа превышает ценовой порог Haiku.
- [ ] **Step 5:** `uv run pytest tests/test_cost.py tests/test_judge.py -v` → PASS.
- [ ] **Step 6: Commit** — `feat: Claude judge with structured output, caching and budget guard`.

---

### Task 9: Раскладка на ленту и stories

**Files:**
- Create: `src/photo_chooser/select.py`
- Test: `tests/test_select.py`

**Interfaces:**
- Consumes: `Asset`, `Judgment`, `LocalFeatures`, `AppConfig`.
- Produces:
  - `select.Pools(feed: list[Scored], stories: list[Scored])`, где `Scored(asset: Asset, score: float, reason: str)`. Пулы упорядочены и не обрезаны: обрезку и замену выполняет Task 11.
  - `select.pools_from_judgments(cands: list[Asset], judgments: dict[str, Judgment], quality: dict[str, float], th: Thresholds, interval_only: bool) -> Pools`:
    - **feed** — оценённые, у которых `matches_theme` или `interval_only` и `feed_score ≥ min_feed_score`; сортировка по `feed_score` по убыванию, при равенстве — по quality.
    - **stories** — оценённые подходящие с `stories_score ≥ min_stories_score`; сначала все вертикальные (`stories_score` по убыванию, затем quality), потом остальные в том же порядке.
    - Один снимок может быть в обоих пулах; исключение пересечений делает Task 11.
  - `select.pools_local(cands: list[Asset], quality: dict[str, float], feats: dict[str, LocalFeatures]) -> Pools` (FR-10.2):
    - feed — по quality;
    - stories — тот же порядок, вертикальные первыми;
    - reason — `"эстетика 0.72 · резкость 0.81 · лица 0.65"` (отсутствующие компоненты опускаются).

- [ ] **Step 1: Тесты.**

```python
def test_feed_threshold_and_order()
def test_feed_excludes_not_matching_theme()
def test_interval_only_ignores_matches_theme()
def test_stories_verticals_first():                    # горизонтальный с 9 после вертикального с 5
def test_stories_lower_threshold()                     # stories_score 4 проходит, feed_score 4 — нет
def test_square_counts_as_horizontal()
def test_unjudged_candidates_excluded()
def test_local_pools_reason_format()
```

- [ ] **Step 2:** `uv run pytest tests/test_select.py -v` → FAIL.
- [ ] **Step 3:** Реализовать.
- [ ] **Step 4:** `uv run pytest tests/test_select.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: feed and stories pools`.

---

### Task 10: Выбор кадра Live Photo

**Files:**
- Create: `src/photo_chooser/analysis/live.py`
- Test: `tests/test_live.py` (короткое `.mov` генерируется в фикстуре через `AVAssetWriter` или берётся из `tests/data/`; ключевой снимок — PNG)

**Interfaces:**
- Consumes: `images.load_cgimage`, `images.gray_array`, `vision.analyze`, `quality.sharpness`.
- Produces:
  - `live.sample_frames(video: Path, n: int) -> list[CGImage]` — `n` из spike-findings.
  - `live.frame_scores(images: list[CGImage], long_side: int) -> list[float]`. Все изображения приводятся к общей длинной стороне = min(ключевой, кадр). Score = 0.5·aesthetic + 0.3·sharpness/max(sharpness) + 0.2·face_quality; отсутствующие компоненты нормируются, как в `local_quality`.
  - `live.choose(key_score: float, frame_scores: list[float], margin: float) -> int | None` — индекс кадра, если `max(frame) ≥ key + margin`, иначе `None`.
  - Использование (Task 11): `s = frame_scores([key] + frames, long_side)`, затем `choose(s[0], s[1:], margin)`. Ключевой снимок оценивается вместе с кадрами, чтобы нормировка резкости была общей.

- [ ] **Step 1: Тесты.**

```python
def test_choose_key_when_within_margin():              # key .70, лучший кадр .80, margin .15 -> None
def test_choose_frame_when_clearly_better():           # key .50, кадры [.40, .70] -> 1
def test_sample_frames_count(tmp_path)                 # n=5 -> 5 изображений
def test_frame_scores_sharp_beats_blurred(tmp_path)
```

- [ ] **Step 2:** `uv run pytest tests/test_live.py -v` → FAIL.
- [ ] **Step 3:** Реализовать через `AVAssetImageGenerator` с `appliesPreferredTrackTransform=True` и нулевыми допусками по времени.
- [ ] **Step 4:** `uv run pytest tests/test_live.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: best frame selection for Live Photos`.

---

### Task 11: Экспорт и отчёт

**Files:**
- Create: `src/photo_chooser/export.py`, `src/photo_chooser/report.py`
- Test: `tests/test_export_report.py`

**Interfaces:**
- Consumes: `PhotosLibrary`, `Pools`/`Scored` (Task 9), `live.*` (Task 10), `images.write_tiff`, `Theme`.
- Produces:
  - `export.safe_name(s: str) -> str`:
    - `/ \ : * ? " < > |` и управляющие символы заменяются на `_`;
    - пробелы по краям обрезаются;
    - результат не длиннее 100 символов;
    - пустая строка → `"_"`.
  - `export.ensure_outside_library(path: Path) -> None` — если путь или любой родитель (после `resolve()`) оканчивается на `.photoslibrary`, бросает `ConfigError`.
  - `export.new_run_dir(output_dir: Path, album_title: str, now: datetime) -> Path` — `<output>/<safe album>/<YYYY-MM-DD_HHMM>`; если такая директория есть — суффиксы `_2`, `_3`…
  - `export.ExportedItem(list_name: str, rank: int, asset: Asset, files: list[str], score: float, reason: str, notes: list[str])`. Заметки:
    - `"пара RAW"`;
    - `"кадр из Live Photo, разрешение ниже оригинала"`;
    - `"видео Live Photo не скачалось — взят ключевой снимок"`;
    - `"нет часового пояса"`.
  - `export.assemble(pools: Pools, n_feed: int, n_stories: int, try_export: Callable[[Scored, str, int], ExportedItem | None]) -> Assembled(feed: list[ExportedItem], stories: list[ExportedItem], failed: list[tuple[Asset, str]], shortfall_feed: int, shortfall_stories: int)`. Алгоритм:
    - идти по `pools.feed`, пока не экспортировано `n_feed`;
    - затем идти по `pools.stories`, пропуская уже экспортированные и упавшие, пока не экспортировано `n_stories`;
    - ранг — порядковый номер успешного экспорта в своём списке.
  - `export.export_asset(library, scored, list_name, rank, theme_dir, tmp, cfg) -> ExportedItem | None`:
    - имена файлов `f"{rank:02d}_{original_name}"`;
    - Live Photo: видео → `live.choose` → оригинал или `…_live-frame.tiff`;
    - `DownloadError` видео → оригинал ключевого снимка и заметка;
    - `DownloadError` оригинала → `None`.
  - `report.write_report(path: Path, theme: Theme, header: ReportHeader, assembled: Assembled, stats: ThemeStats) -> None`:
    - `ReportHeader(model: str | None, effort: str | None, prompt_version: str | None, run_at: datetime)`; `model=None` → «без облака»;
    - `ThemeStats(read, junk, duplicates_collapsed, no_preview, in_theme, sent_to_cloud, from_cache, unjudged, usd_run, usd_album_total)`;
    - таблица: «Ранг | Файл(ы) | Список | Оценка | Ориентация | Пометки | Причина»;
    - блок сводки, недобор, пропущенные снимки с причиной.

- [ ] **Step 1: Тесты.**

```python
def test_safe_name_cases():                            # "Гиза/Каир: день 1 " -> "Гиза_Каир_ день 1"; "" -> "_"
def test_ensure_outside_library(tmp_path)              # .../Photos Library.photoslibrary/x -> ConfigError
def test_new_run_dir_never_overwrites(tmp_path)        # существующий 2026-11-01_1530 -> 2026-11-01_1530_2
def test_assemble_disjoint_and_ranked()                # снимок в обоих пулах попадает только в feed; ранги 1..n без дыр
def test_assemble_replaces_failed_export()             # 2-й в feed падает -> его место занимает следующий, failed содержит его
def test_assemble_shortfall()                          # пулы меньше лимитов -> shortfall_feed/stories > 0
def test_export_raw_pair_both_files(tmp_path)          # FakeLibrary отдаёт DNG+JPG -> "01_IMG_1.DNG", "01_IMG_1.JPG"
def test_export_live_video_failure_falls_back_to_key(tmp_path)
def test_export_live_frame_writes_tiff(tmp_path)       # choose -> индекс -> "…_live-frame.tiff" + заметка
def test_report_contains_header_table_and_summary(tmp_path)   # «без облака», строки таблицы, «Недобор: лента 3»
```

- [ ] **Step 2:** `uv run pytest tests/test_export_report.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по Interfaces.
- [ ] **Step 4:** `uv run pytest tests/test_export_report.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: shortlist export and markdown report`.

---

### Task 12: Конвейер и CLI

**Files:**
- Create: `src/photo_chooser/pipeline.py`, `src/photo_chooser/cli.py`
- Modify: `pyproject.toml` — `[project.scripts] photo-chooser = "photo_chooser.cli:app"`
- Test: `tests/test_pipeline.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: всё из Task 2–11.
- Produces:
  - `pipeline.RunOptions(dry_run: bool, no_cloud: bool, model: str | None, max_cost: float | None)`.
  - `pipeline.prepare_album(library, cache, album: AlbumRef, cfg) -> AlbumState(assets, features: dict[str, LocalFeatures], quality: dict[str, float], junk: dict[str, str], no_preview: list[str], duplicates_collapsed: int, pool: list[Asset])`. Снимки с `preview_path=None` идут в `no_preview` и дальше не анализируются.
  - `pipeline.run(trip: TripConfig, cfg, opts, library, cache, encoder_factory: Callable[[], TextImageEncoder], judge_factory: Callable[[str, str], ClaudeJudge], now: datetime) -> RunResult(run_dir: Path | None, themes: list[ThemeOutcome], estimate_usd: float)`:
    - `judge_factory(model, effort)` создаёт `ClaudeJudge` с превью `images.jpeg_bytes(a.preview_path, cfg.cloud.preview_long_side_px)`;
    - `ThemeOutcome(theme: Theme, report_path: Path | None, assembled: Assembled | None, error: str | None)`.

    Порядок:
    1. `ensure_outside_library(cfg.output_dir)`.
    2. `find_album`, `prepare_album`.
    3. Кандидаты по всем темам. Encoder создаётся, только если в какой-то теме есть `query`.
    4. Если не `no_cloud`: проверка цены модели, общая оценка некэшированных кандидатов, `check_budget(cache.album_spend(...), estimate, opts.max_cost or cfg.cloud.max_cost_usd_per_album)` — **до** первого вызова.
    5. `dry_run` → вернуть оценку без вызовов и экспорта.
    6. По каждой теме: оценка (или `pools_local`) → `assemble` с `export_asset` → `write_report`. Исключение API внутри темы → тема помечается упавшей, остальные продолжаются.
  - `cli.app` (typer):
    - `albums` — строки `«<title> — <count> фото [<folder>]»`;
    - `run [TRIP] --album --theme --query --from --to --dry-run --no-cloud --model --max-cost`. Нужен либо `TRIP`, либо `--album` + `--theme`; `--from`/`--to` в формате `YYYY-MM-DD HH:MM`.
  - Коды выхода: 0 — успех, 2 — `ConfigError`/`UnknownModelPrice`, 3 — `BudgetExceeded`, 4 — `AlbumNotFound`/`AlbumAmbiguous`, 5 — `PhotosAccessError`, 1 — прочее. Сообщения на русском, для `AlbumNotFound` выводятся похожие имена.

- [ ] **Step 1: Тесты `tests/test_pipeline.py`** (`FakeLibrary`, `FakeEncoder`, `FakeClient`, `tmp_path`).

```python
def test_no_cloud_end_to_end(tmp_path)                 # скриншот и дубль не в выдаче; feed/ и stories/ созданы; report.md есть; FakeClient не вызван
def test_assets_without_preview_skipped_and_counted(tmp_path)
def test_zero_candidates_no_api_call(tmp_path)         # query ничего не нашёл -> 0 вызовов, report с недобором 20/40
def test_budget_checked_before_any_call(tmp_path)      # album_spend 4.9 + оценка > 5 -> BudgetExceeded, 0 вызовов, run_dir не создан
def test_max_cost_overrides_limit(tmp_path)
def test_dry_run_no_calls_no_files(tmp_path)           # estimate_usd > 0, run_dir None
def test_unknown_model_price_stops_before_call(tmp_path)
def test_theme_failure_does_not_stop_other_themes(tmp_path)
def test_model_override_keys_cache_separately(tmp_path)  # haiku, затем --model opus -> второй прогон вызывает API заново
```

- [ ] **Step 2: Тесты `tests/test_cli.py`** (`typer.testing.CliRunner`, фабрики подменяются monkeypatch).

```python
def test_run_requires_trip_or_album_and_theme()        # exit 2
def test_album_not_found_exit_4_lists_similar()
def test_budget_exceeded_exit_3()
def test_run_with_args_builds_single_theme()
```

- [ ] **Step 3:** `uv run pytest tests/test_pipeline.py tests/test_cli.py -v` → FAIL.
- [ ] **Step 4:** Реализовать по Interfaces.
- [ ] **Step 5:** `uv run pytest -v` → PASS, весь набор.
- [ ] **Step 6: Commit** — `feat: pipeline orchestration and CLI`.

---

### Task 13: Интеграционные тесты на реальной Photos

**Files:**
- Create: `tests/integration/test_album.py`, `tests/integration/README.md` (как собрать тестовый альбом)
- Modify: `pyproject.toml` — маркеры `integration`, `cloud`; по умолчанию `-m "not integration"`

**Interfaces:**
- Consumes: CLI и `OsxPhotosLibrary`. Тестовый альбом (~30 фото) пользователь собирает вручную по README. Его содержимое:
  - серия из 5 дублей;
  - скриншот;
  - смазанный кадр;
  - Live Photo;
  - пара RAW+JPEG;
  - кадры двух тем: например, «кофе» и «улица».

  Имя альбома передаётся через `PHOTO_CHOOSER_IT_ALBUM`.

- [ ] **Step 1: Тесты.**

```python
@pytest.mark.integration
def test_library_unchanged_after_run(tmp_path):        # NFR-1: множество uuid, count и date_modified (osxphotos) до == после
@pytest.mark.integration
def test_no_cloud_run_on_test_album(tmp_path):         # нет скриншота; из серии максимум 1; RAW-пара — 2 файла; Live — 1 файл
@pytest.mark.integration
@pytest.mark.cloud
def test_haiku_run_on_test_album(tmp_path):            # одна тема, report.md с моделью claude-haiku-5-5 и стоимостью > 0
```

- [ ] **Step 2:** `PHOTO_CHOOSER_IT_ALBUM="photo-chooser-test" uv run pytest -m integration -v` → PASS. Тест `cloud` запускается только при наличии ключа.
- [ ] **Step 3: Commit** — `test: integration tests on a real Photos album`.

---

### Task 14: Приёмка на прошлой поездке и выбор прод-модели (вместе с пользователем, без кода)

- [ ] **Step 1:** Альбом прошлой поездки, `trip.yaml` с 2–3 темами разного типа (SPEC §12). Последовательно выполнить: `--no-cloud`, `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`. Лимит поднимается через `--max-cost` с согласия пользователя.
- [ ] **Step 2:** Записать в `docs/acceptance.md` по каждому прогону:
  - время первого прогона и повторного прогона той же темы (NFR-4);
  - стоимость (NFR-2);
  - оценку пользователя: сколько кадров из 20 ленты и 40 stories пригодны (NFR-3).
- [ ] **Step 3:** Пользователь выбирает модель. Зафиксировать `cloud.model` и `cloud.effort` в `~/.config/photo-chooser/config.yaml` и результат в `docs/acceptance.md`.
- [ ] **Step 4: Commit** — `docs: acceptance results and production model choice`.
