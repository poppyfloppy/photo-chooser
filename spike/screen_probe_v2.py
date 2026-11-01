"""Spike (одноразовый): вариант B первой ступени — все темы поездки в одном запросе.

Модель видит название альбома и весь список тем, для каждого кадра называет подходящие
темы с релевантностью 1–10 (без указания «при сомнении — да»). Пишет
spike/out/screen_v2.jsonl и печатает сводку по темам.

Запуск: uv run python spike/screen_probe_v2.py --go
"""

import base64
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from pydantic import BaseModel

from screen_probe import GROUP, OUT, pick_previews

PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}

ALBUM = "Egypt.26"
THEMES = [
    "Пирамиды в Гиза",
    "GEM",
    "Абу Симбел",
    "Долина Царей и Храм Хатшепсут",
    "Карнакский музей и Люксорский Храм",
    "Нил",
    "Саккара и Дахшур",
    "Оставшиеся храмы",
]

SYSTEM = """You sort travel photos into the themes of one trip for an Instagram shortlist.
You get the trip album name and a numbered list of themes written by the traveller, then photos, each preceded by its id.
Read the themes as the traveller meant them: abbreviations and short names refer to places or subjects of this trip, and a theme may be defined relative to the others (e.g. "remaining temples" = temples not covered by other themes).
For every photo:
- matches: the themes the photo belongs to, each with relevance 1-10 (10 = the theme's place or subject is clearly the photo's content; 6 = clearly from that place but not its main subject; omit themes below 5). A photo may match several themes or none (e.g. airport, plane window, hotel).
- feed_score (1-10): visual strength for an Instagram feed post: composition, light, subject.
- stories_score (1-10): suitability for Instagram stories: people and emotions, interesting moments or details worth telling about. Technical flaws are acceptable.
Return exactly one item for every photo id."""


class Match(BaseModel):
    theme: int
    relevance: int


class Item(BaseModel):
    id: str
    matches: list[Match]
    feed_score: int
    stories_score: int


class Output(BaseModel):
    items: list[Item]


def run_group(client, group, model, effort):
    content = []
    for i, path in enumerate(group, 1):
        content.append({"type": "text", "text": f"id: p{i:02d}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(path.read_bytes()).decode()}})
    themes = "\n".join(f"{n}. {t}" for n, t in enumerate(THEMES, 1))
    content.append({"type": "text", "text": f"Trip album: «{ALBUM}»\nThemes:\n{themes}"})
    started = time.monotonic()
    for _ in range(12):
        try:
            response = client.messages.parse(model=model, max_tokens=16000, system=SYSTEM,
                                             messages=[{"role": "user", "content": content}],
                                             output_format=Output, output_config={"effort": effort})
            break
        except anthropic.RateLimitError:
            time.sleep(20)
    ids = {f"p{i:02d}": p.name for i, p in enumerate(group, 1)}
    items = response.parsed_output.items if response.parsed_output else []
    return {"first": group[0].name, "seconds": round(time.monotonic() - started, 1),
            "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens,
            "items": [{"file": ids[it.id], **it.model_dump(exclude={"id"})} for it in items if it.id in ids],
            "missing": sorted(set(ids) - {it.id for it in items})}


def main():
    if "--go" not in sys.argv:
        return print("добавьте --go для платного прогона")
    args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    model, effort = args.get("--model", "claude-haiku-5-5"), args.get("--effort", "medium")
    tag = args.get("--tag", "")
    price_in, price_out = PRICES[model]
    files = pick_previews()
    groups = [files[i:i + GROUP] for i in range(0, len(files), GROUP)]
    client = anthropic.Anthropic(max_retries=2)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda g: run_group(client, g, model, effort), groups))
    with (OUT / f"screen_v2{tag}.jsonl").open("w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tin, tout = sum(r["input_tokens"] for r in results), sum(r["output_tokens"] for r in results)
    print(f"{model} effort={effort}: requests {len(results)}, missing ids {sum(len(r['missing']) for r in results)}, "
          f"tokens in {tin} out {tout}, usd {tin * price_in / 1e6 + tout * price_out / 1e6:.4f}, "
          f"seconds sum {sum(r['seconds'] for r in results):.0f}")
    items = [it for r in results for it in r["items"]]
    print(f"photos with no theme: {sum(not it['matches'] for it in items)} / {len(items)}")
    for n, t in enumerate(THEMES, 1):
        rel = [m["relevance"] for it in items for m in it["matches"] if m["theme"] == n]
        print(f"{t}: ≥5: {len(rel)}, ≥7: {sum(r >= 7 for r in rel)}, ≥9: {sum(r >= 9 for r in rel)}")


if __name__ == "__main__":
    main()
