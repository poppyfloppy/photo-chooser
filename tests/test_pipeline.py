import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import pytest

from conftest import make_asset
from fakes import FakeClient, FakeLibrary, api_error, auth_error, fake_response, ids_in, last_text, output_model_of
from imaging import blurred, save_jpeg, varied_scene
from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig, ConfigError, ShortlistCfg, Theme, TripConfig
from photo_chooser.judge.cost import BudgetExceeded, UnknownModelPrice
from photo_chooser.judge.prompts import JUDGE_PROMPT_VERSION, SCREEN_PROMPT_VERSION
from photo_chooser.judge.stages import JudgeItem, JudgeOutput, ScreenItem, ScreenMatch, ScreenOutput
from photo_chooser.models import AlbumRef
from photo_chooser.pipeline import RunOptions, run

NOW = datetime(2026, 11, 1, 15, 30)
START = datetime(2026, 3, 14, 6, 0, tzinfo=UTC)  # 08:00 в Каире


class Album:
    """Тестовый альбом: n различимых снимков с шагом 2 минуты, превью и «оригиналы» — JPEG."""

    def __init__(self, tmp_path, n=8, screenshot=False, duplicate=False, live=False, blurry=False, no_preview=0):
        self.assets, self.previews = [], {}
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        for i in range(n):
            vertical = i % 2 == 0
            size = (240, 320) if vertical else (320, 240)
            a = make_asset(uuid=f"P{i:03d}", filename=f"IMG_{i:04d}.JPG", taken_at=START + timedelta(minutes=2 * i),
                           width=size[0], height=size[1])
            self._add(a, save_jpeg(varied_scene(i, size), lib_dir / f"{i}.jpg"))
        t_last = START + timedelta(minutes=2 * n)
        if screenshot:
            a = make_asset(uuid="SHOT", filename="IMG_SHOT.PNG", taken_at=t_last, is_screenshot=True)
            self._add(a, save_jpeg(varied_scene(100), lib_dir / "shot.jpg"))
        if duplicate:  # копия снимка P000 через 5 секунд — та же сцена
            a = make_asset(uuid="DUP", filename="IMG_DUP.JPG", taken_at=START + timedelta(seconds=5),
                           width=240, height=320)
            self._add(a, save_jpeg(varied_scene(0, (240, 320)), lib_dir / "dup.jpg"))
        if live:
            a = make_asset(uuid="LIVE", filename="IMG_LIVE.JPG", taken_at=t_last + timedelta(minutes=5), is_live=True,
                           width=320, height=240)
            self._add(a, save_jpeg(varied_scene(200), lib_dir / "live.jpg"))
        if blurry:
            a = make_asset(uuid="BLUR", filename="IMG_BLUR.JPG", taken_at=t_last + timedelta(minutes=9))
            self._add(a, save_jpeg(blurred(varied_scene(300), 30), lib_dir / "blur.jpg"))
        for k in range(no_preview):
            a = make_asset(uuid=f"NOPREV{k}", filename=f"IMG_NP{k}.JPG", taken_at=t_last + timedelta(minutes=20 + k))
            self.assets.append(a)
        self.album = AlbumRef("ALB", "Egypt.26", len(self.assets))
        self.library = FakeLibrary([self.album], {"ALB": self.assets}, self.previews, files=dict(self.previews))

    def _add(self, asset, path):
        self.assets.append(asset)
        self.previews[asset.uuid] = path


def _cfg(tmp_path, feed=3, stories=5):
    return AppConfig(output_dir=tmp_path / "out", shortlist=ShortlistCfg(feed=feed, stories=stories))


def _trip(*themes, timezone="Africa/Cairo"):
    return TripConfig(album="Egypt.26", timezone=timezone, themes=list(themes))


GIZA = Theme(name="Гиза", query="пирамиды")
NILE = Theme(name="Нил", query="река")
TEMPLES = Theme(name="Храмы", query="храмы")
DAY = Theme(name="День 1", start=datetime(2026, 3, 14, 0, 0), end=datetime(2026, 3, 14, 23, 59))


