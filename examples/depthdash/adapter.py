"""Run the local adapter inside a GPU allocation; stop if the baseline is broken."""
from infinievolve.local import LocalAdapter


class DepthDashAdapter(LocalAdapter):
    def baseline_problem(self, task, root):
        if not root.valid:
            reasons = [reason for score in root.scores for reason in score.get("reasons", [])]
            return "DepthDash baseline failed: " + "; ".join(reasons)
        return None
