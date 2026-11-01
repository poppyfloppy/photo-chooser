"""Заглушки внешних систем для юнит-тестов: Photos (FakeLibrary) и Claude API (FakeClient)."""

import shutil
from pathlib import Path

from photo_chooser.library.base import DownloadError, resolve_album
from photo_chooser.models import AlbumRef, Asset


class FakeLibrary:
    """PhotosLibrary на файлах: previews/files/live — пути по uuid, failing — uuid, оригинал которых не скачивается."""

    def __init__(self, albums: list[AlbumRef], assets: dict[str, list[Asset]], previews: dict[str, Path],
                 files: dict[str, Path] | None = None, live: dict[str, Path] | None = None,
                 failing: set[str] | None = None):
        self._albums = albums
        self._assets = assets
        self._previews = previews
        self._files = files or {}
        self._live = live or {}
        self._failing = failing or set()
        self.exported: list[str] = []
        self.live_requests: list[str] = []

    def albums(self) -> list[AlbumRef]:
        return list(self._albums)

    def find_album(self, title: str) -> AlbumRef:
        return resolve_album(title, self._albums)

    def assets(self, album: AlbumRef) -> list[Asset]:
        return list(self._assets.get(album.uuid, []))

    def preview(self, asset: Asset, long_side: int) -> Path | None:
        return self._previews.get(asset.uuid)

    def export_original(self, asset: Asset, dest_dir: Path) -> Path:
        if asset.uuid in self._failing or asset.uuid not in self._files:
            raise DownloadError(asset.uuid, "3 попытки не удались: network")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / asset.filename
        shutil.copyfile(self._files[asset.uuid], dest)
        self.exported.append(asset.uuid)
        return dest

    def export_live_video(self, asset: Asset, dest_dir: Path) -> Path:
        self.live_requests.append(asset.uuid)
        if asset.uuid not in self._live:
            raise DownloadError(asset.uuid, "3 попытки не удались: network")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (Path(asset.filename).stem + ".MOV")
        shutil.copyfile(self._live[asset.uuid], dest)
        return dest