def handler(match=True, fail_theme=None):
    def h(n, kw):
        content = kw["messages"][0]["content"]
        ids = ids_in(content)
        if output_model_of(kw) is ScreenOutput:
            n_themes = len(re.findall(r"^\d+\. ", last_text(content), flags=re.M))
            matches = [ScreenMatch(theme=t, relevance=8) for t in range(1, n_themes + 1)] if match else []
            return fake_response(ScreenOutput(items=[ScreenItem(id=i, matches=matches, feed_score=6, stories_score=5)
                                                     for i in ids]))
        if fail_theme and f"«{fail_theme}" in last_text(content):
            return api_error()
        return fake_response(JudgeOutput(items=[JudgeItem(id=i, matches_theme=True, feed_score=7, stories_score=5,
                                                          reason="подходит") for i in ids]))
    return h


def _run(album, trip, cfg, cache, client=None, **opts):
    client = client or FakeClient(handler())
    result = run(trip, cfg, RunOptions(**opts), album.library, cache, lambda: client, NOW)
    return result, client


def _files(path):
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


def test_no_cloud_end_to_end(tmp_path):
    album = Album(tmp_path, n=6, screenshot=True, duplicate=True, live=True, blurry=True)
    cfg = _cfg(tmp_path, feed=3, stories=10)

    def no_client():
        raise AssertionError("в режиме без облака клиент Claude не создаётся")

    result = run(_trip(GIZA), cfg, RunOptions(no_cloud=True), album.library, Cache(tmp_path / "c.sqlite"), no_client,
                 NOW)
    theme_dir = result.run_dir / "Гиза"
    assert result.run_dir == tmp_path / "out" / "Egypt.26" / "2026-11-01_1530"
    feed, stories = _files(theme_dir / "feed"), _files(theme_dir / "stories")
    assert len(feed) == 3 and [f[:3] for f in feed] == ["01_", "02_", "03_"]
    exported = " ".join(feed + stories)
    assert "SHOT" not in exported and "BLUR" not in exported
    assert ("IMG_0000" in exported) + ("IMG_DUP" in exported) == 1  # из серии — один кадр
    assert "IMG_LIVE.JPG" in exported  # видео нет — ключевой снимок
    assert len(feed) + len(stories) == 7  # 6 + Live
    report = (theme_dir / "report.md").read_text(encoding="utf-8")
    assert "без облака" in report
    assert "Отсеяно как мусор: 2" in report and "Схлопнуто как дубли: 1" in report
    assert result.themes[0].error is None


def test_stage1_line_is_dash_when_not_applicable(tmp_path):
    album = Album(tmp_path, n=4)
    result, _ = _run(album, _trip(GIZA, DAY), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), no_cloud=True)
    for outcome in result.themes:  # без облака первая ступень не запускалась
        assert "Признано подходящими к теме на первой ступени: —" in outcome.report_path.read_text(encoding="utf-8")
    result, _ = _run(album, _trip(GIZA, DAY), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"))
    giza, day = (o.report_path.read_text(encoding="utf-8") for o in result.themes)
    assert "Признано подходящими к теме на первой ступени: 4" in giza
    assert "Признано подходящими к теме на первой ступени: —" in day  # тема только с интервалом (FR-1.4)


def test_no_cloud_ignores_query_with_warning(tmp_path):
    album = Album(tmp_path, n=4)
    result, _ = _run(album, _trip(GIZA), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), no_cloud=True)
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "запрос темы не учитывается" in report


def test_assets_without_preview_skipped_and_counted(tmp_path):
    album = Album(tmp_path, n=3, no_preview=2)
    result, _ = _run(album, _trip(GIZA), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), no_cloud=True)
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "Без превью: 2" in report and "IMG_NP0.JPG — нет превью" in report
    theme_dir = result.run_dir / "Гиза"
    assert "NP" not in " ".join(_files(theme_dir / "feed") + _files(theme_dir / "stories"))
    assert album.library.exported and not {"NOPREV0", "NOPREV1"} & set(album.library.exported)


