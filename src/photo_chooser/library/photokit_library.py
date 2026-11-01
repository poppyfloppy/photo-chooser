"""Доступ к Photos через PhotoKit (pyobjc) — только чтение.

Изменяющие вызовы PhotoKit (performChanges и т. п.) здесь не используются: медиатека не меняется.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import AppKit
import Foundation
import Photos
import objc

from photo_chooser.library.base import (
    DOWNLOAD_ATTEMPTS,
    DOWNLOAD_TIMEOUT_S,
    DownloadError,
    PhotosAccessError,
    resolve_album,
)
from photo_chooser.models import AlbumRef, Asset

PREVIEW_DIR = Path("~/Library/Caches/photo-chooser/previews").expanduser()
PREVIEW_JPEG_QUALITY = 0.85


def asset_from_phasset(a, resources: list) -> Asset | None:
    """Снимок PhotoKit → Asset; не изображение → None. pixelWidth/Height уже учитывают поворот (spike)."""
    if a.mediaType() != Photos.PHAssetMediaTypeImage:
        return None
    subtypes = a.mediaSubtypes()
    primary = next((r for r in resources if r.type() == Photos.PHAssetResourceTypePhoto), None)
    primary = primary or (resources[0] if resources else None)
    ident = a.localIdentifier()
    filename = primary.originalFilename() if primary else f"{ident.replace('/', '_')}.jpg"
    created = a.creationDate()
    ts = created.timeIntervalSince1970() if created is not None else 0.0
    return Asset(
        uuid=ident,
        filename=filename,
        taken_at=datetime.fromtimestamp(ts, UTC),
        width=int(a.pixelWidth()),
        height=int(a.pixelHeight()),
        is_live=bool(subtypes & Photos.PHAssetMediaSubtypePhotoLive),
        is_screenshot=bool(subtypes & Photos.PHAssetMediaSubtypePhotoScreenshot),
    )


def call_with_timeout(fn: Callable, timeout_s: float):
    """Выполняет fn в фоновом потоке; не успел — TimeoutError (поток-демон дорабатывает сам)."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — исключение передаётся вызывающему
            box["error"] = e

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError(f"нет ответа за {timeout_s:.0f} с")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def with_retries(attempt: Callable, uuid: str, attempts: int = DOWNLOAD_ATTEMPTS):
    last = ""
    for _ in range(attempts):
        try:
            return attempt()
        except Exception as e:  # noqa: BLE001 — любая ошибка загрузки повторяется
            last = str(e) or type(e).__name__
    raise DownloadError(uuid, f"{attempts} попытки не удались: {last}")


def _safe_id(uuid: str) -> str:
    return uuid.replace("/", "_")


