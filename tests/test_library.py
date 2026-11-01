import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from photo_chooser.library.base import AlbumAmbiguous, AlbumNotFound, DownloadError, resolve_album
from photo_chooser.library.photokit_library import asset_from_phasset, call_with_timeout, with_retries
from photo_chooser.models import AlbumRef

IMAGE, VIDEO = 1, 2
SUBTYPE_SCREENSHOT, SUBTYPE_LIVE = 4, 8
RES_PHOTO, RES_ADJUSTMENT, RES_PAIRED_VIDEO = 1, 7, 9


def _phasset(media_type=IMAGE, subtypes=0, ts=1773466200.0, w=3024, h=4032, ident="ABC/L0/001"):
    return SimpleNamespace(
        mediaType=lambda: media_type,
        mediaSubtypes=lambda: subtypes,
        localIdentifier=lambda: ident,
        creationDate=lambda: SimpleNamespace(timeIntervalSince1970=lambda: ts),
        pixelWidth=lambda: w,
        pixelHeight=lambda: h,
    )


def _res(kind, name):
    return SimpleNamespace(type=lambda: kind, originalFilename=lambda: name)


def test_mapping_skips_non_image():
    assert asset_from_phasset(_phasset(media_type=VIDEO), [_res(2, "IMG_1.MOV")]) is None


def test_mapping_live_and_screenshot_bits():
    live = asset_from_phasset(_phasset(subtypes=SUBTYPE_LIVE), [_res(RES_PHOTO, "IMG_1.HEIC")])
    assert live.is_live and not live.is_screenshot
    shot = asset_from_phasset(_phasset(subtypes=SUBTYPE_SCREENSHOT), [_res(RES_PHOTO, "IMG_2.PNG")])
    assert shot.is_screenshot and not shot.is_live
    plain = asset_from_phasset(_phasset(subtypes=0), [_res(RES_PHOTO, "IMG_3.HEIC")])
    assert not plain.is_live and not plain.is_screenshot


def test_mapping_filename_from_primary_resource():
    resources = [_res(RES_ADJUSTMENT, "Adjustments.plist"), _res(RES_PAIRED_VIDEO, "IMG_7507.MOV"),
                 _res(RES_PHOTO, "IMG_7507.HEIC")]
    a = asset_from_phasset(_phasset(subtypes=SUBTYPE_LIVE), resources)
    assert a.filename == "IMG_7507.HEIC"
    assert a.uuid == "ABC/L0/001"
    assert (a.width, a.height) == (3024, 4032)


def test_mapping_taken_at_is_utc():
    a = asset_from_phasset(_phasset(ts=1773466200), [_res(RES_PHOTO, "IMG_1.HEIC")])
    assert a.taken_at == datetime(2026, 3, 14, 5, 30, tzinfo=UTC)
    assert a.taken_at.tzinfo is UTC


def _albums():
    return [AlbumRef("a1", "Египет 2026", 900), AlbumRef("a2", "Таиланд 2017", 300),
            AlbumRef("a3", "Кофе", 30), AlbumRef("a4", "Кофе", 12)]


def test_find_album_exact():
    assert resolve_album("Таиланд 2017", _albums()).uuid == "a2"


def test_find_album_not_found_lists_similar():
    with pytest.raises(AlbumNotFound) as e:
        resolve_album("Египт 2026", _albums())
    assert "Египет 2026" in e.value.similar
    assert "Египет 2026" in str(e.value)


def test_find_album_ambiguous():
    with pytest.raises(AlbumAmbiguous) as e:
        resolve_album("Кофе", _albums())
    assert {a.uuid for a in e.value.matches} == {"a3", "a4"}


def test_with_retries_gives_up_after_three_attempts():
    calls = []

    def attempt():
        calls.append(1)
        raise RuntimeError("PHPhotosErrorNetworkError 3169")

    with pytest.raises(DownloadError) as e:
        with_retries(attempt, "ABC/L0/001")
    assert len(calls) == 3
    assert e.value.uuid == "ABC/L0/001"
    assert "3169" in e.value.reason


def test_with_retries_returns_first_success():
    calls = []

    def attempt():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("network")
        return "ok"

    assert with_retries(attempt, "X") == "ok"
    assert len(calls) == 2


def test_call_with_timeout():
    assert call_with_timeout(lambda: 42, timeout_s=1) == 42
    with pytest.raises(TimeoutError):
        call_with_timeout(lambda: time.sleep(0.5), timeout_s=0.05)
