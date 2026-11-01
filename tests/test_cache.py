import numpy as np
import pytest

from photo_chooser.cache import Cache
from photo_chooser.models import Judgment, LocalFeatures, Screening


def _features(uuid="A/L0/001", aesthetic=0.7, face=None):
    vec = np.arange(768, dtype=np.float32) / 768
    return LocalFeatures(uuid=uuid, aesthetic=aesthetic, is_utility=False, sharpness=12.5, mean_luma=0.4,
                         face_quality=face, feature_print=vec.tobytes())


def test_features_roundtrip(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    f = _features(face=0.61)
    c.put_features(f)
    assert c.get_features(f.uuid) == f
    assert c.get_features("other") is None
    g = _features(aesthetic=None)
    c.put_features(g)
    assert c.get_features(g.uuid) == g


def test_judgment_keyed_by_model_and_prompt(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    j = Judgment(uuid="A/L0/001", matches_theme=True, feed_score=8, stories_score=5, reason="Пирамиды на закате")
    c.put_judgments([j], "t1", "claude-haiku-5-5", "judge-1")
    assert c.get_judgment(j.uuid, "t1", "claude-haiku-5-5", "judge-1") == j
    assert c.get_judgment(j.uuid, "t1", "claude-opus-5-5", "judge-1") is None
    assert c.get_judgment(j.uuid, "t1", "claude-haiku-5-5", "judge-2") is None
    assert c.get_judgment(j.uuid, "t2", "claude-haiku-5-5", "judge-1") is None


def test_judgment_overwritten_by_newer_put(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    j = Judgment(uuid="A", matches_theme=True, feed_score=8, stories_score=5, reason="r")
    j2 = Judgment(uuid="A", matches_theme=False, feed_score=2, stories_score=3, reason="r2")
    c.put_judgments([j], "t1", "m", "judge-1")
    c.put_judgments([j2], "t1", "m", "judge-1")
    assert c.get_judgment("A", "t1", "m", "judge-1") == j2


def test_screening_roundtrip_keyed_by_themes_key(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    s = Screening(uuid="A/L0/001", relevance={"k1": 8, "k2": 5}, feed_score=7, stories_score=4)
    empty = Screening(uuid="B/L0/001", relevance={}, feed_score=3, stories_score=2)
    c.put_screenings([s, empty], "set1", "claude-haiku-5-5", "screen-1")
    assert c.get_screening(s.uuid, "set1", "claude-haiku-5-5", "screen-1") == s
    assert c.get_screening(empty.uuid, "set1", "claude-haiku-5-5", "screen-1") == empty
    assert c.get_screening(s.uuid, "set2", "claude-haiku-5-5", "screen-1") is None
    assert c.get_screening(s.uuid, "set1", "claude-sonnet-5-5", "screen-1") is None
    assert c.get_screening(s.uuid, "set1", "claude-haiku-5-5", "screen-2") is None


def test_album_spend_accumulates(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    assert c.album_spend("album-1") == 0.0
    c.add_spend("album-1", "claude-haiku-5-5", 0.10)
    c.add_spend("album-1", "claude-opus-5-5", 0.25)
    assert c.album_spend("album-1") == pytest.approx(0.35)
    assert c.album_spend("album-2") == 0.0


def test_cache_survives_reopen(tmp_path):
    path = tmp_path / "sub" / "c.sqlite"
    c = Cache(path)
    f = _features()
    c.put_features(f)
    c.add_spend("album-1", "claude-haiku-5-5", 0.5)
    j = Judgment(uuid="A", matches_theme=True, feed_score=8, stories_score=5, reason="r")
    c.put_judgments([j], "t1", "m", "judge-1")
    c.close()
    reopened = Cache(path)
    assert reopened.get_features(f.uuid) == f
    assert reopened.album_spend("album-1") == pytest.approx(0.5)
    assert reopened.get_judgment("A", "t1", "m", "judge-1") == j


def test_composition_roundtrip(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    c.put_composition("t1", "claude-opus-5-5", "compose-1", "fp1", '{"carousels": [], "stories": []}')
    assert c.get_composition("t1", "claude-opus-5-5", "compose-1", "fp1") == '{"carousels": [], "stories": []}'
    assert c.get_composition("t1", "claude-opus-5-5", "compose-1", "fp2") is None
    assert c.get_composition("t1", "claude-haiku-5-5", "compose-1", "fp1") is None
    assert c.get_composition("t2", "claude-opus-5-5", "compose-1", "fp1") is None
