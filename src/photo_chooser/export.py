"""Сборка шорт-листа и экспорт оригиналов в новую директорию запуска (SPEC FR-5, FR-6)."""

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import objc

from photo_chooser import images
from photo_chooser.analysis import live
from photo_chooser.config import AppConfig, ConfigError
from photo_chooser.library.base import DownloadError, PhotosLibrary
from photo_chooser.models import Asset
from photo_chooser.select import Pools, Posts, Scored

MAX_NAME_CHARS = 100
MAX_NAME_BYTES = 200  # APFS — до 255 байт UTF-8 на имя, с запасом под суффиксы
NOTE_LIVE_FRAME = "кадр из Live Photo, разрешение ниже оригинала"
NOTE_LIVE_VIDEO_FAILED = "видео Live Photo не скачалось — взят ключевой снимок"
NOTE_LIVE_FRAME_FAILED = "кадры Live Photo не обработаны — взят ключевой снимок"
_FORBIDDEN = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')


def safe_name(s: str) -> str:
    name = _FORBIDDEN.sub("_", s).strip()
    name = re.sub(r"^\.+", lambda m: "_" * len(m.group()), name)  # «..» и скрытые имена
    name = name[:MAX_NAME_CHARS]
    while len(name.encode("utf-8")) > MAX_NAME_BYTES:
        name = name[:-1]
    return name.rstrip() or "_"


def ensure_outside_library(path: Path) -> None:
    resolved = path.expanduser().resolve()
    if any(part.lower().endswith(".photoslibrary") for part in resolved.parts):
        raise ConfigError(f"output_dir {path} находится внутри медиатеки Photos (*.photoslibrary) — "
                          "выберите другую директорию: медиатеку менять нельзя")


