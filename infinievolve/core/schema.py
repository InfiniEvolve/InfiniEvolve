"""A task's declaration: what its archive records and what its lineage is ranked by.

    metrics:                     every number the evaluator reports
      <name>:
        direction: max | min     which way is better
        scale: absolute | ratio  absolute: differences are meaningful (a pass rate, a size in bytes)
                                 ratio: quotients are meaningful and values are positive (a factor)
        anchor: baseline | none | <number>
                                 where the best-so-far curve starts: the baseline's own value, nothing
                                 until a session has one, or a fixed number
        format: "{:.3f}"         how the value is shown
    gate: [<check>, ...]         the evaluator's checks a version must all pass to be valid
    rank_by: <metric>            the one metric that chooses starting points and feeds the stop rule
    show: [<metric>, ...]        the metrics later sessions and the tables show (default: rank_by)
    confirm:                     score a new best again before it is trusted (optional)
      samples: 2
      aggregate: median | mean
    flags: {<name>: void | soft | hard}   levels of the flags the adapter raises (default: hard)
    report_fields: [{name, hint}, ...]    the fields of the report every session ends with
    artifact: <kind>             the form a session's work is archived in (see infinievolve.artifacts)

An adapter gives the declaration for each of its tasks; a campaign's `evaluation:` section may override
gate, rank_by, show and confirm. The declaration in force when the baseline is archived is fixed for the
life of that lineage.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import statistics

from .types import FLAG_LEVELS

DIRECTIONS = ("max", "min")
SCALES = ("absolute", "ratio")
AGGREGATES = {"median": statistics.median, "mean": statistics.fmean}
OVERRIDABLE = ("gate", "rank_by", "show", "confirm")
DEFAULT_REPORT_FIELDS = (
    ("approach", "one paragraph: what your final work does"),
    ("results", "what you measured or observed, and which version was best"),
    ("worked", "bullets"),
    ("did_not_work", "bullets, each with the reason"),
    ("next", "the most promising directions for the next session"),
)


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class Metric:
    name: str
    direction: str = "max"
    scale: str = "absolute"
    anchor: object = "baseline"       # "baseline" | "none" | a number
    format: str = "{:.3f}"

    def usable(self, value) -> bool:
        """Whether a value can rank: a finite number, and positive on a ratio scale."""
        return is_number(value) and (self.scale == "absolute" or value > 0)

    def better(self, value: float, than: float) -> bool:
        return value > than if self.direction == "max" else value < than

    def best(self, values) -> float | None:
        values = list(values)
        if not values:
            return None
        return max(values) if self.direction == "max" else min(values)

    def position(self, value: float) -> float:
        """The value on an axis where higher is better and distances compare (log of a ratio)."""
        placed = math.log(value) if self.scale == "ratio" else float(value)
        return placed if self.direction == "max" else -placed

    def improved(self, value: float, start: float, margin: float) -> bool:
        """Whether value beats start by more than margin: percent on a ratio scale, an amount otherwise."""
        sign = 1 if self.direction == "max" else -1
        threshold = start * (1 + sign * margin / 100) if self.scale == "ratio" else start + sign * margin
        return self.better(value, threshold)

    def text(self, value) -> str:
        return self.format.format(value) if is_number(value) else "-"

    def to_dict(self) -> dict:
        return {"direction": self.direction, "scale": self.scale, "anchor": self.anchor, "format": self.format}


@dataclass(frozen=True)
class Confirm:
    samples: int
    aggregate: str = "median"

    def combine(self, values) -> float:
        return AGGREGATES[self.aggregate](list(values))


@dataclass(frozen=True)
class Declaration:
    metrics: dict
    rank_by: str
    gate: tuple = ()
    show: tuple = ()
    confirm: Confirm | None = None
    flags: dict | None = None
    report_fields: tuple = DEFAULT_REPORT_FIELDS
    artifact: str = ""              # "" = the default kind

    @property
    def rank(self) -> Metric:
        return self.metrics[self.rank_by]

    def valid(self, checks: dict) -> bool:
        """A check the evaluator did not report counts as failed."""
        return all((checks or {}).get(name) is True for name in self.gate)

    def flag_level(self, name: str) -> str:
        return (self.flags or {}).get(name, "hard")

    def to_dict(self) -> dict:
        return {"metrics": {name: metric.to_dict() for name, metric in self.metrics.items()},
                "rank_by": self.rank_by, "gate": list(self.gate), "show": list(self.show),
                "confirm": ({"samples": self.confirm.samples, "aggregate": self.confirm.aggregate}
                            if self.confirm else None),
                "flags": dict(self.flags or {}),
                "report_fields": [{"name": name, "hint": hint} for name, hint in self.report_fields],
                "artifact": self.artifact}

    def merged(self, overrides: dict | None) -> "Declaration":
        """This declaration with a campaign's evaluation section applied."""
        overrides = dict(overrides or {})
        unknown = set(overrides) - set(OVERRIDABLE)
        if unknown:
            raise ValueError(f"evaluation: unsupported keys {sorted(unknown)}; allowed {OVERRIDABLE}")
        return parse_declaration({**self.to_dict(), **overrides})


