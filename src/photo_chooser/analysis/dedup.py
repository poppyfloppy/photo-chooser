"""Кластеры дублей и серий: близко по времени и похожи по визуальному отпечатку Vision."""

from photo_chooser.analysis.quality import feature_distance
from photo_chooser.config import Thresholds
from photo_chooser.models import Asset, LocalFeatures


def cluster(items: list[tuple[Asset, LocalFeatures]], th: Thresholds) -> list[list[str]]:
    """Кластеры uuid в порядке времени съёмки; связь — пара в окне duplicate_window_s с расстоянием ≤ порога."""
    items = sorted(items, key=lambda it: it[0].taken_at)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (a, fa) in enumerate(items):
        for j in range(i + 1, len(items)):
            b, fb = items[j]
            if (b.taken_at - a.taken_at).total_seconds() > th.duplicate_window_s:
                break
            if feature_distance(fa.feature_print, fb.feature_print) <= th.duplicate_distance:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[str]] = {}
    for i, (a, _) in enumerate(items):
        groups.setdefault(find(i), []).append(a.uuid)
    return [groups[root] for root in sorted(groups)]


def representatives(clusters: list[list[str]], quality: dict[str, float]) -> list[str]:
    """Лучший по локальной оценке в каждом кластере; при равенстве — более ранний."""
    return [max(c, key=lambda u: (quality.get(u, 0.0), -c.index(u))) for c in clusters]
