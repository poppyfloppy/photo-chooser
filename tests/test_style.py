import base64
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from conftest import make_asset
from fakes import FakeClient, FakeLibrary, fake_response
from imaging import save_jpeg, varied_scene
from photo_chooser import style
from photo_chooser.config import AppConfig, ConfigError, StyleCfg
from photo_chooser.library.base import AlbumNotFound
from photo_chooser.models import AlbumRef

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _posts(n, start=NOW, step_days=1, prefix="P"):
    return [make_asset(uuid=f"{prefix}{i:04d}", taken_at=start - timedelta(days=i * step_days)) for i in range(n)]


def _library(albums: dict[str, list], titles: dict[str, str] | None = None):
    titles = titles or {k: k for k in albums}
    refs = [AlbumRef(uuid=k, title=titles[k], count=len(v)) for k, v in albums.items()]
    return FakeLibrary(refs, albums, {})


def _preview(asset, side):
    return f"{asset.uuid}@{side}".encode()


def test_select_posts_half_from_newest_quarter():
    pool = _posts(400)
    shuffled = random.Random(1).sample(pool, len(pool))
    picked = style.select_posts(shuffled, date(2018, 1, 1), 40)
    assert len(picked) == 40 and len({a.uuid for a in picked}) == 40
    assert picked[0].uuid == "P0000"  # самый новый
    newest_quarter = {a.uuid for a in pool[:100]}
    assert sum(a.uuid in newest_quarter for a in picked) == 20
    assert picked == sorted(picked, key=lambda a: a.taken_at, reverse=True)


def test_select_posts_drops_old_and_duplicates():
    fresh = _posts(5)
    old = make_asset(uuid="OLD", taken_at=datetime(2017, 12, 31, tzinfo=UTC))
    epoch = make_asset(uuid="EPOCH", taken_at=datetime(1970, 1, 1, tzinfo=UTC))
    picked = style.select_posts(fresh + [old, epoch, fresh[0]], date(2018, 1, 1), 40)
    assert [a.uuid for a in picked] == [a.uuid for a in fresh]


def test_select_posts_takes_all_when_few():
    pool = _posts(12)
    assert style.select_posts(list(reversed(pool)), date(2018, 1, 1), 40) == pool


def test_gather_samples_merges_albums_with_same_title():
    s1, s2, insta = _posts(8, prefix="S"), _posts(5, prefix="T"), _posts(6, prefix="I")
    lib = _library({"a1": s1, "a2": s2 + [s1[0]], "a3": insta}, {"a1": "Snapseed", "a2": "Snapseed", "a3": "Instasize"})
    posts, shots = style.gather_samples(lib, StyleCfg(albums=["Snapseed", "Instasize"]))
    assert {a.uuid for a in posts} == {a.uuid for a in s1 + s2 + insta}
    assert len(posts) == 19 and shots == []


def test_gather_samples_screenshots_keep_album_order():
    shots = [make_asset(uuid=f"G{i:02d}", taken_at=NOW - timedelta(days=(i * 7) % 5), is_screenshot=True)
             for i in range(15)]
    lib = _library({"a1": _posts(10), "g": shots}, {"a1": "Snapseed", "g": "Instagram style"})
    _, picked = style.gather_samples(lib, StyleCfg(albums=["Snapseed"], screenshots_album="Instagram style"))
    assert [a.uuid for a in picked] == [f"G{i:02d}" for i in range(12)]


def test_gather_samples_unknown_album_lists_similar():
    lib = _library({"a1": _posts(10)}, {"a1": "Snapseed"})
    with pytest.raises(AlbumNotFound) as e:
        style.gather_samples(lib, StyleCfg(albums=["Snapsed"]))
    assert "Snapseed" in e.value.similar


def test_gather_samples_needs_albums_configured():
    with pytest.raises(ConfigError):
        style.gather_samples(_library({"a1": _posts(10)}), StyleCfg())


def test_gather_samples_too_few_posts():
    lib = _library({"a1": _posts(9)}, {"a1": "Snapseed"})
    with pytest.raises(ConfigError):
        style.gather_samples(lib, StyleCfg(albums=["Snapseed"]))


def _texts_before_images(content):
    labels = []
    for prev, block in zip(content, content[1:]):
        if block["type"] == "image":
            labels.append(prev["text"])
    return labels


def test_build_profile_request_shape():
    client = FakeClient(lambda n, kw: fake_response(text="## Лента\nТёплый свет.\n\n## Stories\nЛегко."))
    posts = [make_asset(uuid="P1", taken_at=datetime(2026, 10, 3, tzinfo=UTC)),
             make_asset(uuid="P2", taken_at=datetime(2025, 8, 30, tzinfo=UTC))]
    shots = [make_asset(uuid="G1", is_screenshot=True)]
    result = style.build_profile(client, "claude-opus-5-5", "medium", posts, shots, _preview)
    assert result.text.startswith("## Лента") and result.input_tokens == 20_000 and result.output_tokens == 2_000
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["system"] == style.STYLE_SYSTEM_PROMPT
    assert call["output_config"] == {"effort": "medium"}
    assert call["_beta"] and call["fallbacks"] == "default"
    content = call["messages"][0]["content"]
    assert _texts_before_images(content) == ["post, 2026-10", "post, 2025-08", "page screenshot"]
    images = [base64.b64decode(b["source"]["data"]) for b in content if b["type"] == "image"]
    assert images == [b"P1@1024", b"P2@1024", b"G1@1568"]


