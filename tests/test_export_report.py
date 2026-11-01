from datetime import datetime

import pytest

from conftest import make_asset
from fakes import FakeLibrary
from imaging import blurred, save_jpeg, scene_image, write_mov
from photo_chooser.config import AppConfig, ConfigError, Theme
from photo_chooser.export import (
    ExportedItem,
    assemble,
    ensure_outside_library,
    export_asset,
    new_run_dir,
    safe_name,
)
from photo_chooser.library.base import DownloadError
from photo_chooser.models import AlbumRef
from photo_chooser.report import ReportHeader, ThemeStats, write_report
from photo_chooser.select import Pools, Scored

NOW = datetime(2026, 11, 1, 15, 30)


def test_safe_name_cases():
    assert safe_name("Гиза/Каир: день 1 ") == "Гиза_Каир_ день 1"
    assert safe_name("") == "_"
    assert safe_name("   ") == "_"
    assert safe_name('a\\b*c?d"e<f>g|h\ti') == "a_b_c_d_e_f_g_h_i"
    assert safe_name("..") == "__"
    assert safe_name(".скрытая") == "_скрытая"
    assert safe_name("🏜 Пустыня 🐪") == "🏜 Пустыня 🐪"
    assert len(safe_name("я" * 300)) == 100
    assert len(safe_name("🐪" * 100).encode("utf-8")) <= 255


def test_ensure_outside_library(tmp_path):
    with pytest.raises(ConfigError):
        ensure_outside_library(tmp_path / "Photos Library.photoslibrary" / "out")
    with pytest.raises(ConfigError):
        ensure_outside_library(tmp_path / "Photos Library.photoslibrary")
    ensure_outside_library(tmp_path / "photo-chooser")


def test_new_run_dir_never_overwrites(tmp_path):
    first = new_run_dir(tmp_path, "Египет: 2026", NOW)
    second = new_run_dir(tmp_path, "Египет: 2026", NOW)
    third = new_run_dir(tmp_path, "Египет: 2026", NOW)
    assert first == tmp_path / "Египет_ 2026" / "2026-11-01_1530"
    assert second.name == "2026-11-01_1530_2" and third.name == "2026-11-01_1530_3"
    assert first.is_dir() and second.is_dir() and third.is_dir()


def _sc(a, score=7.0):
    return Scored(a, score, "r")


def _fake_export(failing=()):
    calls = []

    def try_export(scored, list_name, rank):
        calls.append((scored.asset.uuid, list_name, rank))
        if scored.asset.uuid in failing:
            raise DownloadError(scored.asset.uuid, "3 попытки не удались: network")
        return ExportedItem(list_name, rank, scored.asset, [f"{rank:02d}_{scored.asset.filename}"], scored.score,
                            scored.reason, [], scored.crop)

    return try_export, calls


def test_assemble_disjoint_and_ranked():
    a, b, c, d, e = (make_asset() for _ in range(5))
    pools = Pools(feed=[_sc(a), _sc(b), _sc(c)], stories=[_sc(b), _sc(d), _sc(a), _sc(e)])
    try_export, _ = _fake_export()
    got = assemble(pools, 2, 2, try_export)
    assert [(i.asset, i.rank, i.list_name) for i in got.feed] == [(a, 1, "feed"), (b, 2, "feed")]
    assert [(i.asset, i.rank, i.list_name) for i in got.stories] == [(d, 1, "stories"), (e, 2, "stories")]
    assert got.shortfall_feed == 0 and got.shortfall_stories == 0 and got.failed == []


def test_assemble_replaces_failed_export():
    a, b, c, d = (make_asset() for _ in range(4))
    pools = Pools(feed=[_sc(a), _sc(b), _sc(c)], stories=[_sc(b), _sc(d)])
    try_export, calls = _fake_export(failing={b.uuid})
    got = assemble(pools, 2, 5, try_export)
    assert [(i.asset, i.rank) for i in got.feed] == [(a, 1), (c, 2)]
    assert [i.asset for i in got.stories] == [d]
    assert [(x.uuid, "network" in reason) for x, reason in got.failed] == [(b.uuid, True)]
    assert [uuid for uuid, *_ in calls].count(b.uuid) == 1  # упавший снимок не повторяется в stories


