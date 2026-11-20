"""The task-specific sentences of the prompt, for tasks that are about making something faster."""
from __future__ import annotations

from ...core.adapter import PromptText

GOAL = "You are optimising the task below."
PERSISTENCE = ("Your first correct, faster version is a starting point: profile it, find what still dominates, "
               "and keep improving while you see an untried idea with a realistic chance of a meaningful gain. "
               "If you are about to list a promising idea under `next` in your report, try it now instead. End "
               "the session once further work is unlikely to pay off.")
CONTINUING = ("Improve on it and keep it correct; replace parts of it only when you have evidence that something "
              "else is faster.")


def ranking(*, paired: bool, reference: str = "", hardware: str = "", samples: int = 2) -> str:
    """How sessions are ranked, for the prompt. `reference` names what a paired timing is taken against."""
    available = f", with {hardware} available" if hardware else ""
    if paired:
        text = (f"by a paired timing: your version and {reference or 'a reference'} are timed in alternation on "
                f"the same machine{available}, and the ratio is what counts. Only elapsed time matters, so using "
                "several threads or cores is allowed and counts.")
    else:
        text = f"by the speedup the task's own evaluator measures against the unmodified baseline{available}."
    text += " Only versions that pass the task's checks are ranked."
    if samples:
        text += f" A new best is measured {samples} more time(s) and ranked by the median, since timings vary."
    return text + " Time your own changes with alternating runs of both versions and compare medians."


def prompt_text(*, working: str, feedback: str = "", **ranking_settings) -> PromptText:
    return PromptText(goal=GOAL, working=working, ranking=ranking(**ranking_settings), feedback=feedback,
                      persistence=PERSISTENCE, continuing=CONTINUING)
