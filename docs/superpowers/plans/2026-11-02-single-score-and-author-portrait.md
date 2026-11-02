# Версия 0.4: одна сила кадра, пост и stories из лучших оставшихся, портрет автора — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** вместо оценок «лента» и «stories» обе ступени ставят одну силу кадра. Сборка кладёт сильнейший сюжет в пост — одну карусель до 5 кадров, — а следующие по силе кадры и «интересное» — в stories в порядке съёмки (D13). Постановочный портрет путешественника на фоне места поднимает оценку и может быть обложкой (D14).

**Architecture:** поля оценок меняются в моделях (`Screening`, `Judgment`, `ShotFacts`), схемах ответа и промптах. Конвейер остаётся тем же: первая ступень → вторая ступень → сборка → экспорт.
- Новое в `select.py`:
  - окно сборки дополняется «интересными» кадрами сверх 30;
  - stories упорядочиваются по времени съёмки в трёх местах — проверка ответа сборки, раскладка по оценкам, раскладка без облака.
- Порог `min_score` заменяет пару `min_feed_score` / `min_stories_score`. На старые ключи — ошибка конфигурации, как это уже сделано для `shortlist`.

**Tech Stack:** Python 3.12, uv, pytest, pyobjc, anthropic SDK (`messages.create` + `output_config.format`), pydantic v2, typer.

