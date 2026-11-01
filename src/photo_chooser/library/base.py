"""Интерфейс доступа к Photos (только чтение) и его исключения."""

import difflib
from pathlib import Path
from typing import Protocol

from photo_chooser.models import AlbumRef, Asset

DOWNLOAD_TIMEOUT_S = 120
DOWNLOAD_ATTEMPTS = 3


class AlbumNotFound(Exception):
    def __init__(self, title: str, similar: list[str]):
        self.title, self.similar = title, similar
        hint = f" Похожие: {', '.join(f'«{s}»' for s in similar)}." if similar else ""
        super().__init__(f"Альбом «{title}» не найден среди альбомов Photos.{hint}")


class AlbumAmbiguous(Exception):
    def __init__(self, title: str, matches: list[AlbumRef]):
        self.title, self.matches = title, matches
        super().__init__(f"Альбомов с названием «{title}» несколько ({len(matches)}): "
                         "переименуйте лишние в Photos, чтобы название было уникальным.")


class DownloadError(Exception):
    def __init__(self, uuid: str, reason: str):
        self.uuid, self.reason = uuid, reason
        super().__init__(f"Не удалось получить файл снимка {uuid}: {reason}")


class PhotosAccessError(Exception):
    def __init__(self, detail: str = "доступ к Photos не выдан"):
        super().__init__(f"Нет доступа к Photos ({detail}). Откройте System Settings → Privacy & Security → "
                         "Photos → включите Terminal (полный доступ), затем перезапустите команду.")


class PhotosLibrary(Protocol):
    def albums(self) -> list[AlbumRef]: ...

    def find_album(self, title: str) -> AlbumRef: ...

    def assets(self, album: AlbumRef) -> list[Asset]: ...

    def preview(self, asset: Asset, long_side: int) -> Path | None: ...

    def export_original(self, asset: Asset, dest_dir: Path) -> Path: ...

    def export_live_video(self, asset: Asset, dest_dir: Path) -> Path: ...


def resolve_album(title: str, albums: list[AlbumRef]) -> AlbumRef:
    matches = [a for a in albums if a.title == title]
    if not matches:
        similar = difflib.get_close_matches(title, sorted({a.title for a in albums}), n=5, cutoff=0.5)
        raise AlbumNotFound(title, similar)
    if len(matches) > 1:
        raise AlbumAmbiguous(title, matches)
    return matches[0]