def test_assemble_shortfall():
    a, b = make_asset(), make_asset()
    try_export, _ = _fake_export()
    got = assemble(Pools(feed=[_sc(a), _sc(b)], stories=[_sc(a)]), 5, 3, try_export)
    assert got.shortfall_feed == 3
    assert got.shortfall_stories == 3
    empty = assemble(Pools(feed=[], stories=[]), 20, 40, try_export)
    assert (empty.shortfall_feed, empty.shortfall_stories) == (20, 40)


def _library(assets, files, live=None):
    return FakeLibrary([AlbumRef("al", "Egypt", len(assets))], {"al": assets}, {}, files=files, live=live or {})


def test_export_original_ranked_name(tmp_path):
    a = make_asset(filename="IMG_1.HEIC")
    src = tmp_path / "src.heic"
    src.write_bytes(b"original-bytes")
    item = export_asset(_library([a], {a.uuid: src}), _sc(a, 8), "feed", 1, tmp_path / "theme", tmp_path / "tmp",
                        AppConfig())
    assert item.files == ["01_IMG_1.HEIC"]
    assert (tmp_path / "theme" / "feed" / "01_IMG_1.HEIC").read_bytes() == b"original-bytes"
    assert item.notes == [] and item.rank == 1 and item.score == 8


def test_export_original_failure_raises(tmp_path):
    a = make_asset()
    with pytest.raises(DownloadError):
        export_asset(_library([a], {}), _sc(a), "feed", 1, tmp_path / "theme", tmp_path / "tmp", AppConfig())


def test_export_live_video_failure_falls_back_to_key(tmp_path):
    a = make_asset(filename="IMG_2.JPG", is_live=True)
    key = save_jpeg(scene_image(0), tmp_path / "key.jpg")
    lib = _library([a], {a.uuid: key})
    item = export_asset(lib, _sc(a), "stories", 3, tmp_path / "theme", tmp_path / "tmp", AppConfig())
    assert item.files == ["03_IMG_2.JPG"]
    assert (tmp_path / "theme" / "stories" / "03_IMG_2.JPG").exists()
    assert item.notes == ["видео Live Photo не скачалось — взят ключевой снимок"]
    assert lib.live_requests == [a.uuid]


def test_export_live_frame_writes_tiff(tmp_path):
    a = make_asset(filename="IMG_4610.JPG", is_live=True)
    key = save_jpeg(blurred(scene_image(1), 12), tmp_path / "key.jpg", date="2026:03:14 07:31:05",
                    gps=(29.9792, 31.1342))
    video = write_mov(tmp_path / "IMG_4610.MOV", [scene_image(1, size=(480, 360)) for _ in range(10)])
    item = export_asset(_library([a], {a.uuid: key}, {a.uuid: video}), _sc(a), "stories", 1, tmp_path / "theme",
                        tmp_path / "tmp", AppConfig())
    assert item.files == ["01_IMG_4610_live-frame.tiff"]
    assert (tmp_path / "theme" / "stories" / "01_IMG_4610_live-frame.tiff").exists()
    assert item.notes == ["кадр из Live Photo, разрешение ниже оригинала"]


def test_export_live_key_when_frames_not_clearly_better(tmp_path):
    a = make_asset(filename="IMG_7.JPG", is_live=True)
    key = save_jpeg(scene_image(1), tmp_path / "key.jpg")
    video = write_mov(tmp_path / "IMG_7.MOV", [blurred(scene_image(1, size=(480, 360)), 6) for _ in range(10)])
    item = export_asset(_library([a], {a.uuid: key}, {a.uuid: video}), _sc(a), "feed", 2, tmp_path / "theme",
                        tmp_path / "tmp", AppConfig())
    assert item.files == ["02_IMG_7.JPG"] and item.notes == []


