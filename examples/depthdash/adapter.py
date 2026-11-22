"""Run the local adapter inside a GPU allocation; stop if the baseline is broken."""
from infinievolve.local import LocalAdapter


class DepthDashAdapter(LocalAdapter):
    def baseline_problem(self, task, root):
        # A completed baseline below the statistical target is a valid starting
        # point for optimization; infrastructure/equivalence failures are not.
        if not root.valid and not (root.scores and all(
            all(score.get("checks", {}).get(key) for key in ("frozen_sources", "equivalence", "completed"))
            for score in root.scores
        )):
            reasons = [reason for score in root.scores for reason in score.get("reasons", [])]
            return "DepthDash baseline failed: " + "; ".join(reasons)
        return None
