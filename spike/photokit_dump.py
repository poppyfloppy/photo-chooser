"""Spike (одноразовый): снимок альбома Photos через PhotoKit (Task 1).

Только чтение: читает альбом через официальный API PhotoKit, сохраняет превью
и выгружает 3 оригинала из iCloud в spike/out/. Медиатеку не меняет.
Terminal нужен только доступ Photos, Full Disk Access не нужен.

Запуск (в Terminal, не во вкладке Claude):
    uv run python spike/photokit_dump.py                 # доступ + список альбомов
    uv run python spike/photokit_dump.py "Album name"    # снимок альбома
"""

import difflib
import json
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import AppKit
import Foundation
import Photos

OUT = Path(__file__).parent / "out"
BLOCKS = 8            # превью берём блоками по всей поездке,
BLOCK_SIZE = 50       # подряд внутри блока — чтобы сохранились серии дублей
PREVIEW_SIDE = 1024
TIMEOUT_S = 120

RAW_PAIR = {Photos.PHAssetResourceTypePhoto, Photos.PHAssetResourceTypeAlternatePhoto}


def ensure_access():
    level = Photos.PHAccessLevelReadWrite  # PhotoKit требует этот уровень и для чтения
    status = Photos.PHPhotoLibrary.authorizationStatusForAccessLevel_(level)
    if status == Photos.PHAuthorizationStatusNotDetermined:
        done = threading.Event()
        Photos.PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(level, lambda s: done.set())
        done.wait(TIMEOUT_S)
        status = Photos.PHPhotoLibrary.authorizationStatusForAccessLevel_(level)
    if status == Photos.PHAuthorizationStatusLimited:
        print("Внимание: доступ к Photos ограничен выбранными снимками")
    elif status != Photos.PHAuthorizationStatusAuthorized:
        sys.exit("Нет доступа к Photos: System Settings → Privacy & Security → Photos → включите Terminal")


def fetch_albums():
    result = Photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        Photos.PHAssetCollectionTypeAlbum, Photos.PHAssetCollectionSubtypeAlbumRegular, None)
    return [result.objectAtIndex_(i) for i in range(result.count())]


def fetch_assets(collection):
    options = Photos.PHFetchOptions.new()
    options.setSortDescriptors_([Foundation.NSSortDescriptor.sortDescriptorWithKey_ascending_("creationDate", True)])
    result = Photos.PHAsset.fetchAssetsInAssetCollection_options_(collection, options)
    return [result.objectAtIndex_(i) for i in range(result.count())]


def resources(asset):
    return list(Photos.PHAssetResource.assetResourcesForAsset_(asset))


def record(asset):
    res = resources(asset)
    created = asset.creationDate()
    subtypes = asset.mediaSubtypes()
    return {
        "id": asset.localIdentifier(),
        "created_utc": datetime.fromtimestamp(created.timeIntervalSince1970(), timezone.utc).isoformat() if created else None,
        "pixel_width": asset.pixelWidth(), "pixel_height": asset.pixelHeight(),
        "media_type": asset.mediaType(), "subtypes": subtypes,
        "is_live": bool(subtypes & Photos.PHAssetMediaSubtypePhotoLive),
        "is_screenshot": bool(subtypes & Photos.PHAssetMediaSubtypePhotoScreenshot),
        "has_location": asset.location() is not None,
        "resources": [{"type": r.type(), "filename": r.originalFilename(), "uti": r.uniformTypeIdentifier()} for r in res],
        "has_raw_pair": RAW_PAIR <= {r.type() for r in res},
    }


def save_preview(asset, path):
    options = Photos.PHImageRequestOptions.new()
    options.setSynchronous_(True)
    options.setNetworkAccessAllowed_(True)
    options.setDeliveryMode_(Photos.PHImageRequestOptionsDeliveryModeHighQualityFormat)
    options.setResizeMode_(Photos.PHImageRequestOptionsResizeModeExact)
    got = {}

    def handler(image, info):
        got["image"], got["info"] = image, info

    started = time.monotonic()
    Photos.PHImageManager.defaultManager().requestImageForAsset_targetSize_contentMode_options_resultHandler_(
        asset, (PREVIEW_SIDE, PREVIEW_SIDE), Photos.PHImageContentModeAspectFit, options, handler)
    seconds = time.monotonic() - started
    image = got.get("image")
    if image is None:
        return {"ok": False, "seconds": seconds, "info": str(got.get("info"))}
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypeJPEG,
                                                   {AppKit.NSImageCompressionFactor: 0.85})
    data.writeToFile_atomically_(str(path), True)
    return {"ok": True, "seconds": seconds}


