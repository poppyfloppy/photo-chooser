"""Интеграционные тесты на реальной медиатеке Photos (SPEC §10). Запускает пользователь — см. README.md рядом.

PHOTO_CHOOSER_IT_ALBUM="photo-chooser-test" uv run pytest -m integration -v
"""

import os
from datetime import datetime
from pathlib import Path

import Photos
import pytest

from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig, Theme, TripConfig
from photo_chooser.judge.prompts import JUDGE_PROMPT_VERSION, SCREEN_PROMPT_VERSION
from photo_chooser.judge.transport import make_client
from photo_chooser.library.photokit_library import PhotoKitLibrary
from photo_chooser.pipeline import RunOptions, run

ALBUM = os.environ.get("PHOTO_CHOOSER_IT_ALBUM")
QUERY = os.environ.get("PHOTO_CHOOSER_IT_QUERY", "кофе")
BURST_GAP_S = 10  # серия: кадры подряд с интервалом не больше 10 секунд

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not ALBUM, reason="задайте PHOTO_CHOOSER_IT_ALBUM — имя тестового альбома Photos"),
]


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    return PhotoKitLibrary(preview_dir=tmp_path_factory.mktemp("previews"))


@pytest.fixture(scope="module")
def album_assets(library):
    return library.assets(library.find_album(ALBUM))


def _no_client():
    raise AssertionError("в режиме без облака Claude не вызывается")


def _run_no_cloud(library, tmp_path):
    cfg = AppConfig(output_dir=tmp_path / "out")
    trip = TripConfig(album=ALBUM, themes=[Theme(name="Все кадры", query="любые")])
    return run(trip, cfg, RunOptions(no_cloud=True), library, Cache(tmp_path / "cache.sqlite"), _no_client,
               datetime.now())


def _exported(theme_dir: Path) -> list[str]:
    return [p.name for d in ("feed", "stories") for p in sorted((theme_dir / d).rglob("*"))
            if p.is_file() and "alternatives" not in p.parts]


def _stem(exported_name: str) -> str:
    name = exported_name.split("_", 1)[1]  # без префикса ранга «01_»
    return Path(name.removesuffix("_live-frame.tiff")).stem


def _snapshot(library):
    """Состав альбома, даты изменения и флаги снимков, плюс список альбомов с числом фото."""
    album = library.find_album(ALBUM)
    coll = Photos.PHAssetCollection.fetchAssetCollectionsWithLocalIdentifiers_options_([album.uuid], None)
    result = Photos.PHAsset.fetchAssetsInAssetCollection_options_(coll.objectAtIndex_(0), None)
    items = {}
    for i in range(result.count()):
        a = result.objectAtIndex_(i)
        modified = a.modificationDate()
        items[a.localIdentifier()] = (modified.timeIntervalSince1970() if modified else None, bool(a.isFavorite()),
                                      bool(a.isHidden()))
    return result.count(), items, sorted((x.title, x.count) for x in library.albums())


def test_library_unchanged_after_run(library, tmp_path):
    """NFR-1: прогон с экспортом оригиналов ничего не меняет в медиатеке."""
    before = _snapshot(library)
    _run_no_cloud(library, tmp_path)
    after = _snapshot(library)
    assert after[0] == before[0], "изменилось число снимков в альбоме"
    assert after[1] == before[1], "изменились снимки альбома (состав, дата изменения, избранное, скрытые)"
    assert after[2] == before[2], "изменился список альбомов"


def test_no_cloud_run_on_test_album(library, album_assets, tmp_path):
    shots = {a.filename for a in album_assets if a.is_screenshot}
    lives = {Path(a.filename).stem for a in album_assets if a.is_live}
    ordered = sorted(album_assets, key=lambda a: a.taken_at)
    bursts, current = [], [ordered[0]] if ordered else []
    for prev, cur in zip(ordered, ordered[1:]):
        if (cur.taken_at - prev.taken_at).total_seconds() <= BURST_GAP_S:
            current.append(cur)
        else:
            bursts.append(current)
            current = [cur]
    bursts = [b for b in bursts + [current] if len(b) >= 3]
    assert shots, "в тестовом альбоме нет скриншота — см. tests/integration/README.md"
    assert lives, "в тестовом альбоме нет Live Photo — см. README"
    assert bursts, "в тестовом альбоме нет серии из ≥3 кадров подряд — см. README"

    result = _run_no_cloud(library, tmp_path)
    outcome = result.themes[0]
    assert outcome.error is None
    theme_dir = outcome.report_path.parent
    exported = _exported(theme_dir)
    stems = [_stem(n) for n in exported]
    assert exported, "шорт-лист пуст"
    assert all(n[:2].isdigit() and n[2] == "_" for n in exported), exported
    assert not {Path(s).stem for s in shots} & set(stems), "скриншот попал в шорт-лист"
    for burst in bursts:
        names = {Path(a.filename).stem for a in burst}
        assert sum(s in names for s in stems) <= 1, f"из серии {sorted(names)} в шорт-листе больше одного кадра"
    for stem in lives & set(stems):
        assert stems.count(stem) == 1, f"Live Photo {stem} выгружено несколькими файлами"
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "без облака" in report and f"Прочитано снимков: {len(album_assets)}" in report


@pytest.mark.cloud
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="нужен ANTHROPIC_API_KEY")
def test_haiku_run_on_test_album(library, tmp_path):
    cfg = AppConfig(output_dir=tmp_path / "out")
    trip = TripConfig(album=ALBUM, themes=[Theme(name="Тест", query=QUERY)])
    cache = Cache(tmp_path / "cache.sqlite")
    result = run(trip, cfg, RunOptions(), library, cache, make_client, datetime.now())
    outcome = result.themes[0]
    assert outcome.error is None
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "claude-haiku-5-5" in report and f"{SCREEN_PROMPT_VERSION}, {JUDGE_PROMPT_VERSION}" in report
    assert result.album_spend_usd > 0
    assert cache.album_spend(library.find_album(ALBUM).uuid) == pytest.approx(result.album_spend_usd)
