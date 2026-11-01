import Quartz

from imaging import blurred, save_jpeg, scene_image, write_mov
from photo_chooser import images
from photo_chooser.analysis.live import choose, frame_scores, sample_frames


def test_choose_key_when_within_margin():
    assert choose(0.70, [0.80], 0.15) is None


def test_choose_frame_when_clearly_better():
    assert choose(0.50, [0.40, 0.70], 0.15) == 1
    assert choose(0.50, [0.40, 0.65], 0.15) == 1  # ровно на величину запаса — уже «явно лучше»


def test_choose_without_frames_is_key():
    assert choose(0.50, [], 0.15) is None


def test_sample_frames_count(tmp_path):
    video = write_mov(tmp_path / "IMG_1.MOV", [scene_image(i % 2, size=(320, 240)) for i in range(22)])
    frames = sample_frames(video, 5)
    assert len(frames) == 5
    assert (Quartz.CGImageGetWidth(frames[0]), Quartz.CGImageGetHeight(frames[0])) == (320, 240)


def test_frame_scores_sharp_beats_blurred(tmp_path):
    sharp = images.load_cgimage(save_jpeg(scene_image(1), tmp_path / "sharp.jpg"), 2048)
    soft = images.load_cgimage(save_jpeg(blurred(scene_image(1), 8), tmp_path / "soft.jpg"), 2048)
    small_sharp = images.load_cgimage(save_jpeg(scene_image(1, size=(320, 240)), tmp_path / "small.jpg"), 2048)
    scores = frame_scores([soft, sharp, small_sharp], 1024)
    assert len(scores) == 3
    assert scores[1] > scores[0]
    assert scores[2] > scores[0]  # разрешение приводится к общей стороне — маленький резкий кадр не штрафуется
    assert all(0.0 <= s <= 1.0 for s in scores)
