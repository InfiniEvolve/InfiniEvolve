"""What a timing task declares, and when one of its measurements should not be believed."""
from __future__ import annotations

# A paired measurement whose blocks disagree by more than this is taken again.
MAX_DISPERSION = 0.5
# The evaluator's own speedup and the paired one, both against the same reference, differing by more than
# this factor means one of them was taken on a machine that was not behaving.
MAX_MISMATCH = 10.0


def declaration(*, paired: bool, gate, samples: int = 2, flags: dict | None = None, exact: bool = False,
                solution: str | None = None) -> dict:
    """The declaration of a timing task.

    exact: the task's "speed" is a count (instructions, bytes, operations), the same every time it is taken.
    Nothing is paired and nothing is scored again then: both would only repeat the same number.

    speedup          what the task's own evaluator reports: one timing against its baseline
    paired_speedup   the candidate against a reference, timed in alternation on one machine (see measure)

    solution: the name of the reference when it is a known solution of the task rather than the baseline;
    paired_speedup 1 is then as fast as that solution, and the curve marks it.

    Sessions are ranked by the paired value where the task can be paired, by the evaluator's otherwise. Either
    way a new best is scored `samples` more times and ranked by the median: even a paired timing varies.
    """
    metrics = {"speedup": {"direction": "max", "scale": "ratio", "anchor": "baseline", "format": "{:.2f}x"}}
    if exact:
        paired, samples = False, 0
    if paired:
        metrics["paired_speedup"] = {"direction": "max", "scale": "ratio", "anchor": "baseline", "format": "{:.3f}x",
                                     **({"marks": {solution: 1.0}} if solution else {})}
        metrics["paired_dispersion"] = {"direction": "min", "scale": "absolute", "anchor": "none", "format": "{:.1%}"}
    return {"metrics": metrics, "gate": list(gate), "rank_by": "paired_speedup" if paired else "speedup",
            "show": ["paired_speedup", "speedup"] if paired else ["speedup"],
            "confirm": {"samples": samples, "aggregate": "median"} if samples else None,
            "flags": dict(flags or {}), "artifact": "patch"}


def distrust(pairing: dict | None, *, max_dispersion: float = MAX_DISPERSION) -> str | None:
    """Why a paired measurement should be taken again, or None. `pairing` is measure.paired()'s result, with
    "status" set when the timing could not be completed."""
    if not pairing:
        return None
    if pairing.get("status") not in (None, "complete"):
        return f"the paired timing did not complete ({pairing.get('status')})"
    dispersion = pairing.get("dispersion")
    if dispersion is not None and dispersion > max_dispersion:
        return f"the paired timing's blocks disagree by {dispersion:.0%}"
    return None


def same_hardware(expected: str | None, observed: str | None) -> str | None:
    """Why a measurement is not comparable with the others (taken on other hardware), or None. Timings of
    one task belong on one model of processor or accelerator: a ratio measured elsewhere is another ratio."""
    if expected and observed and expected.strip() != observed.strip():
        return f"it was measured on {observed!r}, the campaign's measurements belong on {expected!r}"
    return None
