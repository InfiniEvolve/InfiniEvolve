"""A stand-in adapter for tests: no agent, no processes; every job is over as soon as it is submitted.

options:
  values:    {task: [baseline, session 1, session 2, ...]}   the metric `score` of each job's final version;
             an entry may also be a dict: {value, checks, flags, work, error} or {raise: "unavailable"|"failed"}
  rescore:   {task: [value or dict, ...]}                    what repeat scorings return, in order
  declaration: overrides merged into the default declaration
"""
from __future__ import annotations

from pathlib import Path

from infinievolve.adapters import register_adapter
from infinievolve.core.adapter import AgentUnavailable, JobFailed, PromptText
from infinievolve.core.types import ScoreRow, SessionResult

DECLARATION = {"metrics": {"score": {"direction": "max", "scale": "ratio", "anchor": "baseline"}},
               "gate": ["ok"], "rank_by": "score", "artifact": "text"}


class FakeAdapter:
    archive_location = "`/archive.md`"

    def __init__(self, config) -> None:
        self.options = config.options
        self.submitted: list = []          # every SessionSpec handed to submit_session
        self.rescored: dict = {}
        self.raised: set = set()

    def resolve_tasks(self, selection):
        return sorted(self.options["values"]) if selection == "all" else list(selection)

    def declaration(self, task):
        return {**DECLARATION, **self.options.get("declaration", {})}

    def instruction(self, task):
        return f"Do {task}. Braces like {{start}} in a task statement stay as they are."

    def prompt_text(self, task):
        return PromptText(ranking="by `score`.")

    def default_time_limit_seconds(self, task):
        return 600

    def baseline_problem(self, task, root):
        return None

    def provenance(self, tasks):
        return {"fake": True}

    def submit_baseline(self, task, session_dir, events):
        return {"index": 0}

    def submit_session(self, spec, session_dir, events):
        self.submitted.append(spec)
        return {"index": spec.index}

    def submit_rescore(self, task, name, artifact, job_dir, events, avoid=()):
        count = self.rescored.get(task, 0)
        self.rescored[task] = count + 1
        return {"rescore": count}

    def poll(self, jobs, events):
        return {job.key for job in jobs}

    def _result(self, entry, default_work: bytes) -> SessionResult:
        entry = entry if isinstance(entry, dict) else {"value": entry}
        value = entry.get("value")
        row = ScoreRow(label="final", checks=entry.get("checks", {"ok": True}),
                       metrics={} if value is None else {"score": value})
        return SessionResult(scores=[row], artifacts={"final": entry.get("work", default_work)},
                             report="approach: did something", timing={"agent_seconds": 60.0, "where": "here"},
                             flags=entry.get("flags", {}), error=entry.get("error"))

    def collect(self, job, session_dir, events):
        if "rescore" in job.handle:
            return self._result(self.options["rescore"][job.task][job.handle["rescore"]], b"")
        entry = self.options["values"][job.task][job.handle["index"]]
        if isinstance(entry, dict) and "raise" in entry and job.key not in self.raised:
            self.raised.add(job.key)       # only the first attempt fails
            if entry["raise"] == "unavailable":
                raise AgentUnavailable("usage limit", retry_at=entry.get("retry_at"))
            raise JobFailed("the job broke")
        if isinstance(entry, dict) and "raise" in entry:
            entry = entry["then"]
        return self._result(entry, f"work of {job.session_id}\n".encode())


register_adapter("fake", FakeAdapter)
