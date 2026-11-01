from datetime import UTC, datetime

import anthropic
import pytest
from typer.testing import CliRunner

from conftest import make_asset
from fakes import FakeClient, FakeLibrary
from imaging import save_jpeg, varied_scene
from photo_chooser import cli
from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig
from photo_chooser.library.base import PhotosAccessError
from photo_chooser.models import AlbumRef

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    preview = save_jpeg(varied_scene(1), tmp_path / "p.jpg")
    asset = make_asset(uuid="A1", filename="IMG_1.JPG", taken_at=datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    library = FakeLibrary([AlbumRef("ALB", "Египет 2026", 1), AlbumRef("ALB2", "Кофе", 30)], {"ALB": [asset]},
                          {"A1": preview}, files={"A1": preview})
    cache = Cache(tmp_path / "c.sqlite")
    monkeypatch.setattr(cli, "load_app_config", lambda: AppConfig(output_dir=tmp_path / "out"))
    monkeypatch.setattr(cli, "make_library", lambda: library)
    monkeypatch.setattr(cli, "make_cache", lambda: cache)

    def no_calls(n, kwargs):
        raise AssertionError("Claude не должен вызываться")

    monkeypatch.setattr(cli, "make_client", lambda: FakeClient(no_calls))
    return tmp_path, library, cache


def test_run_requires_trip_or_album_and_theme(env):
    assert runner.invoke(cli.app, ["run"]).exit_code == 2
    result = runner.invoke(cli.app, ["run", "--album", "Египет 2026"])
    assert result.exit_code == 2 and "--theme" in result.output


def test_trip_file_and_theme_flags_are_exclusive(env):
    tmp_path, *_ = env
    trip = tmp_path / "trip.yaml"
    trip.write_text("album: Египет 2026\nthemes:\n  - name: Гиза\n    query: пирамиды\n", encoding="utf-8")
    assert runner.invoke(cli.app, ["run", str(trip), "--theme", "X"]).exit_code == 2


def test_interval_without_timezone_exit_2(env):
    result = runner.invoke(cli.app, ["run", "--album", "Египет 2026", "--theme", "Гиза", "--query", "пирамиды",
                                     "--from", "2026-03-12 06:00", "--to", "2026-03-12 14:00"])
    assert result.exit_code == 2
    assert "--timezone" in result.output


def test_album_not_found_exit_4_lists_similar(env):
    result = runner.invoke(cli.app, ["run", "--album", "Египт 2026", "--theme", "Гиза", "--query", "пирамиды",
                                     "--no-cloud"])
    assert result.exit_code == 4
    assert "Египет 2026" in result.output


def test_budget_exceeded_exit_3(env):
    _, _, cache = env
    cache.add_spend("ALB", "claude-haiku-5-5", 5.0)
    result = runner.invoke(cli.app, ["run", "--album", "Египет 2026", "--theme", "Гиза", "--query", "пирамиды"])
    assert result.exit_code == 3
    assert "--max-cost" in result.output


def test_unknown_model_exit_2(env):
    result = runner.invoke(cli.app, ["run", "--album", "Египет 2026", "--theme", "Гиза", "--query", "пирамиды",
                                     "--model", "claude-mystery-1"])
    assert result.exit_code == 2 and "claude-mystery-1" in result.output


def test_photos_access_error_exit_5_mentions_settings(env, monkeypatch):
    def denied():
        raise PhotosAccessError()

    monkeypatch.setattr(cli, "make_library", denied)
    result = runner.invoke(cli.app, ["albums"])
    assert result.exit_code == 5
    assert "System Settings → Privacy & Security → Photos" in result.output


def test_missing_api_key_exit_2_with_hint(env, monkeypatch):
    monkeypatch.setattr(cli, "make_client", lambda: anthropic.Anthropic(api_key=None, auth_token=None))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(env[0] / "home"))
    result = runner.invoke(cli.app, ["run", "--album", "Египет 2026", "--theme", "Гиза", "--query", "пирамиды"])
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.output and "--no-cloud" in result.output


def test_albums_lists_titles_and_counts(env):
    result = runner.invoke(cli.app, ["albums"])
    assert result.exit_code == 0
    assert "«Египет 2026» — 1 фото" in result.output and "«Кофе» — 30 фото" in result.output


def test_run_no_cloud_writes_shortlist(env):
    tmp_path, library, _ = env
    result = runner.invoke(cli.app, ["run", "--album", "Египет 2026", "--theme", "Гиза", "--query", "пирамиды",
                                     "--no-cloud"])
    assert result.exit_code == 0, result.output
    assert str(tmp_path / "out" / "Египет 2026") in result.output
    assert library.exported == ["A1"]


def test_dry_run_prints_counts_and_estimate(env):
    tmp_path, library, _ = env
    trip = tmp_path / "trip.yaml"
    trip.write_text("album: Египет 2026\ntimezone: Africa/Cairo\nthemes:\n  - name: Гиза\n    query: пирамиды\n"
                    "  - name: День 1\n    from: 2026-03-12 00:00\n    to: 2026-03-12 23:59\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["run", str(trip), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "«Гиза»: 1" in result.output and "«День 1»: 1" in result.output
    assert "Оценка стоимости" in result.output and "$" in result.output
    assert library.exported == [] and not (tmp_path / "out").exists()
