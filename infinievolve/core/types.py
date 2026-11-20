"""Records passed between the scheduler and an adapter, and the archived node."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

ROOT_ID = "s000"
# What a flag does to its node:
#   void  no session happened (the agent never worked): invisible to ranking, the stop rule, the session
#         count and later prompts
#   soft  recorded and shown, but the node is ranked as usual
#   hard  archived and shown, but never a starting point, a best or part of the stop curve
FLAG_LEVELS = ("void", "soft", "hard")


def session_id(index: int) -> str:
    return f"s{index:03d}"


@dataclass(frozen=True)
class ScoreRow:
    """One evaluated version of a session's work (the final state, or an intermediate submission).

    checks are named pass/fail results; the task's declaration says which of them a version must pass to
    be valid. metrics are named numbers; the declaration says what each means and which one ranks.
    """
    label: str
    checks: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)   # why checks failed, in the evaluator's words
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "ScoreRow":
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})


@dataclass(frozen=True)
class SessionSpec:
    """Everything an adapter needs to run one agent session; written under input/ before launch."""
    task: str
    session_id: str
    index: int
    parent_id: str | None
    start_strategy: str
    time_limit_seconds: int
    prompt: str
    archive_md: str
    start_artifact: bytes | None      # the parent's archived work; None when starting from the baseline


@dataclass
class Job:
    """A submitted unit of work, persisted as job.json so a restarted scheduler finds it again.

    handle is whatever the adapter needs to find the job after a restart.
    """
    task: str
    session_id: str
    kind: str                      # "baseline" | "session" | "confirm"
    handle: dict
    submitted_at: str

    @property
    def key(self) -> str:
        return f"{self.task}/{self.session_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})


@dataclass
class SessionResult:
    """What an adapter hands back once a job is over.

    artifacts maps every scored label to that version's work, in the form the task's artifact kind
    defines. flags maps a flag name to why the adapter raised it; the declaration gives its level.
    """
    scores: list
    artifacts: dict = field(default_factory=dict)
    report: str = ""
    timing: dict = field(default_factory=dict)    # seconds by phase; "where" names the place it ran
    usage: dict = field(default_factory=dict)     # tokens, cost
    stats: dict = field(default_factory=dict)     # anything else worth keeping per session
    flags: dict = field(default_factory=dict)
    error: str | None = None
    remeasure: str | None = None                  # why this measurement should be taken again, if it should


@dataclass
class Node:
    """One archived session. The root (s000) is the unmodified baseline."""
    session_id: str
    index: int
    parent_id: str | None
    start_strategy: str
    task: str
    agent: dict | None             # the campaign's agent settings; None for the root
    chosen_label: str | None
    artifact: dict                 # {kind, fingerprint, summary}
    valid: bool
    rank_metric: str
    rank_value: float | None
    scores: list
    report: str = ""
    timing: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)     # name -> {level, note}
    flags_cleared: dict | None = None             # set once a person checked the flags
    error: str | None = None
    archived_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Node":
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})

    def _flagged(self, level: str) -> bool:
        return not self.flags_cleared and any(flag.get("level") == level for flag in self.flags.values())

    @property
    def void(self) -> bool:
        return self._flagged("void")

    @property
    def excluded(self) -> bool:
        """Flagged and not cleared: archived and shown, but not ranked."""
        return self._flagged("hard") or self._flagged("void")

    @property
    def is_root(self) -> bool:
        return self.session_id == ROOT_ID

    def chosen(self) -> dict:
        """The score row of the archived version (the dict itself, so updates stick); {} if none."""
        return next((row for row in self.scores if row.get("label") == self.chosen_label), {})

    def flag_notes(self) -> str:
        return "; ".join(flag.get("note") or name for name, flag in self.flags.items())
