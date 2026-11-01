"""Spike (одноразовый): первая ступень (отбор по теме) на Haiku 5.5 (Task 1, Step 4–5).

Берёт каждое второе превью из spike/out/previews (по времени съёмки), прогоняет
каждую тему группами по 20 и пишет spike/out/screen.json и spike/out/screen.html —
контактный лист в порядке съёмки с отметками тем, которые выбрала модель.

Запуск:
    uv run python spike/screen_probe.py          # только оценка стоимости
    uv run python spike/screen_probe.py --go     # прогон (платный)
"""

import base64
import html
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from PIL import Image
from pydantic import BaseModel

OUT = Path(__file__).parent / "out"
PREVIEWS = OUT / "previews"
MODEL = "claude-haiku-5-5"
PRICE_IN, PRICE_OUT = 0.10, 0.50  # $ за 1 млн токенов
GROUP = 20
EST_OUTPUT_PER_PHOTO = 60

THEMES = [
    "Пирамиды в Гиза",
    "GEM",
    "Абу Симбел",
    "Долина Царей и Храм Хатшепсут",
    "Карнакский музей и Люксорский Храм",
    "Нил",
    "Саккара и Дахшур",
    "Оставшиеся храмы",
    "Храмы Египта, кроме Абу-Симбела, Карнака, Луксорского храма и храма Хатшепсут",
]

SYSTEM = """You screen travel photos for an Instagram shortlist.
For every photo (each is preceded by its id) decide whether it matches the theme given at the end, and give draft scores.
- matches_theme: true if the photo plausibly belongs to the theme (the place, object or subject it describes). When in doubt, answer true: missing a matching photo is worse than including an extra one. The theme may be in any language and may use abbreviations; read it as the traveller who wrote it would.
- feed_score (1-10): visual strength for an Instagram feed post: composition, light, subject.
- stories_score (1-10): suitability for Instagram stories: people and emotions, interesting moments or details worth telling about within this theme. Technical flaws are acceptable.
Return exactly one item for every photo id."""


class ScreenItem(BaseModel):
    id: str
    matches_theme: bool
    feed_score: int
    stories_score: int


class ScreenOutput(BaseModel):
    items: list[ScreenItem]


def pick_previews():
    album = {r["id"].replace("/", "_"): r["created_utc"] for r in json.loads((OUT / "album.json").read_text())}
    files = sorted(PREVIEWS.glob("*.jpg"), key=lambda p: album.get(p.stem) or "")
    return files[::2]


def image_tokens(path):
    with Image.open(path) as im:
        w, h = im.size
    return math.ceil(w * h / 750)