def _stats(**kw):
    base = dict(read=120, no_preview=2, junk=5, duplicates_collapsed=40, in_interval=73, sent_to_screen=73,
                matched=30, sent_to_judge=30, from_cache=10, unjudged=1, usd_screen=0.0123, usd_judge=0.0045,
                usd_album_total=0.42, skipped=[("IMG_9.HEIC", "нет превью")])
    base.update(kw)
    return ThemeStats(**base)


def test_report_contains_header_table_and_summary(tmp_path):
    v = make_asset(filename="IMG_1.HEIC", width=3024, height=4032)
    h = make_asset(filename="IMG_2.HEIC", width=4032, height=3024)
    failed = make_asset(filename="IMG_3.HEIC")
    try_export, _ = _fake_export(failing={failed.uuid})
    pools = Pools(feed=[Scored(v, 9, "Пирамиды | на закате"), Scored(failed, 8, "x")],
                  stories=[Scored(h, 0.61, "эстетика 0.61 · резкость 0.50", crop="до 9:16: столб справа")])
    assembled = assemble(pools, 5, 3, try_export)
    assembled.feed[0].notes.append("кадр из Live Photo, разрешение ниже оригинала")
    theme = Theme(name="Гиза", query="пирамиды")
    path = tmp_path / "report.md"
    header = ReportHeader(screen_model=None, model=None, effort=None, prompt_versions=None, run_at=NOW,
                          warnings=["Режим без облака: query темы не учитывается"], album="Egypt.26")
    write_report(path, theme, header, assembled, _stats())
    text = path.read_text(encoding="utf-8")
    assert "# Гиза" in text and "Egypt.26" in text
    assert "без облака" in text
    assert "Режим без облака: query темы не учитывается" in text
    assert "| Ранг | Файл(ы) | Список | Оценка | Ориентация | Пометки | Причина | Кадрирование |" in text
    assert "| 1 | 01_IMG_1.HEIC | лента | 9 | вертикальная | кадр из Live Photo, разрешение ниже оригинала | " \
           "Пирамиды \\| на закате | — |" in text
    assert "| 1 | 01_IMG_2.HEIC | stories | 0.61 | горизонтальная |" in text
    assert text.count("до 9:16: столб справа |") == 1
    assert "Недобор: лента 4, stories 2" in text
    assert "первая ступень $0.0123" in text and "вторая ступень $0.0045" in text
    assert "по альбому $0.42" in text
    assert "IMG_3.HEIC" in text and "network" in text
    assert "IMG_9.HEIC — нет превью" in text


def test_report_cloud_header(tmp_path):
    try_export, _ = _fake_export()
    assembled = assemble(Pools(feed=[], stories=[]), 0, 0, try_export)
    header = ReportHeader(screen_model="claude-haiku-5-5", model="claude-opus-5-5", effort="medium",
                          prompt_versions="screen-1, judge-1", run_at=NOW, warnings=[], album="Egypt.26")
    path = tmp_path / "report.md"
    write_report(path, Theme(name="Гиза", query="пирамиды"), header, assembled, _stats())
    text = path.read_text(encoding="utf-8")
    assert "claude-haiku-5-5" in text and "claude-opus-5-5" in text
    assert "effort: medium" in text and "screen-1, judge-1" in text
    assert "без облака" not in text
    assert "Недобор: нет" in text


def test_report_shows_style_line(tmp_path):
    try_export, _ = _fake_export()
    assembled = assemble(Pools(feed=[], stories=[]), 0, 0, try_export)
    header = ReportHeader(screen_model="claude-haiku-5-5", model="claude-haiku-5-5", effort="medium",
                          prompt_versions="screen-2, judge-2/style-ab12cd34", run_at=NOW, warnings=[], album="Egypt.26",
                          style="style.md от 2026-11-01, отпечаток ab12cd34, скриншотов в запросе: 2")
    path = tmp_path / "report.md"
    write_report(path, Theme(name="Гиза", query="пирамиды"), header, assembled, _stats())
    assert "- Стиль: style.md от 2026-11-01, отпечаток ab12cd34, скриншотов в запросе: 2" in path.read_text(
        encoding="utf-8")


