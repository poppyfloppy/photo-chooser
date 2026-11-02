"""Стиль страницы Instagram (SPEC FR-11): образцы из Photos, построение профиля style.md."""

import base64
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from photo_chooser import images
from photo_chooser.config import DEFAULT_CONFIG_PATH, AppConfig, ConfigError, StyleCfg
from photo_chooser.judge.cost import PROMPT_OVERHEAD_TOKENS, image_tokens, price_for, usage_usd
from photo_chooser.judge.prompts import STYLE_SYSTEM_PROMPT
from photo_chooser.judge.stages import call_model
from photo_chooser.library.base import AlbumAmbiguous, AlbumNotFound, PhotosLibrary, resolve_album
from photo_chooser.models import AlbumRef, Asset

DEFAULT_STYLE_PATH = DEFAULT_CONFIG_PATH.parent / "style.md"
POST_LONG_SIDE = 1024
SCREENSHOT_LONG_SIDE = 1568
MAX_SCREENSHOTS = 12
MIN_POSTS = 10
EST_PROFILE_OUTPUT_TOKENS = 1500

@dataclass(frozen=True)
class ProfileResult:
    text: str
    input_tokens: int
    output_tokens: int


def select_posts(assets: list[Asset], since: date, n: int) -> list[Asset]:
    """Посты не раньше since, от новых к старым; при избытке — индексы floor(N·(i/n)²): ровно половина выборки
    приходится на самую новую четверть, дальше всё реже."""
    unique = {a.uuid: a for a in assets if a.taken_at.date() >= since}
    pool = sorted(unique.values(), key=lambda a: a.taken_at, reverse=True)
    if len(pool) <= n:
        return pool
    picked, last = [], -1
    for i in range(n):
        last = max(int(len(pool) * (i / n) ** 2), last + 1)
        picked.append(pool[last])
    return picked


def _albums_titled(title: str, albums: list[AlbumRef]) -> list[AlbumRef]:
    try:
        return [resolve_album(title, albums)]
    except AlbumAmbiguous as e:  # одноимённые альбомы-образцы берутся все (FR-11.1)
        return e.matches


def gather_samples(library: PhotosLibrary, cfg: StyleCfg) -> tuple[list[Asset], list[Asset]]:
    if not cfg.albums:
        raise ConfigError("Не заданы альбомы-образцы стиля: добавьте в ~/.config/photo-chooser/config.yaml "
                          "раздел style с полем albums, например albums: [Snapseed, Instasize]")
    albums = library.albums()
    candidates = [a for title in cfg.albums for album in _albums_titled(title, albums) for a in library.assets(album)]
    posts = select_posts(candidates, cfg.since, cfg.sample)
    if len(posts) < MIN_POSTS:
        raise ConfigError(f"Постов-образцов с {cfg.since:%Y-%m-%d} — {len(posts)}, нужно не меньше {MIN_POSTS}: "
                          "проверьте style.albums и style.since")
    screenshots: list[Asset] = []
    if cfg.screenshots_album:
        album = resolve_album(cfg.screenshots_album, albums)
        screenshots = library.assets(album, album_order=True)[:MAX_SCREENSHOTS]
    return posts, screenshots


def estimate_profile_usd(posts: list[Asset], screenshots: list[Asset], cfg: AppConfig) -> float:
    tokens = (sum(image_tokens(a.width, a.height, POST_LONG_SIDE) for a in posts)
              + sum(image_tokens(a.width, a.height, SCREENSHOT_LONG_SIDE) for a in screenshots)
              + PROMPT_OVERHEAD_TOKENS)
    return usage_usd(tokens, EST_PROFILE_OUTPUT_TOKENS, price_for(cfg.cloud, cfg.style.model))


def _image_block(data: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(data).decode()}}


@dataclass(frozen=True)
class Sample:
    asset: Asset
    kind: str  # "post" | "screenshot"
    label: str  # подпись перед картинкой: тип и месяц (FR-11.2)
    image: bytes


