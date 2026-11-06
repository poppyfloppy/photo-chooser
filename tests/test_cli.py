from datetime import UTC, datetime

import anthropic
import pytest
from typer.testing import CliRunner

from conftest import make_asset
from fakes import FakeClient, FakeLibrary, credit_error
from imaging import save_jpeg, varied_scene
from photo_chooser import cli
from photo_chooser.cache import Cache
from photo_chooser.config import AppConfig
from photo_chooser.judge import transport
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


def test_credit_error_exit_6(env, monkeypatch):
    monkeypatch.setattr(cli, "make_client", lambda: FakeClient(lambda n, kw: credit_error()))
    result = runner.invoke(cli.app, ["run", "--album", "Египет 2026", "--theme", "Гиза", "--query", "пирамиды"])
    assert result.exit_code == 6
    assert ("На балансе Claude API не хватает средств — пополните баланс в Claude Console. Оплаченное сохранено "
            "в кэше, повторный запуск продолжит") in result.output


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
    assert "photo-chooser key set" in result.output


def test_make_client_uses_keychain_key(monkeypatch):
    monkeypatch.setattr(transport.keychain, "read_api_key", lambda: "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert transport.make_client().api_key == "sk-test"


def test_make_client_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(transport.keychain, "read_api_key", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert transport.make_client().api_key == "sk-env"


@pytest.mark.parametrize(("code", "line", "exit_code"), [
    (0, "Ключ сохранён в Keychain (запись anthropic-api-key)", 0),
    (1, "Ключ не сохранён: security завершилась с кодом 1", 1),
])
def test_key_set_success_and_failure(monkeypatch, code, line, exit_code):
    monkeypatch.setattr(cli.keychain, "store_api_key", lambda: code)
    result = runner.invoke(cli.app, ["key", "set"])
    assert result.exit_code == exit_code
    assert line in result.output


def test_key_delete_confirms(monkeypatch):
    deleted: list[bool] = []
    monkeypatch.setattr(cli.keychain, "delete_api_key", lambda: deleted.append(True) or True)
    declined = runner.invoke(cli.app, ["key", "delete"], input="n\n")
    assert declined.exit_code == 1 and "Отменено: ключ не удалён" in declined.output
    assert deleted == []
    confirmed = runner.invoke(cli.app, ["key", "delete", "--yes"])
    assert confirmed.exit_code == 0 and "Ключ удалён из Keychain" in confirmed.output


def test_key_delete_without_item(monkeypatch):
    monkeypatch.setattr(cli.keychain, "delete_api_key", lambda: False)
    result = runner.invoke(cli.app, ["key", "delete", "--yes"])
    assert result.exit_code == 0 and "В Keychain нет записи anthropic-api-key" in result.output


@pytest.mark.parametrize(("in_keychain", "env_key", "ant", "line"), [
    (True, "sk-env", True, "Ключ Claude API: Keychain (запись anthropic-api-key)"),
    (False, "sk-env", True, "Ключ Claude API: переменная ANTHROPIC_API_KEY"),
    (False, None, True, "Ключ Claude API: профиль ant auth login"),
    (False, None, False, "Ключ Claude API не найден — выполните `photo-chooser key set`"),
])
def test_key_status_order(monkeypatch, in_keychain, env_key, ant, line):
    monkeypatch.setattr(cli.keychain, "has_api_key", lambda: in_keychain)
    monkeypatch.setattr(cli, "has_credentials", lambda client: ant)
    if env_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", env_key)
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["key", "status"])
    assert result.exit_code == 0
    assert result.output.strip() == line
    assert "sk-" not in result.output


def test_albums_lists_titles_and_counts(env):
    result = runner.invoke(cli.app, ["albums"])
    assert result.exit_code == 0
    assert "«Египет 2026» — 1 фото" in result.output and "«Кофе» — 30 фото" in result.output


def test_run_no_cloud_writes_posts(env):
    tmp_path, library, _ = env
    result = runner.invoke(cli.app, ["run", "--album", "Египет 2026", "--theme", "Гиза", "--query", "пирамиды",
                                     "--no-cloud"])
    assert result.exit_code == 0, result.output
    assert str(tmp_path / "out" / "Египет 2026") in result.output
    assert "«Гиза»: каруселей 1 (1), stories 0" in result.output
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


def _style_env(tmp_path, monkeypatch, n_posts=12, text="## Лента\nТёплый свет.\n\n## Stories\nЛегко.",
               without_preview=0):
    from datetime import timedelta

    from fakes import fake_response
    from photo_chooser.config import StyleCfg

    posts = [make_asset(uuid=f"S{i:02d}", taken_at=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(days=i))
             for i in range(n_posts)]
    shots = [make_asset(uuid="G0", width=1179, height=2556, is_screenshot=True)]
    previews = {a.uuid: save_jpeg(varied_scene(i), tmp_path / f"s{i}.jpg") for i, a in enumerate(posts + shots)
                if i >= without_preview}
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



def test_style_command_missing_previews_keeps_profile(tmp_path, monkeypatch):
    client, _ = _style_env(tmp_path, monkeypatch, without_preview=11)
    profile = tmp_path / "cfg" / "style.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("хороший профиль\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["style", "--yes"])
    assert result.exit_code == 2 and client.calls == []
    assert profile.read_text(encoding="utf-8") == "хороший профиль\n"
    assert not (tmp_path / "cfg" / "style.prev.md").exists()


def test_batch_flag_removed(env):
    result = runner.invoke(cli.app, ["run", "--album", "Египет 2026", "--theme", "Гиза", "--query", "пирамиды",
                                     "--batch"])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_style_command_uses_judge_effort(tmp_path, monkeypatch):
    client, _ = _style_env(tmp_path, monkeypatch)
    result = runner.invoke(cli.app, ["style", "--yes"])
    assert result.exit_code == 0, result.output
    assert client.calls[0]["output_config"]["effort"] == "high"