class PhotoKitLibrary:
    def __init__(self, preview_dir: Path = PREVIEW_DIR):
        self._preview_dir = preview_dir
        self._ensure_access()

    @staticmethod
    def _ensure_access() -> None:
        level = Photos.PHAccessLevelReadWrite  # PhotoKit требует этот уровень и для чтения (spike)
        status = Photos.PHPhotoLibrary.authorizationStatusForAccessLevel_(level)
        if status == Photos.PHAuthorizationStatusNotDetermined:
            done = threading.Event()
            Photos.PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(level, lambda s: done.set())
            done.wait(DOWNLOAD_TIMEOUT_S)
            status = Photos.PHPhotoLibrary.authorizationStatusForAccessLevel_(level)
        if status == Photos.PHAuthorizationStatusLimited:
            raise PhotosAccessError("доступ ограничен выбранными снимками")
        if status != Photos.PHAuthorizationStatusAuthorized:
            raise PhotosAccessError()

    @staticmethod
    def _image_options():
        options = Photos.PHFetchOptions.new()
        options.setPredicate_(Foundation.NSPredicate.predicateWithFormat_(
            "mediaType == %d", Photos.PHAssetMediaTypeImage))
        return options

    def _collections(self) -> list:
        result = Photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
            Photos.PHAssetCollectionTypeAlbum, Photos.PHAssetCollectionSubtypeAlbumRegular, None)
        return [result.objectAtIndex_(i) for i in range(result.count())]

    def albums(self) -> list[AlbumRef]:
        return [AlbumRef(uuid=c.localIdentifier(), title=c.localizedTitle() or "",
                         count=Photos.PHAsset.fetchAssetsInAssetCollection_options_(c, self._image_options()).count())
                for c in self._collections()]

    def find_album(self, title: str) -> AlbumRef:
        return resolve_album(title, self.albums())

    def assets(self, album: AlbumRef, *, album_order: bool = False) -> list[Asset]:
        found = Photos.PHAssetCollection.fetchAssetCollectionsWithLocalIdentifiers_options_([album.uuid], None)
        if found.count() == 0:
            return []
        options = self._image_options()
        if not album_order:  # без sortDescriptors PhotoKit отдаёт снимки в порядке альбома
            by_date = Foundation.NSSortDescriptor.sortDescriptorWithKey_ascending_("creationDate", True)
            options.setSortDescriptors_([by_date])
        result = Photos.PHAsset.fetchAssetsInAssetCollection_options_(found.objectAtIndex_(0), options)
        out = []
        for i in range(result.count()):
            a = result.objectAtIndex_(i)
            mapped = asset_from_phasset(a, list(Photos.PHAssetResource.assetResourcesForAsset_(a)))
            if mapped is not None:
                out.append(mapped)
        return out

    @staticmethod
    def _phasset(uuid: str):
        result = Photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_([uuid], None)
        if result.count() == 0:
            raise DownloadError(uuid, "снимок не найден в медиатеке")
        return result.objectAtIndex_(0)

    def preview(self, asset: Asset, long_side: int) -> Path | None:
        path = self._preview_dir / f"{_safe_id(asset.uuid)}_{long_side}.jpg"
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        def write() -> bool:
            with objc.autorelease_pool():
                return self._write_preview(asset.uuid, long_side, path)

        try:
            ok = call_with_timeout(write, DOWNLOAD_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — любая ошибка превью = «нет превью»
            return None
        return path if ok and path.exists() else None

    def _write_preview(self, uuid: str, long_side: int, path: Path) -> bool:
        # synchronous=True: асинхронные ответы PHImageManager приходят в главный поток (spike).
        options = Photos.PHImageRequestOptions.new()
        options.setSynchronous_(True)
        options.setNetworkAccessAllowed_(True)
        options.setDeliveryMode_(Photos.PHImageRequestOptionsDeliveryModeHighQualityFormat)
        options.setResizeMode_(Photos.PHImageRequestOptionsResizeModeExact)
        got: dict = {}

        def handler(image, info):
            got["image"] = image

        Photos.PHImageManager.defaultManager().requestImageForAsset_targetSize_contentMode_options_resultHandler_(
            self._phasset(uuid), (long_side, long_side), Photos.PHImageContentModeAspectFit, options, handler)
        image = got.get("image")
        if image is None:
            return False
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        data = rep.representationUsingType_properties_(
            AppKit.NSBitmapImageFileTypeJPEG, {AppKit.NSImageCompressionFactor: PREVIEW_JPEG_QUALITY})
        return bool(data.writeToFile_atomically_(str(path), True))

    def _export_resource(self, asset: Asset, resource_type: int, dest_dir: Path, what: str) -> Path:
        resources = list(Photos.PHAssetResource.assetResourcesForAsset_(self._phasset(asset.uuid)))
        resource = next((r for r in resources if r.type() == resource_type), None)
        if resource is None:
            raise DownloadError(asset.uuid, f"у снимка нет ресурса «{what}»")
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / resource.originalFilename()

        def attempt() -> Path:
            path.unlink(missing_ok=True)
            options = Photos.PHAssetResourceRequestOptions.new()
            options.setNetworkAccessAllowed_(True)
            done, result = threading.Event(), {}

            def handler(error):
                result["error"] = error
                done.set()

            Photos.PHAssetResourceManager.defaultManager().writeDataForAssetResource_toFile_options_completionHandler_(
                resource, Foundation.NSURL.fileURLWithPath_(str(path)), options, handler)
            if not done.wait(DOWNLOAD_TIMEOUT_S):
                raise TimeoutError(f"нет ответа за {DOWNLOAD_TIMEOUT_S} с")
            if result.get("error") is not None:
                raise RuntimeError(str(result["error"]))
            if not path.exists():
                raise RuntimeError("файл не записан")
            return path

        return with_retries(attempt, asset.uuid)

    def export_original(self, asset: Asset, dest_dir: Path) -> Path:
        return self._export_resource(asset, Photos.PHAssetResourceTypePhoto, dest_dir, "оригинал")

    def export_live_video(self, asset: Asset, dest_dir: Path) -> Path:
        return self._export_resource(asset, Photos.PHAssetResourceTypePairedVideo, dest_dir, "видео Live Photo")
