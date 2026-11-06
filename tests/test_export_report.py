from datetime import datetime

import pytest

from conftest import make_asset
from fakes import FakeLibrary
from imaging import blurred, save_jpeg, scene_image, write_mov
from photo_chooser.config import AppConfig, ConfigError, Theme
from photo_chooser.export import ensure_outside_library, export_posts, export_shot, new_run_dir, safe_name
from photo_chooser.library.base import DownloadError
from photo_chooser.models import AlbumRef
from photo_chooser.report import ReportHeader, ThemeStats, write_report
from photo_chooser.select import Carousel, Posts, ShotFacts, Slide, StoryPick

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


def _library(assets, files, live=None):
    return FakeLibrary([AlbumRef("al", "Egypt", len(assets))], {"al": assets}, {}, files=files, live=live or {})


def test_export_original_ranked_name(tmp_path):
    a = make_asset(filename="IMG_1.HEIC")
    src = tmp_path / "src.heic"
    src.write_bytes(b"original-bytes")
    shot = export_shot(_library([a], {a.uuid: src}), a, tmp_path / "dest", "01", tmp_path / "tmp", AppConfig())
    assert shot.files == ["01_IMG_1.HEIC"]
    assert (tmp_path / "dest" / "01_IMG_1.HEIC").read_bytes() == b"original-bytes"
    assert shot.notes == []


def test_export_original_failure_raises(tmp_path):
    a = make_asset()
    with pytest.raises(DownloadError):
        export_shot(_library([a], {}), a, tmp_path / "dest", "01", tmp_path / "tmp", AppConfig())


def test_export_live_video_failure_falls_back_to_key(tmp_path):
    a = make_asset(filename="IMG_2.JPG", is_live=True)
    key = save_jpeg(scene_image(0), tmp_path / "key.jpg")
    lib = _library([a], {a.uuid: key})
    item = export_shot(lib, a, tmp_path / "stories", "03", tmp_path / "tmp", AppConfig())
    assert item.files == ["03_IMG_2.JPG"]
    assert (tmp_path / "stories" / "03_IMG_2.JPG").exists()
    assert item.notes == ["видео Live Photo не скачалось — взят ключевой снимок"]
    assert lib.live_requests == [a.uuid]


def test_export_live_frame_writes_tiff(tmp_path):
    a = make_asset(filename="IMG_4610.JPG", is_live=True)
    key = save_jpeg(blurred(scene_image(1), 12), tmp_path / "key.jpg", date="2026:03:14 07:31:05",
                    gps=(29.9792, 31.1342))
    video = write_mov(tmp_path / "IMG_4610.MOV", [scene_image(1, size=(480, 360)) for _ in range(10)])
    item = export_shot(_library([a], {a.uuid: key}, {a.uuid: video}), a, tmp_path / "stories", "01",
                       tmp_path / "tmp", AppConfig())
    assert item.files == ["01_IMG_4610_live-frame.tiff"]
    assert (tmp_path / "stories" / "01_IMG_4610_live-frame.tiff").exists()
    assert item.notes == ["кадр из Live Photo, разрешение ниже оригинала"]


def test_export_live_key_when_frames_not_clearly_better(tmp_path):
    a = make_asset(filename="IMG_7.JPG", is_live=True)
    key = save_jpeg(scene_image(1), tmp_path / "key.jpg")
    video = write_mov(tmp_path / "IMG_7.MOV", [blurred(scene_image(1, size=(480, 360)), 6) for _ in range(10)])
    item = export_shot(_library([a], {a.uuid: key}, {a.uuid: video}), a, tmp_path / "feed", "02",
                       tmp_path / "tmp", AppConfig())
    assert item.files == ["02_IMG_7.JPG"] and item.notes == []


def _stats(**kw):
    base = dict(read=120, no_preview=2, junk=5, duplicates_collapsed=40, in_interval=73, sent_to_screen=73,
                matched=30, sent_to_judge=30, from_cache=10, unjudged=1, usd_screen=0.0123, usd_judge=0.0045,
                usd_album_total=0.42, skipped=[("IMG_9.HEIC", "нет превью")])
    base.update(kw)
    return ThemeStats(**base)