# --- посты: карусели и stories (v0.3) ---------------------------------------------------------

from photo_chooser.export import export_posts  # noqa: E402
from photo_chooser.report import write_posts_report  # noqa: E402
from photo_chooser.select import Carousel, Posts, ShotFacts, Slide, StoryPick  # noqa: E402


def _posts_library(tmp_path, n, failing=()):
    assets = [make_asset(uuid=f"S{i}", filename=f"IMG_{i}.HEIC") for i in range(n)]
    files = {}
    for a in assets:
        f = tmp_path / f"{a.uuid}.heic"
        f.write_bytes(a.uuid.encode())
        files[a.uuid] = f
    return assets, FakeLibrary([AlbumRef("al", "Egypt", n)], {"al": assets}, {}, files=files, failing=set(failing))


def _names(path):
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


def test_export_posts_layout(tmp_path):
    a, lib = _posts_library(tmp_path, 7)
    posts = Posts([Carousel("Пирамиды", [Slide(a[0], [a[1], a[2]]), Slide(a[3])]), Carousel("Закат", [Slide(a[4])])],
                  [StoryPick(a[5], "best", ""), StoryPick(a[6], "interesting", "рассказать о ладье")])
    out = export_posts(lib, posts, tmp_path / "theme", tmp_path / "tmp", AppConfig())
    feed = tmp_path / "theme" / "feed"
    assert _names(feed) == ["carousel-1 Пирамиды", "carousel-2 Закат"]
    assert _names(feed / "carousel-1 Пирамиды") == ["01_IMG_0.HEIC", "02_IMG_3.HEIC", "alternatives"]
    assert _names(feed / "carousel-1 Пирамиды" / "alternatives") == ["01a_IMG_1.HEIC", "01b_IMG_2.HEIC"]
    assert _names(tmp_path / "theme" / "stories") == ["01_IMG_5.HEIC", "02_IMG_6.HEIC"]
    assert [c.folder for c in out.carousels] == ["carousel-1 Пирамиды", "carousel-2 Закат"]
    assert out.stories[1].hint == "рассказать о ладье" and out.failed == []


def test_export_posts_replaces_failed_slide_with_alternative(tmp_path):
    a, lib = _posts_library(tmp_path, 4, failing={"S1"})
    posts = Posts([Carousel("X", [Slide(a[0]), Slide(a[1], [a[2], a[3]])])], [])
    out = export_posts(lib, posts, tmp_path / "theme", tmp_path / "tmp", AppConfig())
    slide = out.carousels[0].slides[1]
    assert slide.replaced and slide.shot.files == ["02_IMG_2.HEIC"] and "замена" in " ".join(slide.shot.notes)
    assert [s.files for s in slide.alternatives] == [["02a_IMG_3.HEIC"]]
    assert [x.uuid for x, _ in out.failed] == ["S1"]


def test_export_posts_drops_slot_without_alternatives(tmp_path):
    a, lib = _posts_library(tmp_path, 3, failing={"S1"})
    posts = Posts([Carousel("X", [Slide(a[0]), Slide(a[1]), Slide(a[2])])], [])
    out = export_posts(lib, posts, tmp_path / "theme", tmp_path / "tmp", AppConfig())
    assert [s.shot.files for s in out.carousels[0].slides] == [["01_IMG_0.HEIC"], ["02_IMG_2.HEIC"]]
    assert [x.uuid for x, _ in out.failed] == ["S1"]
    assert _names(tmp_path / "theme" / "stories") == []  # папки создаются всегда


