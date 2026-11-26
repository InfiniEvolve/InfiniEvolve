"""Campaign scheduler: sequential sessions by default, optional parallel rounds per task.

The global max_parallel limits all jobs. RoundController persists cohort snapshots,
parent plans and barriers; the legacy one-agent path retains existing behavior.
Never-submitted inputs are refreshed from current instructions on resume. Submitted
jobs reattach to their saved handles and retain their original inputs.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import time
from typing import Callable

from ..artifacts import artifact_kind
from ..config import CampaignConfig, TaskSettings
from . import prompt as prompts
from . import selection, stopping
from .rounds import RoundController, round_state
from .adapter import Adapter, AgentUnavailable, HostFault, JobFailed
from .archive import CampaignStore, TaskStore, read_json, sha256_bytes, write_json
from .events import now, parse_time
from .schema import AGGREGATES, Declaration, parse_declaration
from .types import ROOT_ID, Job, Node, ScoreRow, SessionResult, SessionSpec, session_id
from .views import current_stop, write_matrix, write_summary


def choose_scored(result: SessionResult, declaration: Declaration) -> tuple[str | None, ScoreRow | None]:
    """The best valid scored version by the ranking metric, else a valid one without a value (archived,
    but not a starting point), else the final state (archived for reference)."""
    metric = declaration.rank
    valid = [row for row in result.scores if declaration.valid(row.checks)]
    best = None
    for row in valid:
        value = row.metrics.get(metric.name)
        if metric.usable(value) and (best is None or metric.better(value, best.metrics[metric.name])):
            best = row
    if best is not None:
        return best.label, best
    if valid:
        return valid[0].label, valid[0]
    if "final" in result.artifacts:
        return "final", None
    return next(iter(result.artifacts), None), None


def task_state(store: TaskStore, settings: TaskSettings) -> dict:
    """One task's state, derived only from its archived nodes, session files and events."""
    declaration = store.declaration()
    nodes = [node for node in store.nodes() if not node.void]
    best = selection.best_node(nodes, declaration.rank) if declaration else None
    stop_event = current_stop(store)
    paused = store.events.last("task_paused")
    return {
        "task": store.name, "start_strategy": settings.start, "rank_by": declaration.rank_by if declaration else None,
        "root_scored": any(node.is_root for node in nodes),
        "sessions": len([node for node in nodes if not node.is_root]),
        "best_session": best.session_id if best else None, "best": best.rank_value if best else None,
        "best_curve": stopping.best_curve(nodes, declaration.rank) if declaration else [],
        "round": round_state(store),
        "in_flight": store.in_flight(), "stopped": stop_event is not None,
        "stop_reason": stop_event["data"].get("reason") if stop_event else None,
        "last_pause": paused["data"].get("message") if paused else None,
        "total_agent_seconds": sum(node.timing.get("agent_seconds") or 0 for node in nodes),
        "total_wall_seconds": sum(node.timing.get("wall_seconds") or 0 for node in nodes),
    }


def campaign_state(store: CampaignStore, config: CampaignConfig, tasks: list[str]) -> dict:
    states = {task: task_state(store.task(task), config.for_task(task)) for task in tasks}
    return {
        "campaign": config.name, "adapter": config.adapter, "tasks": states,
        "tasks_stopped": sum(state["stopped"] for state in states.values()), "tasks_total": len(tasks),
        "sessions": sum(state["sessions"] for state in states.values()),
        "in_flight": [f"{task}/{sid}" for task, state in states.items() for sid in state["in_flight"]],
        **{key: sum(state[key] for state in states.values()) for key in ("total_agent_seconds", "total_wall_seconds")},
    }