def new_run_dir(output_dir: Path, album_title: str, now: datetime) -> Path:
    base = output_dir / safe_name(album_title)
    base.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    n = 1
    while True:
        candidate = base / (stamp if n == 1 else f"{stamp}_{n}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            n += 1


@dataclass
class ExportedItem:
    list_name: str  # "feed" | "stories"
    rank: int
    asset: Asset
    files: list[str]
    score: float
    reason: str
    notes: list[str] = field(default_factory=list)
    crop: str = ""


@dataclass
class Assembled:
    feed: list[ExportedItem]
    stories: list[ExportedItem]
    failed: list[tuple[Asset, str]]
    shortfall_feed: int
    shortfall_stories: int


def assemble(pools: Pools, n_feed: int, n_stories: int,
             try_export: Callable[[Scored, str, int], ExportedItem | None]) -> Assembled:
    """Лента — первые n_feed экспортированных из pools.feed; stories — из pools.stories без уже взятых и упавших.
    Упавший экспорт пропускается, на его место встаёт следующий кандидат (SPEC §9)."""
    taken: set[str] = set()
    failed: list[tuple[Asset, str]] = []
    failed_ids: set[str] = set()

    def fill(pool: list[Scored], list_name: str, limit: int) -> list[ExportedItem]:
        out: list[ExportedItem] = []
        for scored in pool:
            if len(out) >= limit:
                break
            uuid = scored.asset.uuid
            if uuid in taken or uuid in failed_ids:
                continue
            try:
                item = try_export(scored, list_name, len(out) + 1)
                reason = "не удалось скачать оригинал"
            except DownloadError as e:
                item, reason = None, f"не удалось скачать оригинал: {e.reason}"
            if item is None:
                failed.append((scored.asset, reason))
                failed_ids.add(uuid)
                continue
            taken.add(uuid)
            out.append(item)
        return out

    feed = fill(pools.feed, "feed", n_feed)
    stories = fill(pools.stories, "stories", n_stories)
    return Assembled(feed=feed, stories=stories, failed=failed, shortfall_feed=max(0, n_feed - len(feed)),
                     shortfall_stories=max(0, n_stories - len(stories)))


def _live_frame(library: PhotosLibrary, asset: Asset, original: Path, tmp: Path, cfg: AppConfig):
    """Лучший кадр видеочасти, если он явно лучше ключевого снимка; иначе None. Ошибка загрузки видео → DownloadError."""
    video = library.export_live_video(asset, tmp)
    frames = live.sample_frames(video, live.LIVE_FRAMES_PER_VIDEO)
    if not frames:
        return None
    side = cfg.cloud.preview_long_side_px
    scores = live.frame_scores([images.load_cgimage(original, side)] + frames, side)
    idx = live.choose(scores[0], scores[1:], cfg.thresholds.live_frame_margin)
    return frames[idx] if idx is not None else None


@dataclass
class ExportedShot:
    asset: Asset
    files: list[str]
    notes: list[str] = field(default_factory=list)


@dataclass
class ExportedSlide:
    shot: ExportedShot
    alternatives: list[ExportedShot]
    replaced: bool  # слайд заменён альтернативой: оригинал не скачался


@dataclass
class ExportedCarousel:
    title: str
    folder: str
    slides: list[ExportedSlide]


@dataclass
class ExportedStory:
    shot: ExportedShot
    kind: str
    hint: str


@dataclass
class ExportedPosts:
    carousels: list[ExportedCarousel]
    stories: list[ExportedStory]
    failed: list[tuple[Asset, str]]


def export_shot(library: PhotosLibrary, asset: Asset, dest_dir: Path, prefix: str, tmp: Path,
                cfg: AppConfig) -> ExportedShot:
    """Копирует оригинал (или кадр Live Photo в TIFF) как <prefix>_<имя>. DownloadError оригинала поднимается."""
    work = tmp / safe_name(asset.uuid)
    original = library.export_original(asset, work)
    dest_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    frame = None
    if asset.is_live:
        try:
            with objc.autorelease_pool():
                frame = _live_frame(library, asset, original, work, cfg)
        except DownloadError:
            notes.append(NOTE_LIVE_VIDEO_FAILED)
        except Exception:  # noqa: BLE001 — сбой разбора видео не должен ронять экспорт
            notes.append(NOTE_LIVE_FRAME_FAILED)
    if frame is not None:
        name = f"{prefix}_{original.stem}_live-frame.tiff"
        images.write_tiff(frame, dest_dir / name, metadata_from=original)
        notes.append(NOTE_LIVE_FRAME)
    else:
        name = f"{prefix}_{original.name}"
        shutil.copy2(original, dest_dir / name)
    return ExportedShot(asset, [name], notes)


def export_asset(library: PhotosLibrary, scored: Scored, list_name: str, rank: int, theme_dir: Path, tmp: Path,
                 cfg: AppConfig) -> ExportedItem | None:
    """Плоский список версии 0.2 (удаляется вместе с assemble)."""
    shot = export_shot(library, scored.asset, theme_dir / list_name, f"{rank:02d}", tmp, cfg)
    return ExportedItem(list_name, rank, scored.asset, shot.files, scored.score, scored.reason, shot.notes, scored.crop)


CAROUSEL_FOLDER_CHARS = 60
NOTE_REPLACED = "замена: оригинал не скачался, взят запасной ракурс"


def _carousel_folder(feed_dir: Path, n: int, title: str, taken: set[str]) -> str:
    base = safe_name(f"carousel-{n} {title}")[:CAROUSEL_FOLDER_CHARS].rstrip() or f"carousel-{n}"
    name, k = base, 1
    while name in taken or (feed_dir / name).exists():
        k += 1
        name = f"{base}_{k}"
    taken.add(name)
    return name


def export_posts(library: PhotosLibrary, posts: Posts, theme_dir: Path, tmp: Path, cfg: AppConfig) -> ExportedPosts:
    """Карусели в feed/carousel-N <сюжет>/, запасные — в alternatives/ (01a_, 01b_), stories — в stories/ (FR-6.1).
    Не скачался слайд — его место занимает первая скачавшаяся альтернатива; без альтернатив слот выпадает."""
    feed_dir, stories_dir = theme_dir / "feed", theme_dir / "stories"
    feed_dir.mkdir(parents=True, exist_ok=True)
    stories_dir.mkdir(parents=True, exist_ok=True)
    failed: list[tuple[Asset, str]] = []
    taken: set[str] = set()

    def attempt(asset: Asset, dest: Path, prefix: str) -> ExportedShot | None:
        try:
            return export_shot(library, asset, dest, prefix, tmp, cfg)
        except DownloadError as e:
            failed.append((asset, f"не удалось скачать оригинал: {e.reason}"))
            return None

    carousels: list[ExportedCarousel] = []
    for n, carousel in enumerate(posts.carousels, 1):
        folder = _carousel_folder(feed_dir, n, carousel.title, taken)
        cdir = feed_dir / folder
        slides: list[ExportedSlide] = []
        for slide in carousel.slides:
            pos = len(slides) + 1
            chain = [slide.asset] + slide.alternatives
            shot, used = None, 0
            for used, asset in enumerate(chain):
                shot = attempt(asset, cdir, f"{pos:02d}")
                if shot is not None:
                    break
            if shot is None:
                continue
            if used:
                shot.notes.append(NOTE_REPLACED)
            alternatives: list[ExportedShot] = []
            for asset in chain[used + 1:]:
                alt = attempt(asset, cdir / "alternatives", f"{pos:02d}{'ab'[len(alternatives)]}")
                if alt is not None:
                    alternatives.append(alt)
            slides.append(ExportedSlide(shot, alternatives, replaced=bool(used)))
        if slides:
            carousels.append(ExportedCarousel(carousel.title, folder, slides))
    stories: list[ExportedStory] = []
    for pick in posts.stories:
        shot = attempt(pick.asset, stories_dir, f"{len(stories) + 1:02d}")
        if shot is not None:
            stories.append(ExportedStory(shot, pick.kind, pick.hint))
    return ExportedPosts(carousels, stories, failed)