**Spec:** [SPEC.md](../../../SPEC.md) версии 0.4 (PR #16): FR-3 п.5, п.7, п.8; FR-4.1–4.4; FR-6.1, FR-6.3; FR-7.1; FR-8; FR-10.2; FR-11.2, FR-11.4; §9 «Выход»; §10; D13, D14. Процесс — [DEVELOPMENT.md](../../../DEVELOPMENT.md).

## Global Constraints

- **Ветки и PR.** Одна ветка `feat/single-score-and-author-portrait` от `main`, один PR после Task 4. Мердж сквошем делает пользователь.
- **Тесты.** TDD на каждом шаге: тест → FAIL → код → PASS. Перед каждым коммитом `uv run pytest` зелёный. Только `FakeClient`, без платных вызовов.
- **Photos только на чтение** (NFR-1).
- **Значения по умолчанию:**
  - `posts.carousels` = 1, `posts.carousel_size` = 5, `posts.stories` = 9;
  - `thresholds.min_score` = 5;
  - `cloud.compose_candidates` = 30; «интересных» сверх окна — не больше `posts.stories`;
  - запасных на слот — до 2 (`MAX_ALTERNATIVES`).
- **Версии промптов:** `screen-3`, `judge-6`, `compose-3`. У промпта профиля стиля версии нет: профиль пересобирает пользователь командой `style`.
- **Тексты.** CLI, отчёт, названия сюжетов, подсказки, ошибки конфигурации — на русском. Промпты — на английском.

## Review Focus

1. **Модель ставит «интересное» почти всем кадрам.** Сверх окна сборки добавляется не больше `posts.stories` «интересных», запрос не раздувается. Тест в Task 3.
2. **У нескольких stories одинаковое время съёмки** (серия, кадры одной секунды). Порядок устойчивый — как вернула модель, запуск не падает. Тест в Task 3.
3. **Все кандидаты темы слабее `min_score`, но есть «интересные».** «Интересные» всё равно попадают в сборку (FR-4.1: «не ниже `min_score` или пометка «интересное»»). Тест в Task 3.
4. **Модель вернула силу 0 или 11.** Оценка обрезается до 1–10, как сейчас обрезаются старые. Тест в Task 1 (первая ступень) и Task 2 (вторая).
5. **Сборка не удалась, а сильных кадров нет** — все ниже `min_score`. Раскладка по оценкам даёт пустую карусель и пустые stories, отчёт пишется, тема не падает. Тест в Task 2.

---

### Task 1: Первая ступень — одна черновая оценка (screen-3)

**Files:**
- Modify: `src/photo_chooser/models.py` (`Screening`), `src/photo_chooser/judge/prompts.py` (`SCREEN_PROMPT_VERSION`, `SCREEN_SYSTEM_PROMPT`), `src/photo_chooser/judge/stages.py` (`ScreenItem`, `ScreenRunner.run_group`), `src/photo_chooser/select.py` (`screen_candidates`), `src/photo_chooser/pipeline.py` (`_draft`)
- Test: `tests/test_stages.py`, `tests/test_select.py`, `tests/test_cache.py`; в остальных тестах фикстуры `Screening(...)` и ответы первой ступени переводятся на `score`

**Interfaces:**
- Produces:
  - `Screening(uuid: str, relevance: dict[str, int], score: int)` — поля `feed_score` и `stories_score` удалены;
  - `ScreenItem(id: str, matches: list[ScreenMatch], score: int)`;
  - `SCREEN_PROMPT_VERSION = "screen-3"`;
  - `screen_candidates(...)` — сигнатура прежняя, ранжирование `(relevance, score, quality)`;
  - `pipeline._draft(s: Screening | None) -> float` возвращает `float(s.score)` или `0.0`.
- Текст `SCREEN_SYSTEM_PROMPT`: две строки `feed_score` и `stories_score` заменяются одной, слово в слово:

  ```
  - score (1-10): strength of the photo as an Instagram post of the trip: composition, light, visual impact. A posed portrait of the traveller — the main subject posing in front of the place — is a typical cover of the author's feed and a strength; crowds and passers-by that distract from the place are a weakness.
  ```

  Остальные строки промпта не меняются. Фразы «Photos without people are often the strongest feed posts» в промпте больше нет.

- [ ] **Step 1: Тесты.**
  - `test_screen_schema_single_score`: `set(ScreenItem.model_fields) == {"id", "matches", "score"}`.
  - `test_screen_prompt_portrait_without_people_bias`: `"posed portrait of the traveller" in SCREEN_SYSTEM_PROMPT`, `"without people" not in SCREEN_SYSTEM_PROMPT`, `"feed_score" not in SCREEN_SYSTEM_PROMPT`.
  - `test_screen2_cache_not_reused`: скрининг в кэше под `"screen-2"` → `run_screen` делает 1 вызов.
  - `test_screen_score_clamped`: ответ модели `score=0` и `score=11` → `Screening.score` равен 1 и 10.
  - `test_screen_candidates_rank_by_draft_score`: два снимка с релевантностью 8, `score` 5 и 9 → первым идёт снимок с 9.
  - `test_screening_roundtrip` в `tests/test_cache.py` — с полем `score`.
- [ ] **Step 2:** `uv run pytest tests/test_stages.py tests/test_select.py tests/test_cache.py -k "screen or screening" -v` → FAIL.
- [ ] **Step 3: Код.** Поменять модель, схему, `convert` в `ScreenRunner.run_group` (`score=_clamp(item.score)`), промпт и версию, `screen_candidates.rank`, `_draft`. Фикстуры других тестов перевести на `score`.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит.**

  ```bash
  git add src/photo_chooser tests
  git commit -m "feat: одна черновая оценка первой ступени (screen-3, D13)"
  ```

### Task 2: Вторая ступень — сила кадра, «интересное», портрет автора (judge-6) и порог `min_score`

**Files:**
- Modify:
  - `src/photo_chooser/models.py` (`Judgment`);
  - `src/photo_chooser/judge/prompts.py` (`JUDGE_PROMPT_VERSION`, `JUDGE_SYSTEM_PROMPT`, `STYLE_BLOCK`);
  - `src/photo_chooser/judge/stages.py` (`JudgeItem`, `JudgeRunner.run_group`, подпись кандидата в `ComposeRunner.run`, `compose_input_fingerprint`);
  - `src/photo_chooser/select.py` (`ShotFacts`, `shot_facts`, `local_facts`, `compose_candidates`, `fallback_posts`);
  - `src/photo_chooser/config.py` (`Thresholds`);
  - `src/photo_chooser/report.py` (`SCORES_NOTE`, `_scores`, заголовки таблиц);
  - `src/photo_chooser/pipeline.py` — места, где читаются старые поля.
- Test: `tests/test_stages.py`, `tests/test_posts.py`, `tests/test_compose.py`, `tests/test_config.py`, `tests/test_export_report.py`, `tests/test_cache.py`; фикстуры `Judgment(...)` и `Thresholds(...)` в остальных тестах переводятся на новые поля

**Interfaces:**
- Consumes: `Screening.score` из Task 1.
- Produces:
  - `Judgment(uuid: str, matches_theme: bool, score: int, reason: str, interesting: bool = False, crop_feed: str = "", crop_stories: str = "")`;
  - `JudgeItem(id, matches_theme: bool, score: int, interesting: bool, reason: str, crop_feed: str, crop_stories: str)`;
  - `ShotFacts(score: float, interesting: bool, reason: str, crop_feed: str, crop_stories: str)`. `local_facts` ставит `score` = локальная оценка, `interesting=False`;
  - `Thresholds.min_score: int = 5`; поля `min_feed_score` и `min_stories_score` удалены. `Thresholds` получает `model_validator(mode="before")` по образцу `StyleCfg._renamed_keys`. На любой из двух старых ключей он поднимает `ConfigError` с текстом: `thresholds.min_feed_score и min_stories_score заменены на thresholds.min_score (версия 0.4): одна сила кадра для поста и stories`;
  - `compose_candidates(cands, judgments, quality, th, interval_only, limit)` — фильтр `matches_theme` (кроме тем-интервалов) и `score >= th.min_score or interesting`; сортировка `(score, quality)` по убыванию; первые `limit`. Добавление «интересных» сверх окна — в Task 3;
  - `fallback_posts(...)` — сигнатура прежняя. Карусель — до `carousel_size` сильнейших с `score >= min_score`. Stories — следующие по `(score, quality)` с `score >= min_score`, до `posts.stories`, вид `"best"`;
  - подпись кандидата сборки: `f"id: c{i:02d} ({orientation}) · сила {score}"`, затем `" · интересное"` только при `interesting`, затем `f" · «{reason}»"`;
  - отпечаток входа сборки: `f"{uuid}:{score}:{int(interesting)}"` вместо пары оценок;
  - `JUDGE_PROMPT_VERSION = "judge-6"`.
- Текст `JUDGE_SYSTEM_PROMPT` после строки `matches_theme` и до строки `crop_feed, crop_stories` (обе без изменений), слово в слово:

  ```
  - score (1-10): strength of the photo for the author's Instagram — the strongest photos make the feed post, the next ones go to stories. Aesthetics come first: light, colour, composition, mood, visual impact; technical quality and informational value are secondary.
  - Judge score by the photo's potential after editing and cropping in the page's style: problems that editing fixes (exposure, colour and white balance, a tilted horizon, the crop) are not drawbacks; problems it cannot fix (motion blur, missed focus, blown highlights, a cut-off subject) are.
  - People: a posed portrait of the traveller — the person is the main subject and poses in front of the place — is a typical cover of the author's feed and raises the score; posing is not a drawback. Crowds, passers-by and random tourists that distract from the place are a drawback. The absence of people is not a drawback.
  - interesting: true for a moment that needs a short explanatory text (what is shown, the story of the place) and is therefore good for stories even at a middling score; otherwise false.
  - reason: one short sentence in the language of the theme on what makes the photo work or not.
  ```

- Последняя строка `STYLE_BLOCK` (после `{text}`) заменяется, слово в слово:

  ```
  Fit to the feed section of this style is, together with the photo's aesthetics, the main criterion of score; technical quality and informational value matter less.
  ```

- Отчёт:
  - `SCORES_NOTE = "Оценка — сила кадра по второй ступени, 1–10; «Причина» — её пояснение к оценке, а не решение сборки."`;
  - в обеих таблицах колонка `Оценка` вместо `Оценка (лента / stories)`, в ячейке — `_score(f.score)`.

- [ ] **Step 1: Тесты.**
  - `test_judge_schema_score_and_interesting`: `set(JudgeItem.model_fields) == {"id", "matches_theme", "score", "interesting", "reason", "crop_feed", "crop_stories"}`.
  - `test_judge_prompt_portrait_and_interesting`:
    - `"posed portrait of the traveller"`, `"random tourists"` и `"interesting: true"` есть в `JUDGE_SYSTEM_PROMPT`;
    - `"without people"`, `"feed_score"`, `"stories_score"` в нём нет;
    - `"stories_score"` нет и в `STYLE_BLOCK`.
  - `test_judge5_cache_not_reused`: оценка в кэше под `"judge-5"` → `run_judge` делает 1 вызов.
  - `test_judge_score_clamped_and_interesting_kept`: ответ модели `score=11, interesting=True` → `Judgment.score == 10`, `Judgment.interesting is True`.
  - `test_judgment_roundtrip_with_interesting` (`tests/test_cache.py`).
  - `test_old_threshold_keys_rejected` (`tests/test_config.py`): `thresholds: {min_feed_score: 6}` и `thresholds: {min_stories_score: 4}` → `ConfigError`, в тексте есть `"thresholds.min_score"`.
  - `test_min_score_default`: `Thresholds().min_score == 5`.
  - `test_compose_candidates_single_score` (`tests/test_posts.py`): оценки 9, 7, 4 и 3 (у последнего `interesting=True`), `limit=10` → `[9, 7, 3-интересный]`; кадр с 4 и без пометки отброшен.
  - `test_fallback_posts_single_score`: `carousel_size=2`, силы 9, 8, 7, 4 → карусель `[9, 8]`, stories `[7]`.
  - `test_fallback_posts_nothing_strong`: все силы ниже `min_score` → `Posts([], [])`.
  - `test_compose_label_strength_and_interesting` (`tests/test_compose.py`): подписи `"id: c01 (horizontal) · сила 8 · интересное · «r»"` и `"id: c02 (horizontal) · сила 6 · «r»"`.
  - `test_compose_fingerprint_depends_on_interesting`: смена `interesting` у одного кадра меняет отпечаток.
  - `test_report_single_score_column` (`tests/test_export_report.py`): в отчёте есть `"| Слайд | Файл | Оценка | Ориентация |"` и `"| № | Файл | Оценка | Тип |"`, нет `"лента / stories"`, в строке слайда — `"| 8 |"`.
- [ ] **Step 2:** `uv run pytest tests/test_stages.py tests/test_posts.py tests/test_compose.py tests/test_config.py tests/test_export_report.py tests/test_cache.py -v` → новые тесты FAIL.
- [ ] **Step 3: Код.** По Interfaces. Фикстуры остальных тестов перевести на `score`, `interesting`, `min_score`. Прежние тесты текста judge-5 переписываются под judge-6: правила о потенциале после обработки и эстетике первыми в judge-6 остаются.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит.**

  ```bash
  git add src/photo_chooser tests
  git commit -m "feat: сила кадра, пометка «интересное» и портрет автора на второй ступени (judge-6, D13, D14)"
  ```

### Task 3: Сборка — один пост до 5 кадров, stories из лучших оставшихся по времени съёмки (compose-3)

**Files:**
- Modify:
  - `src/photo_chooser/config.py` (`PostsCfg`);
  - `src/photo_chooser/judge/prompts.py` (`COMPOSE_PROMPT_VERSION`, `COMPOSE_SYSTEM_PROMPT`, `COMPOSE_STYLE_BLOCK`);
  - `src/photo_chooser/select.py` (`compose_candidates`, `validate_composition`, `fallback_posts`, `local_posts`);
  - `src/photo_chooser/pipeline.py` (вызовы `compose_candidates` в `run` и `_estimate`; оценка числа превью сборки до второй ступени).
- Test: `tests/test_posts.py`, `tests/test_compose.py`, `tests/test_config.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `Judgment.score`, `Judgment.interesting`, `Thresholds.min_score` из Task 2.
- Produces:
  - `PostsCfg`: `carousels: int = 1`, `carousel_size: int = 5`, `stories: int = 9`;
  - `compose_candidates(cands, judgments, quality, th, interval_only, limit, extra_interesting: int = 0) -> list[Asset]` — первые `limit` по Task 2, затем «интересные» из оставшихся по `(score, quality)`, не больше `extra_interesting`. Конвейер передаёт `cfg.posts.stories`;
  - `_chronological(stories: list[StoryPick]) -> list[StoryPick]` в `select.py` — устойчивая сортировка по `asset.taken_at`;
  - `validate_composition` сначала обрезает stories до `posts.stories` в порядке модели, затем применяет `_chronological`;
  - `fallback_posts` и `local_posts` возвращают stories через `_chronological`;
  - `_estimate`: пока оценки второй ступени неизвестны, число превью сборки — `min(len(assets), compose_candidates + posts.stories)`; когда известны — `len(compose_candidates(..., extra_interesting=cfg.posts.stories))`;
  - `COMPOSE_PROMPT_VERSION = "compose-3"`.
- Текст `COMPOSE_SYSTEM_PROMPT` целиком, слово в слово (плейсхолдеры `{carousels}`, `{carousel_size}`, `{stories}` — как сейчас):

  ```
  You are a photo editor assembling Instagram posts for one theme of a trip from the author's candidate photos.
  Each candidate is preceded by a label with its id, orientation, strength score ("сила", 1-10), the mark "интересное" for a moment that needs an explanatory text, and a short reason written by the editor who scored it; the theme is given at the end. Choose first of all by aesthetics and fit to the page's style, judging photos by their potential after editing and cropping in that style.
  You do not have to use every candidate: leave out any photo the author would not publish. Fewer, stronger photos are better than more.
  Build:
  - carousels: the feed post — the strongest storyline of the theme, at most {carousels} carousel(s) of at most {carousel_size} photos, usually 3-5 photos. Make another carousel only if more than one is allowed and the theme has a clearly separate storyline with at least 4 strong photos of its own; otherwise the other storyline goes to stories. Give each carousel a short title in the language of the theme. The first photo is the cover — the strongest and most characteristic shot of the storyline; a posed portrait of the traveller in front of the place makes a good cover. Then order the photos so they tell the story. Each slide must add something new to the story. The same subject may appear several times if each slide shows it differently (e.g. a wide view, the subject against another landmark, a close detail). Two slides with the same framing from almost the same viewpoint are not allowed: keep the stronger one, put the other into its alternatives.
  - alternatives: for each carousel photo, up to 2 other candidates showing the same moment from another angle that the author could publish in its place.
  - stories: up to {stories} photos not used above, fewer is better — the strongest of the rest, including another storyline of the theme, without repeating the framing of a carousel slide and without flaws that light editing will not fix (kind "best", empty hint), and moments marked "интересное" that need a short explanatory text (kind "interesting", hint: a short note for the author on what to explain, in the language of the theme — not a finished caption). Their order does not matter: stories are arranged by shooting time.
  Each id may appear at most once across carousels, alternatives and stories. Return ids exactly as in the labels (e.g. c07), without the orientation.
  ```

- `COMPOSE_STYLE_BLOCK`: из последней строки удаляется предложение `The presence or absence of people is not part of the style.` Остальное без изменений.

- [ ] **Step 1: Тесты.**
  - `test_posts_defaults_one_carousel_of_five` (`tests/test_config.py`): `PostsCfg() == PostsCfg(carousels=1, carousel_size=5, stories=9)`.
  - `test_compose_prompt_post_and_stories` (`tests/test_compose.py`), промпт отформатирован значениями `PostsCfg()`:
    - есть `"at most 1 carousel(s) of at most 5 photos"`, `"usually 3-5"`, `"posed portrait of the traveller"`, `"stories are arranged by shooting time"`;
    - в `COMPOSE_STYLE_BLOCK` нет `"presence or absence of people"`.
  - `test_compose_prompt_version_bumped`: `COMPOSE_PROMPT_VERSION == "compose-3"` (заменяет прежний тест с `"compose-2"`).
  - `test_compose_candidates_adds_interesting_beyond_window` (`tests/test_posts.py`): силы 9, 8, 6, 5-интересный, 4-интересный, 7-интересный; `limit=2`, `extra_interesting=1` → `[9, 8, 7-интересный]`. С `extra_interesting=0` → `[9, 8]`.
  - `test_compose_candidates_interesting_flood_capped`: 20 «интересных», `limit=2`, `extra_interesting=3` → длина 5.
  - `test_compose_candidates_only_interesting_below_threshold`: все силы ниже `min_score`, два «интересных» → оба в результате.
  - `test_validate_stories_by_shooting_time`: модель вернула stories c03, c01, c02 с `taken_at` 12:00, 10:00, 11:00 → порядок c01, c02, c03. С `posts.stories=2` остаются c03 и c01 (первые два в порядке модели) → c01, c03.
  - `test_validate_stories_same_time_keep_model_order`: три stories с одинаковым `taken_at` → порядок как у модели.
  - `test_fallback_and_local_stories_by_shooting_time`: у `fallback_posts` и `local_posts` stories отсортированы по `taken_at`.
  - `test_dry_run_estimate_includes_interesting_window` (`tests/test_pipeline.py`): пустой кэш, тема с 60 снимками → оценка равна оценке с `n_compose=[39]` (30 + 9) при прочих равных; расчёт — через `estimate_run_usd`, как в соседнем `test_dry_run_estimate_includes_compose`.
- [ ] **Step 2:** `uv run pytest tests/test_posts.py tests/test_compose.py tests/test_config.py tests/test_pipeline.py -v` → новые тесты FAIL.
- [ ] **Step 3: Код.** По Interfaces. Тесты, которые опирались на старые значения по умолчанию (`carousels=2`, `carousel_size=9`), задают их явно через `PostsCfg(...)`. Прежние тесты текста compose-2 (`test_compose_prompt_*`) переписываются под новый текст: проверяемые ими правила — «можно не брать всех», разные планы, планка stories, эстетика и стиль первыми — в compose-3 остаются.
- [ ] **Step 4:** `uv run pytest` → PASS.
- [ ] **Step 5: Коммит.**

  ```bash
  git add src/photo_chooser tests
  git commit -m "feat: один пост до 5 кадров, stories из лучших оставшихся в порядке съёмки (compose-3, D13)"
  ```

### Task 4: Профиль стиля — портрет автора; README

**Files:**
- Modify: `src/photo_chooser/judge/prompts.py` (`STYLE_SYSTEM_PROMPT`), `README.md`
- Test: `tests/test_style.py`

**Interfaces:**
- В `STYLE_SYSTEM_PROMPT` строка `Whether people appear in the photos is not part of the style: do not mention people, the author or selfies as a trait of the page.` заменяется, слово в слово:

  ```
  Whether people appear in the photos is not part of the style; the one exception is a posed portrait of the author in front of the place, which you may name as a trait of the feed. Do not describe selfies or other people as a trait of the page.
  ```

- README, разделы «Результат», «Настройки», первый абзац:
  - на тему — одна карусель до 5 кадров с запасными ракурсами и до 9 stories, stories в порядке съёмки;
  - пример `posts: {carousels: 1, carousel_size: 5, stories: 9}`;
  - в дереве результата нет строки `carousel-2`;
  - в описании `report.md` — одна оценка (сила кадра) вместо «обеих оценок»;
  - одна строка о портрете автора: постановочный портрет на фоне места поднимает оценку, автора от спутника программа не отличает.

- [ ] **Step 1: Тест** `test_style_prompt_allows_author_portrait`: `"posed portrait of the author"` есть в `STYLE_SYSTEM_PROMPT`, `"do not mention people, the author"` нет.
- [ ] **Step 2:** `uv run pytest tests/test_style.py -k author_portrait -v` → FAIL.
- [ ] **Step 3: Код и README.**
- [ ] **Step 4:** `uv run pytest` → PASS. Проверить: `grep -n "до 2 каруселей\|carousel-2\|обеими оценками\|carousels: 2" README.md` → пусто.
- [ ] **Step 5: Коммит, PR.**

  ```bash
  git add src/photo_chooser/judge/prompts.py tests/test_style.py README.md
  git commit -m "feat: портрет автора в профиле стиля, README версии 0.4 (D14)"
  git push -u origin feat/single-score-and-author-portrait
  ```

  PR в `main` с описанием версии 0.4 и ссылками на D13, D14.

### После мерджа: повторная приёмка (делает пользователь)

Не задача кода — порядок для пользователя, команды в Terminal.app из корня репозитория:

1. `uv run photo-chooser style` — пересобрать профиль с новым правилом о портрете автора. Сверить его с [docs/acceptance/style-reference.md](../../acceptance/style-reference.md) и при необходимости поправить вручную.
2. `uv run photo-chooser run docs/acceptance/egypt26.yaml --dry-run` — оценка: около $2.8, первая ступень заново (`screen-3`).
3. `time (ANTHROPIC_API_KEY="$(security find-generic-password -s anthropic-api-key -w)" uv run photo-chooser run docs/acceptance/egypt26.yaml)` — лимит альбома $5 уже частично израсходован ($2.74), может понадобиться `--max-cost`; решение за пользователем.
4. Оценка по NFR-3 и сверка с образцом стиля → раздел «Версия 0.4» в `docs/acceptance.md` отдельным PR `docs/acceptance-0-4`.