def parse_metric(name: str, data: dict | None) -> Metric:
    data = dict(data or {})
    unknown = set(data) - {"direction", "scale", "anchor", "format"}
    if unknown:
        raise ValueError(f"metrics.{name}: unsupported keys {sorted(unknown)}")
    metric = Metric(name=name, direction=str(data.get("direction", "max")), scale=str(data.get("scale", "absolute")),
                    anchor=data.get("anchor", "baseline"), format=str(data.get("format", "{:.3f}")))
    if metric.direction not in DIRECTIONS:
        raise ValueError(f"metrics.{name}.direction must be one of {DIRECTIONS}")
    if metric.scale not in SCALES:
        raise ValueError(f"metrics.{name}.scale must be one of {SCALES}")
    if metric.anchor not in ("baseline", "none") and not metric.usable(metric.anchor):
        raise ValueError(f"metrics.{name}.anchor must be 'baseline', 'none' or a number the metric can hold")
    try:
        metric.format.format(1.0)
    except (IndexError, KeyError, ValueError) as error:
        raise ValueError(f"metrics.{name}.format is not a format for one number: {error}") from None
    return metric


def parse_declaration(data: dict) -> Declaration:
    if not isinstance(data, dict):
        raise ValueError("a declaration must be a mapping")
    unknown = set(data) - {"metrics", "rank_by", "gate", "show", "confirm", "flags", "report_fields", "artifact"}
    if unknown:
        raise ValueError(f"declaration: unsupported keys {sorted(unknown)}")
    raw = data.get("metrics") or {}
    if not isinstance(raw, dict) or not raw:
        raise ValueError("a declaration needs at least one metric")
    metrics = {str(name): parse_metric(str(name), spec) for name, spec in raw.items()}
    rank_by = data.get("rank_by") or (next(iter(metrics)) if len(metrics) == 1 else None)
    if rank_by not in metrics:
        raise ValueError(f"rank_by must name one of the declared metrics {sorted(metrics)}, got {rank_by!r}")
    gate = tuple(str(name) for name in data.get("gate") or ())
    show = tuple(str(name) for name in data.get("show") or (rank_by,))
    missing = [name for name in show if name not in metrics]
    if missing:
        raise ValueError(f"show names undeclared metrics {missing}")
    confirm = None
    if data.get("confirm"):
        spec = dict(data["confirm"])
        confirm = Confirm(samples=int(spec.get("samples", 2)), aggregate=str(spec.get("aggregate", "median")))
        if confirm.samples < 1 or confirm.aggregate not in AGGREGATES:
            raise ValueError(f"confirm needs samples >= 1 and aggregate in {sorted(AGGREGATES)}")
    flags = {str(name): str(level) for name, level in (data.get("flags") or {}).items()}
    wrong = {name: level for name, level in flags.items() if level not in FLAG_LEVELS}
    if wrong:
        raise ValueError(f"flags: levels must be one of {FLAG_LEVELS}, got {wrong}")
    fields = data.get("report_fields")
    if fields is None:
        report_fields = DEFAULT_REPORT_FIELDS
    else:
        report_fields = tuple((str(item["name"]), str(item.get("hint", ""))) for item in fields)
        if not report_fields:
            raise ValueError("report_fields must not be empty")
    return Declaration(metrics=metrics, rank_by=str(rank_by), gate=gate, show=show, confirm=confirm, flags=flags,
                       report_fields=report_fields, artifact=str(data.get("artifact") or ""))