def run_group(client, theme, group):
    content = []
    for i, path in enumerate(group, 1):
        content.append({"type": "text", "text": f"id: p{i:02d}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(path.read_bytes()).decode()}})
    content.append({"type": "text", "text": f"Theme: «{theme}»"})
    started = time.monotonic()
    response = client.messages.parse(model=MODEL, max_tokens=16000, system=SYSTEM,
                                     messages=[{"role": "user", "content": content}],
                                     output_format=ScreenOutput, output_config={"effort": "medium"})
    seconds = time.monotonic() - started
    ids = {f"p{i:02d}": p.name for i, p in enumerate(group, 1)}
    items = response.parsed_output.items if response.parsed_output else []
    return {
        "theme": theme, "seconds": round(seconds, 1), "stop_reason": response.stop_reason,
        "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens,
        "items": [{"file": ids[it.id], **it.model_dump(exclude={"id"})} for it in items if it.id in ids],
        "missing": sorted(set(ids) - {it.id for it in items}),
    }


def main():
    files = pick_previews()
    groups = [files[i:i + GROUP] for i in range(0, len(files), GROUP)]
    est_in = sum(image_tokens(p) for p in files) * len(THEMES)
    est_out = len(files) * EST_OUTPUT_PER_PHOTO * len(THEMES)
    estimate = est_in * PRICE_IN / 1e6 + est_out * PRICE_OUT / 1e6
    print(f"{len(files)} превью × {len(THEMES)} тем, {len(groups) * len(THEMES)} запросов; "
          f"оценка ≈ {est_in:,} вх. токенов, ≈ ${estimate:.2f} (без учёта размышлений модели)")
    if "--go" not in sys.argv:
        return

    client = anthropic.Anthropic(max_retries=2)
    done_file = OUT / "screen_results.jsonl"
    done = [json.loads(line) for line in done_file.read_text().splitlines()] if done_file.exists() else []
    done_keys = {(r["theme"], r["first"]) for r in done}
    jobs = [(t, g) for t in THEMES for g in groups if (t, g[0].name) not in done_keys]
    print(f"готово ранее: {len(done)}, осталось: {len(jobs)}")
    lock = threading.Lock()

    def job(theme, group):
        for attempt in range(12):
            try:
                result = run_group(client, theme, group)
                break
            except anthropic.RateLimitError:  # лимит токенов в минуту: ждём и повторяем
                time.sleep(20)
        else:
            raise RuntimeError("rate limit: исчерпаны попытки")
        result["first"] = group[0].name
        with lock, done_file.open("a") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = done + list(pool.map(lambda j: job(*j), jobs))

    tokens_in = sum(r["input_tokens"] for r in results)
    tokens_out = sum(r["output_tokens"] for r in results)
    per_theme = {}
    for theme in THEMES:
        rs = [r for r in results if r["theme"] == theme]
        per_theme[theme] = {
            "matched": sum(it["matches_theme"] for r in rs for it in r["items"]),
            "missing_ids": sum(len(r["missing"]) for r in rs),
            "seconds_sum": round(sum(r["seconds"] for r in rs), 1),
            "usd": round(sum(r["input_tokens"] for r in rs) * PRICE_IN / 1e6 + sum(r["output_tokens"] for r in rs) * PRICE_OUT / 1e6, 4),
        }
    summary = {
        "previews": len(files), "requests": len(results),
        "input_tokens": tokens_in, "output_tokens": tokens_out,
        "input_tokens_per_image": round(tokens_in / (len(files) * len(THEMES)), 1),
        "output_tokens_per_image": round(tokens_out / (len(files) * len(THEMES)), 1),
        "usd_total": round(tokens_in * PRICE_IN / 1e6 + tokens_out * PRICE_OUT / 1e6, 4),
        "stop_reasons": sorted({r["stop_reason"] for r in results}),
        "per_theme": per_theme,
    }
    (OUT / "screen.json").write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1))

    labels = {p.name: [] for p in files}
    for r in results:
        for it in r["items"]:
            if it["matches_theme"]:
                labels[it["file"]].append(f'{THEMES.index(r["theme"]) + 1}')
    legend = "".join(f"<li><b>{i}</b> — {html.escape(t)}</li>" for i, t in enumerate(THEMES, 1))
    cells = []
    for i, p in enumerate(files, 1):
        with Image.open(p) as im:
            im.thumbnail((200, 200))
            from io import BytesIO
            buf = BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=70)
        data = base64.b64encode(buf.getvalue()).decode()
        tags = ", ".join(labels[p.name]) or "—"
        cells.append(f'<figure><img src="data:image/jpeg;base64,{data}"><figcaption>#{i} · темы: {tags}</figcaption></figure>')
    style = ("body{font:14px -apple-system,sans-serif;margin:16px;background:#fff;color:#111}"
             "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}}"
             ".grid{display:flex;flex-wrap:wrap;gap:8px}figure{margin:0;width:200px}img{max-width:200px;max-height:200px;display:block}"
             "figcaption{font-size:12px}")
    (OUT / "screen.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>Screen probe</title><style>{style}</style>"
        f"<h1>Первая ступень: {MODEL}</h1><ol>{legend}</ol><div class='grid'>{''.join(cells)}</div>")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