class TaskLoop:
    """One task's lineage: decides its next job and archives finished ones."""

    def __init__(self, store: TaskStore, settings: TaskSettings, adapter: Adapter,
                 log: Callable[[str], None]) -> None:
        self.store, self.settings, self.adapter, self.log = store, settings, adapter, log
        self.paused: str | None = None       # reason, for the rest of this run
        self.stopped = False
        self._hold_reported = False
        self._mismatch_reported = False
        self.unavailable: AgentUnavailable | None = None   # read and cleared by the scheduler

    @property
    def task(self) -> str:
        return self.store.name

    # -- the declaration ---------------------------------------------------------------
    def configured_declaration(self) -> Declaration:
        """What the adapter declares for this task, with the campaign's evaluation section applied."""
        declaration = parse_declaration(self.adapter.declaration(self.task)).merged(self.settings.evaluation)
        artifact_kind(declaration.artifact)   # fails here on an unknown kind, before any job
        if declaration.confirm and not hasattr(self.adapter, "submit_rescore"):
            raise ValueError("the declaration sets `confirm`, but this adapter cannot score archived work "
                             "again (it has no submit_rescore)")
        return declaration

    def declaration(self) -> Declaration:
        """The lineage's declaration: fixed when the baseline is archived (task.json) and kept afterwards,
        so one lineage never mixes values that mean different things.

        `confirm` is one exception: it does not change what a value means, only how many more scorings a
        new best gets before it is trusted, which an operator may want to change mid-campaign for its
        cost. It follows the configuration; a change is written into task.json and recorded. The metric's
        marks are the other: they never rank, they are only drawn, so the lineage takes the configured ones."""
        pinned = self.store.declaration()
        configured = self.configured_declaration()
        if pinned is None:
            return configured
        if configured.confirm != pinned.confirm:
            pinned = self._follow_confirm(pinned, configured)
        if configured.to_dict() != pinned.to_dict() and configured.without_marks() == pinned.without_marks():
            # marks are only drawn: the lineage takes the configured ones
            meta = read_json(self.store.meta_path) or {}
            write_json(self.store.meta_path, {**meta, "declaration": configured.to_dict()})
            return configured
        if configured.to_dict() != pinned.to_dict() and not self._mismatch_reported:
            self._mismatch_reported = True
            self.store.events.emit("declaration_mismatch", None, pinned=pinned.to_dict(),
                                   configured=configured.to_dict())
            self.log(f"[{self.task}] the configured declaration differs from the one this lineage is fixed to; "
                     "keeping the lineage's (reset the task to change it)")
        return pinned

    def _follow_confirm(self, pinned: Declaration, configured: Declaration) -> Declaration:
        """Take the configured `confirm` into the lineage's declaration. Confirmations already requested
        finish with the number of scorings they were asked for; later new bests use the new setting."""
        previous, current = pinned.to_dict()["confirm"], configured.to_dict()["confirm"]
        meta = read_json(self.store.meta_path)
        meta["declaration"]["confirm"] = current
        write_json(self.store.meta_path, meta)
        self.store.events.emit("confirm_changed", None, previous=previous, confirm=current)
        said = lambda value: (f"{value['samples']} more scoring(s), {value['aggregate']}" if value else "off")
        self.log(f"[{self.task}] confirm changed: {said(previous)} -> {said(current)}")
        return replace(pinned, confirm=configured.confirm)

    def ranked(self, nodes: list[Node]) -> list[Node]:
        """The real sessions (selection and the stop rule see only these)."""
        return [node for node in nodes if not node.void]

    # -- time limit --------------------------------------------------------------------
    def time_limit_seconds(self) -> int:
        if self.settings.session_minutes is not None:
            return int(self.settings.session_minutes * 60)
        return int(self.adapter.default_time_limit_seconds(self.task))

    # -- scheduling --------------------------------------------------------------------
    def submitted_jobs(self) -> list[Job]:
        """Jobs submitted earlier whose results are not archived yet (re-attached after a restart)."""
        jobs = [Job.from_dict(read_json(self.store.session_dir(sid) / "job.json")) for sid in self.store.in_flight()]
        for path in sorted((self.store.root / "sessions").glob("*/confirm/r*/job.json")):
            if not (path.parent / "sample.json").is_file():
                jobs.append(Job.from_dict(json.loads(path.read_text())))
        return jobs

    # -- confirming a new best ---------------------------------------------------------
    def check_confirmation(self, node: Node, distrusted: str | None = None, reference_nodes=None) -> None:
        """Score a node's work again where one scoring is not enough: a new best of a task that declares
        `confirm` (one lucky measurement must not become the start of every later session), and any
        measurement its adapter does not trust. The node is then ranked by the aggregate."""
        declaration = self.declaration()
        metric, confirm = declaration.rank, declaration.confirm
        if not hasattr(self.adapter, "submit_rescore") or node.excluded or not node.valid:
            return
        if distrusted:
            samples, why = max(2, confirm.samples if confirm else 2), f"its measurement is not trusted: {distrusted}"
        elif confirm and not node.is_root and metric.usable(node.rank_value):
            others = [other for other in (self.ranked(self.store.nodes()) if reference_nodes is None else reference_nodes)
                      if other.session_id != node.session_id]
            best = selection.best_node(others, metric)
            if best is not None and not metric.better(node.rank_value, best.rank_value):
                return
            samples, why = confirm.samples, f"new best {metric.text(node.rank_value)}"
        else:
            return
        directory = self.store.session_dir(node.session_id) / "confirm"
        write_json(directory / "needed.json", {"samples": samples, "original": node.rank_value, "reason": why,
                                               "aggregate": confirm.aggregate if confirm else "median",
                                               "where": node.timing.get("where"), "detected_at": now()})
        self.store.events.emit("confirmation_needed", node.session_id, samples=samples, original=node.rank_value,
                               reason=why)
        self.log(f"[{self.task}/{node.session_id}] {why}; scoring it {samples} more time(s) before it is trusted")

    def pool(self, fingerprint: str | None) -> None:
        """Nodes whose work is identical are one piece of work measured several times: rank each of them by
        the aggregate of all those measurements, so that no copy wins on the luck of its own."""
        declaration = self.declaration()
        metric = declaration.rank
        same = [node for node in self.store.nodes() if fingerprint is not None and not node.void and node.valid
                and node.artifact.get("fingerprint") == fingerprint]
        if len(same) < 2:
            return
        values = [value for node in same for value in self.measurements(node, metric)]
        if not values:
            return
        how = declaration.confirm.aggregate if declaration.confirm else "median"
        value = AGGREGATES[how](values)
        for node in same:
            if node.rank_value != value:
                previous, node.rank_value = node.rank_value, value
                node.chosen().setdefault("extra", {})["pooled"] = {
                    "values": values, "aggregate": how, "nodes": [other.session_id for other in same]}
                self.store.update_node(node)
                self.store.events.emit("rescored", node.session_id, values=values, value=value, previous=previous,
                                       pooled_with=[other.session_id for other in same])
        self.log(f"[{self.task}] {', '.join(node.session_id for node in same)} hold the same work: each ranked by "
                 f"{metric.text(value)}, the {how} of its {len(values)} measurements")
        write_matrix(self.store)

    @staticmethod
    def measurements(node: Node, metric) -> list:
        """Every usable value this node's own work was measured at: its scoring, and any repeat scorings."""
        row = node.chosen()
        values = [(row.get("metrics") or {}).get(metric.name)]
        values += [sample.get("value") for sample in ((row.get("extra") or {}).get("confirmation") or {}).get("samples", [])]
        return [value for value in values if metric.usable(value)]

    def _confirm_dirs(self) -> list[Path]:
        return sorted(path.parent for path in (self.store.root / "sessions").glob("*/confirm/needed.json")
                      if not (path.parent / "outcome.json").is_file())

    def _confirm_running(self) -> bool:
        return any((path / "job.json").is_file() and not (path / "sample.json").is_file()
                   for directory in self._confirm_dirs() for path in directory.glob("r*"))

    def pending_confirmation(self) -> Path | None:
        directories = self._confirm_dirs()
        return directories[0] if directories and not self._confirm_running() else None

    def _submit_confirmation(self, directory: Path) -> Job:
        sid = directory.parent.name
        done = sorted(directory.glob("r*/sample.json"))
        attempt = len(done) + 1
        places = [read_json(directory / "needed.json").get("where")] + [read_json(path).get("where") for path in done]
        avoid = tuple(sorted({place for place in places if place}))
        name = f"{sid}-c{attempt}"
        self._write_reference(directory / f"r{attempt}" / "input", self.ranked(self.store.nodes()), self.declaration())
        handle = self.adapter.submit_rescore(self.task, name, self.store.artifact(sid), directory / f"r{attempt}",
                                             self.store.events, avoid=avoid)
        job = Job(self.task, name, "confirm", handle, now())
        write_json(directory / f"r{attempt}" / "job.json", job.to_dict())
        self.log(f"[{self.task}/{sid}] confirmation scoring {attempt} submitted")
        return job

    def _finish_confirmation(self, job: Job) -> None:
        sid, attempt = job.session_id.rsplit("-c", 1)
        directory = self.store.session_dir(sid) / "confirm"
        declaration = self.declaration()
        metric, confirm = declaration.rank, declaration.confirm
        result = self.adapter.collect(job, directory / f"r{attempt}", self.store.events)
        row = result.scores[0] if result.scores else None
        valid = bool(row) and declaration.valid(row.checks)
        value = row.metrics.get(metric.name) if valid and metric.usable(row.metrics.get(metric.name)) else None
        sample = {"value": value, "valid": valid, "where": result.timing.get("where")}
        write_json(directory / f"r{attempt}" / "sample.json", sample)
        self.store.events.emit("confirmation_sample", sid, attempt=int(attempt), **sample)
        self.log(f"[{self.task}/{sid}] confirmation {attempt}: {metric.text(value)}")
        needed = read_json(directory / "needed.json")
        samples = [read_json(path) for path in sorted(directory.glob("r*/sample.json"))]
        if len(samples) < needed["samples"]:
            return
        how = needed.get("aggregate") or (confirm.aggregate if confirm else "median")
        values = [value for value in [needed["original"]] + [item["value"] for item in samples] if value is not None]
        node = self.store.node(sid)
        combined = AGGREGATES[how](values) if values else node.rank_value
        node.rank_value = combined
        node.chosen().setdefault("extra", {})["confirmation"] = {
            "original": needed["original"], "samples": samples, "aggregate": how, "reason": needed.get("reason")}
        failed = [item for item in samples if not item["valid"]]
        if failed:   # a second scoring that rejects the work contradicts the first: a person decides
            node.flags["confirmation_failed"] = {
                "level": "hard", "note": f"{len(failed)} of {len(samples)} repeat scorings did not pass the checks"}
            node.flags_cleared = None
            self.store.events.emit("flagged", sid, flag="confirmation_failed", level="hard",
                                   reason=node.flags["confirmation_failed"]["note"], source="loop")
        self.store.update_node(node)
        write_json(directory / "outcome.json", {"values": values, "value": combined, "failed": len(failed)})
        self.store.events.emit("rescored", sid, values=values, value=combined, previous=needed["original"])
        self.log(f"[{self.task}/{sid}] ranked by {metric.text(combined)}, the {how} of "
                 f"{[metric.text(v) for v in values]}")
        write_matrix(self.store)
        self.pool(node.artifact.get("fingerprint"))

    # -- the next job ------------------------------------------------------------------
    def next_job(self) -> Job | None:
        """Submit this task's next unit of work; None when the task is stopped or paused."""
        if self.paused or self.stopped:
            return None
        try:
            rounds = RoundController(self)
            if rounds.active() is not None:
                return rounds.next_job()
            if self.store.in_flight():
                return None
            if self._confirm_running():
                return None                    # rank only once the new best was scored again
            confirming = self.pending_confirmation()
            if confirming:
                return self._submit_confirmation(confirming)
            root = self.store.node(ROOT_ID)
            if root is None:
                return self._submit_root()
            problem = self.adapter.baseline_problem(self.task, root)
            if problem:
                raise RuntimeError(f"the baseline result points to a broken environment ({problem}); "
                                   f"fix it, then reset the task {self.task}")
            if rounds.enabled and self._prepared_session() is None:
                return rounds.next_job()
            declaration = self.declaration()
            nodes = self.store.nodes()
            ranked = self.ranked(nodes)
            decision = stopping.check(ranked, declaration.rank, max_sessions=self.settings.max_sessions,
                                      window_k=self.settings.window_k,
                                      min_improvement=self.settings.min_improvement)
            prepared = self._prepared_session()
            if decision.stop and prepared is None:
                self.store.events.emit("stop_check", None, **decision.to_dict())
                if current_stop(self.store) is None:
                    self.store.events.emit("task_stop", None, **decision.to_dict())
                    self.log(f"[{self.task}] stop: {decision.reason}")
                self.stopped = True
                return None
            if prepared is None and self.settings.hold:   # held by the operator: no new session, no stop
                if not self._hold_reported:
                    self._hold_reported = True
                    self.log(f"[{self.task}] held (task_overrides hold: true); no new sessions")
                return None
            if prepared is None:
                self.store.events.emit("stop_check", None, **decision.to_dict())
                index = max((node.index for node in nodes), default=0) + 1
                spec = self._prepare(index, nodes, ranked, declaration)
            else:
                spec = self._prepare_again(prepared, nodes, ranked, declaration)
            handle = self.adapter.submit_session(spec, self.store.session_dir(spec.session_id), self.store.events)
            job = self._record_job(spec.session_id, "session", handle)
            self.log(f"[{self.task}/{spec.session_id}] submitted, start from {spec.parent_id or 'baseline'} "
                     f"({spec.start_strategy}); limit {spec.time_limit_seconds // 60} min")
            return job
        except Exception as error:  # a broken task must not take the other tasks down
            self.pause("submit", error)
            return None

    def finish(self, job: Job) -> Node | None:
        if job.kind == "confirm":
            try:
                self._finish_confirmation(job)
            except JobFailed as error:   # retried: the scoring job is submitted again
                sid, attempt = job.session_id.rsplit("-c", 1)
                (self.store.session_dir(sid) / "confirm" / f"r{attempt}" / "job.json").unlink(missing_ok=True)
                self.log(f"[{job.key}] confirmation scoring failed, retrying: {str(error).splitlines()[0]}")
            except Exception as error:
                self.pause("confirmation", error)
            return None
        directory = self.store.session_dir(job.session_id)
        try:
            result = self.adapter.collect(job, directory, self.store.events)
        except HostFault as error:
            (directory / "job.json").unlink(missing_ok=True)  # resubmitted at once, elsewhere
            self.store.events.emit("host_fault", job.session_id, message=str(error)[-2000:])
            self.log(f"[{job.key}] the place it ran is broken ({str(error).splitlines()[0]}); not archived, "
                     "resubmitting")
            return None
        except AgentUnavailable as error:
            (directory / "job.json").unlink(missing_ok=True)  # the same session is resubmitted later
            self.unavailable = error
            self.store.events.emit("agent_unavailable", job.session_id, message=str(error)[-2000:],
                                   retry_at=error.retry_at)
            self.log(f"[{job.key}] the agent never ran ({str(error).splitlines()[0]}); not archived, will resubmit")
            return None
        except JobFailed as error:
            (directory / "job.json").unlink(missing_ok=True)  # the next run resubmits this session
            self.pause("job failed", error)
            return None
        except Exception as error:  # e.g. a copy that failed: keep job.json, collect again next run
            self.pause("collect", error)
            return None
        try:
            if job.kind == "baseline":
                spec = SessionSpec(self.task, ROOT_ID, 0, None, "root", 0, "", "", None)
            else:
                spec = self._load_spec(job.session_id)
            if spec.round_id:
                write_json(directory / "remeasure.json", {"reason": result.remeasure})
            node = self._archive(spec, result, prepared_at=read_json(directory / "input" / "spec.json")["prepared_at"])
        except Exception as error:
            self.pause("archive", error)
            return None
        if job.kind == "baseline":
            self.store.events.emit("baseline_scored", ROOT_ID, value=node.rank_value, valid=node.valid)
        self.log(self._summary(node))
        if "duplicate" in node.flags:   # the same work measured once more: all its copies share what was measured
            self.pool(node.artifact.get("fingerprint"))
            node = self.store.node(node.session_id)
        if not node.round_id:
            self.check_confirmation(node, distrusted=result.remeasure)
        return node

    def pause(self, where: str, error: Exception) -> None:
        self.paused = f"{where}: {error}"
        self.store.events.emit("error", None, where=where, message=str(error)[-3000:])
        self.store.events.emit("task_paused", None, message=self.paused[-3000:])
        first_line = str(error).splitlines()[0] if str(error) else repr(error)
        self.log(f"[{self.task}] paused for this run ({where}): {first_line}")

    # -- internals ---------------------------------------------------------------------
    def _record_job(self, sid: str, kind: str, handle: dict) -> Job:
        job = Job(self.task, sid, kind, handle, now())
        write_json(self.store.session_dir(sid) / "job.json", job.to_dict())
        return job

    def _submit_root(self) -> Job:
        self.configured_declaration()   # a declaration that does not parse stops the task before any job
        directory = self.store.session_dir(ROOT_ID)
        prepared_at = read_json(directory / "input" / "spec.json", {}).get("prepared_at") or now()
        write_json(directory / "input" / "spec.json", {"task": self.task, "session_id": ROOT_ID, "index": 0,
                                                       "kind": "baseline", "prepared_at": prepared_at})
        handle = self.adapter.submit_baseline(self.task, directory, self.store.events)
        self.log(f"[{self.task}/{ROOT_ID}] submitted baseline scoring")
        return self._record_job(ROOT_ID, "baseline", handle)

    def _prepared_session(self) -> str | None:
        """A session whose inputs exist but which was never submitted: a crash in between, or a submit the
        adapter refused (a round gate, a quota)."""
        for path in sorted((self.store.root / "sessions").glob("s*/input/spec.json")):
            directory = path.parent.parent
            if directory.name != ROOT_ID and not (directory / "node.json").is_file() \
                    and not (directory / "job.json").is_file():
                return directory.name
        return None

    def _prepare_again(self, sid: str, nodes: list[Node], ranked: list[Node], declaration: Declaration) -> SessionSpec:
        """Prepare a session that was never submitted once more, from the current state: the instruction, the
        prompt text and the archive may all have changed since. It keeps its id; nothing of the earlier
        preparation is handed on."""
        directory = self.store.session_dir(sid) / "input"
        old = read_json(directory / "spec.json")
        old_prompt = (directory / "prompt.md").read_text() if (directory / "prompt.md").is_file() else ""
        # spec.json first: a crash from here on leaves a session that is no longer prepared, and the next
        # run prepares the same index from scratch
        (directory / "spec.json").unlink()
        for name in ("prompt.md", "archive.md", "selection.json"):
            (directory / name).unlink(missing_ok=True)
        for pattern in ("start.*", "reference.*"):
            for path in directory.glob(pattern):
                path.unlink()
        spec = self._prepare(int(old["index"]), nodes, ranked, declaration)
        changed = {"prompt_changed": spec.prompt != old_prompt,
                   "parent_changed": spec.parent_id != old.get("parent_id")}
        self.store.events.emit("session_reprepared", sid, previously_prepared_at=old.get("prepared_at"),
                               previous_parent=old.get("parent_id"), parent=spec.parent_id, **changed)
        self.log(f"[{self.task}/{sid}] was prepared but never submitted; prepared again from the current state"
                 + (f" ({', '.join(key.split('_')[0] for key, value in changed.items() if value)} changed)"
                    if any(changed.values()) else ""))
        return spec

    def _prepare(self, index: int, nodes: list[Node], ranked: list[Node], declaration: Declaration,
                 *, chosen=None, metadata=None, extra_prompt="") -> SessionSpec:
        sid = session_id(index)
        if self.store.node(sid) is not None or (self.store.session_dir(sid) / "job.json").exists():
            raise ValueError(f"cannot prepare {sid}: it is already submitted or archived")
        kind = artifact_kind(declaration.artifact)
        chosen = chosen or selection.select(self.settings.start, ranked, declaration.rank, seed=self.settings.seed,
                                  index=index, sharpness=self.settings.sharpness)
        parent = next((node for node in nodes if node.session_id == chosen.parent_id), None)
        start = self.store.artifact(parent.session_id) if parent and not parent.is_root else b""
        carry = self.settings.carry
        text = prompts.session_prompt(
            text=self.adapter.prompt_text(self.task), instruction=self.adapter.instruction(self.task),
            declaration=declaration, parent=parent, nodes=ranked, carry=carry,
            report_chars=self.settings.report_chars, location=self.adapter.archive_location,
            hidden=self.settings.hide_scores, rules=self.settings.prompt_rules)
        text += extra_prompt
        archive_md = (prompts.archive_markdown(self.store, ranked, declaration, self.settings.hide_scores,
                                               with_work=carry == "full") if carry != "none" else "")
        directory = self.store.session_dir(sid) / "input"
        directory.mkdir(parents=True, exist_ok=True)
        # Also used for never-submitted round members: remove stale optional inputs.
        (directory / "spec.json").unlink(missing_ok=True)
        for pattern in ("start.*", "reference.*", "archive.md"):
            for path in directory.glob(pattern):
                path.unlink()
        (directory / "prompt.md").write_text(text)
        if carry != "none":   # the file exists from the first session on, so an adapter can always mount it
            (directory / "archive.md").write_text(archive_md)
        if start:
            (directory / f"start{kind.suffix}").write_bytes(start)
        reference = self._write_reference(directory, ranked, declaration)
        write_json(directory / "selection.json", chosen.to_dict())
        self.store.events.emit("selection", sid, **chosen.to_dict())
        self.store.events.emit("prompt_rendered", sid, prompt_chars=len(text), archive_chars=len(archive_md),
                               prompt_sha256=sha256_bytes(text.encode()), nodes_in_archive=len(ranked))
        # spec.json last: its presence marks the session as prepared
        write_json(directory / "spec.json", {
            "task": self.task, "session_id": sid, "index": index, "parent_id": chosen.parent_id,
            "start_strategy": chosen.strategy, "time_limit_seconds": self.time_limit_seconds(),
            "time_limit_source": "config" if self.settings.session_minutes is not None else "task",
            "agent": self.settings.agent, "start_file": f"start{kind.suffix}" if start else None,
            "start_sha256": sha256_bytes(start) if start else None, "reference_id": reference,
            "prepared_at": now(), **(metadata or {})})
        return self._load_spec(sid)

    def _write_reference(self, directory: Path, ranked: list[Node], declaration: Declaration) -> str | None:
        """Put the reference node's work beside a job's inputs (input/reference<suffix>), for adapters that
        measure against it. Nothing is written when the reference is the baseline, or when there is none."""
        reference = selection.reference_node(ranked, declaration.rank)
        if reference is not None and not reference.is_root:
            directory.mkdir(parents=True, exist_ok=True)
            suffix = artifact_kind(declaration.artifact).suffix
            (directory / f"reference{suffix}").write_bytes(self.store.artifact(reference.session_id))
        return reference.session_id if reference else None

    def _load_spec(self, sid: str) -> SessionSpec:
        directory = self.store.session_dir(sid) / "input"
        data = read_json(directory / "spec.json")
        archive = directory / "archive.md"
        start = directory / data["start_file"] if data.get("start_file") else None
        return SessionSpec(self.task, sid, data["index"], data["parent_id"], data["start_strategy"],
                           data["time_limit_seconds"], (directory / "prompt.md").read_text(),
                           archive.read_text() if archive.is_file() else "",   # absent when nothing is carried
                           start.read_bytes() if start else None, data.get("round_id"),
                           data.get("parent_ids", []), data.get("variation", {}))

    def _archive(self, spec: SessionSpec, result: SessionResult, *, prepared_at: str) -> Node:
        declaration = self.declaration()
        metric, kind = declaration.rank, artifact_kind(declaration.artifact)
        directory = self.store.session_dir(spec.session_id)
        derived = directory / "derived"
        derived.mkdir(parents=True, exist_ok=True)
        write_json(derived / "scores.json", [row.__dict__ for row in result.scores])
        write_json(derived / "stats.json", result.stats)
        (derived / "report.md").write_text(result.report or "")
        for label, data in result.artifacts.items():   # every scored version, not only the archived one
            (derived / "artifacts").mkdir(exist_ok=True)
            (derived / "artifacts" / f"{label}{kind.suffix}").write_bytes(data)

        label, best = choose_scored(result, declaration)
        value = best.metrics.get(metric.name) if best is not None else None
        value = value if metric.usable(value) else None
        is_root = spec.session_id == ROOT_ID
        data = result.artifacts.get(label, b"") if label else b""
        fingerprint = kind.fingerprint(data)
        flags = {name: {"level": declaration.flag_level(name), "note": why} for name, why in result.flags.items()}
        if not is_root and fingerprint is not None:   # the same work scored again must not look like progress
            same = [node for node in self.store.nodes()
                    if not node.void and node.session_id != spec.session_id
                    and node.artifact.get("fingerprint") == fingerprint]
            if same:
                first = min(same, key=lambda node: node.index)
                flags["duplicate"] = {"level": "soft", "note": f"its work is identical to {first.session_id}'s, "
                                      "which is already scored"}
        timing = dict(result.timing)
        started = parse_time(prepared_at)
        timing["wall_seconds"] = round(time.time() - started.timestamp(), 1) if started else None
        node = Node(
            session_id=spec.session_id, index=spec.index, parent_id=spec.parent_id, start_strategy=spec.start_strategy,
            task=self.task, agent=None if is_root else dict(self.settings.agent), chosen_label=label,
            artifact={"kind": kind.name, "fingerprint": fingerprint, "summary": kind.summary(data)},
            valid=best is not None, rank_metric=metric.name, rank_value=value,
            scores=[row.__dict__ for row in result.scores], report=result.report, timing=timing, usage=result.usage,
            stats=result.stats, flags=flags,
            error=result.error or (f"passes the checks, but has no usable {metric.name} value (not a starting point)"
                                   if best is not None and value is None else None),
            archived_at=now(), round_id=spec.round_id, parent_ids=spec.parent_ids, variation=spec.variation)
        self.store.archive_node(node, data, kind.suffix)
        if is_root and not self.store.meta_path.is_file():
            write_json(self.store.meta_path, {"declaration": declaration.to_dict(), "pinned_at": now()})
            self.store.events.emit("declaration_pinned", None, declaration=declaration.to_dict())
        self.store.write_manifest(spec.session_id)
        self.store.events.emit("archived", spec.session_id, chosen_label=label, value=node.rank_value,
                               valid=node.valid, timing=timing)
        for name, flag in flags.items():
            self.store.events.emit("flagged", spec.session_id, flag=name, level=flag["level"], reason=flag["note"],
                                   source="loop" if name == "duplicate" else "adapter")
            self.log(f"[{self.task}/{spec.session_id}] flagged {name} ({flag['level']}): {flag['note']}")
        write_matrix(self.store)
        return node

    def _summary(self, node: Node) -> str:
        declaration = self.declaration()
        metric = declaration.rank
        parts = [f"[{self.task}/{node.session_id}] archived {node.chosen_label or 'nothing'}: "
                 f"{prompts.describe(node, declaration)} ({'valid' if node.valid else 'not valid'})"]
        for key in ("queue_seconds", "agent_seconds", "wall_seconds"):
            if node.timing.get(key) is not None:
                parts.append(f"{key.replace('_seconds', '')} {node.timing[key] / 60:.1f} min")
        curve = stopping.best_curve(self.ranked(self.store.nodes()), metric)
        parts.append(f"best so far {metric.text(curve[-1])}")
        return "; ".join(parts)