def test_screen_runs_once_for_all_themes(tmp_path):
    album = Album(tmp_path, n=45)
    cache = Cache(tmp_path / "c.sqlite")
    result, client = _run(album, _trip(GIZA, NILE, TEMPLES), _cfg(tmp_path), cache)
    assert len(client.calls_for(ScreenOutput)) == 3  # 45 снимков, группы по 20 — один проход на все темы
    assert len(client.calls_for(JudgeOutput)) == 9  # 3 темы × 45 кандидатов → по 3 группы
    for outcome in result.themes:
        assert outcome.error is None
        assert len(_files(outcome.report_path.parent / "feed")) == 3
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "claude-haiku-5-5" in report and f"{SCREEN_PROMPT_VERSION}, {JUDGE_PROMPT_VERSION}" in report
    spent = cache.album_spend("ALB")
    assert spent == pytest.approx(12 * (20_000 * 0.10 / 1e6 + 2_000 * 0.50 / 1e6))
    last_report = result.themes[-1].report_path.read_text(encoding="utf-8")  # накоплено на момент отчёта
    assert f"по альбому ${spent:.2f}" in last_report


def test_no_matches_in_screen_skips_judge(tmp_path):
    album = Album(tmp_path, n=5)
    client = FakeClient(handler(match=False))
    result, _ = _run(album, _trip(GIZA), _cfg(tmp_path, feed=20, stories=40), Cache(tmp_path / "c.sqlite"), client)
    assert len(client.calls_for(ScreenOutput)) == 1
    assert client.calls_for(JudgeOutput) == []
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "Недобор: лента 20, stories 40" in report


def test_interval_only_trip_skips_screen(tmp_path):
    album = Album(tmp_path, n=5)
    result, client = _run(album, _trip(DAY), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"))
    assert client.calls_for(ScreenOutput) == []
    assert len(client.calls_for(JudgeOutput)) == 1
    assert len(_files(result.run_dir / "День 1" / "feed")) == 3


def test_empty_interval_no_calls(tmp_path):
    album = Album(tmp_path, n=5)
    later = Theme(name="Луксор", query="храм", start=datetime(2026, 3, 20, 0, 0), end=datetime(2026, 3, 20, 23, 0))
    result, client = _run(album, _trip(later), _cfg(tmp_path, feed=20, stories=40), Cache(tmp_path / "c.sqlite"))
    assert client.calls == []
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "Прошло интервал: 0" in report and "Недобор: лента 20, stories 40" in report


def test_interval_uses_trip_timezone(tmp_path):
    album = Album(tmp_path, n=6)  # 08:00–08:10 по Каиру
    morning = Theme(name="Утро", start=datetime(2026, 3, 14, 8, 0), end=datetime(2026, 3, 14, 8, 3))
    result, _ = _run(album, _trip(morning), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), no_cloud=True)
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "Прошло интервал: 2" in report  # 08:00 и 08:02


def test_budget_checked_before_any_call(tmp_path):
    album = Album(tmp_path, n=5)
    cache = Cache(tmp_path / "c.sqlite")
    cache.add_spend("ALB", "claude-haiku-5-5", 4.9999)
    client = FakeClient(handler())
    with pytest.raises(BudgetExceeded):
        _run(album, _trip(GIZA), _cfg(tmp_path), cache, client)
    assert client.calls == []
    assert not (tmp_path / "out").exists()


def test_max_cost_overrides_limit(tmp_path):
    album = Album(tmp_path, n=5)
    cache = Cache(tmp_path / "c.sqlite")
    cache.add_spend("ALB", "claude-haiku-5-5", 4.9999)
    result, client = _run(album, _trip(GIZA), _cfg(tmp_path), cache, max_cost=10.0)
    assert client.calls and result.run_dir.exists()


