import Quartz
import Foundation
import numpy as np
import pytest
from PIL import Image

from imaging import blurred, noise_image, save_jpeg, scene_image
from photo_chooser import images
from photo_chooser.analysis.quality import (
    compute_features,
    feature_distance,
    local_quality,
    mean_luma,
    percentile_ranks,
    sharpness,
)
from photo_chooser.models import LocalFeatures


def _feat(aesthetic, face):
    return LocalFeatures(uuid="A", aesthetic=aesthetic, is_utility=False, sharpness=10, mean_luma=0.5,
                         face_quality=face, feature_print=b"")


def test_blurred_image_less_sharp(tmp_path):
    sharp = save_jpeg(noise_image(), tmp_path / "sharp.jpg")
    soft = save_jpeg(blurred(noise_image()), tmp_path / "soft.jpg")
    s_sharp = sharpness(images.gray_array(images.load_cgimage(sharp, 1024)))
    s_soft = sharpness(images.gray_array(images.load_cgimage(soft, 1024)))
    assert s_soft < s_sharp / 10


def test_black_image_low_luma(tmp_path):
    black = save_jpeg(Image.new("RGB", (320, 240), (2, 2, 2)), tmp_path / "black.jpg")
    white = save_jpeg(Image.new("RGB", (320, 240), (255, 255, 255)), tmp_path / "white.jpg")
    assert mean_luma(images.gray_array(images.load_cgimage(black, 1024))) < 0.03
    assert mean_luma(images.gray_array(images.load_cgimage(white, 1024))) > 0.97


def test_gray_array_range_and_shape(tmp_path):
    path = save_jpeg(noise_image(size=(400, 300)), tmp_path / "n.jpg")
    g = images.gray_array(images.load_cgimage(path, 1024))
    assert g.shape == (300, 400)
    assert g.dtype == np.float32
    assert 0.0 <= g.min() and g.max() <= 1.0


def test_load_cgimage_scales_and_applies_orientation(tmp_path):
    path = save_jpeg(noise_image(size=(800, 400)), tmp_path / "rot.jpg", orientation=6)
    img = images.load_cgimage(path, 200)
    assert (Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)) == (100, 200)


def test_jpeg_bytes_long_side(tmp_path):
    path = save_jpeg(noise_image(size=(800, 600)), tmp_path / "n.jpg")
    data = images.jpeg_bytes(path, 400)
    assert data[:2] == b"\xff\xd8"
    out = tmp_path / "out.jpg"
    out.write_bytes(data)
    with Image.open(out) as im:
        assert im.size == (400, 300)


def test_feature_distance_self_zero(tmp_path):
    a = save_jpeg(scene_image(0), tmp_path / "a.jpg")
    b = save_jpeg(scene_image(1), tmp_path / "b.jpg")
    fa1, fa2, fb = compute_features("a", a), compute_features("a", a), compute_features("b", b)
    assert len(fa1.feature_print) == 768 * 4
    assert feature_distance(fa1.feature_print, fa2.feature_print) == pytest.approx(0.0, abs=1e-6)
    assert feature_distance(fa1.feature_print, fb.feature_print) > 0.1


def test_compute_features_fields(tmp_path):
    path = save_jpeg(scene_image(0), tmp_path / "a.jpg")
    f = compute_features("ABC/L0/001", path)
    assert f.uuid == "ABC/L0/001"
    assert f.aesthetic is not None and 0.0 <= f.aesthetic <= 1.0
    assert f.face_quality is None  # на сцене нет лиц
    assert 0.0 < f.mean_luma < 1.0
    assert f.sharpness > 0


def test_feature_distance_known_vectors():
    a = np.zeros(768, dtype=np.float32)
    b = np.zeros(768, dtype=np.float32)
    b[0], b[1] = 3.0, 4.0
    assert feature_distance(a.tobytes(), b.tobytes()) == pytest.approx(5.0)


def test_local_quality_renormalizes_missing():
    assert local_quality(_feat(1.0, None), 1.0) == pytest.approx(1.0)
    assert local_quality(_feat(None, None), 0.4) == pytest.approx(0.4)


def test_local_quality_weights():
    assert local_quality(_feat(0.0, 1.0), 1.0) == pytest.approx(0.5)
    assert local_quality(_feat(1.0, 0.0), 0.0) == pytest.approx(0.5)


def test_percentile_ranks_bounds():
    assert percentile_ranks([3, 1, 2]) == [1.0, 0.0, 0.5]
    assert percentile_ranks([]) == []
    assert percentile_ranks([7]) == [0.5]
    assert percentile_ranks([1, 5, 5]) == [0.0, 0.75, 0.75]


def test_write_tiff_copies_date_and_gps(tmp_path):
    src = save_jpeg(noise_image(size=(320, 240)), tmp_path / "IMG_1.JPG", orientation=6,
                    date="2026:03:14 07:31:05", gps=(29.9792, 31.1342))
    frame = images.load_cgimage(src, 200)
    dest = tmp_path / "IMG_1_live-frame.tiff"
    images.write_tiff(frame, dest, metadata_from=src)
    source = Quartz.CGImageSourceCreateWithURL(Foundation.NSURL.fileURLWithPath_(str(dest)), None)
    assert Quartz.CGImageSourceGetType(source) == "public.tiff"
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(source, 0, None)
    assert props[Quartz.kCGImagePropertyExifDictionary][Quartz.kCGImagePropertyExifDateTimeOriginal] == \
        "2026:03:14 07:31:05"
    gps = props[Quartz.kCGImagePropertyGPSDictionary]
    assert gps[Quartz.kCGImagePropertyGPSLatitude] == pytest.approx(29.9792, abs=1e-3)
    assert gps[Quartz.kCGImagePropertyGPSLongitudeRef] == "E"
    # кадр уже повёрнут — ориентация оригинала не должна повернуть его второй раз
    assert props.get(Quartz.kCGImagePropertyOrientation, 1) == 1
    with Image.open(dest) as im:
        assert im.size == (Quartz.CGImageGetWidth(frame), Quartz.CGImageGetHeight(frame))