class Scheduler:
    UNAVAILABLE_WAIT_SECONDS = 900   # retry delay when the agent's service gives no time to come back

    def __init__(self, store: CampaignStore, config: CampaignConfig, adapter: Adapter,
                 log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.store, self.config, self.adapter, self.log, self.sleep = store, config, adapter, log, sleep
        self.clock = clock
        self.hold_until = 0.0   # the agent's service refused work: submit nothing new before this time
        self.tasks = adapter.resolve_tasks(config.tasks)
        self.loops = {task: TaskLoop(store.task(task), config.for_task(task), adapter, log) for task in self.tasks}

    def run(self) -> dict:
        """Run until every task is stopped or paused; returns the campaign state."""
        running: dict[str, tuple[TaskLoop, Job]] = {}
        poll_failures, failing_since = 0, None
        for loop in self.loops.values():
            for job in loop.submitted_jobs():
                running[job.key] = (loop, job)
                self.log(f"[{job.key}] re-attached to a submitted {job.kind} job")
        while True:
            held = self.clock() < self.hold_until   # _fill submits nothing while held
            self._fill(running)
            self.write_state()
            if not running:
                if self.clock() < self.hold_until:   # nothing left but waiting for the agent's service
                    self.sleep(min(self.hold_until - self.clock(), 300))
                    continue
                if held:   # the hold ran out between the two clock reads: fill again before concluding
                    continue
                break
            try:
                finished = self.adapter.poll([job for _, job in running.values()], self.store.events)
                if poll_failures:
                    self.store.events.emit("poll_recovered", None, failures=poll_failures, since=failing_since)
                    self.log(f"polling works again after {poll_failures} failed polls (since {failing_since})")
                    poll_failures, failing_since = 0, None
            except Exception as error:  # a hiccup on the way to the jobs: not evidence that any job ended
                poll_failures += 1
                failing_since = failing_since or now()
                if poll_failures == 1 or poll_failures % 30 == 0:  # one record per outage, then every 30 polls
                    self.store.events.emit("error", None, where="poll", failures=poll_failures,
                                           message=str(error)[-2000:])
                    self.log(f"poll failed ({poll_failures}x since {failing_since}); jobs keep running, retrying")
                finished = set()
            for key in sorted(finished):
                loop, job = running.pop(key)
                loop.finish(job)
                if loop.unavailable is not None:
                    self._hold(loop.unavailable)
                    loop.unavailable = None
            if not finished:
                self.sleep(self.config.poll_seconds)
        state = self.write_state()
        if state["tasks_stopped"] == state["tasks_total"] and self.store.events.last("campaign_stop") is None:
            self.store.events.emit("campaign_stop", None, sessions=state["sessions"])
        paused = sorted(task for task, loop in self.loops.items() if loop.paused)
        if paused:
            self.log(f"{len(paused)} task(s) paused; fix the cause and `run` again to resume them: {paused}")
        return state

    def _hold(self, error: AgentUnavailable) -> None:
        until = (error.retry_at + 60) if error.retry_at else self.clock() + self.UNAVAILABLE_WAIT_SECONDS
        if until > self.hold_until:
            self.hold_until = until
            self.store.events.emit("campaign_hold", None, until=until, reason=str(error).splitlines()[0][:500])
            self.log("the agent's service is unavailable: no new work until "
                     f"{time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(until))}")

    def _fill(self, running: dict[str, tuple[TaskLoop, Job]]) -> None:
        if self.clock() < self.hold_until:
            return
        # One slot per task per pass; repeat to fill rounds without starving other tasks.
        while len(running) < self.config.max_parallel:
            progress = False
            counts = {task: sum(loop.task == task for loop, _ in running.values()) for task in self.tasks}
            order = sorted(self.loops.values(), key=lambda loop: (
                loop.pending_confirmation() is None, counts[loop.task], len(loop.store.nodes()), loop.task))
            for loop in order:
                if len(running) >= self.config.max_parallel:
                    return
                job = loop.next_job()
                if job:
                    running[job.key] = (loop, job)
                    progress = True
            if not progress:
                return

    def write_state(self) -> dict:
        state = campaign_state(self.store, self.config, self.tasks)
        state["paused"] = {task: loop.paused for task, loop in self.loops.items() if loop.paused}
        write_json(self.store.root / "state.json", state)
        write_summary(self.store, self.tasks)
        return state


def open_campaign(root) -> CampaignStore:
    store = CampaignStore(root)
    if not store.config_path.is_file():
        raise FileNotFoundError(f"{root} is not a campaign directory (no config.yaml)")
    return store