def test_build_profile_skips_samples_without_preview():
    client = FakeClient(lambda n, kw: fake_response(text="## Лента\n…"))
    posts = [make_asset(uuid="P1"), make_asset(uuid="P2")]
    style.build_profile(client, "claude-haiku-5-5", "medium", posts, [],
                        lambda a, side: None if a.uuid == "P2" else b"x")
    content = client.calls[0]["messages"][0]["content"]
    assert sum(b["type"] == "image" for b in content) == 1


def test_build_profile_refusal_raises():
    client = FakeClient(lambda n, kw: fake_response(text="I can't help", stop_reason="refusal"))
    with pytest.raises(RuntimeError):
        style.build_profile(client, "claude-haiku-5-5", "medium", _posts(2), [], _preview)


def test_save_profile_rotates_previous(tmp_path):
    path = tmp_path / "cfg" / "style.md"
    style.save_profile(path, "первый")
    style.save_profile(path, "второй")
    assert path.read_text(encoding="utf-8").strip() == "второй"
    assert (tmp_path / "cfg" / "style.prev.md").read_text(encoding="utf-8").strip() == "первый"


def test_estimate_profile_usd():
    posts = [make_asset(width=4032, height=3024) for _ in range(40)]
    shots = [make_asset(width=1179, height=2556, is_screenshot=True) for _ in range(2)]
    expected = (40 * 1049 + 2 * 1512 + 500) * 4.00 / 1e6 + 1500 * 20.00 / 1e6
    assert style.estimate_profile_usd(posts, shots, AppConfig()) == pytest.approx(expected)


# --- профиль на второй ступени ---------------------------------------------------------------


def _shots_library(tmp_path, n_shots, title="Instagram style"):
    shots = [make_asset(uuid=f"G{i}", width=1179, height=2556, is_screenshot=True) for i in range(n_shots)]
    previews = {a.uuid: save_jpeg(varied_scene(i, (590, 1278)), tmp_path / f"g{i}.jpg") for i, a in enumerate(shots)}
    return FakeLibrary([AlbumRef("g", title, n_shots)], {"g": shots}, previews)


def _cfg(**style_kw):
    return AppConfig(style=StyleCfg(albums=["Snapseed"], screenshots_album="Instagram style", **style_kw))


def test_load_style_missing_or_blank_is_none(tmp_path):
    lib = _shots_library(tmp_path, 2)
    assert style.load_style(tmp_path / "style.md", lib, _cfg()) is None
    (tmp_path / "style.md").write_text("  \n", encoding="utf-8")
    assert style.load_style(tmp_path / "style.md", lib, _cfg()) is None


def test_load_style_reads_profile_and_screenshots(tmp_path):
    path = tmp_path / "style.md"
    path.write_text("## Лента\nТёплый свет.\n", encoding="utf-8")
    ctx = style.load_style(path, _shots_library(tmp_path, 3), _cfg())
    assert ctx.text.startswith("## Лента") and len(ctx.references) == 2 and ctx.warnings == []
    assert all(r[:2] == b"\xff\xd8" for r in ctx.references)
    assert len(ctx.fingerprint) == 8
    assert ctx.extra_tokens == 2 * 1512 + len(ctx.text) // 2 + 100


def test_load_style_fingerprint_changes_with_text_and_screens(tmp_path):
    path = tmp_path / "style.md"
    path.write_text("## Лента\nА\n", encoding="utf-8")
    lib = _shots_library(tmp_path, 3)
    first = style.load_style(path, lib, _cfg()).fingerprint
    assert style.load_style(path, lib, _cfg()).fingerprint == first
    assert style.load_style(path, lib, _cfg(reference_images=1)).fingerprint != first
    path.write_text("## Лента\nБ\n", encoding="utf-8")
    assert style.load_style(path, lib, _cfg()).fingerprint != first


def test_load_style_short_screenshot_album_warns(tmp_path):
    path = tmp_path / "style.md"
    path.write_text("## Лента\n…\n", encoding="utf-8")
    ctx = style.load_style(path, _shots_library(tmp_path, 1), _cfg())
    assert len(ctx.references) == 1 and len(ctx.warnings) == 1


def test_load_style_missing_screenshot_album_warns(tmp_path):
    path = tmp_path / "style.md"
    path.write_text("## Лента\n…\n", encoding="utf-8")
    ctx = style.load_style(path, _shots_library(tmp_path, 2, title="Другое"), _cfg())
    assert ctx.references == [] and len(ctx.warnings) == 1 and "Instagram style" in ctx.warnings[0]