def test_dry_run_no_calls_no_files(tmp_path):
    album = Album(tmp_path, n=5, screenshot=True)
    result, client = _run(album, _trip(GIZA, DAY), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), dry_run=True)
    assert client.calls == [] and album.library.exported == []
    assert result.run_dir is None and not (tmp_path / "out").exists()
    assert result.estimate_usd > 0
    assert result.theme_counts == {"Гиза": 5, "День 1": 5}


def test_dry_run_estimate_drops_cached_work(tmp_path):
    album = Album(tmp_path, n=5)
    cache, cfg = Cache(tmp_path / "c.sqlite"), _cfg(tmp_path)
    first, _ = _run(album, _trip(GIZA), cfg, cache, dry_run=True)
    _run(album, _trip(GIZA), cfg, cache)
    again, _ = _run(album, _trip(GIZA), cfg, cache, dry_run=True)
    assert first.estimate_usd > 0 and again.estimate_usd == 0  # повтор той же темы не платит


def test_unknown_model_price_stops_before_call(tmp_path):
    album = Album(tmp_path, n=5)
    client = FakeClient(handler())
    with pytest.raises(UnknownModelPrice):
        _run(album, _trip(GIZA), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), client, model="claude-mystery-1")
    assert client.calls == []


def test_theme_failure_does_not_stop_other_themes(tmp_path):
    album = Album(tmp_path, n=5)
    client = FakeClient(handler(fail_theme="Гиза"))
    result, _ = _run(album, _trip(GIZA, NILE), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), client)
    giza, nile = result.themes
    assert giza.error is not None and nile.error is None
    assert len(_files(result.run_dir / "Нил" / "feed")) == 3
    assert "Тема не обработана" in giza.report_path.read_text(encoding="utf-8")


def test_screen_failure_stops_run(tmp_path):
    album = Album(tmp_path, n=5)
    client = FakeClient(lambda n, kw: api_error())
    with pytest.raises(Exception):
        _run(album, _trip(GIZA), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), client)
    assert not (tmp_path / "out" / "Egypt.26").exists() or not any((tmp_path / "out" / "Egypt.26").iterdir())


def test_model_override_keys_cache_separately(tmp_path):
    album = Album(tmp_path, n=5)
    cache, cfg = Cache(tmp_path / "c.sqlite"), _cfg(tmp_path)
    _, haiku = _run(album, _trip(GIZA), cfg, cache)
    assert len(haiku.calls_for(ScreenOutput)) == 1 and len(haiku.calls_for(JudgeOutput)) == 1
    result, opus = _run(album, _trip(GIZA), cfg, cache, model="claude-opus-5-5")
    assert opus.calls_for(ScreenOutput) == []  # первая ступень — из кэша
    judge_calls = opus.calls_for(JudgeOutput)
    assert len(judge_calls) == 1 and judge_calls[0]["model"] == "claude-opus-5-5" and judge_calls[0]["_beta"]
    assert "claude-opus-5-5" in result.themes[0].report_path.read_text(encoding="utf-8")
    _, again = _run(album, _trip(GIZA), cfg, cache, model="claude-opus-5-5")
    assert again.calls == []


def test_theme_dirs_with_unsafe_names(tmp_path):
    album = Album(tmp_path, n=4)
    themes = [Theme(name=" Гиза/Каир: день 1 🐪 ", query="пирамиды"), Theme(name="Гиза_Каир_ день 1 🐪", query="x")]
    result, _ = _run(album, _trip(*themes), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), no_cloud=True)
    dirs = sorted(p.name for p in result.run_dir.iterdir())
    assert dirs == ["Гиза_Каир_ день 1 🐪", "Гиза_Каир_ день 1 🐪_2"]
    assert all(o.report_path.exists() for o in result.themes)