def load_samples(posts: list[Asset], screenshots: list[Asset],
                 preview: Callable[[Asset, int], bytes | None]) -> list[Sample]:
    """Превью образцов — до подтверждения и отправки. Постов с превью меньше MIN_POSTS → ConfigError:
    профиль из пары картинок хуже, чем прежний style.md."""
    samples = [Sample(a, "post", f"post, {a.taken_at:%Y-%m}", data)
               for a in posts if (data := preview(a, POST_LONG_SIDE)) is not None]
    if len(samples) < MIN_POSTS:
        raise ConfigError(f"Превью получено для {len(samples)} постов-образцов из {len(posts)}, нужно не меньше "
                          f"{MIN_POSTS}: проверьте подключение к iCloud и повторите")
    samples += [Sample(a, "screenshot", f"page screenshot, {a.taken_at:%Y-%m}", data)
                for a in screenshots if (data := preview(a, SCREENSHOT_LONG_SIDE)) is not None]
    return samples


def build_profile(client, model: str, effort: str, samples: list[Sample]) -> ProfileResult:
    """Один запрос: подписанные посты (от новых к старым) и скриншоты страницы → описание стиля на русском."""
    content: list[dict] = []
    for sample in samples:
        content += [{"type": "text", "text": sample.label}, _image_block(sample.image)]
    content.append({"type": "text", "text": "Describe the style of this Instagram page."})
    response = call_model(client, model, effort, STYLE_SYSTEM_PROMPT, content, None)
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if response.stop_reason == "refusal" or not text:
        raise RuntimeError(f"Модель не написала профиль стиля (stop_reason: {response.stop_reason})")
    return ProfileResult(text=text, input_tokens=response.usage.input_tokens,
                         output_tokens=response.usage.output_tokens)


def save_profile(path: Path, text: str) -> None:
    """style.md; прежний профиль переименовывается в style.prev.md (FR-11.3)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(path.with_name("style.prev.md"))
    path.write_text(text.strip() + "\n", encoding="utf-8")


# --- профиль на второй ступени (FR-11.4, FR-11.5) ----------------------------------------------


@dataclass(frozen=True)
class StyleContext:
    text: str
    references: list[tuple[str, bytes]]  # ("feed" | "stories", JPEG при SCREENSHOT_LONG_SIDE) — FR-11.4
    fingerprint: str  # 8 hex: текст профиля и вид+uuid выбранных скриншотов — часть ключа кэша второй ступени
    created_at: datetime
    extra_tokens: int  # входные токены профиля и образцов в каждом запросе второй ступени и сборки
    warnings: list[str] = field(default_factory=list)


def load_style(path: Path, library: PhotosLibrary, cfg: AppConfig) -> StyleContext | None:
    """Профиль и скриншоты в порядке альбома: первые reference_feed — гриды ленты, следующие reference_stories —
    stories; при нехватке укорачиваются stories. Нет файла или он пуст — None."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    sc, warnings = cfg.style, []
    need = sc.reference_feed + sc.reference_stories
    shots: list[Asset] = []
    if need and sc.screenshots_album:
        try:
            album = resolve_album(sc.screenshots_album, library.albums())
            shots = library.assets(album, album_order=True)[:need]
        except (AlbumNotFound, AlbumAmbiguous) as e:
            warnings.append(f"Скриншоты-образцы не приложены: {e}")
        else:
            if len(shots) < need:
                warnings.append(f"В альбоме «{sc.screenshots_album}» скриншотов {len(shots)} из {need} "
                                f"(style.reference_feed + reference_stories)")
    references, used = [], []
    for i, a in enumerate(shots):
        preview = library.preview(a, SCREENSHOT_LONG_SIDE)
        if preview is None:
            warnings.append(f"Нет превью скриншота-образца {a.filename}")
            continue
        kind = "feed" if i < sc.reference_feed else "stories"
        references.append((kind, images.jpeg_bytes(preview, SCREENSHOT_LONG_SIDE)))
        used.append((kind, a))
    fingerprint = hashlib.sha256("|".join([text] + [f"{k}:{a.uuid}" for k, a in used]).encode("utf-8")).hexdigest()[:8]
    extra = sum(image_tokens(a.width, a.height, SCREENSHOT_LONG_SIDE) for _, a in used) + len(text) // 2 + 100
    return StyleContext(text=text, references=references, fingerprint=fingerprint,
                        created_at=datetime.fromtimestamp(path.stat().st_mtime), extra_tokens=extra,
                        warnings=warnings)
