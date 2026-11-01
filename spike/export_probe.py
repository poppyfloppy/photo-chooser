"""Spike (одноразовый): выгрузка оригиналов из iCloud и пар RAW (Task 1, Step 2).

Ищет в альбоме пары RAW+JPEG/HEIC, одиночные RAW и самые старые снимки (скорее всего
лежат только в iCloud), выгружает по 2 штуки каждого вида в spike/out/export_probe/
и печатает время: секунды и больше означают реальное скачивание из iCloud.

Запуск (через ! или в Terminal):
    uv run python spike/export_probe.py "FUJIFILM X-T5"
"""

import json
import sys

import Photos

from photokit_dump import OUT, ensure_access, fetch_albums, fetch_assets, record, resources, write_resource

RAW_UTI_HINTS = ("raw", "dng", "raf", "cr2", "cr3", "nef", "arw")


def is_raw(res):
    uti = (res.uniformTypeIdentifier() or "").lower()
    return any(hint in uti for hint in RAW_UTI_HINTS)


def main(title):
    ensure_access()
    album = next((c for c in fetch_albums() if c.localizedTitle() == title), None)
    if album is None:
        sys.exit(f"Альбом «{title}» не найден")
    images = [a for a in fetch_assets(album) if a.mediaType() == Photos.PHAssetMediaTypeImage]
    pairs = [a for a in images if record(a)["has_raw_pair"]]
    raw_only = [a for a in images if not record(a)["has_raw_pair"] and any(is_raw(r) for r in resources(a))]
    oldest = [a for a in images if a not in pairs and a not in raw_only][:2]

    report = {"album": title, "images": len(images), "raw_pairs": len(pairs), "raw_only": len(raw_only),
              "utis": sorted({r.uniformTypeIdentifier() for a in images[:300] for r in resources(a)})}
    for kind, assets in (("raw_pair", pairs[:2]), ("raw_only", raw_only[:2]), ("oldest", oldest)):
        dest = OUT / "export_probe" / kind
        dest.mkdir(parents=True, exist_ok=True)
        report[kind] = [{"id": a.localIdentifier(), "files": [
            write_resource(r, dest / r.originalFilename()) for r in resources(a)
            if r.type() in (Photos.PHAssetResourceTypePhoto, Photos.PHAssetResourceTypeAlternatePhoto)]}
            for a in assets]
    (OUT / "export_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