def _empty_posts(tmp_path):
    return export_posts(FakeLibrary([], {}, {}), Posts([], []), tmp_path / "theme", tmp_path / "tmp", AppConfig())


def test_report_no_cloud_header_and_summary(tmp_path):
    path = tmp_path / "report.md"
    header = ReportHeader(screen_stage=None, judge_stage=None, prompt_versions=None, run_at=NOW,
                          warnings=["Режим без облака: запрос темы не учитывается"], album="Egypt.26")
    write_report(path, Theme(name="Гиза", query="пирамиды"), header, _empty_posts(tmp_path), {}, _stats())
    text = path.read_text(encoding="utf-8")
    assert "# Гиза" in text and "Egypt.26" in text and "без облака" in text
    assert "Режим без облака: запрос темы не учитывается" in text
    assert "первая ступень $0.0123" in text and "вторая ступень $0.0045" in text and "по альбому $0.42" in text
    assert "IMG_9.HEIC — нет превью" in text and "Недобор" not in text


CACHE_LINE = "- Кэш мета-блока (вторая ступень и сборка): записано 4.5 тыс., прочитано 36.0 тыс. токенов"


def test_report_cache_line(tmp_path):
    cloud = ReportHeader(screen_stage="claude-haiku-5-5, medium, 1024 px", judge_stage="claude-opus-5-5, medium, 768 px",
                         prompt_versions="screen-4, judge-7, compose-5", run_at=NOW, album="Egypt.26")
    path = tmp_path / "report.md"
    stats = _stats(cache_write_tokens=4500, cache_read_tokens=36000)
    write_report(path, Theme(name="Гиза", query="пирамиды"), cloud, _empty_posts(tmp_path), {}, stats)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[lines.index(CACHE_LINE) - 1].startswith("- Стоимость:")
    local = ReportHeader(screen_stage=None, judge_stage=None, prompt_versions=None, run_at=NOW)
    write_report(path, Theme(name="Гиза", query="пирамиды"), local, _empty_posts(tmp_path), {}, stats)
    assert "Кэш мета-блока" not in path.read_text(encoding="utf-8")


def test_report_cloud_header_and_style_line(tmp_path):
    header = ReportHeader(screen_stage="claude-haiku-5-5, medium, 1024 px", judge_stage="claude-opus-5-5, medium, 768 px",
                          prompt_versions="screen-2, judge-4/style-ab12cd34, compose-1", run_at=NOW, warnings=[],
                          album="Egypt.26", style="style.md от 2026-11-01, отпечаток ab12cd34, скриншотов: лента 2, "
                                                  "stories 2")
    path = tmp_path / "report.md"
    write_report(path, Theme(name="Гиза", query="пирамиды"), header, _empty_posts(tmp_path), {}, _stats())
    text = path.read_text(encoding="utf-8")
    assert "claude-haiku-5-5" in text and "claude-opus-5-5" in text and "без облака" not in text
    assert "вторая ступень и сборка: claude-opus-5-5, medium, 768 px" in text
    assert "screen-2, judge-4/style-ab12cd34, compose-1" in text
    assert "- Стиль: style.md от 2026-11-01, отпечаток ab12cd34, скриншотов: лента 2, stories 2" in text


