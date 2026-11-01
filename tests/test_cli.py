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


def test_unexpected_error_writes_traceback_log(env, monkeypatch):
    tmp_path, *_ = env
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path / "logs")

    def broken():
        raise KeyError("ASSET-1/L0/001")

    monkeypatch.setattr(cli, "make_library", broken)
    result = runner.invoke(cli.app, ["albums"])
    assert result.exit_code == 1
    logs = list((tmp_path / "logs").glob("error-*.log"))
    assert len(logs) == 1 and str(logs[0]) in result.output
    text = logs[0].read_text(encoding="utf-8")
    assert "Traceback" in text and "KeyError: 'ASSET-1/L0/001'" in text and "in broken" in text


def _style_env(tmp_path, monkeypatch, n_posts=12, text="## Лента\nТёплый свет.\n\n## Stories\nЛегко."):
    from datetime import timedelta

    from fakes import fake_response
    from photo_chooser.config import StyleCfg

    posts = [make_asset(uuid=f"S{i:02d}", taken_at=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(days=i))
             for i in range(n_posts)]
    shots = [make_asset(uuid="G0", width=1179, height=2556, is_screenshot=True)]
    previews = {a.uuid: save_jpeg(varied_scene(i), tmp_path / f"s{i}.jpg") for i, a in enumerate(posts + shots)}
    library = FakeLibrary([AlbumRef("SN", "Snapseed", n_posts), AlbumRef("GR", "Instagram style", 1)],
                          {"SN": posts, "GR": shots}, previews)
    cache = Cache(tmp_path / "c.sqlite")
    client = FakeClient(lambda n, kw: fake_response(text=text))
    monkeypatch.setattr(cli, "load_app_config", lambda: AppConfig(
        output_dir=tmp_path / "out", style=StyleCfg(albums=["Snapseed"], screenshots_album="Instagram style")))
    monkeypatch.setattr(cli, "make_library", lambda: library)
    monkeypatch.setattr(cli, "make_cache", lambda: cache)
    monkeypatch.setattr(cli, "make_client", lambda: client)
    monkeypatch.setattr(cli, "STYLE_PATH", tmp_path / "cfg" / "style.md")
    return client, cache


def test_style_command_yes_writes_profile(tmp_path, monkeypatch):
    client, cache = _style_env(tmp_path, monkeypatch)
    result = runner.invoke(cli.app, ["style", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Отправить 13 изображений (постов 12, скриншотов 1) модели claude-opus-5-5" in result.output
    assert (tmp_path / "cfg" / "style.md").read_text(encoding="utf-8").startswith("## Лента")
    assert str(tmp_path / "cfg" / "style.md") in result.output
    assert len(client.calls) == 1 and cache.album_spend("style") > 0


def test_style_command_declined_sends_nothing(tmp_path, monkeypatch):
    client, _ = _style_env(tmp_path, monkeypatch)
    result = runner.invoke(cli.app, ["style"], input="n\n")
    assert result.exit_code == 1 and "Отменено" in result.output
    assert client.calls == [] and not (tmp_path / "cfg" / "style.md").exists()
    result = runner.invoke(cli.app, ["style"], input="")  # ввод не с терминала, ответа нет
    assert result.exit_code == 1 and client.calls == []


def test_style_command_too_few_posts_exit_2(tmp_path, monkeypatch):
    client, _ = _style_env(tmp_path, monkeypatch, n_posts=9)
    result = runner.invoke(cli.app, ["style", "--yes"])
    assert result.exit_code == 2 and client.calls == []
