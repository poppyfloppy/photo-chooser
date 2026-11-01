"""Spike (одноразовый): Vision и локальные метрики по превью из spike/out (Task 1, Step 3).

Считает для каждого превью эстетику и флаг «утилитарный» (Vision), качество лиц,
визуальный отпечаток, резкость (дисперсия лапласиана) и яркость. Пишет
spike/out/vision.json и spike/out/vision.html — контактный лист для подбора порогов.

Запуск: uv run python spike/vision_probe.py
"""

import base64
import html
import json
import statistics
from pathlib import Path

import numpy as np
import Foundation
import Vision
from PIL import Image

OUT = Path(__file__).parent / "out"
PREVIEWS = OUT / "previews"


def gray(path):
    with Image.open(path) as im:
        im.thumbnail((1024, 1024))
        return np.asarray(im.convert("L"), dtype=np.float32) / 255.0


def laplacian_var(g):
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var() * 1e4)


def vision(path):
    aesthetics = Vision.VNCalculateImageAestheticsScoresRequest.alloc().init()
    faces = Vision.VNDetectFaceCaptureQualityRequest.alloc().init()
    fprint = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(Foundation.NSURL.fileURLWithPath_(str(path)), {})
    ok, err = handler.performRequests_error_([aesthetics, faces, fprint], None)
    if not ok:
        return {"error": str(err)}
    a = aesthetics.results()[0] if aesthetics.results() else None
    face_q = [float(f.faceCaptureQuality()) for f in (faces.results() or []) if f.faceCaptureQuality() is not None]
    fp = fprint.results()[0]
    dtype = np.float32 if fp.elementType() == 1 else np.float64
    vec = np.frombuffer(bytes(fp.data()), dtype=dtype, count=fp.elementCount()).astype(np.float32)
    return {
        "aesthetic": float(a.overallScore()) if a else None,
        "is_utility": bool(a.isUtility()) if a else None,
        "faces": len(face_q), "face_quality": max(face_q) if face_q else None,
        "_fp_obs": fp, "_vec": vec,
    }


def thumb_tag(path, caption):
    with Image.open(path) as im:
        im.thumbnail((240, 240))
        from io import BytesIO
        buf = BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=70)
    data = base64.b64encode(buf.getvalue()).decode()
    return f'<figure><img src="data:image/jpeg;base64,{data}"><figcaption>{html.escape(caption)}</figcaption></figure>'


def main():
    album = {r["id"].replace("/", "_"): r for r in json.loads((OUT / "album.json").read_text())}
    rows = []
    for path in sorted(PREVIEWS.glob("*.jpg")):
        rid = path.stem
        g = gray(path)
        row = {"file": path.name, "created": album.get(rid, {}).get("created_utc"),
               "is_screenshot": album.get(rid, {}).get("is_screenshot"),
               "sharpness": laplacian_var(g), "mean_luma": float(g.mean())}
        row.update(vision(path))
        rows.append(row)
    rows.sort(key=lambda r: r["created"] or "")

    # Отпечатки: соседние по времени пары и сверка computeDistance с L2.
    pairs = []
    for prev, cur in zip(rows, rows[1:]):
        l2 = float(np.linalg.norm(prev["_vec"] - cur["_vec"]))
        try:
            ok, dist = prev["_fp_obs"].computeDistance_toFeaturePrintObservation_error_(None, cur["_fp_obs"], None)[:2]
            vn = float(dist) if ok else None
        except Exception as e:  # spike: фиксируем, если сигнатура pyobjc другая
            vn = f"error: {e!r}"
        from datetime import datetime
        dt = (datetime.fromisoformat(cur["created"]) - datetime.fromisoformat(prev["created"])).total_seconds()
        pairs.append({"a": prev["file"], "b": cur["file"], "seconds": dt, "l2": l2, "vn_distance": vn})

    def pct(values):
        values = sorted(v for v in values if v is not None)
        return {p: round(values[int(len(values) * p / 100) - (p == 100)], 3) for p in (5, 10, 25, 50, 75, 90, 100)} if values else None

    summary = {
        "count": len(rows),
        "sharpness_pct": pct([r["sharpness"] for r in rows]),
        "luma_pct": pct([r["mean_luma"] for r in rows]),
        "aesthetic_pct": pct([r.get("aesthetic") for r in rows]),
        "utility": sum(bool(r.get("is_utility")) for r in rows),
        "with_faces": sum(r.get("faces", 0) > 0 for r in rows),
        "pair_l2_within_10s_pct": pct([p["l2"] for p in pairs if p["seconds"] <= 10]),
        "pair_l2_over_10min_pct": pct([p["l2"] for p in pairs if p["seconds"] > 600]),
        "vn_equals_l2": [round(p["l2"], 4) == round(p["vn_distance"], 4) if isinstance(p["vn_distance"], float) else p["vn_distance"]
                         for p in pairs[:5]],
    }
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    (OUT / "vision.json").write_text(json.dumps({"summary": summary, "rows": clean, "pairs": pairs}, ensure_ascii=False, indent=1))

    by_sharp = sorted(clean, key=lambda r: r["sharpness"])[:48]
    by_luma = sorted(clean, key=lambda r: r["mean_luma"])[:12] + sorted(clean, key=lambda r: r["mean_luma"])[-12:]
    utility = [r for r in clean if r.get("is_utility")][:24]
    close = sorted((p for p in pairs if p["seconds"] <= 120), key=lambda p: p["l2"])
    sections = [
        ("Самые нерезкие (sharpness по возрастанию)", [thumb_tag(PREVIEWS / r["file"], f'sharp {r["sharpness"]:.1f}') for r in by_sharp]),
        ("Самые тёмные и самые светлые (mean_luma)", [thumb_tag(PREVIEWS / r["file"], f'luma {r["mean_luma"]:.3f}') for r in by_luma]),
        ("Vision: утилитарные кадры", [thumb_tag(PREVIEWS / r["file"], f'aesth {r["aesthetic"]:.2f}') for r in utility]),
    ]
    pair_html = ""
    for p in close[::max(len(close) // 40, 1)][:40]:
        caption = f'L2 {p["l2"]:.3f}, {p["seconds"]:.0f} c'
        pair_html += f'<div class="pair">{thumb_tag(PREVIEWS / p["a"], "")}{thumb_tag(PREVIEWS / p["b"], caption)}</div>'
    body = "".join(f"<h2>{html.escape(t)}</h2><div class='grid'>{''.join(items)}</div>" for t, items in sections)
    body += f"<h2>Соседние кадры (≤ 2 мин) по возрастанию расстояния отпечатков</h2><div class='grid'>{pair_html}</div>"
    style = ("body{font:14px -apple-system,sans-serif;margin:16px;background:#fff;color:#111}"
             "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}}"
             ".grid{display:flex;flex-wrap:wrap;gap:8px}figure{margin:0;width:240px}"
             "img{max-width:240px;max-height:240px;display:block}figcaption{font-size:12px}.pair{display:flex;gap:4px;border:1px solid #888;padding:4px}")
    (OUT / "vision.html").write_text(f"<!doctype html><meta charset=utf-8><title>Vision probe</title><style>{style}</style>{body}")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