# --- посты: карусели и stories (v0.3) ---------------------------------------------------------


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
    facts = {x.uuid: ShotFacts(8, False, f"Причина {x.uuid}", "до 4:5: справа", "до 9:16: центр") for x in a}
    header = ReportHeader(screen_stage="claude-haiku-5-5, medium, 1024 px", judge_stage="claude-opus-5-5, medium, 768 px",
                          prompt_versions="screen-2, judge-4, compose-1", run_at=NOW, warnings=[], album="Egypt.26")
    path = tmp_path / "report.md"
    write_report(path, Theme(name="Гиза", query="пирамиды"), header, exported, facts, _posts_stats())
    text = path.read_text(encoding="utf-8")
    assert "### Карусель 1 — Пирамиды" in text
    assert "| Слайд | Файл | Оценка | Ориентация | Пометки | Причина | Кадрирование (4:5) | Запасные |" in text
    assert "| 1 | 01_IMG_0.HEIC | 8 | горизонтальная |  | Причина S0 | до 4:5: справа | 01a_IMG_1.HEIC |" in text
    assert "| № | Файл | Оценка | Тип | Что пояснить | Причина | Кадрирование (9:16) |" in text
    assert "| 2 | 02_IMG_4.HEIC | 8 | интересное | рассказать о ладье Хеопса | Причина S4 | до 9:16: центр |" in text
    assert "лента / stories" not in text
    assert "Посты: каруселей 1 (2), stories 2" in text
    assert "сборка $0.1900 (по теме)" in text and "Отправлено в сборку: 30" in text


def test_report_fallback_warning_and_empty_posts(tmp_path):
    exported = export_posts(FakeLibrary([], {}, {}), Posts([], []), tmp_path / "theme", tmp_path / "tmp", AppConfig())
    header = ReportHeader(screen_stage="claude-haiku-5-5, medium, 1024 px", judge_stage="claude-opus-5-5, medium, 768 px",
                          prompt_versions="screen-2, judge-4, compose-1", run_at=NOW,
                          warnings=["Сборка не удалась — разложено по оценкам (refusal)"], album="Egypt.26")
    path = tmp_path / "report.md"
    write_report(path, Theme(name="Гиза", query="пирамиды"), header, exported, {}, _posts_stats())
    text = path.read_text(encoding="utf-8")
    assert "разложено по оценкам" in text and "Каруселей нет" in text and "Посты: каруселей 0, stories 0" in text


def test_report_explains_scores(tmp_path):
    # приёмка 0.3: колонка «Лента / stories» была непонятна, а «Причину» принимали за решение сборки
    exported = export_posts(FakeLibrary([], {}, {}), Posts([], []), tmp_path / "theme", tmp_path / "tmp", AppConfig())
    cloud = ReportHeader(screen_stage="claude-haiku-5-5, medium, 1024 px", judge_stage="claude-opus-5-5, medium, 768 px",
                         prompt_versions="v", run_at=NOW, warnings=[], album="Egypt.26")
    local = ReportHeader(screen_stage=None, judge_stage=None, prompt_versions=None, run_at=NOW, warnings=[])
    theme = Theme(name="Гиза", query="пирамиды")
    write_report(tmp_path / "cloud.md", theme, cloud, exported, {}, _posts_stats())
    write_report(tmp_path / "local.md", theme, local, exported, {}, _posts_stats())
    assert ("Оценка — сила кадра по второй ступени, 1–10; «Причина» — её пояснение к оценке, "
            "а не решение сборки.") in (tmp_path / "cloud.md").read_text(encoding="utf-8")
    assert "Оценка — локальная оценка качества 0–1" in (tmp_path / "local.md").read_text(encoding="utf-8")



def test_report_header_stage_lines(tmp_path):
    from photo_chooser.report import stage_line

    assert stage_line("claude-opus-5-5", "high", 768) == "claude-opus-5-5, high, 768 px"
    header = ReportHeader(screen_stage=stage_line("claude-haiku-5-5", "medium", 1024),
                          judge_stage=stage_line("claude-opus-5-5", "high", 768),
                          prompt_versions="screen-4, judge-7, compose-5", run_at=NOW)
    path = tmp_path / "report.md"
    write_report(path, Theme(name="Гиза", query="пирамиды"), header, _empty_posts(tmp_path), {}, _stats())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert ("- Первая ступень: claude-haiku-5-5, medium, 1024 px; вторая ступень и сборка: claude-opus-5-5, "
            "high, 768 px") in lines
    assert not any(line.startswith("- effort:") for line in lines)
