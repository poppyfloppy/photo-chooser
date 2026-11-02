from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import make_asset
from photo_chooser.config import (
    ConfigError,
    Theme,
    TripConfig,
    load_app_config,
    load_trip,
    themes_key,
    trip_from_args,
)


def test_theme_requires_query_or_interval():
    with pytest.raises(ConfigError):
        Theme(name="X")


def test_theme_interval_needs_both_bounds():
    with pytest.raises(ConfigError):
        Theme(name="X", start=datetime(2026, 3, 14, 5, 0))


def test_theme_interval_start_before_end():
    with pytest.raises(ConfigError):
        Theme(name="X", start=datetime(2026, 3, 14, 12, 0), end=datetime(2026, 3, 14, 5, 0))
    with pytest.raises(ConfigError):
        Theme(name="X", start=datetime(2026, 3, 14, 5, 0), end=datetime(2026, 3, 14, 5, 0))


def test_theme_key_stable_and_sensitive():
    a = Theme(name="Гиза", query="пирамиды")
    assert a.key() == Theme(name="Гиза", query="пирамиды").key()
    assert len(a.key()) == 16
    assert a.key() != Theme(name="Гиза", query="сфинкс").key()
    assert a.key() != Theme(name="Гиза 2", query="пирамиды").key()
    with_interval = Theme(name="Гиза", query="пирамиды",
                          start=datetime(2026, 3, 14, 5, 0), end=datetime(2026, 3, 14, 12, 0))
    assert a.key() != with_interval.key()
    later = Theme(name="Гиза", query="пирамиды",
                  start=datetime(2026, 3, 14, 6, 0), end=datetime(2026, 3, 14, 12, 0))
    assert with_interval.key() != later.key()


def test_theme_kind_properties():
    interval = dict(start=datetime(2026, 3, 15, 0, 0), end=datetime(2026, 3, 15, 23, 59))
    assert Theme(name="День 3", **interval).interval_only
    assert Theme(name="День 3", **interval).has_interval
    assert not Theme(name="Гиза", query="пирамиды", **interval).interval_only
    assert not Theme(name="Гиза", query="пирамиды").has_interval


def test_timezone_required_when_any_theme_has_interval():
    themes = [Theme(name="Гиза", query="пирамиды"),
              Theme(name="День 3", start=datetime(2026, 3, 15, 0, 0), end=datetime(2026, 3, 15, 23, 59))]
    with pytest.raises(ConfigError):
        TripConfig(album="Египет 2026", themes=themes)


def test_timezone_not_required_for_query_only_themes():
    trip = TripConfig(album="Египет 2026", themes=[Theme(name="Гиза", query="пирамиды")])
    assert trip.tz is None


def test_timezone_must_be_iana_name():
    themes = [Theme(name="Гиза", query="пирамиды")]
    with pytest.raises(ConfigError):
        TripConfig(album="Египет 2026", timezone="Cairo", themes=themes)
    trip = TripConfig(album="Египет 2026", timezone="Africa/Cairo", themes=themes)
    assert trip.tz == ZoneInfo("Africa/Cairo")


def test_trip_requires_themes_with_unique_names():
    with pytest.raises(ConfigError):
        TripConfig(album="Египет 2026", themes=[])
    with pytest.raises(ConfigError):
        TripConfig(album="Египет 2026", themes=[Theme(name="Гиза", query="пирамиды"),
                                                Theme(name="гиза", query="сфинкс")])


def test_query_themes_keep_file_order():
    themes = [Theme(name="Б", query="b"),
              Theme(name="День", start=datetime(2026, 3, 15, 0, 0), end=datetime(2026, 3, 15, 23, 59)),
              Theme(name="А", query="a")]
    trip = TripConfig(album="X", timezone="UTC", themes=themes)
    assert [t.name for t in trip.query_themes] == ["Б", "А"]


def test_load_trip_parses_aliases(tmp_path):
    path = tmp_path / "trip.yaml"
    path.write_text(
        'album: "Египет 2026"\n'
        "timezone: Africa/Cairo\n"
        "themes:\n"
        "  - name: Абу-Симбел\n"
        '    query: "храм с колоссами Рамзеса"\n'
        "    from: 2026-03-14 05:00\n"
        "    to:   2026-03-14 12:00\n",
        encoding="utf-8",
    )
    trip = load_trip(path)
    assert trip.album == "Египет 2026"
    assert trip.themes[0].start == datetime(2026, 3, 14, 5, 0)
    assert trip.themes[0].end == datetime(2026, 3, 14, 12, 0)


def test_load_trip_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_trip(tmp_path / "nope.yaml")


