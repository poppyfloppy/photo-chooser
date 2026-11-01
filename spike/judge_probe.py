"""Spike (одноразовый): расход токенов второй ступени (оценка с причиной), одна группа.

Запуск: uv run python spike/judge_probe.py
"""

import base64
import json

import anthropic
from pydantic import BaseModel

from screen_probe import GROUP, OUT, pick_previews

THEME = "Пирамиды в Гиза"
SYSTEM = """You are a photo editor choosing travel photos for Instagram.
For every photo (each is preceded by its id), judge it against the theme given at the end:
- matches_theme: whether the photo belongs to the theme.
- feed_score (1-10): strength as an Instagram feed post: composition, light, visual impact.
- stories_score (1-10): suitability for Instagram stories: people and emotions, interesting moments or details worth telling about within the theme. Technical flaws are acceptable.
- reason: one short sentence in the language of the theme explaining the scores.
Return exactly one item for every photo id."""


class Item(BaseModel):
    id: str
    matches_theme: bool
    feed_score: int
    stories_score: int
    reason: str


class Output(BaseModel):
    items: list[Item]


def main():
    rows = [json.loads(line) for line in (OUT / "screen_v2.jsonl").read_text().splitlines()]
    giza = {it["file"] for r in rows for it in r["items"] if any(m["theme"] == 1 and m["relevance"] >= 5 for m in it["matches"])}
    group = [p for p in pick_previews() if p.name in giza][:GROUP]
    content = []
    for i, path in enumerate(group, 1):
        content.append({"type": "text", "text": f"id: p{i:02d}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(path.read_bytes()).decode()}})
    content.append({"type": "text", "text": f"Theme: «{THEME}»"})
    response = anthropic.Anthropic().messages.parse(
        model="claude-haiku-5-5", max_tokens=16000, system=SYSTEM,
        messages=[{"role": "user", "content": content}], output_format=Output, output_config={"effort": "medium"})
    items = response.parsed_output.items
    print(f"photos {len(group)}, in {response.usage.input_tokens}, out {response.usage.output_tokens}, "
          f"out/photo {response.usage.output_tokens / len(group):.0f}")
    for it in items[:5]:
        print(it.id, it.matches_theme, it.feed_score, it.stories_score, it.reason)


if __name__ == "__main__":
    main()