def test_export_posts_unsafe_and_duplicate_titles(tmp_path):
    a, lib = _posts_library(tmp_path, 3)
    long = "Гиза/Каир: 🐪 " + "очень длинное название сюжета " * 3
    posts = Posts([Carousel(long, [Slide(a[0])]), Carousel("Сфинкс", [Slide(a[1])]), Carousel("Сфинкс", [Slide(a[2])])],
                  [])
    posts.carousels = posts.carousels[:3]
    out = export_posts(lib, posts, tmp_path / "theme", tmp_path / "tmp", AppConfig())
    folders = [c.folder for c in out.carousels]
    assert "/" not in folders[0] and ":" not in folders[0] and len(folders[0]) <= 60
    assert folders[1:] == ["carousel-2 Сфинкс", "carousel-3 Сфинкс"]
    same = Posts([Carousel("Сфинкс", [Slide(a[0])])], [])
    again = export_posts(lib, same, tmp_path / "theme", tmp_path / "tmp", AppConfig())
    assert again.carousels[0].folder == "carousel-1 Сфинкс"
    (tmp_path / "theme2" / "feed" / "carousel-1 Сфинкс").mkdir(parents=True)
    clash = export_posts(lib, same, tmp_path / "theme2", tmp_path / "tmp", AppConfig())
    assert clash.carousels[0].folder == "carousel-1 Сфинкс_2"


def _posts_stats(**kw):
    base = dict(read=100, no_preview=0, junk=1, duplicates_collapsed=10, in_interval=89, sent_to_screen=89, matched=40,
                sent_to_judge=40, from_cache=0, unjudged=0, usd_screen=0.0142, usd_judge=0.75, usd_album_total=1.2,
                sent_to_compose=30, usd_compose=0.19)
    base.update(kw)
    return ThemeStats(**base)


def test_report_posts_tables(tmp_path):
    a, lib = _posts_library(tmp_path, 5)
    posts = Posts([Carousel("Пирамиды", [Slide(a[0], [a[1]]), Slide(a[2])])],
                  [StoryPick(a[3], "best", ""), StoryPick(a[4], "interesting", "рассказать о ладье Хеопса")])
    exported = export_posts(lib, posts, tmp_path / "theme", tmp_path / "tmp", AppConfig())
    facts = {x.uuid: ShotFacts(8, 6, f"Причина {x.uuid}", "до 4:5: справа", "до 9:16: центр") for x in a}
    header = ReportHeader(screen_model="claude-haiku-5-5", model="claude-opus-5-5", effort="medium",
                          prompt_versions="screen-2, judge-4, compose-1", run_at=NOW, warnings=[], album="Egypt.26")
    path = tmp_path / "report.md"
    write_posts_report(path, Theme(name="Гиза", query="пирамиды"), header, exported, facts, _posts_stats())
    text = path.read_text(encoding="utf-8")
    assert "### Карусель 1 — Пирамиды" in text
    assert "| Слайд | Файл | Лента / stories | Ориентация | Пометки | Причина | Кадрирование (4:5) | Запасные |" in text
    assert "| 1 | 01_IMG_0.HEIC | 8 / 6 | горизонтальная |  | Причина S0 | до 4:5: справа | 01a_IMG_1.HEIC |" in text
    assert "| № | Файл | Лента / stories | Тип | Что пояснить | Причина | Кадрирование (9:16) |" in text
    assert "| 2 | 02_IMG_4.HEIC | 8 / 6 | интересное | рассказать о ладье Хеопса | Причина S4 | до 9:16: центр |" in text
    assert "Посты: каруселей 1 (2), stories 2" in text
    assert "сборка $0.1900 (по теме)" in text and "Отправлено в сборку: 30" in text


def test_report_fallback_warning_and_empty_posts(tmp_path):
    exported = export_posts(FakeLibrary([], {}, {}), Posts([], []), tmp_path / "theme", tmp_path / "tmp", AppConfig())
    header = ReportHeader(screen_model="claude-haiku-5-5", model="claude-opus-5-5", effort="medium",
                          prompt_versions="screen-2, judge-4, compose-1", run_at=NOW,
                          warnings=["Сборка не удалась — разложено по оценкам (refusal)"], album="Egypt.26")
    path = tmp_path / "report.md"
    write_posts_report(path, Theme(name="Гиза", query="пирамиды"), header, exported, {}, _posts_stats())
    text = path.read_text(encoding="utf-8")
    assert "разложено по оценкам" in text and "Каруселей нет" in text and "Посты: каруселей 0, stories 0" in text
