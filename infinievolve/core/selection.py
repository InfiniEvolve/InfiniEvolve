"""Choose the node whose work a new session starts from.

    best      the valid node with the best ranking value
    explore   sample among valid nodes (root included) with probability proportional to
              sigmoid(sharpness * z) / (1 + children): z is the node's distance from the median on the
              metric's own axis (the log of a ratio; an absolute value in units of the candidates' spread),
              and children is how many sessions already started from it. Better nodes are drawn more
              often, much-used ones less often, and every valid node keeps some chance.
    baseline  always the unmodified baseline (root); the archive is context only

Each draw uses its own RNG seeded by (seed, session index), so a resumed campaign makes the same choice.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import random
import statistics

from .schema import Metric
from .types import ROOT_ID, Node

STRATEGIES = ("best", "explore", "baseline")


@dataclass(frozen=True)
class Selection:
    strategy: str
    parent_id: str | None
    reason: str
    candidates: list
    seed: str
    draw: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def eligible(nodes: list[Node], metric: Metric) -> list[Node]:
    return [node for node in nodes if node.valid and metric.usable(node.rank_value) and not node.excluded]


def best_node(nodes: list[Node], metric: Metric) -> Node | None:
    """The best eligible node; the earliest one among equals."""
    best = None
    for node in sorted(eligible(nodes, metric), key=lambda node: node.index):
        if best is None or metric.better(node.rank_value, best.rank_value):
            best = node
    return best


def reference_node(nodes: list[Node], metric: Metric) -> Node | None:
    """The node the others are compared with: the baseline when it ranks, else the first session that does
    (a task may start from nothing, and then the first valid work is the yardstick). Derived from the
    archive each time, so flagging or clearing a node moves it without anything to repair."""
    ranked = sorted(eligible(nodes, metric), key=lambda node: node.index)
    return ranked[0] if ranked else None


def versus(metric: Metric, value, reference) -> float | None:
    """A value against the reference's: their quotient on a ratio scale, their difference otherwise."""
    if not metric.usable(value) or not metric.usable(reference):
        return None
    return value / reference if metric.scale == "ratio" else value - reference


def versus_text(metric: Metric, value, reference) -> str:
    compared = versus(metric, value, reference)
    if compared is None:
        return "-"
    return f"{compared:.2f}x" if metric.scale == "ratio" else ("+" if compared >= 0 else "-") + metric.text(abs(compared))


def children(nodes: list[Node]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes:
        if node.parent_id:
            counts[node.parent_id] = counts.get(node.parent_id, 0) + 1
    return counts


def explore_weights(candidates: list[Node], counts: dict[str, int], metric: Metric, sharpness: float) -> dict[str, float]:
    places = {node.session_id: metric.position(node.rank_value) for node in candidates}
    middle = statistics.median(places.values())
    spread = 1.0
    if metric.scale == "absolute":   # an absolute metric has no natural unit: use the candidates' own spread
        spread = statistics.pstdev(places.values()) or 1.0
    return {sid: 1 / (1 + math.exp(-sharpness * (place - middle) / spread)) / (1 + counts.get(sid, 0))
            for sid, place in places.items()}


def select(strategy: str, nodes: list[Node], metric: Metric, *, seed: int, index: int,
           sharpness: float = 2.0) -> Selection:
    seed_text = f"{seed}:{index}"
    root = ROOT_ID if any(node.session_id == ROOT_ID for node in nodes) else None
    pool = eligible(nodes, metric)
    counts = children(nodes)
    rows = [{"session_id": node.session_id, "value": node.rank_value, "children": counts.get(node.session_id, 0)}
            for node in pool]
    if strategy == "baseline":
        return Selection(strategy, root, "baseline strategy always starts from the unmodified baseline", rows, seed_text)
    if not pool:
        return Selection(strategy, root, "no valid node yet: start from the baseline", rows, seed_text)
    if strategy == "best":
        best = best_node(pool, metric)
        return Selection(strategy, best.session_id, f"best {metric.name} {metric.text(best.rank_value)}", rows,
                         seed_text)
    if strategy == "explore":
        weights = explore_weights(pool, counts, metric, sharpness)
        for row in rows:
            row["weight"] = weights[row["session_id"]]
        draw = random.Random(seed_text).random()
        total, cumulative = sum(weights.values()), 0.0
        chosen = pool[-1].session_id
        for node in pool:
            cumulative += weights[node.session_id] / total
            if draw < cumulative:
                chosen = node.session_id
                break
        return Selection(strategy, chosen, f"weighted draw {draw:.4f} over {len(pool)} candidates", rows, seed_text,
                         draw)
    raise ValueError(f"unknown start strategy {strategy}")