def test_load_trip_unknown_key_is_config_error(tmp_path):
    path = tmp_path / "trip.yaml"
    path.write_text("album: X\nthemes:\n  - name: A\n    querry: a\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_trip(path)


def test_app_config_defaults_when_file_missing(tmp_path):
    cfg = load_app_config(tmp_path / "nope.yaml")
    assert (cfg.posts.carousels, cfg.posts.carousel_size, cfg.posts.stories) == (2, 9, 9)
    assert cfg.cloud.compose_candidates == 30
    assert cfg.cloud.screen_model == "claude-haiku-5-5" and cfg.cloud.model == "claude-haiku-5-5"
    assert cfg.cloud.max_cost_usd_per_album == 5.0
    assert cfg.cloud.pricing["claude-opus-5-5"].output == 20.00
    assert cfg.cloud.candidates_per_theme == 100
    assert cfg.cloud.group_size == 20
    assert cfg.cloud.preview_long_side_px == 1024
    assert cfg.thresholds.duplicate_distance == 0.45
    assert cfg.thresholds.min_theme_relevance == 5
    assert "~" not in str(cfg.output_dir)


def test_app_config_partial_override(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("posts:\n  carousel_size: 5\n", encoding="utf-8")
    cfg = load_app_config(path)
    assert cfg.posts.carousel_size == 5
    assert cfg.posts.stories == 9


def test_app_config_pricing_override_keeps_other_models(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("cloud:\n  pricing:\n    claude-haiku-5-5: {input: 0.2, output: 1.0}\n", encoding="utf-8")
    cfg = load_app_config(path)
    assert cfg.cloud.pricing["claude-haiku-5-5"].input == 0.2
    assert cfg.cloud.pricing["claude-opus-5-5"].input == 4.00


def test_app_config_bad_effort_is_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("cloud:\n  effort: extreme\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_app_config(path)


def test_trip_from_args_builds_single_theme():
    trip = trip_from_args("Египет 2026", "Гиза", "пирамиды", "2026-03-12 06:00", "2026-03-12 14:00",
                          "Africa/Cairo")
    assert trip.themes[0].start == datetime(2026, 3, 12, 6, 0)
    assert trip.themes[0].query == "пирамиды"
    with pytest.raises(ConfigError):
        trip_from_args("Египет 2026", "Гиза", None, "2026-03-12 06:00", "2026-03-12 14:00", None)
    with pytest.raises(ConfigError):
        trip_from_args("Египет 2026", "Гиза", None, "12.03.2026", "2026-03-12 14:00", "Africa/Cairo")


def test_asset_is_vertical():
    assert make_asset(width=3024, height=4032).is_vertical
    assert not make_asset(width=4032, height=4032).is_vertical
    assert not make_asset(width=4032, height=3024).is_vertical


def test_themes_key_depends_on_album_and_theme_list():
    a = Theme(name="Гиза", query="пирамиды")
    b = Theme(name="Нил", query="река")
    base = themes_key("Egypt.26", [a, b])
    assert base == themes_key("Egypt.26", [Theme(name="Гиза", query="пирамиды"), Theme(name="Нил", query="река")])
    assert base != themes_key("Egypt.26", [b, a])
    assert base != themes_key("Egypt.26", [a, Theme(name="Нил", query="река Нил")])
    assert base != themes_key("Egypt.27", [a, b])
    a_interval = Theme(name="Гиза", query="пирамиды",
                       start=datetime(2026, 3, 14, 5, 0), end=datetime(2026, 3, 14, 12, 0))
    assert base == themes_key("Egypt.26", [a_interval, b])


def test_style_defaults(tmp_path):
    from datetime import date

    style = load_app_config(tmp_path / "nope.yaml").style
    assert style.sample == 40 and style.reference_feed == 2 and style.reference_stories == 2
    assert style.model == "claude-opus-5-5"
    assert style.since == date(2018, 1, 1)
    assert style.albums == [] and style.screenshots_album is None


def test_style_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("style:\n  albums: [Snapseed, Instasize]\n  screenshots_album: Instagram style\n", encoding="utf-8")
    style = load_app_config(path).style
    assert style.albums == ["Snapseed", "Instasize"]
    assert style.screenshots_album == "Instagram style"
    assert style.sample == 40


def test_style_rejects_negative_reference_feed(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("style:\n  reference_feed: -1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_app_config(path)


def test_style_reference_images_key_is_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("style:\n  reference_images: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_app_config(path)
    assert "reference_feed" in str(e.value) and "reference_stories" in str(e.value)


def test_shortlist_key_is_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("shortlist:\n  feed: 10\n", encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_app_config(path)
    assert "posts.carousels" in str(e.value)
