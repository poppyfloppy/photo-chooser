"""Spike (одноразовый): сравнение конфигураций варианта B первой ступени.

Читает spike/out/screen_v2*.jsonl, печатает число кадров с релевантностью ≥ 7 по темам,
стоимость и время, а в spike/out/montage/v2_disagree.png собирает кадры, где темы
базовой Haiku medium и Sonnet medium расходятся.

Запуск: uv run python spike/compare_v2.py
"""

import json
from pathlib import Path

from PIL import Image, ImageDraw

from screen_probe_v2 import PRICES, THEMES

OUT = Path(__file__).parent / "out"
CONFIGS = {
    "haiku medium": ("screen_v2.jsonl", "claude-haiku-5-5"),
    "haiku high": ("screen_v2_haiku_high.jsonl", "claude-haiku-5-5"),
    "sonnet low": ("screen_v2_sonnet_low.jsonl", "claude-sonnet-5-5"),
    "sonnet medium": ("screen_v2_sonnet_medium.jsonl", "claude-sonnet-5-5"),
}
THRESHOLD = 7


def load(name):
    return [json.loads(line) for line in (OUT / name).read_text().splitlines()]


def themes_by_file(results):
    return {it["file"]: {m["theme"] for m in it["matches"] if m["relevance"] >= THRESHOLD}
            for r in results for it in r["items"]}


def main():
    data = {label: (load(f), model) for label, (f, model) in CONFIGS.items() if (OUT / f).exists()}
    header = f"{'тема':40s}" + "".join(f"{label:>15s}" for label in data)
    print(header)
    maps = {label: themes_by_file(rs) for label, (rs, _) in data.items()}
    for n, theme in enumerate(THEMES, 1):
        print(f"{theme[:40]:40s}" + "".join(f"{sum(n in t for t in m.values()):>15d}" for m in maps.values()))
    print(f"{'без темы':40s}" + "".join(f"{sum(not t for t in m.values()):>15d}" for m in maps.values()))
    for label, (rs, model) in data.items():
        pin, pout = PRICES[model]
        tin, tout = sum(r["input_tokens"] for r in rs), sum(r["output_tokens"] for r in rs)
        print(f"{label}: ${tin * pin / 1e6 + tout * pout / 1e6:.3f}, out tokens/photo {tout / 201:.0f}, "
              f"seconds sum {sum(r['seconds'] for r in rs):.0f}")

    base, other = maps.get("haiku medium"), maps.get("sonnet medium")
    if not (base and other):
        return
    diff = [f for f in base if base[f] != other.get(f, set())]
    print(f"расхождений haiku medium vs sonnet medium: {len(diff)} из {len(base)}")
    size, cols = 170, 6
    sheet = Image.new("RGB", (cols * size, ((len(diff[:48]) + cols - 1) // cols) * size), "white")
    draw = ImageDraw.Draw(sheet)
    for i, f in enumerate(diff[:48]):
        with Image.open(OUT / "previews" / f) as im:
            im.thumbnail((size - 4, size - 22))
            x, y = (i % cols) * size + 2, (i // cols) * size + 20
            sheet.paste(im.convert("RGB"), (x, y))
        label = f"H:{','.join(map(str, sorted(base[f]))) or '-'}  S:{','.join(map(str, sorted(other.get(f, set())))) or '-'}"
        draw.text((x + 2, y - 18), label, fill="black")
    sheet.save(OUT / "montage" / "v2_disagree.png")


if __name__ == "__main__":
    main()