def write_resource(resource, path):
    path.unlink(missing_ok=True)
    options = Photos.PHAssetResourceRequestOptions.new()
    options.setNetworkAccessAllowed_(True)
    done, result = threading.Event(), {}

    def handler(error):
        result["error"] = str(error) if error else None
        done.set()

    started = time.monotonic()
    Photos.PHAssetResourceManager.defaultManager().writeDataForAssetResource_toFile_options_completionHandler_(
        resource, Foundation.NSURL.fileURLWithPath_(str(path)), options, handler)
    finished = done.wait(TIMEOUT_S)
    return {"file": path.name, "ok": finished and result.get("error") is None and path.exists(),
            "error": result.get("error") if finished else "timeout", "seconds": round(time.monotonic() - started, 1)}


def export_samples(assets):
    by_kind = {
        "plain": (lambda r: not r["is_live"] and not r["has_raw_pair"], {Photos.PHAssetResourceTypePhoto}),
        "raw_pair": (lambda r: r["has_raw_pair"], RAW_PAIR),
        "live": (lambda r: r["is_live"], {Photos.PHAssetResourceTypePhoto, Photos.PHAssetResourceTypePairedVideo}),
    }
    images = [a for a in assets if a.mediaType() == Photos.PHAssetMediaTypeImage]
    report = {}
    for kind, (match, types) in by_kind.items():
        asset = next((a for a in images if match(record(a))), None)
        if asset is None:
            report[kind] = None
            continue
        dest = OUT / "export" / kind
        dest.mkdir(parents=True, exist_ok=True)
        report[kind] = {"id": asset.localIdentifier(), "files": [
            write_resource(r, dest / r.originalFilename()) for r in resources(asset) if r.type() in types]}
    return report


def main():
    ensure_access()
    albums = fetch_albums()
    if len(sys.argv) < 2:
        rows = sorted(((Photos.PHAsset.fetchAssetsInAssetCollection_options_(c, None).count(), c.localizedTitle())
                       for c in albums), reverse=True)
        for count, title in rows[:40]:
            print(f"{count:6d}  {title}")
        return

    title = sys.argv[1]
    matches = [c for c in albums if c.localizedTitle() == title]
    if not matches:
        similar = difflib.get_close_matches(title, [c.localizedTitle() for c in albums], n=5, cutoff=0.5)
        sys.exit(f"Альбом «{title}» не найден. Похожие: {similar}")
    if len(matches) > 1:
        print(f"Внимание: альбомов «{title}» несколько ({len(matches)}), беру первый")

    assets = fetch_assets(matches[0])
    OUT.mkdir(parents=True, exist_ok=True)
    records = [record(a) for a in assets]
    (OUT / "album.json").write_text(json.dumps(records, ensure_ascii=False, indent=1))

    images = [a for a in assets if a.mediaType() == Photos.PHAssetMediaTypeImage]
    step = max(len(images) // BLOCKS, 1)
    picked = [a for b in range(0, len(images), step) for a in images[b:b + BLOCK_SIZE]]
    previews_dir = OUT / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    print(f"Сохраняю {len(picked)} превью…")
    previews = []
    for a in picked:
        name = a.localIdentifier().replace("/", "_") + ".jpg"
        previews.append(save_preview(a, previews_dir / name))
    seconds = [p["seconds"] for p in previews]

    print("Выгружаю 3 оригинала из iCloud…")
    exports = export_samples(assets)

    summary = {
        "album": title, "total": len(records),
        "images": sum(r["media_type"] == Photos.PHAssetMediaTypeImage for r in records),
        "live": sum(r["is_live"] for r in records),
        "raw_pairs": sum(r["has_raw_pair"] for r in records),
        "screenshots": sum(r["is_screenshot"] for r in records),
        "previews_requested": len(previews), "previews_failed": sum(not p["ok"] for p in previews),
        "preview_seconds": {"min": round(min(seconds), 3), "median": round(statistics.median(seconds), 3),
                            "max": round(max(seconds), 3), "total": round(sum(seconds), 1)} if seconds else None,
        "preview_failures": [p["info"] for p in previews if not p["ok"]][:5],
        "exports": exports,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"\nГотово: {OUT}")


if __name__ == "__main__":
    main()
