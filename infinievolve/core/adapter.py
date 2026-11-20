"""Interface between the campaign scheduler and one kind of task.

An adapter knows everything the framework does not: what the tasks are, where and how an agent session
runs, how its work is evaluated, and how that work is captured and restored. Work is asynchronous: the
scheduler submits jobs (scoring the baseline, or one agent session), polls them together, and collects
each one when it is done. A Job is persisted in <session>/job.json, so a restarted scheduler re-attaches
to jobs that are still running.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .events import EventLog
from .types import Job, Node, SessionResult, SessionSpec


class JobFailed(RuntimeError):
    """A job broke before producing results (infrastructure, not the agent's work).

    The adapter removes the attempt's local outputs before raising; nothing is archived, the task is
    paused for this run, and the next `run` resubmits the same session with the same inputs.
    """


class AgentUnavailable(JobFailed):
    """The agent never worked: its service refused it (a usage or rate limit, authentication, an outage).

    Nothing is archived and the task is not paused: the scheduler submits no new work for any task until
    retry_at (unix time; None = the scheduler's default wait), then resubmits the same session.
    """

    def __init__(self, message: str, retry_at: float | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class HostFault(JobFailed):
    """The place the job ran is broken, so the attempt says nothing about the work.

    The adapter has already arranged not to use that place again; nothing is archived, the task is not
    paused, and the same session is resubmitted at once.
    """


@dataclass(frozen=True)
class PromptText:
    """The task-specific sentences of the session prompt. Every one has a default."""
    ranking: str = ""            # how sessions are compared and on what it is measured ("": said from the declaration)
    goal: str = "You are working on the task below."
    working: str = "Do your work in the directory you are started in."   # how to leave it to be captured
    feedback: str = ""           # how to check work during the session, if the task offers a way
    persistence: str = ("Keep going while you still see an untried idea with a realistic chance of a "
                        "meaningful gain, and end the session once further work is unlikely to pay off.")
    continuing: str = ("Improve on it and keep it valid; replace parts of it only when you have evidence "
                       "that something else is better.")


class Adapter(Protocol):
    #: where the full archive is readable from inside the agent's environment
    archive_location: str

    def resolve_tasks(self, selection: tuple[str, ...] | str) -> list[str]:
        """Validate an explicit task list, or expand "all"."""

    def declaration(self, task: str) -> dict:
        """The task's declaration (see core.schema): its metrics, gate, ranking metric and artifact kind."""

    def instruction(self, task: str) -> str:
        """The task statement the agent is given."""

    def prompt_text(self, task: str) -> PromptText:
        """The task-specific sentences of the session prompt."""

    def default_time_limit_seconds(self, task: str) -> int:
        """The task's own per-session time limit, used when the config sets none."""

    def baseline_problem(self, task: str, root: Node) -> str | None:
        """Why the baseline result shows a broken environment (then no sessions are started), else None.

        A baseline that fails the gate is not a problem by itself: a task may start from nothing."""

    def provenance(self, tasks: list[str]) -> dict:
        """Versions and digests that pin what the campaign ran against."""

    def submit_baseline(self, task: str, session_dir: Path, events: EventLog) -> dict:
        """Start scoring the unmodified baseline (the root node s000); return the job handle."""

    def submit_session(self, spec: SessionSpec, session_dir: Path, events: EventLog) -> dict:
        """Start one agent session; its inputs are already under session_dir/input. Return the job handle."""

    def poll(self, jobs: list[Job], events: EventLog) -> set[str]:
        """Keys (Job.key) of the jobs that have finished, given all jobs in flight."""

    def collect(self, job: Job, session_dir: Path, events: EventLog) -> SessionResult:
        """Fetch and parse a finished job's outputs; raise JobFailed when it produced none."""

    # Optional, needed only by tasks whose declaration sets `confirm`:
    #   submit_rescore(task, name, artifact, job_dir, events, avoid=()) -> handle
    #       score archived work once more, without an agent; avoid lists the places (timing["where"])
    #       earlier scorings of it ran, for adapters that can choose another one
