"""A base class for adapters of timing tasks: it declares, words and assembles; the subclass runs jobs."""
from __future__ import annotations

from ...core.adapter import PromptText
from ...core.types import ScoreRow
from . import policy, wording


class PerfAdapter:
    """Subclass it, implement the job methods of the adapter interface, and say what your benchmark is like:

        gate                the evaluator's checks a version must pass
        exact(task)         True when the task's measure is a count that is the same every time (no pairing,
                            no repeat scoring)
        confirm_samples     how often a new best is scored again (0: never)
        working             how the agent must leave its work so that it can be captured
        pairs_with(task)    what a paired timing of the task is taken against ("the task's reference
                            solution", "the unmodified baseline", ...), or None when it cannot be paired
        hardware(task)      what a measurement has available, for the prompt
        feedback(task)      how the agent can check its work during a session
        flag_levels()       levels of the flags the adapter raises

    and build each scored version with timing_row(), which also says whether the measurement should be
    taken again.
    """
    gate: tuple = ("correct",)
    confirm_samples: int = 2
    working: str = "Do your work in the directory you are started in."

    def pairs_with(self, task: str) -> str | None:
        return None

    def exact(self, task: str) -> bool:
        return False

    def hardware(self, task: str) -> str:
        return ""

    def feedback(self, task: str) -> str:
        return ""

    def flag_levels(self) -> dict:
        return {}

    def declaration(self, task: str) -> dict:
        return policy.declaration(paired=self.pairs_with(task) is not None, gate=self.gate,
                                  samples=self.confirm_samples, flags=self.flag_levels(), exact=self.exact(task))

    def prompt_text(self, task: str) -> PromptText:
        reference = None if self.exact(task) else self.pairs_with(task)
        samples = 0 if self.exact(task) else self.confirm_samples
        return wording.prompt_text(working=self.working, feedback=self.feedback(task), paired=reference is not None,
                                   reference=reference or "", hardware=self.hardware(task), samples=samples)

    @staticmethod
    def timing_row(*, checks: dict, speedup, pairing: dict | None = None, reasons=(), extra: dict | None = None,
                   label: str = "final") -> tuple[ScoreRow, str | None]:
        """(the score row of one version, why its measurement should be taken again or None)."""
        metrics = {"speedup": speedup}
        if pairing:
            metrics.update(paired_speedup=pairing.get("ratio"), paired_dispersion=pairing.get("dispersion"))
        row = ScoreRow(label=label, checks=dict(checks), metrics=metrics, reasons=list(reasons),
                       extra={**(extra or {}), **({"pairing": pairing} if pairing else {})})
        return row, policy.distrust(pairing)
