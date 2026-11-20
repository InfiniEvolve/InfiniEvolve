"""Append-only event stream of a campaign (events.jsonl), one line per event:
{ts, type, task, session_id, data}. A log bound to a task stamps that task on every event."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

TYPES = ("campaign_init", "campaign_stop", "campaign_hold", "task_reset", "task_stop", "task_paused",
         "declaration_pinned", "declaration_mismatch", "baseline_scored", "selection", "prompt_rendered",
         "job_submitted", "job_started", "job_finished", "collected", "archived", "stop_check", "error",
         "poll_recovered", "agent_unavailable", "host_fault", "flagged", "flags_cleared",
         "confirmation_needed", "confirmation_sample", "rescored", "job_held", "host_excluded")
# An adapter may also record events of its own under any name with this prefix.
ADAPTER_PREFIX = "adapter_"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class EventLog:
    def __init__(self, path: Path, task: str | None = None) -> None:
        self.path = Path(path)
        self.task = task

    def for_task(self, task: str) -> "EventLog":
        return EventLog(self.path, task)

    def emit(self, kind: str, session_id: str | None = None, **data) -> dict:
        if kind not in TYPES and not kind.startswith(ADAPTER_PREFIX):
            raise ValueError(f"unknown event type {kind}: use one of the framework's, or a name starting "
                             f"with {ADAPTER_PREFIX!r}")
        event = {"ts": now(), "type": kind, "task": self.task, "session_id": session_id, "data": data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as stream:
            stream.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        return event

    def read(self) -> list[dict]:
        """Events of the bound task since its last reset (all events when unbound)."""
        if not self.path.is_file():
            return []
        events = []
        for line in self.path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line cut short by a crash
            if self.task is None or event.get("task") == self.task:
                if self.task is not None and event["type"] == "task_reset":
                    events = []
                events.append(event)
        return events

    def last(self, kind: str, session_id: str | None = None) -> dict | None:
        for event in reversed(self.read()):
            if event["type"] == kind and (session_id is None or event["session_id"] == session_id):
                return event
        return None
