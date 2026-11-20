"""Stop rule on the best-so-far ranking value.

best[0] is where the curve starts (the metric's anchor: the baseline's own value, a fixed number, or
nothing); best[i] is the best valid value after agent session i. With m finished sessions the task stops
when
    m >= max_sessions, or
    m >= window_k and best[m] does not beat best[m - window_k] by more than min_improvement
where min_improvement is a percentage for a ratio metric and an amount for an absolute one.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .schema import Metric
from .types import Node


@dataclass(frozen=True)
class StopDecision:
    stop: bool
    reason: str
    sessions: int
    best: float | None
    window_start_best: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def ranks(node: Node, metric: Metric) -> bool:
    return node.valid and metric.usable(node.rank_value) and not node.excluded


def best_curve(nodes: list[Node], metric: Metric) -> list[float | None]:
    ordered = sorted(nodes, key=lambda node: node.index)
    root = next((node for node in ordered if node.is_root), None)
    if metric.anchor == "baseline":
        best = root.rank_value if root and ranks(root, metric) else None
    elif metric.anchor == "none":
        best = None
    else:
        best = float(metric.anchor)
    curve = [best]
    for node in ordered:
        if node.is_root:
            continue
        if ranks(node, metric) and (best is None or metric.better(node.rank_value, best)):
            best = node.rank_value
        curve.append(best)
    return curve


def check(nodes: list[Node], metric: Metric, *, max_sessions: int, window_k: int,
          min_improvement: float) -> StopDecision:
    curve = best_curve(nodes, metric)
    sessions = len(curve) - 1
    unit = "%" if metric.scale == "ratio" else ""
    if sessions >= max_sessions:
        return StopDecision(True, f"reached max_sessions={max_sessions}", sessions, curve[-1], None)
    if sessions < window_k:
        return StopDecision(False, f"{sessions} < window_k={window_k} sessions", sessions, curve[-1], None)
    start = curve[sessions - window_k]
    if start is None:
        return StopDecision(False, f"no ranked value {window_k} sessions ago yet", sessions, curve[-1], None)
    if metric.improved(curve[-1], start, min_improvement):
        return StopDecision(False, f"best {metric.text(curve[-1])} beats {metric.text(start)} by more than "
                            f"{min_improvement}{unit}", sessions, curve[-1], start)
    return StopDecision(True, f"best {metric.text(curve[-1])} within {min_improvement}{unit} of "
                        f"{metric.text(start)} over the last {window_k} sessions", sessions, curve[-1], start)
