"""Spike (одноразовый): диагностика скачивания оригиналов из iCloud (Task 1, Step 2).

Для каждого альбома: показывает типы ресурсов и UTI (есть ли RAW), затем пробует скачать
2 самых старых снимка двумя способами, с прогрессом и тайм-аутом 5 минут:
  A — PHAssetResourceManager.writeData (как в продукте);
  B — PHImageManager.requestImageDataAndOrientation, версия Original.

Запуск (через ! или в Terminal):
    uv run python spike/download_probe.py "FUJIFILM X-T5" --skip=10
    uv run python spike/download_probe.py "Thailand 2017" --offline   # только снимки, которых нет на Mac
"""

import collections
import json
import sys
import threading
import time

import Foundation
import Photos

from photokit_dump import OUT, ensure_access, fetch_albums, fetch_assets, resources

TIMEOUT_S = 300


def locally_available(asset):
    # Недокументированный ключ PHAssetResource — только для диагностики в spike.
    res = next(r for r in resources(asset) if r.type() == Photos.PHAssetResourceTypePhoto)
    try:
        return bool(res.valueForKey_("locallyAvailable"))
    except Exception as e:
        return f"unknown: {e!r}"


def via_resource(asset, dest):
    res = next(r for r in resources(asset) if r.type() == Photos.PHAssetResourceTypePhoto)
    path = dest / f"A_{res.originalFilename()}"
    path.unlink(missing_ok=True)
    options = Photos.PHAssetResourceRequestOptions.new()
    options.setNetworkAccessAllowed_(True)
    progress = []
    options.setProgressHandler_(lambda p: progress.append(round(p, 2)))
    done, out = threading.Event(), {}

    def handler(error):
        out["error"] = str(error) if error else None
        done.set()

    started = time.monotonic()
    Photos.PHAssetResourceManager.defaultManager().writeDataForAssetResource_toFile_options_completionHandler_(
        res, Foundation.NSURL.fileURLWithPath_(str(path)), options, handler)
    finished = done.wait(TIMEOUT_S)
    return {"method": "A writeData", "file": res.originalFilename(), "seconds": round(time.monotonic() - started, 1),
            "ok": finished and not out.get("error") and path.exists(),
            "error": out.get("error") if finished else "timeout",
            "progress_last": progress[-3:], "progress_events": len(progress),
            "bytes": path.stat().st_size if path.exists() else 0}


def via_image_manager(asset):
    options = Photos.PHImageRequestOptions.new()
    options.setNetworkAccessAllowed_(True)
    options.setVersion_(Photos.PHImageRequestOptionsVersionOriginal)
    options.setDeliveryMode_(Photos.PHImageRequestOptionsDeliveryModeHighQualityFormat)
    options.setSynchronous_(True)  # асинхронный ответ приходит в главный поток — ждать его в нём нельзя
    done, out = threading.Event(), {}

    def handler(data, uti, orientation, info):
        out["bytes"] = data.length() if data else 0
        out["uti"] = uti
        out["error"] = str(info.get(Photos.PHImageErrorKey)) if info and info.get(Photos.PHImageErrorKey) else None
        out["in_cloud"] = bool(info.get(Photos.PHImageResultIsInCloudKey)) if info else None
        done.set()

    started = time.monotonic()
    Photos.PHImageManager.defaultManager().requestImageDataAndOrientationForAsset_options_resultHandler_(
        asset, options, handler)
    finished = done.wait(TIMEOUT_S)
    return {"method": "B imageData(Original)", "seconds": round(time.monotonic() - started, 1),
            "ok": finished and out.get("bytes", 0) > 0, **out, **({} if finished else {"error": "timeout"})}


def main(argv):
    skip = int(next((a.split("=", 1)[1] for a in argv if a.startswith("--skip=")), "0"))
    offline = "--offline" in argv
    titles = [a for a in argv if not a.startswith("--")]
    ensure_access()
    albums = {c.localizedTitle(): c for c in fetch_albums()}
    report = {}
    for title in titles:
        album = albums.get(title)
        if album is None:
            report[title] = "не найден"
            continue
        images = [a for a in fetch_assets(album) if a.mediaType() == Photos.PHAssetMediaTypeImage]
        combos = collections.Counter(tuple(sorted((r.type(), r.uniformTypeIdentifier()) for r in resources(a)))
                                     for a in images[:400])
        dest = OUT / "download_probe"
        dest.mkdir(parents=True, exist_ok=True)
        if offline:
            scanned = images[:600]
            local = [locally_available(a) for a in scanned]
            picked = [a for a, is_local in zip(scanned, local) if is_local is False][:3]
            print(f"{title}: из {len(scanned)} снимков локально {sum(x is True for x in local)}, "
                  f"только в iCloud {sum(x is False for x in local)}", flush=True)
            if len(picked) < 3:
                sys.exit("Недостаточно снимков только в iCloud — возьмите альбом постарше")
        else:
            picked = images[skip:skip + 3]
        print(f"{title}: снимки #{skip}–{skip + 2}, локально до скачивания: {[locally_available(a) for a in picked]}", flush=True)
        report[title] = {
            "images": len(images),
            "resource_combos_first_400": [[list(map(list, k)), v] for k, v in combos.most_common(6)],
            "local_before": [locally_available(a) for a in picked],
            "download": [via_resource(picked[0], dest), via_image_manager(picked[1]), via_resource(picked[2], dest)],
        }
        print(json.dumps(report[title]["download"], ensure_ascii=False, default=str), flush=True)
    (OUT / "download_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
