from datetime import timedelta

import numpy as np

from conftest import make_asset
from photo_chooser.analysis.dedup import cluster, representatives
from photo_chooser.analysis.junk import junk_reason
from photo_chooser.config import Thresholds
from photo_chooser.models import LocalFeatures

TH = Thresholds(junk_blur=1.0, junk_dark_luma=0.03, junk_bright_luma=0.97, duplicate_distance=0.45,
                duplicate_window_s=60)


def _vec(*head: float) -> bytes:
    v = np.zeros(768, dtype=np.float32)
    v[: len(head)] = head
    return v.tobytes()


def _feat(uuid="A", *, utility=False, sharp=50.0, luma=0.5, fp=b""):
    return LocalFeatures(uuid=uuid, aesthetic=0.6, is_utility=utility, sharpness=sharp, mean_luma=luma,
                         face_quality=None, feature_print=fp)


def test_junk_reasons_order():
    assert junk_reason(make_asset(is_screenshot=True), _feat(utility=True, sharp=0.1, luma=0.01), TH) == "скриншот"
    assert junk_reason(make_asset(), _feat(utility=True, sharp=0.1, luma=0.01), TH) == "утилитарный кадр"
    assert junk_reason(make_asset(), _feat(sharp=0.5, luma=0.01), TH) == "смаз"
    assert junk_reason(make_asset(), _feat(luma=0.02), TH) == "слишком тёмный"
    assert junk_reason(make_asset(), _feat(luma=0.98), TH) == "пересвечен"


def test_good_photo_not_junk():
    assert junk_reason(make_asset(), _feat(sharp=1.0, luma=0.03), TH) is None
    assert junk_reason(make_asset(), _feat(sharp=3.0, luma=0.97), TH) is None


def _item(t0, seconds, vec, uuid):
    a = make_asset(uuid=uuid, taken_at=t0 + timedelta(seconds=seconds))
    return a, _feat(uuid, fp=vec)


def test_cluster_requires_both_time_and_similarity():
    t0 = make_asset().taken_at
    items = [
        _item(t0, 0, _vec(1.0, 0.0), "A"),
        _item(t0, 5, _vec(1.0, 0.3), "B"),        # рядом и похож на A (0.3)
        _item(t0, 7200, _vec(1.0, 0.1), "C"),     # похож, но через 2 часа
        _item(t0, 10, _vec(0.0, 1.0), "D"),       # рядом, но не похож (≈1.4)
    ]
    clusters = cluster(items, TH)
    assert sorted(sorted(c) for c in clusters) == [["A", "B"], ["C"], ["D"]]


def test_cluster_window_and_distance_bounds_inclusive():
    t0 = make_asset().taken_at
    items = [_item(t0, 0, _vec(0.0), "A"), _item(t0, 60, _vec(0.45), "B"),
             _item(t0, 200, _vec(0.0), "C"), _item(t0, 260.5, _vec(0.0), "D")]
    clusters = cluster(items, TH)
    assert sorted(sorted(c) for c in clusters) == [["A", "B"], ["C"], ["D"]]


def test_cluster_is_transitive():
    t0 = make_asset().taken_at
    items = [_item(t0, 0, _vec(0.0), "A"), _item(t0, 40, _vec(0.4), "B"), _item(t0, 80, _vec(0.8), "C")]
    # A–C не похожи (0.8) и разнесены на 80 с, но связаны через B
    assert [sorted(c) for c in cluster(items, TH)] == [["A", "B", "C"]]


def test_clusters_in_time_order():
    t0 = make_asset().taken_at
    items = [_item(t0, 500, _vec(0.0), "late"), _item(t0, 0, _vec(5.0), "early")]
    assert cluster(items, TH) == [["early"], ["late"]]


def test_representative_is_best_quality():
    clusters = [["A", "B", "C"], ["D"]]
    quality = {"A": 0.4, "B": 0.9, "C": 0.7, "D": 0.1}
    assert representatives(clusters, quality) == ["B", "D"]