def test_editing_theme_interval_keeps_stage1_relevance(tmp_path):
    album = Album(tmp_path, n=6)  # 08:00–08:10 по Каиру
    cache, cfg = Cache(tmp_path / "c.sqlite"), _cfg(tmp_path)
    first = Theme(name="Гиза", query="пирамиды", start=datetime(2026, 3, 14, 7, 0), end=datetime(2026, 3, 14, 9, 0))
    _run(album, _trip(first), cfg, cache)
    edited = Theme(name="Гиза", query="пирамиды", start=datetime(2026, 3, 14, 7, 30), end=datetime(2026, 3, 14, 9, 0))
    result, client = _run(album, _trip(edited), cfg, cache)
    assert client.calls_for(ScreenOutput) == []  # набор тем тот же — первая ступень из кэша
    assert len(client.calls_for(JudgeOutput)) == 1  # ключ второй ступени включает интервал (FR-9.3)
    assert len(_files(result.run_dir / "Гиза" / "feed")) == 3
    query_only = Theme(name="Гиза", query="пирамиды")
    result, _ = _run(album, _trip(query_only), cfg, cache)
    assert len(_files(result.run_dir / "Гиза" / "feed")) == 3


def test_screen_covers_whole_pool_when_any_query_theme_in_scope(tmp_path):
    # FR-3 п.5: первая ступень — по всем снимкам после отсева; черновые оценки нужны и темам-интервалам
    album = Album(tmp_path, n=6)
    early = Theme(name="Утро", query="пирамиды", start=datetime(2026, 3, 14, 8, 0), end=datetime(2026, 3, 14, 8, 3))
    result, client = _run(album, _trip(early, DAY), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"))
    screen_calls = client.calls_for(ScreenOutput)
    assert len(screen_calls) == 1 and len(ids_in(screen_calls[0]["messages"][0]["content"])) == 6
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "Отправлено на первую ступень (общая на запуск): 6" in report


def test_missing_api_key_stops_before_previews(tmp_path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    album = Album(tmp_path, n=3)
    with pytest.raises(ConfigError) as e:
        run(_trip(GIZA), _cfg(tmp_path), RunOptions(), album.library, Cache(tmp_path / "c.sqlite"),
            lambda: anthropic.Anthropic(), NOW)
    assert "ANTHROPIC_API_KEY" in str(e.value)
    assert album.library.preview_requests == []  # до долгого чтения превью
    result = run(_trip(GIZA), _cfg(tmp_path), RunOptions(dry_run=True), album.library, Cache(tmp_path / "c.sqlite"),
                 lambda: anthropic.Anthropic(), NOW)
    assert result.estimate_usd > 0  # --dry-run ключа не требует


def test_auth_error_in_judge_stops_run(tmp_path):
    album = Album(tmp_path, n=3)
    ok = handler()
    client = FakeClient(lambda n, kw: auth_error() if output_model_of(kw) is JudgeOutput else ok(n, kw))
    with pytest.raises(anthropic.AuthenticationError):
        _run(album, _trip(GIZA, NILE), _cfg(tmp_path), Cache(tmp_path / "c.sqlite"), client)


MEMORY_PROBE = """
import resource, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from conftest import make_asset
from fakes import FakeLibrary
from imaging import save_jpeg, varied_scene
from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig
from photo_chooser.models import AlbumRef
from photo_chooser.pipeline import prepare_album

tmp = Path(sys.argv[1])
t0 = datetime(2026, 3, 14, 6, 0, tzinfo=UTC)
assets, previews = [], {}
for i in range(120):
    a = make_asset(uuid=f"M{i:03d}", taken_at=t0 + timedelta(minutes=2 * i))
    assets.append(a)
    previews[a.uuid] = save_jpeg(varied_scene(i, (1024, 768)), tmp / f"{i}.jpg")
lib = FakeLibrary([AlbumRef("A", "A", 120)], {"A": assets}, previews)
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
prepare_album(lib, Cache(tmp / "c.sqlite"), lib.find_album("A"), AppConfig())
print((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - base) // (1024 * 1024))
"""


def test_prepare_album_memory_stays_flat(tmp_path):
    # Vision/ImageIO в цикле без autorelease pool копят объекты: ~5 МБ на снимок, альбом в 2500 фото — гигабайты
    tests_dir = str(Path(__file__).parent)
    out = subprocess.run([sys.executable, "-c", MEMORY_PROBE, str(tmp_path), tests_dir], capture_output=True,
                         text=True, check=True)
    growth_mb = int(out.stdout.strip().splitlines()[-1])
    assert growth_mb < 250, f"пиковая память выросла на {growth_mb} МБ за 120 превью"


def _with_screenshots(album, tmp_path, n=1):
    shots = [make_asset(uuid=f"SHOT{i}", width=1179, height=2556, is_screenshot=True) for i in range(n)]
    previews = {a.uuid: save_jpeg(varied_scene(500 + i, (590, 1278)), tmp_path / f"shot{i}.jpg")
                for i, a in enumerate(shots)}
    album.library.add_album(AlbumRef("GRID", "Instagram style", n), shots, previews)


def _style_cfg(tmp_path, **kw):
    from photo_chooser.config import StyleCfg

    return AppConfig(output_dir=tmp_path / "out", shortlist=ShortlistCfg(feed=3, stories=5),
                     style=StyleCfg(albums=["Snapseed"], screenshots_album="Instagram style", **kw))


def test_run_with_style_sends_profile_and_marks_header(tmp_path):
    album = Album(tmp_path, n=4)
    _with_screenshots(album, tmp_path)
    style_path = tmp_path / "style.md"
    style_path.write_text("## Лента\nМягкий тёплый свет.\n", encoding="utf-8")
    client = FakeClient(handler())
    result = run(_trip(GIZA), _style_cfg(tmp_path, reference_images=1), RunOptions(), album.library,
                 Cache(tmp_path / "c.sqlite"), lambda: client, NOW, style_path=style_path)
    judge_calls = client.calls_for(JudgeOutput)
    assert judge_calls and "Мягкий тёплый свет." in judge_calls[0]["system"]
    assert "Мягкий тёплый свет." not in client.calls_for(ScreenOutput)[0]["system"]
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "- Стиль: style.md от " in report and "скриншотов в запросе: 1" in report
    assert "judge-2/style-" in report


def test_run_without_profile_warns(tmp_path):
    album = Album(tmp_path, n=3)
    result = run(_trip(GIZA), _style_cfg(tmp_path), RunOptions(), album.library, Cache(tmp_path / "c.sqlite"),
                 lambda: FakeClient(handler()), NOW, style_path=tmp_path / "nope.md")
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "Профиль стиля не найден" in report and "photo-chooser style" in report


def test_run_no_cloud_ignores_style(tmp_path):
    album = Album(tmp_path, n=3)
    style_path = tmp_path / "style.md"
    style_path.write_text("## Лента\n…\n", encoding="utf-8")
    result = run(_trip(GIZA), _style_cfg(tmp_path), RunOptions(no_cloud=True), album.library,
                 Cache(tmp_path / "c.sqlite"), lambda: None, NOW, style_path=style_path)
    report = result.themes[0].report_path.read_text(encoding="utf-8")
    assert "Стиль:" not in report and "Профиль стиля не найден" not in report


def test_dry_run_estimate_includes_style_tokens(tmp_path):
    album = Album(tmp_path, n=5)
    _with_screenshots(album, tmp_path, n=2)
    style_path = tmp_path / "style.md"
    style_path.write_text("## Лента\n" + "тёплый свет " * 200, encoding="utf-8")
    common = dict(library=album.library, client_factory=lambda: None, now=NOW)
    plain = run(_trip(GIZA), _style_cfg(tmp_path), RunOptions(dry_run=True), cache=Cache(tmp_path / "a.sqlite"),
                **common)
    styled = run(_trip(GIZA), _style_cfg(tmp_path), RunOptions(dry_run=True), cache=Cache(tmp_path / "b.sqlite"),
                 style_path=style_path, **common)
    assert styled.estimate_usd > plain.estimate_usd
